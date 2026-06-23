"""ZapSign API client — assinatura eletrônica de contratos (roadmap 3.x).

Contrato (https://docs.zapsign.com.br, mapeado 2026-06-15):
  - Auth: header ``Authorization: Bearer <token>`` (painel → Integrações → API)
  - POST /models/create-doc/        → cria doc a partir de um modelo/template
  - GET  /docs/{token}/             → detalha o doc (status autoritativo)
  - POST /user/company/webhook/     → cadastra webhook (com headers customizados)
  - signed_file (URL no doc)        → PDF assinado, EXPIRA em 60 min

A ZapSign NÃO assina o webhook com HMAC. A segurança é um header secreto
que cadastramos junto do webhook (``register_webhook``) e validamos
constant-time ao receber. Defesa extra: ao receber o evento, re-buscar
``get_doc`` pra confirmar o status (não confiar cego no payload).

Feature opcional: sem ZAPSIGN_API_TOKEN no .env, nada é instanciado.
"""

import logging
from typing import Any

import httpx

from .outbound import with_retry

logger = logging.getLogger(__name__)


class ZapSignClient:
    """Wrapper fino sobre a ZapSign REST API."""

    def __init__(
        self,
        api_token: str,
        base_url: str = "https://api.zapsign.com.br/api/v1",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"Authorization": f"Bearer {api_token}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_doc_from_template(
        self, corpo: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /models/create-doc/ — cria o doc a partir do template.

        ``corpo`` vem de ``contrato.montar_corpo_create_doc``. Retorna o
        JSON da ZapSign (``token``, ``status``, ``signers[]`` com sign_url).

        SEM with_retry de propósito: o create-doc é um POST NÃO-idempotente
        (a ZapSign não dedupe por ``external_id``), então um retry cego após
        a criação real — ACK perdido em timeout/5xx — geraria um 2º documento
        e um 2º email ao cliente. Uma tentativa só; a falha sobe e o caller
        mantém o contrato em ``aprovado`` pra retry deliberado.
        """
        resp = await self._client.post(
            f"{self._base_url}/models/create-doc/", json=corpo,
        )
        if resp.status_code >= 400:
            # NÃO logar o corpo no nível error: a resposta da ZapSign pode
            # ecoar PII/honorários (LGPD). status + external_id correlacionam;
            # o corpo só em DEBUG (off em produção), útil no sandbox.
            logger.error(
                "zapsign create-doc falhou status=%d external_id=%s",
                resp.status_code, corpo.get("external_id"),
            )
            logger.debug("zapsign create-doc resp_body=%r", resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    async def add_signer(
        self, doc_token: str, signer: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /docs/{token}/add-signer/ — adiciona um signatário a um doc já
        criado.

        O create-doc-from-template só registra o signatário PRIMÁRIO
        (``signer_name``); escritório + testemunhas entram por aqui, na ORDEM de
        adição (= ordem de assinatura quando ``signature_order_active`` está
        ligado no doc). SEM with_retry: POST não-idempotente — re-tentar cego
        adicionaria o mesmo signatário 2x.
        """
        resp = await self._client.post(
            f"{self._base_url}/docs/{doc_token}/add-signer/", json=signer,
        )
        if resp.status_code >= 400:
            logger.error(
                "zapsign add-signer falhou status=%d doc=%s",
                resp.status_code, doc_token,
            )
            logger.debug("zapsign add-signer resp_body=%r", resp.text[:300])
        resp.raise_for_status()
        return resp.json()

    async def delete_doc(self, doc_token: str) -> None:
        """DELETE /docs/{token}/ — apaga um doc. Usado pra limpar um doc parcial
        quando um add-signer falha no meio (doc incompleto → recria limpo no
        retry). Best-effort: falha aqui só loga (warning)."""
        resp = await self._client.delete(f"{self._base_url}/docs/{doc_token}/")
        if resp.status_code >= 400:
            logger.warning(
                "zapsign delete-doc status=%d doc=%s",
                resp.status_code, doc_token,
            )

    async def create_doc_from_pdf(
        self, corpo: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /docs/ — cria o doc a partir de um PDF que NÓS geramos.

        É o caminho B (a IA redige o texto → PDF → upload). ``corpo`` vem de
        ``redacao.montar_corpo_upload`` (name, base64_pdf, signers[], external_id).
        Mesma disciplina do create_doc_from_template: SEM with_retry (POST
        não-idempotente — a ZapSign não dedupe por external_id; um retry cego
        após criação real geraria 2 documentos), e log LGPD-safe (sem o corpo,
        que carrega o base64 + dados do cliente). Resposta idêntica:
        {token, status, signers:[{token, sign_url}]}.
        """
        resp = await self._client.post(f"{self._base_url}/docs/", json=corpo)
        if resp.status_code >= 400:
            logger.error(
                "zapsign create-doc-pdf falhou status=%d external_id=%s",
                resp.status_code, corpo.get("external_id"),
            )
            logger.debug("zapsign create-doc-pdf resp_body=%r", resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    async def get_doc(
        self, doc_token: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """GET /docs/{token}/ — status AUTORITATIVO do documento.

        Usado no webhook pra confirmar o evento antes de agir (a ZapSign
        não assina o payload). Traz ``status`` ('signed' quando todos
        assinaram) e ``signed_file`` (URL efêmera do PDF).
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(f"{self._base_url}/docs/{doc_token}/")
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def download_signed_file(
        self, url: str, *, base_delay: float = 1.0,
    ) -> bytes:
        """Baixa o PDF assinado (URL do campo ``signed_file``).

        A URL expira em 60 min — baixar imediatamente ao confirmar a
        assinatura. Retorna os bytes do PDF.
        """

        async def op() -> bytes:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.content

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def register_webhook(
        self,
        *,
        url: str,
        event_type: str = "",
        secret_header_name: str = "X-Zapsign-Secret",
        secret_value: str = "",
        base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /user/company/webhook/ — cadastra o webhook.

        Como a ZapSign não assina com HMAC, mandamos um header secreto
        (``secret_header_name: secret_value``) que ela devolve em todo POST
        — validamos constant-time ao receber. ``event_type`` vazio = todos
        os eventos; ou 'doc_signed', 'doc_refused', etc. Idempotência fica
        a cargo do Mario (cadastrar uma vez no painel/script).
        """
        body: dict[str, Any] = {"url": url, "type": event_type}
        if secret_value:
            body["headers"] = [
                {"name": secret_header_name, "value": secret_value},
            ]

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/user/company/webhook/", json=body,
            )
            if resp.status_code >= 400:
                logger.error(
                    "zapsign register_webhook status=%d body=%r",
                    resp.status_code, resp.text[:500],
                )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    # --- Caminho A: ler modelos + verificação humana ---------------------

    async def list_templates(
        self, *, page: int = 1, base_delay: float = 1.0,
    ) -> list[dict[str, Any]]:
        """GET /templates/ — lista os modelos (cada um com ``token``, ``name``).

        Auto-descoberta: casa o NOME do modelo que o Mario escolhe com o
        ``token`` (UUID que vira ``template_id`` no create-doc). Pagina 20/vez.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/templates/", params={"page": page},
            )
            resp.raise_for_status()
            return resp.json()

        data = await with_retry(op, attempts=3, base_delay=base_delay)
        if isinstance(data, dict):
            return list(data.get("results", []))
        return list(data or [])

    async def get_template(
        self, token: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """GET /templates/{token}/ — detalhe do modelo com ``inputs[]``.

        Cada input traz ``variable`` (o placeholder literal '{{...}}'),
        ``label`` (texto do painel), ``required``, ``input_type``. É daqui que
        o sistema descobre as variáveis a preencher — o Mario não digita nenhum
        {{campo}} à mão.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(f"{self._base_url}/templates/{token}/")
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def resend_notifications_bulk(
        self, doc_token: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /docs/{token}/resend-notifications-bulk/ — LIBERA a assinatura.

        Depois da aprovação humana, dispara a notificação. Com
        ``signature_order_active`` notifica APENAS o order_group 1 (cliente);
        os demais entram quando o anterior assina. Tolera retry (efeito =
        notificar; no pior caso reenvia ao mesmo cliente).
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/docs/{doc_token}/resend-notifications-bulk/",
            )
            if resp.status_code >= 400:
                logger.error(
                    "zapsign resend-notifications status=%d body=%r",
                    resp.status_code, resp.text[:300],
                )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def refuse(
        self, doc_token: str, rejected_reason: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /refuse/ — REPROVA o doc (verificação humana negada).

        O doc vai a status 'recusado' e fica inassinável. Como foi criado em
        silêncio (sem notificar), o cliente nunca soube que existiu. Exige doc
        em andamento (não cancela um já assinado).
        """
        body = {"doc_token": doc_token, "rejected_reason": rejected_reason}

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/refuse/", json=body,
            )
            if resp.status_code >= 400:
                logger.error(
                    "zapsign refuse status=%d body=%r",
                    resp.status_code, resp.text[:300],
                )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)
