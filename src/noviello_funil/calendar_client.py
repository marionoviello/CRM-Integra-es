"""Google Calendar client — find available slots + create events.

Implementação intencionalmente sem ``google-api-python-client`` (síncrono,
pesado) ou ``google-auth`` (não precisamos do framework completo). Tudo
em httpx puro:

  - OAuth refresh: POST https://oauth2.googleapis.com/token
  - Free/Busy:     POST /calendar/v3/freeBusy
  - Create event:  POST /calendar/v3/calendars/{calId}/events

O refresh_token (gerado one-time por ``scripts/google_oauth_setup.py``)
não expira a menos que Mario revogue manualmente em
``myaccount.google.com/permissions``. Access tokens duram 1h e são
renovados sob demanda.

Decisões de produto (2026-06-08):
  - Horário comercial: 14h-19h (apenas tarde, manhã é audiência)
  - Slots de 30 min, sem buffer
  - Próximos 5 dias úteis (seg-sex)
  - Oferecer 3 slots próximos disponíveis
  - Evento criado SÓ com Mario convidado (lead não dá email no chat;
    nome+telefone+resumo vão no description)
"""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .outbound import OutboundError, with_retry

logger = logging.getLogger(__name__)

# Auditoria 22/ago: o push name do WhatsApp vira título de evento na agenda do
# Mario, e vinha cru — "[Atendimento] 🙏", "Lu🌹", "14 99181-7005" ou vazio
# (lead 207). Limpamos só pra EXIBIÇÃO; o contato_nome no banco fica intacto,
# porque outros 20+ pontos (notificações, conflito, contrato) dependem do cru.
# Variation selector-16, zero-width joiner e zero-width space — sobram quando
# se remove o emoji e deixariam lixo invisível no título.
_SEM_LARGURA = "️‍​"


def _nome_exibicao(nome: str | None, telefone: str) -> str:
    """Nome apresentável pro título do evento.

    Tira emoji/pictogramas e, quando o que sobra não identifica ninguém
    (vazio ou só dígitos), cai pros 4 últimos dígitos do telefone — que ao
    menos permite achar a conversa no WhatsApp.
    """
    bruto = (nome or "").strip()
    sem_emoji = "".join(
        c for c in bruto
        if unicodedata.category(c) not in {"So", "Sk"} and c not in _SEM_LARGURA
    )
    limpo = re.sub(r"\s+", " ", sem_emoji).strip(" -_.,·|")
    # Push name que é só o número ("14 99181-7005") não é nome.
    if limpo and not re.search(r"[A-Za-zÀ-ÿ]", limpo):
        limpo = ""
    if limpo:
        return limpo
    digits = "".join(c for c in telefone if c.isdigit())
    return f"Lead {digits[-4:]}" if len(digits) >= 4 else "Lead"


_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CAL_BASE = "https://www.googleapis.com/calendar/v3"


@dataclass(frozen=True)
class Slot:
    """Horário disponível na agenda do Mario.

    ``start`` é timezone-aware (no tz configurado pelo Mario,
    default America/Sao_Paulo).
    """

    start: datetime.datetime
    duration_min: int

    @property
    def end(self) -> datetime.datetime:
        return self.start + datetime.timedelta(minutes=self.duration_min)

    def format_human(self) -> str:
        """Formato pro WhatsApp: 'terça (10/jun) às 14h30'."""
        dias = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
        meses = [
            "jan", "fev", "mar", "abr", "mai", "jun",
            "jul", "ago", "set", "out", "nov", "dez",
        ]
        d = self.start
        hora = d.strftime("%Hh") if d.minute == 0 else d.strftime("%Hh%M")
        return f"{dias[d.weekday()]} ({d.day:02d}/{meses[d.month-1]}) às {hora}"


class GoogleCalendarError(Exception):
    """Falha persistente na integração Calendar."""


