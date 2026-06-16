"""Casca HTTP do pipeline de fechamento de contrato (escopos→Asaas→ZapSign).

A engine (orquestrador_contrato) já decide tudo; este módulo só expõe:

  * GET  /contrato/aprovar/{token}   — PÁGINA de verificação (SEM efeito).
  * POST /contrato/aprovar/{token}   — aprova_e_libera (libera a assinatura).
  * POST /contrato/reprovar/{token}  — reprova (refuse + cancela cobrança).
  * POST /webhooks/zapsign           — assinatura confirmada → ASSINADO.
  * POST /webhooks/asaas             — cobrança paga → cobranca_paga_em.

ESPELHA o webhook Jurichat (``webhooks.py``): valida o header secreto custom
constant-time (a ZapSign/Asaas NÃO assinam com HMAC — mandam o header que
cadastramos), idempotência via is_webhook_processed/mark_webhook_processed,
responde 200 RÁPIDO e processa pesado em BackgroundTask.

INVARIANTE dos webhooks: NUNCA deixar exceção vazar do processamento (200
sempre, exceto 401 de auth) — senão a fila do Asaas trava após 15 falhas e a
do ZapSign re-tenta. O GET de aprovação é PREFETCHER-SAFE: zero efeito
colateral (a aprovação real é o POST do botão).
"""

import hashlib
import hmac
import html
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from .contrato import (
    EstadoContrato,
    registrar_assinatura,
    transicao_contrato,
)
from .orquestrador_contrato import aprovar_e_liberar, reprovar_contrato
from .outbound import notify_mario
from .state import is_webhook_processed, mark_webhook_processed

logger = logging.getLogger(__name__)


# --- HTML helpers (página simples, sem framework de template) -------------

def _pagina(titulo: str, corpo: str, *, status_code: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang='pt-br'><head><meta charset='utf-8'>"
        "<meta name='robots' content='noindex'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(titulo)}</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:760px;"
        "margin:24px auto;padding:0 16px;line-height:1.5'>"
        f"{corpo}</body></html>"
    )
    return HTMLResponse(content=doc, status_code=status_code)


def _pagina_resultado(titulo: str, mensagem: str) -> HTMLResponse:
    corpo = (
        f"<h1>{html.escape(titulo)}</h1>"
        f"<p>{html.escape(mensagem)}</p>"
    )
    return _pagina(titulo, corpo)


def _verify_secret(expected: str, recebido: str | None) -> bool:
    """Comparação constant-time do header secreto custom.

    A ZapSign/Asaas devolvem o header que cadastramos (NÃO é HMAC do body).
    ``expected`` vazio (feature mal configurada) → sempre rejeita.
    """
    if not expected or not recebido:
        return False
    return hmac.compare_digest(expected, recebido)


