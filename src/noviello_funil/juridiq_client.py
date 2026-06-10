"""Juridiq API client — gestão de processos do escritório.

Contrato (OpenAPI 3.1, https://api.juridiq.com.br/api-docs/client/openapi.json,
mapeado 2026-06-10):
  - Auth: header ``x-juridiq-api-key`` (chave criada no painel)
  - GET  /person/search?phoneNumber=...  → 200 objeto único | 4xx não achou
  - POST /person/                        → 201 pessoa criada

Uso atual: intake automático — quando lead agenda reunião via bot,
cria a Pessoa no Juridiq com a qualificação completa. Feature é
opcional: sem JURIDIQ_API_KEY no .env, nada acontece.
"""

import logging
from typing import Any

import httpx

from .outbound import OutboundError, with_retry

logger = logging.getLogger(__name__)


class JuridiqClient:
    """Thin wrapper sobre a Juridiq REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.juridiq.com.br",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"x-juridiq-api-key": api_key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search_person_by_phone(
        self, phone: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any] | None:
        """GET /person/search?phoneNumber= — None se não encontrada.

        A API retorna 4xx quando não acha (não um 200 vazio), então
        tratamos qualquer 4xx-de-busca como "não existe".
        """

        async def op() -> dict[str, Any] | None:
            resp = await self._client.get(
                f"{self._base_url}/person/search",
                params={"phoneNumber": phone},
            )
            if resp.status_code in (400, 404):
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", data) if isinstance(data, dict) else data

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def create_person(
        self,
        *,
        name: str,
        phone: str,
        email: str | None = None,
        person_origin: str = "lead",
        client_discover_office: str = "WhatsApp — Funil Julia (IA)",
        annotation: str = "",
        base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /person/ — cria pessoa. Retorna a pessoa criada (201)."""
        body: dict[str, Any] = {
            "name": name,
            "personOrigin": person_origin,
            "phone": phone,
            "clientDiscoverOffice": client_discover_office,
        }
        if email:
            body["email"] = email
        if annotation:
            body["annotation"] = annotation

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/person/",
                json=body,
            )
            if resp.status_code >= 400:
                logger.error(
                    "juridiq create_person status=%d body=%r",
                    resp.status_code, resp.text[:400],
                )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", data) if isinstance(data, dict) else data

        return await with_retry(op, attempts=3, base_delay=base_delay)


async def intake_lead_agendado(
    juridiq: "JuridiqClient",
    *,
    nome: str,
    telefone: str,
    email: str | None,
    resumo_caso: str,
    horario_humano: str,
    meet_link: str,
) -> str | None:
    """Cria a pessoa no Juridiq quando lead agenda reunião (intake).

    Idempotente por telefone: se já existe pessoa com esse número,
    não duplica (loga e retorna o id existente).

    Fire-and-forget por contrato: NUNCA levanta — falha no Juridiq não
    pode impedir o agendamento (que já aconteceu). Retorna o person_id
    ou None em falha.
    """
    try:
        existing = await juridiq.search_person_by_phone(telefone)
        if existing and existing.get("id"):
            logger.info(
                "intake: pessoa já existe no Juridiq (id=%s, nome=%r) — skip",
                existing["id"], existing.get("name"),
            )
            return existing["id"]

        annotation = (
            f"Lead qualificado pela Julia (bot IA) via WhatsApp.\n\n"
            f"Resumo do caso:\n{resumo_caso}\n\n"
            f"Reunião agendada: {horario_humano}"
        )
        if meet_link:
            annotation += f"\nMeet: {meet_link}"

        person = await juridiq.create_person(
            name=nome,
            phone=telefone,
            email=email,
            annotation=annotation,
        )
        logger.info(
            "intake: pessoa criada no Juridiq id=%s nome=%r",
            person.get("id"), nome,
        )
        return person.get("id")
    except (OutboundError, httpx.HTTPError, Exception) as exc:
        logger.exception("intake juridiq falhou pra %r: %s", nome, exc)
        return None