class GoogleCalendarClient:
    """Cliente assíncrono pro Google Calendar API (OAuth2 user creds).

    Thread-safe pra uso single-event-loop. Mantém access_token cacheado
    em memória; renova sob 401 ou quando expira (margem de 60s).
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
        timezone: str = "America/Sao_Paulo",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._calendar_id = calendar_id
        self._tz = ZoneInfo(timezone)
        self._http = http_client or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http_client is None

        self._access_token: str | None = None
        self._access_expires_at: datetime.datetime | None = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # --- OAuth -----------------------------------------------------------

    async def _refresh_access_token(self) -> str:
        """Troca o refresh_token por um access_token novo (válido 1h)."""

        async def op() -> dict[str, Any]:
            resp = await self._http.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if resp.status_code >= 400:
                logger.error(
                    "google oauth refresh failed: %d %r",
                    resp.status_code, resp.text[:300],
                )
            resp.raise_for_status()
            return resp.json()

        try:
            data = await with_retry(op, attempts=3, base_delay=1.0)
        except OutboundError as e:
            raise GoogleCalendarError(
                "Falha persistente ao renovar access_token Google",
            ) from e

        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._access_expires_at = (
            datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(seconds=expires_in - 60)  # margem
        )
        return self._access_token

    async def _get_access_token(self) -> str:
        if self._access_token and self._access_expires_at:
            if datetime.datetime.now(datetime.UTC) < self._access_expires_at:
                return self._access_token
        return await self._refresh_access_token()

    async def _authed_request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Wrapper que injeta Bearer e refaz 1x em caso de 401 (token revoked)."""
        token = await self._get_access_token()
        headers = kwargs.pop("headers", {}) or {}
        headers["Authorization"] = f"Bearer {token}"
        resp = await self._http.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            # token possivelmente revogado mid-flight — força refresh e retenta 1x
            self._access_token = None
            token = await self._get_access_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._http.request(method, url, headers=headers, **kwargs)
        return resp

    # --- Free/Busy + slot search -----------------------------------------

    async def find_available_slots(
        self,
        *,
        business_hours_start: int,
        business_hours_end: int,
        slot_min: int,
        buffer_min: int,
        lookahead_days: int,
        num_slots: int,
        morning_start: int = 0,
        morning_end: int = 0,
        exclude_isos: set[str] | None = None,
        permitir_dias: set[int] | None = None,
        periodo: str = "",
        now: datetime.datetime | None = None,
    ) -> list[Slot]:
        """Slots livres com estratégia de ESCASSEZ (decisão Mario 2026-06-10).

        Em vez dos N primeiros slots consecutivos ("14h, 14h30, 15h" —
        parece agenda vazia), oferecemos horários espalhados:

          - 2 do primeiro dia útil disponível (primeiro E último livres)
          - 1 do dia útil seguinte com vaga (primeiro livre)
          - 1 do dia útil seguinte a esse (primeiro livre)

        Lead percebe agenda concorrida. ``num_slots`` atua como teto
        (default 4). Com menos dias disponíveis, degrada graciosamente
        (2+1, ou só 2).

        ``now`` é injetável pra testes; default = ``datetime.now(tz)``.
        Pula sábado, domingo e horários já passados de hoje (com 30 min
        de antecedência mínima).

        ``morning_start``/``morning_end`` (2026-06-16): se válidos (>0 e
        end>start), o gerador oferece a janela da manhã ANTES da tarde —
        ``do_dia`` fica em ordem crescente, então o 1º slot do dia é o mais
        cedo (manhã) e o último o mais tarde. ``exclude_isos``: horários já
        oferecidos (e recusados) que NÃO devem reaparecer — a re-oferta
        sempre traz horários novos (não repete os que o lead já recusou).
        """
        tz = self._tz
        now = (now or datetime.datetime.now(tz)).astimezone(tz)
        # Janela: do agora até N dias úteis à frente. Usamos N*2 dias
        # corridos pra dar folga e cobrir N dias úteis mesmo com fim de
        # semana no meio (suficiente porque N <= 30).
        end_window = now + datetime.timedelta(days=lookahead_days * 2)

        busy = await self._fetch_busy_intervals(start=now, end=end_window)

        exclude = exclude_isos or set()
        # Janelas do dia: manhã (se ligada e válida) + tarde. Em ordem
        # crescente → do_dia[0] = mais cedo (manhã), do_dia[-1] = mais tarde.
        windows: list[tuple[int, int]] = []
        if morning_start and morning_end and morning_end > morning_start:
            windows.append((morning_start, morning_end))
        windows.append((business_hours_start, business_hours_end))
        # Preferência do lead (caso José Lucas 03/ago): "manha"/"tarde"
        # derruba as janelas fora do período — antes o texto do modelo
        # prometia a tarde e a lista vinha da estratégia padrão.
        if periodo == "manha":
            windows = [w for w in windows if w[0] < 12]
        elif periodo == "tarde":
            windows = [w for w in windows if w[0] >= 12]

        # 1. Coleta slots livres AGRUPADOS POR DIA (até 3 dias com vaga).
        dias_com_vagas: list[list[Slot]] = []
        cursor_day = now.date()
        max_day = end_window.date()
        dias_uteis_visitados = 0
        while (
            cursor_day <= max_day
            and dias_uteis_visitados < lookahead_days
            and len(dias_com_vagas) < 3
        ):
            if cursor_day.weekday() >= 5:  # pula fim de semana
                cursor_day += datetime.timedelta(days=1)
                continue
            # Preferência de DIA (caso José Lucas): fora dos dias pedidos →
            # pula SEM consumir o orçamento de lookahead (senão "só sex"
            # esgotaria os dias úteis antes de chegar na sexta).
            if permitir_dias is not None and cursor_day.weekday() not in permitir_dias:
                cursor_day += datetime.timedelta(days=1)
                continue
            dias_uteis_visitados += 1

            do_dia: list[Slot] = []
            do_dia_isos: set[str] = set()
            for win_start, win_end in windows:
                slot_start = datetime.datetime.combine(
                    cursor_day, datetime.time(win_start, 0), tzinfo=tz,
                )
                win_end_dt = datetime.datetime.combine(
                    cursor_day, datetime.time(win_end, 0), tzinfo=tz,
                )
                while slot_start + datetime.timedelta(minutes=slot_min) <= win_end_dt:
                    slot_end = slot_start + datetime.timedelta(minutes=slot_min)
                    iso = slot_start.isoformat()
                    # Pula: já passou (margem 30min), já oferecido (G1, exclude),
                    # ou duplicado de janelas sobrepostas (config manhã×tarde).
                    if (
                        slot_start <= now + datetime.timedelta(minutes=30)
                        or iso in exclude
                        or iso in do_dia_isos
                    ):
                        slot_start = slot_end + datetime.timedelta(minutes=buffer_min)
                        continue
                    if not _overlaps_any(slot_start, slot_end, busy):
                        do_dia.append(Slot(start=slot_start, duration_min=slot_min))
                        do_dia_isos.add(iso)
                    slot_start = slot_end + datetime.timedelta(minutes=buffer_min)

            if do_dia:
                dias_com_vagas.append(do_dia)
            cursor_day += datetime.timedelta(days=1)

        # 2. Padrão 2+1+1: dia1 primeiro+último, dia2 primeiro, dia3 primeiro.
        slots: list[Slot] = []
        if dias_com_vagas:
            dia1 = dias_com_vagas[0]
            slots.append(dia1[0])
            if len(dia1) > 1:
                slots.append(dia1[-1])
        if len(dias_com_vagas) > 1:
            slots.append(dias_com_vagas[1][0])
        if len(dias_com_vagas) > 2:
            slots.append(dias_com_vagas[2][0])

        return slots[:num_slots]

    async def _fetch_busy_intervals(
        self,
        *,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> list[tuple[datetime.datetime, datetime.datetime]]:
        """Chama freeBusy.query e devolve intervalos ocupados (em ``self._tz``)."""
        resp = await self._authed_request(
            "POST",
            f"{_CAL_BASE}/freeBusy",
            json={
                "timeMin": start.astimezone(datetime.UTC).isoformat().replace(
                    "+00:00", "Z",
                ),
                "timeMax": end.astimezone(datetime.UTC).isoformat().replace(
                    "+00:00", "Z",
                ),
                "timeZone": str(self._tz),
                "items": [{"id": self._calendar_id}],
            },
        )
        if resp.status_code >= 400:
            logger.error(
                "freeBusy failed: %d %r", resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()

        data = resp.json()
        cal_data = data.get("calendars", {}).get(self._calendar_id, {})
        # Auditoria 2026-06-11: o freeBusy retorna 200 com um bloco
        # ``errors`` por calendário (ex: notFound). Ignorá-lo fazia a
        # agenda parecer 100% livre → double-booking. Erro → exceção,
        # que degrada pro handoff humano no scheduler.
        if cal_data.get("errors"):
            logger.error(
                "freeBusy retornou errors pro calendário %s: %r",
                self._calendar_id, cal_data["errors"],
            )
            raise GoogleCalendarError(
                f"freeBusy errors: {cal_data['errors']}"
            )
        busy_raw = cal_data.get("busy", [])

        intervals: list[tuple[datetime.datetime, datetime.datetime]] = []
        for b in busy_raw:
            s = datetime.datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
            e = datetime.datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
            intervals.append((s.astimezone(self._tz), e.astimezone(self._tz)))
        return intervals

    # --- Event creation --------------------------------------------------

    async def create_event(
        self,
        *,
        start: datetime.datetime,
        duration_min: int,
        lead_nome: str,
        lead_telefone: str,
        resumo_caso: str,
        lead_email: str | None = None,
    ) -> dict[str, Any]:
        """Cria evento no calendar do Mario.

        Se ``lead_email`` for fornecido:
          - Adiciona lead como attendee (Google manda convite ICS por email)
          - Cria Google Meet automático (``conferenceData.createRequest``)
          - Response traz o link em ``data.hangoutLink``

        Sem email, evento fica privado no calendar do Mario com
        nome+telefone no description.
        """
        # Guarda contra datetime NAIVE: astimezone() em naive assume o
        # tz do SO (UTC no VPS) e desloca o evento 3h. Naive aqui é
        # interpretado como horário da agenda (self._tz).
        if start.tzinfo is None:
            start = start.replace(tzinfo=self._tz)
        start_tz = start.astimezone(self._tz)
        end_tz = start_tz + datetime.timedelta(minutes=duration_min)

        # Tira não-dígitos do telefone pra montar link wa.me
        digits = "".join(c for c in lead_telefone if c.isdigit())
        wa_link = f"https://wa.me/{digits}" if digits else lead_telefone

        nome_titulo = _nome_exibicao(lead_nome, lead_telefone)

        body: dict[str, Any] = {
            "summary": f"[Atendimento] {nome_titulo}",
            "description": (
                f"Lead qualificado pelo bot.\n\n"
                f"Nome: {lead_nome}\n"
                f"Telefone: {lead_telefone}\n"
                f"WhatsApp: {wa_link}\n\n"
                f"Resumo do caso:\n{resumo_caso}"
            ),
            "start": {
                "dateTime": start_tz.isoformat(),
                "timeZone": str(self._tz),
            },
            "end": {
                "dateTime": end_tz.isoformat(),
                "timeZone": str(self._tz),
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 15},
                    {"method": "email", "minutes": 60},
                ],
            },
        }

        params: dict[str, str] = {}
        if lead_email:
            body["attendees"] = [{"email": lead_email}]
            # requestId precisa ser único por evento (Google deduplica
            # tentativas). Usamos start ISO + email — mesmo evento criado
            # 2x não duplica o Meet (idempotente).
            req_id = f"noviello-{start_tz.strftime('%Y%m%dT%H%M%S')}-{abs(hash(lead_email)) % 10**8}"
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": req_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                },
            }
            # conferenceDataVersion=1 é OBRIGATÓRIO pra Google criar Meet.
            params["conferenceDataVersion"] = "1"
            # sendUpdates=all garante que Google manda convite ICS pro lead.
            params["sendUpdates"] = "all"

        resp = await self._authed_request(
            "POST",
            f"{_CAL_BASE}/calendars/{self._calendar_id}/events",
            params=params,
            json=body,
        )
        if resp.status_code >= 400:
            logger.error(
                "create_event failed: %d %r",
                resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()
        return resp.json()

    async def list_events(
        self,
        *,
        time_min: datetime.datetime,
        time_max: datetime.datetime,
    ) -> list[dict[str, Any]]:
        """D4 (25/jun): eventos do calendário na janela [time_min, time_max].

        Usado pra detectar reuniões marcadas FORA do bot. Por evento retorna::

            {"id", "summary", "start_iso" (None se all-day), "meet_link",
             "attendees": [{"email" (lowercase), "self", "organizer"}]}

        ``singleEvents=true`` expande recorrências; ``orderBy=startTime`` ordena.
        """
        resp = await self._authed_request(
            "GET",
            f"{_CAL_BASE}/calendars/{self._calendar_id}/events",
            params={
                "timeMin": time_min.astimezone(datetime.UTC)
                .isoformat().replace("+00:00", "Z"),
                "timeMax": time_max.astimezone(datetime.UTC)
                .isoformat().replace("+00:00", "Z"),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "250",
            },
        )
        if resp.status_code >= 400:
            logger.error(
                "list_events failed: %d %r", resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()

        eventos: list[dict[str, Any]] = []
        for item in resp.json().get("items", []):
            start = item.get("start") or {}
            attendees = [
                {
                    "email": (a.get("email") or "").strip().lower(),
                    "self": bool(a.get("self")),
                    "organizer": bool(a.get("organizer")),
                }
                for a in (item.get("attendees") or [])
            ]
            eventos.append({
                "id": item.get("id", "") or "",
                "summary": item.get("summary", "") or "",
                "start_iso": start.get("dateTime"),  # None em all-day (só "date")
                "attendees": attendees,
                "meet_link": item.get("hangoutLink", "") or "",
            })
        return eventos

    async def cancel_event(self, event_id: str) -> None:
        """DELETE /calendars/{calId}/events/{eventId}?sendUpdates=all.

        ``sendUpdates=all`` notifica o attendee (lead) por email do
        cancelamento. Não levanta exceção em 404/410 (evento já não
        existe — pode ter sido cancelado manualmente pelo Mario).
        """
        resp = await self._authed_request(
            "DELETE",
            f"{_CAL_BASE}/calendars/{self._calendar_id}/events/{event_id}",
            params={"sendUpdates": "all"},
        )
        if resp.status_code in (404, 410):
            logger.info(
                "cancel_event: evento %s já não existia (%d)",
                event_id, resp.status_code,
            )
            return
        if resp.status_code >= 400:
            logger.error(
                "cancel_event failed: %d %r",
                resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()


def _overlaps_any(
    start: datetime.datetime,
    end: datetime.datetime,
    busy: list[tuple[datetime.datetime, datetime.datetime]],
) -> bool:
    """True se [start, end) cruza qualquer intervalo busy."""
    for b_start, b_end in busy:
        if start < b_end and b_start < end:
            return True
    return False
