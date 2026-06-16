"""Asaas API client — cobrança de honorários no fechamento de contrato (3.x).

SÓ cria/cancela cobrança PENDENTE (faturamento do escritório: o cliente paga os
honorários pelo ``invoiceUrl``). NUNCA estorno/transferência/saque — esses
endpoints não existem aqui de propósito.

Contrato (docs.asaas.com): auth por header ``access_token`` (NÃO Bearer).
  - GET    /v3/customers?cpfCnpj=             busca cliente por CPF
  - POST   /v3/customers                      cria cliente
  - GET    /v3/payments?externalReference=    dedupe da cobrança
  - POST   /v3/payments                       cria cobrança (UNDEFINED → invoiceUrl)
  - GET    /v3/payments/{id}                  status autoritativo
  - DELETE /v3/payments/{id}                  cancela cobrança PENDENTE

Os POST (create_customer, create_payment) NÃO passam por with_retry — não são
idempotentes (a Asaas não dedupe; 2 POSTs = 2 cobranças). O dedupe é nossa
responsabilidade: ``find_*`` ANTES de criar. GET e DELETE usam with_retry.
Feature opcional: sem ASAAS_API_KEY, nada é instanciado.
"""

import logging
import re
from typing import Any

import httpx

from .outbound import with_retry

logger = logging.getLogger(__name__)


def _so_digitos(s: object) -> str:
    return re.sub(r"\D", "", str(s or ""))


class AsaasClient:
    """Wrapper fino sobre a Asaas REST API (subset de cobrança)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api-sandbox.asaas.com",
        *,
        user_agent: str = "noviello-bot/1.0",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "access_token": api_key,
                "Content-Type": "application/json",
                "User-Agent": user_agent,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Cliente ---------------------------------------------------------

    async def find_customer_by_cpf(
        self, cpf: str, *, base_delay: float = 1.0,
    ) -> str | None:
        """GET /v3/customers?cpfCnpj= — id do cliente (cus_...) ou None."""

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/v3/customers",
                params={"cpfCnpj": _so_digitos(cpf)},
            )
            resp.raise_for_status()
            return resp.json()

        data = await with_retry(op, attempts=3, base_delay=base_delay)
        itens = data.get("data") or []
        return itens[0].get("id") if itens else None

    async def create_customer(
        self,
        *,
        name: str,
        cpf: str,
        email: str | None = None,
        mobile_phone: str | None = None,
        external_reference: str | None = None,
        notification_disabled: bool = False,
    ) -> str | None:
        """POST /v3/customers — cria cliente. SEM retry (POST não-idempotente).

        ``notification_disabled=True`` em SANDBOX (lá a Asaas dispara email/SMS
        reais — não notificar terceiros num teste).
        """
        body: dict[str, Any] = {"name": name, "cpfCnpj": _so_digitos(cpf)}
        if email:
            body["email"] = email
        if mobile_phone:
            body["mobilePhone"] = _so_digitos(mobile_phone)
        if external_reference:
            body["externalReference"] = external_reference
        if notification_disabled:
            body["notificationDisabled"] = True

        resp = await self._client.post(
            f"{self._base_url}/v3/customers", json=body,
        )
        if resp.status_code >= 400:
            logger.error(
                "asaas create_customer status=%d ref=%s",
                resp.status_code, external_reference,
            )
            logger.debug("asaas create_customer body=%r", resp.text[:300])
        resp.raise_for_status()
        return resp.json().get("id")

    async def get_or_create_customer(
        self, *, name: str, cpf: str, **kwargs: Any,
    ) -> str | None:
        """Busca por CPF e reusa; senão cria. Dedupe por CPF (a Asaas não faz)."""
        existing = await self.find_customer_by_cpf(cpf)
        if existing:
            return existing
        return await self.create_customer(name=name, cpf=cpf, **kwargs)

    # --- Cobrança --------------------------------------------------------

    async def find_payment_by_external_reference(
        self, external_reference: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any] | None:
        """GET /v3/payments?externalReference= — cobrança existente ou None.

        Dedupe ANTES de create_payment: a Asaas NÃO deduplica por
        externalReference (2 POSTs = 2 cobranças).
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/v3/payments",
                params={"externalReference": external_reference},
            )
            resp.raise_for_status()
            return resp.json()

        data = await with_retry(op, attempts=3, base_delay=base_delay)
        itens = data.get("data") or []
        return itens[0] if itens else None

    async def create_payment(
        self,
        *,
        customer_id: str,
        value: float,
        due_date: str,
        description: str,
        external_reference: str,
        billing_type: str = "UNDEFINED",
    ) -> dict[str, Any]:
        """POST /v3/payments — cria cobrança. SEM retry (não-idempotente).

        ``billing_type='UNDEFINED'`` → o ``invoiceUrl`` deixa o cliente escolher
        PIX/cartão/boleto numa fatura única. Retorna o JSON
        ({``id`` pay_..., ``invoiceUrl``, ``status`` 'PENDING'}).
        """
        body: dict[str, Any] = {
            "customer": customer_id,
            "billingType": billing_type,
            "value": value,
            "dueDate": due_date,
            "description": description,
            "externalReference": external_reference,
        }
        resp = await self._client.post(
            f"{self._base_url}/v3/payments", json=body,
        )
        if resp.status_code >= 400:
            logger.error(
                "asaas create_payment status=%d ref=%s",
                resp.status_code, external_reference,
            )
            logger.debug("asaas create_payment body=%r", resp.text[:300])
        resp.raise_for_status()
        return resp.json()

    async def get_payment(
        self, payment_id: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """GET /v3/payments/{id} — status autoritativo (ler ANTES de cancelar)."""

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/v3/payments/{payment_id}",
            )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    # --- Webhook ---------------------------------------------------------

    async def register_webhook(
        self,
        *,
        url: str,
        auth_token: str,
        email: str,
        name: str = "Noviello Funil — cobrancas",
        events: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /v3/webhooks — cadastra o webhook de cobrança. SEM retry.

        ``auth_token`` volta no header ``asaas-access-token`` de cada POST do
        webhook (a Asaas não assina com HMAC) — validado constant-time no
        ``/webhooks/asaas``. ``email`` recebe aviso da Asaas se o webhook
        falhar repetidamente. ``events`` default cobre PIX (PAYMENT_RECEIVED)
        e cartão/boleto (PAYMENT_CONFIRMED→RECEIVED). SEM with_retry: POST
        não-idempotente (re-cadastrar duplica o webhook no painel).
        """
        body: dict[str, Any] = {
            "name": name,
            "url": url,
            "email": email,
            "enabled": True,
            "interrupted": False,
            "apiVersion": 3,
            "sendType": "SEQUENTIALLY",
            "authToken": auth_token,
            "events": events or ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"],
        }
        resp = await self._client.post(
            f"{self._base_url}/v3/webhooks", json=body,
        )
        if resp.status_code >= 400:
            logger.error(
                "asaas register_webhook status=%d body=%r",
                resp.status_code, resp.text[:300],
            )
        resp.raise_for_status()
        return resp.json()

    async def delete_payment(
        self, payment_id: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """DELETE /v3/payments/{id} — cancela cobrança PENDENTE (soft-delete).

        SÓ chamar se o status for PENDING|OVERDUE. Em cobrança já paga
        (CONFIRMED|RECEIVED) NÃO cancelar — vira estorno manual do Mario.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.delete(
                f"{self._base_url}/v3/payments/{payment_id}",
            )
            if resp.status_code >= 400:
                logger.error(
                    "asaas delete_payment status=%d id=%s",
                    resp.status_code, payment_id,
                )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)
