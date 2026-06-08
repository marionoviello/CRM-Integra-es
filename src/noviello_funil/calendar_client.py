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
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .outbound import OutboundError, with_retry

logger = logging.getLogger(__name__)

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
        now: datetime.datetime | None = None,
    ) -> list[Slot]:
        """Devolve até ``num_slots`` slots livres nos próximos
        ``lookahead_days`` dias úteis, dentro do horário comercial.

        ``now`` é injetável pra testes; default = ``datetime.now(tz)``.
        Pula sábado, domingo e horários já passados de hoje.
        """
        tz = self._tz
        now = (now or datetime.datetime.now(tz)).astimezone(tz)
        # Janela: do agora até N dias úteis à frente. Usamos N*2 dias
        # corridos pra dar folga e cobrir N dias úteis mesmo com fim de
        # semana no meio (suficiente porque N <= 30).
        end_window = now + datetime.timedelta(days=lookahead_days * 2)

        busy = await self._fetch_busy_intervals(start=now, end=end_window)

        # Gerar candidatos: dia a dia, hora a hora dentro do horário comercial.
        slots: list[Slot] = []
        cursor_day = now.date()
        max_day = end_window.date()
        dias_uteis_visitados = 0
        while cursor_day <= max_day and dias_uteis_visitados < lookahead_days:
            # Pula fim de semana.
            if cursor_day.weekday() >= 5:
                cursor_day += datetime.timedelta(days=1)
                continue
            dias_uteis_visitados += 1

            slot_start = datetime.datetime.combine(
                cursor_day,
                datetime.time(business_hours_start, 0),
                tzinfo=tz,
            )
            day_end = datetime.datetime.combine(
                cursor_day,
                datetime.time(business_hours_end, 0),
                tzinfo=tz,
            )
            while slot_start + datetime.timedelta(minutes=slot_min) <= day_end:
                slot_end = slot_start + datetime.timedelta(minutes=slot_min)
                # Slot só vale se for futuro (com 30 min de antecedência mínima
                # pra Mario receber notify e estar presente)
                if slot_start <= now + datetime.timedelta(minutes=30):
                    slot_start = slot_end + datetime.timedelta(minutes=buffer_min)
                    continue
                if not _overlaps_any(slot_start, slot_end, busy):
                    slots.append(Slot(start=slot_start, duration_min=slot_min))
                    if len(slots) >= num_slots:
                        return slots
                slot_start = slot_end + datetime.timedelta(minutes=buffer_min)
            cursor_day += datetime.timedelta(days=1)

        return slots

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
        start_tz = start.astimezone(self._tz)
        end_tz = start_tz + datetime.timedelta(minutes=duration_min)

        # Tira não-dígitos do telefone pra montar link wa.me
        digits = "".join(c for c in lead_telefone if c.isdigit())
        wa_link = f"https://wa.me/{digits}" if digits else lead_telefone

        body: dict[str, Any] = {
            "summary": f"[Atendimento] {lead_nome}",
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
