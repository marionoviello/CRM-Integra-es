"""#36 (25/jun) — pós-assinatura ZapSign: arquivo do PDF + orquestração.

Quando um contrato é assinado (estado ASSINADO), roda 3 sub-passos best-effort e
independentes — intake do cliente no Juridiq, arquivo do PDF assinado, tarefa de
abertura — cada um com marcação durável por-passo (carimba só após sucesso →
passo que falha/crasha fica NULL e re-tenta no SWEEP do scheduler). NUNCA levanta
(o ASSINADO é fato consumado; o webhook já respondeu 200). O webhook signed NÃO
reentrega após o 200 (e o gate de idempotência descartaria) → a retomada é
responsabilidade do sweep periódico, não do webhook.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .contrato import marcar_passo_pos_assinatura
from .juridiq_client import intake_cliente_assinado
from .outbound import notify_mario
from .state import marcar_pos_iniciado

logger = logging.getLogger(__name__)

_TZ_BR = ZoneInfo("America/Sao_Paulo")

# Disco local do VPS (gitignored via data/) — PII do contrato fica no servidor
# do escritório, nunca versionada. Decisão do Mario (25/jun): só disco, sem email.
DIR_CONTRATOS_ASSINADOS = "data/contratos_assinados"


async def arquivar_pdf_assinado(
    zapsign: Any,
    *,
    signed_file_url: str | None,
    contrato_id: int,
    dir_base: str = DIR_CONTRATOS_ASSINADOS,
) -> str | None:
    """Baixa o PDF assinado (a URL ``signed_file`` expira em ~60min) e grava em
    disco durável. Retorna o caminho ou None em falha. Best-effort — NUNCA
    levanta."""
    if not signed_file_url:
        logger.warning("arquivar_pdf: contrato=%s sem signed_file_url", contrato_id)
        return None
    try:
        conteudo = await zapsign.download_signed_file(signed_file_url)
        destino = Path(dir_base)
        destino.mkdir(parents=True, exist_ok=True)
        caminho = destino / f"contrato-{contrato_id}.pdf"
        caminho.write_bytes(conteudo)
        logger.info(
            "arquivar_pdf: contrato=%s salvo em %s (%d bytes)",
            contrato_id, caminho, len(conteudo),
        )
        return str(caminho)
    except Exception as exc:
        logger.exception("arquivar_pdf falhou contrato=%s: %s", contrato_id, exc)
        return None


def montar_corpo_tarefa_abertura(
    *,
    person_id: str,
    cliente_nome: str,
    tipo_caso: str | None,
    column_id: str,
    priority: str,
    initial_date: str,
) -> dict[str, Any]:
    """Corpo do POST /task/ pra abertura do caso — vinculada SÓ à Pessoa
    (``personIds``), SEM lawSuit (o cliente recém-assinado ainda não tem
    processo). A aceitação de 'sem lawSuitId' é validada no smoke da sandbox
    antes de ligar a flag (decisão do Mario, 25/jun)."""
    descricao = (
        f"Cliente {cliente_nome} ASSINOU o contrato de honorários (fechamento "
        f"via bot). Tipo de caso: {tipo_caso or '(não informado)'}. "
        "Conferir documentos e dar andamento à abertura."
    )
    return {
        "title": f"Abrir caso — {cliente_nome}",
        "description": descricao,
        "priority": priority,
        "columnId": column_id,
        "initialDate": initial_date,
        "personIds": [person_id],
    }


async def criar_tarefa_abertura(
    juridiq: Any,
    *,
    person_id: str,
    cliente_nome: str,
    tipo_caso: str | None,
    column_id: str,
    priority: str,
    initial_date: str,
) -> str | None:
    """Cria a tarefa de abertura do caso no Juridiq (só na Pessoa). Retorna o
    task_id ou None. Best-effort — NUNCA levanta. Sem column_id ou person_id,
    não cria (loga)."""
    if not column_id or not person_id:
        logger.warning(
            "criar_tarefa_abertura: pré-req faltando (column_id=%r person_id=%r)",
            bool(column_id), bool(person_id),
        )
        return None
    try:
        corpo = montar_corpo_tarefa_abertura(
            person_id=person_id, cliente_nome=cliente_nome, tipo_caso=tipo_caso,
            column_id=column_id, priority=priority, initial_date=initial_date,
        )
        task_id, detalhe = await juridiq.create_task(corpo)
        if task_id is None:
            logger.error("criar_tarefa_abertura falhou: %s", detalhe)
        return task_id
    except Exception as exc:
        logger.exception("criar_tarefa_abertura erro: %s", exc)
        return None


def _resumo_pos(conn: Any, contrato_id: int, cliente_nome: str) -> str:
    """Linha de resumo dos 3 passos (re-lê o estado atual do contrato)."""
    r = conn.execute(
        "SELECT intake_juridiq_em, person_id, arquivo_pdf_em, tarefa_abertura_em "
        "FROM contrato WHERE id = ?",
        (contrato_id,),
    ).fetchone()
    if r["person_id"]:
        ficha = "✅"
    elif r["intake_juridiq_em"]:
        ficha = "⚠️ pulada (sem telefone) — cadastrar à mão"
    else:
        ficha = "⏳ pendente (re-tenta)"
    pdf = "✅" if r["arquivo_pdf_em"] else "⏳ pendente (re-tenta)"
    tarefa = "✅" if r["tarefa_abertura_em"] else "⏳ pendente (re-tenta)"
    return (
        f"📋 Pós-assinatura — {cliente_nome} (contrato #{contrato_id})\n"
        f"• Ficha no Juridiq: {ficha}\n"
        f"• PDF arquivado: {pdf}\n"
        f"• Tarefa de abertura: {tarefa}"
    )


async def processar_pos_assinatura(
    conn: Any,
    *,
    juridiq: Any,
    zapsign: Any,
    jurichat: Any,
    settings: Any,
    contrato_id: int,
    signed_file_url: str | None,
) -> None:
    """Roda os 3 sub-passos (intake / arquivo / tarefa) best-effort e idempotentes
    por-passo, após o ASSINADO. Cada passo só roda se o seu timestamp está NULL;
    carimba só após sucesso (passo que falha fica NULL e re-tenta no próximo SWEEP
    do scheduler — o webhook signed NÃO reentrega após o 200). NUNCA levanta.

    Chamado pelo webhook (1ª vez) E pelo sweep (retomada). ``signed_file_url`` deve
    ser FRESCO (o sweep re-busca via get_doc; a URL salva expira em ~60min)."""
    try:
        contrato = conn.execute(
            "SELECT * FROM contrato WHERE id = ?", (contrato_id,),
        ).fetchone()
        if contrato is None:
            return
        # Marca o contrato como elegível ao sweep (set-once) — discrimina os
        # pós-feature dos pré-existentes. Carimbado na 1ª execução (webhook).
        marcar_pos_iniciado(conn, contrato_id)
        houve_acao = False
        person_id = contrato["person_id"]

        # 1. INTAKE — garante a Pessoa do cliente no Juridiq.
        if contrato["intake_juridiq_em"] is None:
            houve_acao = True
            telefone = contrato["cliente_telefone"] or ""
            if not person_id and not telefone:
                # Sem ficha E sem telefone → não dá pra dedupe/criar com segurança.
                # Carimba (pra não re-tentar/re-alertar em loop); o resumo avisa.
                logger.warning(
                    "pós #%s: sem person_id e sem telefone — intake pulado",
                    contrato_id,
                )
                marcar_passo_pos_assinatura(
                    conn, contrato_id, passo_em="intake_juridiq_em",
                )
            else:
                pid = await intake_cliente_assinado(
                    juridiq, person_id=person_id, nome=contrato["cliente_nome"],
                    telefone=telefone, email=contrato["cliente_email"],
                    tipo_caso=contrato["tipo_caso"],
                )
                if pid:
                    marcar_passo_pos_assinatura(
                        conn, contrato_id, passo_em="intake_juridiq_em",
                        ref_col="person_id", ref_valor=pid,
                    )
                    person_id = pid

        # 2. ARQUIVO — baixa e grava o PDF assinado (independente do intake).
        if contrato["arquivo_pdf_em"] is None:
            houve_acao = True
            caminho = await arquivar_pdf_assinado(
                zapsign, signed_file_url=signed_file_url, contrato_id=contrato_id,
            )
            if caminho:
                marcar_passo_pos_assinatura(
                    conn, contrato_id, passo_em="arquivo_pdf_em",
                    ref_col="signed_file_path", ref_valor=caminho,
                )

        # 3. TAREFA — abertura do caso (precisa de person_id do intake).
        if contrato["tarefa_abertura_em"] is None and person_id:
            houve_acao = True
            task_id = await criar_tarefa_abertura(
                juridiq, person_id=person_id, cliente_nome=contrato["cliente_nome"],
                tipo_caso=contrato["tipo_caso"], column_id=settings.task_column_id,
                priority=settings.task_priority,
                # DATE pura 'YYYY-MM-DD' — único formato comprovadamente aceito
                # pelo POST /task/ (descoberto no teste de campo 15/jun; espelha
                # montar_corpo_tarefa). isoformat() completo (com tz) é rejeitado.
                initial_date=datetime.datetime.now(_TZ_BR).date().isoformat(),
            )
            if task_id:
                marcar_passo_pos_assinatura(
                    conn, contrato_id, passo_em="tarefa_abertura_em",
                    ref_col="juridiq_task_id", ref_valor=task_id,
                )

        # Resumo pro Mario — só se algo foi tentado nesta reentrega (evita spam
        # na reentrega já-tudo-feito do webhook).
        if houve_acao and jurichat is not None and settings.mario_conversation_id:
            await notify_mario(
                jurichat,
                mario_conversation_id=settings.mario_conversation_id,
                mensagem=_resumo_pos(conn, contrato_id, contrato["cliente_nome"]),
            )
    except Exception:
        logger.exception("processar_pos_assinatura erro contrato=%s", contrato_id)