def register_contrato_routes(
    app: FastAPI,
    *,
    get_db: Callable[[], Any],
    settings: Any,
    zapsign: Any,
    asaas: Any,
    jurichat: Any = None,
) -> None:
    """Registra as rotas do pipeline de contrato em ``app``.

    ``zapsign``/``asaas`` podem ser None quando as flags estão off — as rotas
    ainda são registradas mas respondem de forma segura (503/aviso) em vez de
    quebrar. ``jurichat`` é opcional (notify_mario só dispara se presente).
    """

    # --- Página de aprovação (GET — SEM efeito colateral) -----------------

    @app.get("/contrato/aprovar/{token}", response_class=HTMLResponse)
    async def pagina_aprovar(token: str) -> HTMLResponse:
        conn = get_db()
        contrato = conn.execute(
            "SELECT * FROM contrato WHERE aprovacao_token = ?", (token,),
        ).fetchone()
        if contrato is None:
            return _pagina_resultado(
                "Contrato não encontrado",
                "Este link de aprovação é inválido ou expirou.",
            )

        if contrato["estado"] != EstadoContrato.PENDENTE_REVISAO:
            return _pagina_resultado(
                "Contrato já processado",
                f"Este contrato não está mais aguardando revisão "
                f"(estado atual: {contrato['estado']}).",
            )

        # PDF FRESCO: a URL do original_file expira em ~60min, então buscamos
        # ao vivo (nunca a cacheada). Falha não derruba a página — cai no
        # link do sign_url salvo.
        pdf_url = contrato["sign_url"]
        doc_token = contrato["zapsign_doc_token"]
        if zapsign is not None and doc_token:
            try:
                doc = await zapsign.get_doc(doc_token)
                pdf_url = (
                    doc.get("original_file")
                    or doc.get("signed_file")
                    or pdf_url
                )
            except Exception as exc:  # noqa: BLE001 — página não pode quebrar
                logger.warning(
                    "get_doc falhou na página de aprovação (contrato=%s): %s",
                    contrato["id"], exc,
                )

        corpo = _montar_corpo_revisao(contrato, token, pdf_url)
        return _pagina("Revisar contrato", corpo)

    # --- Aprovar (POST — libera a assinatura) -----------------------------

    @app.post("/contrato/aprovar/{token}", response_class=HTMLResponse)
    async def post_aprovar(token: str) -> HTMLResponse:
        if zapsign is None:
            return _pagina_resultado(
                "Indisponível",
                "A assinatura eletrônica não está habilitada.",
            )
        res = await aprovar_e_liberar(get_db(), zapsign, token=token)
        status = res.get("status")
        if status == "liberado":
            return _pagina_resultado(
                "Assinatura liberada",
                "O contrato foi aprovado e a assinatura foi liberada ao "
                "cliente. Ele receberá o link para assinar.",
            )
        if status == "ja_processado":
            return _pagina_resultado(
                "Já processado",
                f"Este contrato já saiu da revisão (estado: "
                f"{res.get('estado')}). Nenhuma ação foi repetida.",
            )
        if status == "token_invalido":
            return _pagina_resultado(
                "Link inválido", "Este link de aprovação não é válido.",
            )
        return _pagina_resultado(
            "Não foi possível liberar",
            "Houve um problema ao liberar a assinatura "
            f"({status}). Tente novamente em instantes.",
        )

    # --- Reprovar (POST — refuse + cancela cobrança) ----------------------

    @app.post("/contrato/reprovar/{token}", response_class=HTMLResponse)
    async def post_reprovar(token: str, request: Request) -> HTMLResponse:
        if zapsign is None or asaas is None:
            return _pagina_resultado(
                "Indisponível",
                "O fechamento de contrato não está habilitado.",
            )
        # Parse manual do form urlencoded (sem python-multipart, que o projeto
        # não usa): o form do botão posta application/x-www-form-urlencoded.
        body = await request.body()
        campos = parse_qs(body.decode("utf-8", errors="replace"))
        motivo = (campos.get("motivo", [""])[0]).strip() or "Reprovado na revisão interna"
        res = await reprovar_contrato(
            get_db(), zapsign, asaas, token=token, motivo=motivo,
        )
        status = res.get("status")
        if status == "reprovado":
            avisos = []
            if res.get("estorno_manual"):
                avisos.append(
                    "Atenção: havia cobrança paga — o estorno é MANUAL."
                )
            if res.get("cobranca_cancelamento_falhou"):
                avisos.append(
                    "Atenção: o cancelamento da cobrança falhou — confira no "
                    "Asaas antes de encerrar."
                )
            msg = "O contrato foi reprovado e a cobrança pendente cancelada."
            if avisos:
                msg = msg + " " + " ".join(avisos)
            return _pagina_resultado("Contrato reprovado", msg)
        if status == "ja_processado":
            return _pagina_resultado(
                "Já processado",
                f"Este contrato já saiu da revisão (estado: "
                f"{res.get('estado')}). Nenhuma ação foi repetida.",
            )
        if status == "token_invalido":
            return _pagina_resultado(
                "Link inválido", "Este link de reprovação não é válido.",
            )
        return _pagina_resultado(
            "Não foi possível reprovar",
            f"Houve um problema ao reprovar ({status}). Tente novamente.",
        )

    # --- Webhook ZapSign (assinatura confirmada) --------------------------

    @app.post("/webhooks/zapsign")
    async def webhook_zapsign(
        request: Request, background_tasks: BackgroundTasks,
    ) -> Response:
        recebido = request.headers.get("X-Zapsign-Secret")
        if not _verify_secret(settings.zapsign_webhook_secret, recebido):
            logger.warning("webhook zapsign: header secreto inválido")
            return Response(
                content=b'{"ok":false,"detail":"invalid secret"}',
                media_type="application/json",
                status_code=401,
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — payload ruim não trava a fila
            logger.warning("webhook zapsign: json inválido")
            return Response(
                content=b'{"ok":true,"ignored":true}',
                media_type="application/json",
            )

        doc_token = _doc_token_zapsign(payload)
        event = payload.get("event_type") or payload.get("event") or ""
        if not doc_token:
            return Response(
                content=b'{"ok":true,"ignored":true}',
                media_type="application/json",
            )

        conn = get_db()
        evento_id = f"{doc_token}:{event}"
        if is_webhook_processed(conn, "zapsign", evento_id):
            return Response(
                content=b'{"ok":true,"duplicated":true}',
                media_type="application/json",
            )
        body = await request.body()
        mark_webhook_processed(
            conn, "zapsign", evento_id, hashlib.sha256(body).hexdigest(),
        )

        background_tasks.add_task(
            _processar_zapsign, get_db, zapsign, jurichat, settings, doc_token,
        )
        return Response(content=b'{"ok":true}', media_type="application/json")

    # --- Webhook Asaas (cobrança paga) ------------------------------------

    @app.post("/webhooks/asaas")
    async def webhook_asaas(
        request: Request, background_tasks: BackgroundTasks,
    ) -> Response:
        recebido = request.headers.get("asaas-access-token")
        if not _verify_secret(settings.asaas_webhook_token, recebido):
            logger.warning("webhook asaas: token inválido")
            return Response(
                content=b'{"ok":false,"detail":"invalid token"}',
                media_type="application/json",
                status_code=401,
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — payload ruim não trava a fila
            logger.warning("webhook asaas: json inválido")
            return Response(
                content=b'{"ok":true,"ignored":true}',
                media_type="application/json",
            )

        event = payload.get("event") or ""
        payment = payload.get("payment") or {}
        payment_id = payment.get("id")
        if not payment_id:
            return Response(
                content=b'{"ok":true,"ignored":true}',
                media_type="application/json",
            )

        conn = get_db()
        evento_id = f"{event}:{payment_id}"
        if is_webhook_processed(conn, "asaas", evento_id):
            return Response(
                content=b'{"ok":true,"duplicated":true}',
                media_type="application/json",
            )
        body = await request.body()
        mark_webhook_processed(
            conn, "asaas", evento_id, hashlib.sha256(body).hexdigest(),
        )

        background_tasks.add_task(
            _processar_asaas, get_db, jurichat, settings, event, payment_id,
        )
        return Response(content=b'{"ok":true}', media_type="application/json")


# --- Background processors (try/except: NUNCA vaza exceção) ---------------

async def _processar_zapsign(
    get_db: Callable[[], Any],
    zapsign: Any,
    jurichat: Any,
    settings: Any,
    doc_token: str,
) -> None:
    """Re-busca o doc (não confia no payload) e marca ASSINADO se signed.

    Só transiciona quando o DOCUMENTO INTEIRO está assinado (status='signed',
    todos os signers). Idempotente: se já está ASSINADO, no-op. Toda falha é
    engolida — o webhook já respondeu 200.
    """
    try:
        if zapsign is None:
            return
        doc = await zapsign.get_doc(doc_token)
        if (doc.get("status") or "").lower() != "signed":
            return

        conn = get_db()
        contrato = conn.execute(
            "SELECT * FROM contrato WHERE zapsign_doc_token = ?", (doc_token,),
        ).fetchone()
        if contrato is None:
            logger.warning(
                "webhook zapsign: doc %s sem contrato correspondente", doc_token,
            )
            return
        if contrato["estado"] == EstadoContrato.ASSINADO:
            return  # idempotente

        contrato_id = contrato["id"]
        registrar_assinatura(
            conn, contrato_id, signed_file_url=doc.get("signed_file"),
        )
        transicao_contrato(
            conn, contrato_id, EstadoContrato.ASSINADO,
            motivo="assinatura confirmada (webhook zapsign)", ator="webhook",
        )
        if jurichat is not None and settings.mario_conversation_id:
            await notify_mario(
                jurichat,
                mario_conversation_id=settings.mario_conversation_id,
                mensagem=(
                    f"✅ Contrato ASSINADO — {contrato['cliente_nome']} "
                    f"(contrato #{contrato_id})."
                ),
            )
    except Exception:  # noqa: BLE001 — nada vaza pro webhook
        logger.exception("erro processando webhook zapsign (doc=%s)", doc_token)


async def _processar_asaas(
    get_db: Callable[[], Any],
    jurichat: Any,
    settings: Any,
    event: str,
    payment_id: str,
) -> None:
    """Marca cobranca_paga_em nos eventos de pagamento. Idempotente.

    PIX pula CONFIRMED → tratamos PAYMENT_CONFIRMED e PAYMENT_RECEIVED como
    pago. Toda falha é engolida — o webhook já respondeu 200.
    """
    try:
        if event not in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
            return
        conn = get_db()
        contrato = conn.execute(
            "SELECT * FROM contrato WHERE asaas_payment_id = ?", (payment_id,),
        ).fetchone()
        if contrato is None:
            logger.warning(
                "webhook asaas: payment %s sem contrato correspondente",
                payment_id,
            )
            return
        # UPDATE idempotente (re-carimbar a mesma data é inofensivo).
        conn.execute(
            "UPDATE contrato SET cobranca_paga_em = datetime('now'), "
            "atualizado_em = datetime('now') WHERE id = ?",
            (contrato["id"],),
        )
        if jurichat is not None and settings.mario_conversation_id:
            await notify_mario(
                jurichat,
                mario_conversation_id=settings.mario_conversation_id,
                mensagem=(
                    f"💰 Cobrança PAGA — {contrato['cliente_nome']} "
                    f"(contrato #{contrato['id']})."
                ),
            )
    except Exception:  # noqa: BLE001 — nada vaza pro webhook
        logger.exception("erro processando webhook asaas (payment=%s)", payment_id)


# --- Extratores de payload ------------------------------------------------

def _doc_token_zapsign(payload: dict[str, Any]) -> str | None:
    """Pega o token do doc do payload da ZapSign (vários formatos possíveis).

    A ZapSign manda o token em ``token`` (top-level) ou aninhado em ``doc``.
    Não confiamos no resto do payload — o token só serve pra re-buscar o doc.
    """
    token = payload.get("token")
    if token:
        return token
    doc = payload.get("doc") or payload.get("document") or {}
    if isinstance(doc, dict):
        return doc.get("token")
    return None


def _montar_corpo_revisao(contrato: Any, token: str, pdf_url: str | None) -> str:
    """HTML da página de revisão: PDF + valores + forms aprovar/reprovar."""
    nome = html.escape(contrato["cliente_nome"] or "")
    tipo = html.escape(contrato["tipo_caso"] or "")
    valor = html.escape(contrato["valor_honorarios"] or "")
    invoice = contrato["invoice_url"] or ""

    if pdf_url:
        safe_pdf = html.escape(pdf_url, quote=True)
        bloco_pdf = (
            f"<iframe src='{safe_pdf}' style='width:100%;height:520px;"
            "border:1px solid #ccc' title='Contrato'></iframe>"
            f"<p><a href='{safe_pdf}' target='_blank' rel='noopener'>"
            "Abrir o PDF em nova aba</a></p>"
        )
    else:
        bloco_pdf = "<p><em>PDF indisponível no momento.</em></p>"

    bloco_invoice = ""
    if invoice:
        safe_inv = html.escape(invoice, quote=True)
        bloco_invoice = (
            f"<li>Cobrança: <a href='{safe_inv}' target='_blank' "
            "rel='noopener'>ver fatura</a></li>"
        )

    safe_token = html.escape(token, quote=True)
    return (
        "<h1>Revisar contrato</h1>"
        "<ul>"
        f"<li>Cliente: <strong>{nome}</strong></li>"
        f"<li>Tipo de caso: {tipo}</li>"
        f"<li>Honorários: R$ {valor}</li>"
        f"{bloco_invoice}"
        "</ul>"
        f"{bloco_pdf}"
        "<form method='post' "
        f"action='/contrato/aprovar/{safe_token}' style='margin:16px 0'>"
        "<button type='submit' "
        "style='padding:12px 20px;font-size:16px;background:#137333;"
        "color:#fff;border:0;border-radius:6px;cursor:pointer'>"
        "Aprovar e liberar assinatura</button></form>"
        "<form method='post' "
        f"action='/contrato/reprovar/{safe_token}'>"
        "<p><label>Motivo da reprovação:<br>"
        "<textarea name='motivo' rows='3' "
        "style='width:100%;max-width:520px'></textarea></label></p>"
        "<button type='submit' "
        "style='padding:10px 18px;font-size:15px;background:#a50e0e;"
        "color:#fff;border:0;border-radius:6px;cursor:pointer'>"
        "Reprovar</button></form>"
    )
