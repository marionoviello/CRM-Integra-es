"""Orquestrador do pipeline de fechamento de contrato (escopos→Asaas→ZapSign).

Conecta as primitivas já prontas (contrato.py, asaas.py, escopos.py,
zapsign_client.py) no fluxo com GATE HUMANO sobre o PDF REAL:

  [1] CONFLITO bloqueia ANTES de qualquer chamada externa (nenhuma cobrança
      nem doc se há suspeita de impedimento — decisão é humana).
  [2] ESCOPO determinístico (a IA só seleciona o tipo, nunca redige cláusula).
  [3] contrato em MONTAGEM (tokens de aprovar/reprovar distintos).
  [4] ASAAS com DEDUPE obrigatório (find_payment_by_external_reference antes de
      create_payment — 2 POSTs = 2 cobranças, proibido). Falha → fica em
      MONTAGEM (Mario retenta), NADA no ZapSign.
  [5] ZAPSIGN em SILÊNCIO: send_automatic_email=False FORÇADO (INVARIANTE — o
      cliente não recebe nada até a aprovação). CLAIM atômico MONTAGEM→
      CRIANDO_DOC; idempotente por zapsign_doc_token. → PENDENTE_REVISAO.
  [7a] aprovar_e_liberar: resend_notifications_bulk LIBERA a assinatura. → LIBERADO.
  [7b] reprovar_contrato: refuse no ZapSign + cancela a cobrança Asaas SÓ se
      PENDING/OVERDUE (lê o status FRESCO antes; cobrança paga NÃO é deletada —
      vira estorno manual). NUNCA refund/transfer/saque. → REPROVADO.

IDEMPOTÊNCIA POR CHAVE DE NEGÓCIO (cpf+tipo_caso): um duplo-comando "gerar
contrato" do mesmo cliente/caso REUSA o contrato aberto (retoma o pipeline de
onde parou) em vez de criar um 2º — caso contrário nasceriam 2 cobranças vivas.
O retry reusa o MESMO external_ref (contrato-<id>) pra que
find_payment_by_external_reference reencontre a cobrança (dedupe) e o create-doc
seja retentado de forma idempotente.

Honorários SEMPRE vêm do parâmetro (valor digitado pelo Mario) — a IA nunca
precifica. Toda transição é auditada em contrato_transicao.
"""

import contextlib
import logging
import re
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .conflito import checar_conflito
from .contrato import (
    EstadoContrato,
    _inserir_transicao,
    criar_contrato_pipeline,
    formatar_valor_brl,
    get_contrato,
    link_aprovacao,
    montar_data_contrato,
    montar_signer,
    registrar_cobranca,
    registrar_doc_preview,
    transicao_contrato,
)
from .escopos import resolver_escopo

logger = logging.getLogger(__name__)


def montar_signers_padrao(settings: Any) -> list[dict[str, Any]]:
    """Signatários FIXOS (da config) pro ``signers_extra`` do gerar_contrato:
    escritório (order_group 2, contra-assina depois do cliente) + 2 testemunhas
    (order_group 3). O cliente (order_group 1) é montado dentro do orquestrador.
    Signatário sem e-mail é omitido (não teria como receber o link)."""
    signers: list[dict[str, Any]] = []
    if settings.contrato_escritorio_email:
        signers.append(montar_signer(
            name=settings.contrato_escritorio_nome or "Escritório",
            email=settings.contrato_escritorio_email,
            cpf=settings.contrato_escritorio_cpf or None,
            qualification="Contratado", order_group=2,
        ))
    testemunhas = (
        (settings.contrato_testemunha_1_nome,
         settings.contrato_testemunha_1_email,
         settings.contrato_testemunha_1_cpf),
        (settings.contrato_testemunha_2_nome,
         settings.contrato_testemunha_2_email,
         settings.contrato_testemunha_2_cpf),
    )
    for nome, email, cpf in testemunhas:
        if email:
            signers.append(montar_signer(
                name=nome or "Testemunha", email=email, cpf=cpf or None,
                qualification="Testemunha", order_group=3,
            ))
    return signers


def _payload_add_signer(signer: dict[str, Any]) -> dict[str, Any]:
    """Corpo do add-signer (escritório/testemunha) a partir de um dict
    ``montar_signer``. Mantém os campos que a API documenta (name/email/phone/
    qualification) + ``send_automatic_email`` SEMPRE False (silêncio até a
    aprovação). order_group/cpf NÃO entram (a API do add-signer não os expõe; a
    ordem vem da própria sequência de adição)."""
    out: dict[str, Any] = {"name": signer["name"], "send_automatic_email": False}
    for chave in ("email", "phone_country", "phone_number", "qualification"):
        if signer.get(chave):
            out[chave] = signer[chave]
    return out


# Estados ABERTOS (um contrato nesses estados ainda está em curso — é o que o
# lookup de idempotência por chave de negócio considera "já em andamento").
_ESTADOS_ABERTOS: tuple[str, ...] = (
    EstadoContrato.MONTAGEM,
    EstadoContrato.CRIANDO_DOC,
    EstadoContrato.PENDENTE_REVISAO,
)

# Regex pra detectar placeholder {{...}} residual nos valores do data[] antes
# do create-doc (defesa: um placeholder não-resolvido vaza CRAVADO no PDF).
_PLACEHOLDER_RESIDUAL = re.compile(r"\{\{[^}]+\}\}")


def _cpf_digitos(cpf: str | None) -> str:
    return re.sub(r"\D", "", cpf or "")


def _quantizar_valor(valor: float) -> float:
    """Arredonda o float a 2 casas (ROUND_HALF_UP) para Asaas e contrato baterem
    nos centavos (1234.999 → 1235.0; sem isto o Asaas levaria 1234.999 e o
    contrato mostraria 1.235,00)."""
    return float(Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _buscar_contrato_aberto(
    conn: sqlite3.Connection, cpf_digitos: str, tipo_caso: str,
) -> sqlite3.Row | None:
    """Lookup de idempotência: contrato ABERTO pra (cpf, tipo_caso). O mais
    recente, caso haja mais de um (não deveria, dado o índice único parcial)."""
    return conn.execute(
        "SELECT * FROM contrato WHERE cpf = ? AND tipo_caso = ? "
        "AND estado IN (?, ?, ?) ORDER BY id DESC LIMIT 1",
        (cpf_digitos, tipo_caso, *_ESTADOS_ABERTOS),
    ).fetchone()


async def gerar_contrato(
    conn: sqlite3.Connection,
    asaas: Any,
    zapsign: Any,
    *,
    cliente: dict[str, Any],
    tipo_caso: str,
    valor_honorarios: float,
    valor_extenso: str,
    template_id: str,
    signers_extra: list[dict[str, Any]],
    due_date: str,
    base_url: str = "",
    person_id: str | None = None,
    lead_id: int | None = None,
) -> dict[str, Any]:
    """Executa [1]-[6]: conflito → escopo → contrato → Asaas → ZapSign silêncio.

    Retorna um dict com ``status``:
      - 'bloqueado_conflito' → suspeita de impedimento (nada criado).
      - 'valor_invalido' → honorários <= 0 (nada criado).
      - 'erro_valor_extenso' → valor_extenso vazio (nada criado).
      - 'cpf_invalido' → cpf sem 11 dígitos (nada criado).
      - 'sem_canal_contato' → cliente sem email nem celular (nada criado).
      - 'escopo_indisponivel' → tipo_caso sem escopo na biblioteca.
      - 'ja_em_andamento' → já existe contrato aberto pra esse (cpf, tipo_caso);
        RETOMA o pipeline e devolve os links (NÃO cria 2ª cobrança).
      - 'erro_asaas' → cobrança falhou; contrato fica em MONTAGEM (Mario retenta).
      - 'pendente_revisao' → doc criado em SILÊNCIO; devolve link de aprovação,
        sign_url (PDF real) e invoice_url.
    """
    cliente_nome = cliente["nome_completo"]

    # [0] VALIDAÇÕES DE ENTRADA (barram cedo, ANTES de tocar Asaas/ZapSign ou
    # persistir contrato). Ordem: depois do nome, antes de conflito/escopo.
    if not (valor_honorarios and valor_honorarios > 0):
        logger.warning("honorários inválidos (<=0) pra cliente=%r", cliente_nome)
        return {"status": "valor_invalido", "detalhe": "honorários <= 0"}
    if not (valor_extenso or "").strip():
        logger.warning("valor_extenso vazio pra cliente=%r", cliente_nome)
        return {"status": "erro_valor_extenso",
                "detalhe": "valor por extenso obrigatório"}
    cpf_digitos = _cpf_digitos(cliente.get("cpf"))
    if len(cpf_digitos) != 11:
        logger.warning("cpf inválido pra cliente=%r", cliente_nome)
        return {"status": "cpf_invalido", "detalhe": "cpf precisa de 11 dígitos"}
    if not (cliente.get("email") or cliente.get("celular")):
        logger.warning("cliente sem canal de contato (cliente=%r)", cliente_nome)
        return {"status": "sem_canal_contato",
                "detalhe": "cliente sem email nem celular — informe ao menos um"}

    # [1] CONFLITO (bloqueante, ANTES de qualquer chamada externa).
    conflitos = checar_conflito(conn, cliente_nome)
    if conflitos:
        logger.warning(
            "contrato BLOQUEADO por conflito (cliente=%r, %d processo(s))",
            cliente_nome, len(conflitos),
        )
        return {"status": "bloqueado_conflito", "conflitos": conflitos}

    # [2] ESCOPO (determinístico; placeholders monetários pré-resolvidos).
    valor_q = _quantizar_valor(valor_honorarios)
    valor_fmt = formatar_valor_brl(valor_q)
    escopo = resolver_escopo(
        tipo_caso,
        substituicoes={
            "{{VALOR_HONORARIOS}}": valor_fmt,
            "{{VALOR_HONORARIOS_EXTENSO}}": valor_extenso,
        },
    )
    if escopo is None:
        logger.warning("escopo indisponível para tipo_caso=%r", tipo_caso)
        return {"status": "escopo_indisponivel", "tipo_caso": tipo_caso}

    # [3] IDEMPOTÊNCIA por chave de negócio: se já há contrato ABERTO pra esse
    # (cpf, tipo_caso), RETOMA-o (não cria um 2º → não nasce 2ª cobrança).
    aberto = _buscar_contrato_aberto(conn, cpf_digitos, tipo_caso)
    if aberto is not None:
        logger.info(
            "duplo-comando: contrato %s já aberto pra (cpf, %s) — retomando",
            aberto["id"], tipo_caso,
        )
        return await continuar_contrato(
            conn, asaas, zapsign,
            contrato_id=aberto["id"],
            cliente=cliente, escopo=escopo, tipo_caso=tipo_caso,
            valor_q=valor_q, valor_fmt=valor_fmt, valor_extenso=valor_extenso,
            template_id=template_id, signers_extra=signers_extra,
            due_date=due_date, base_url=base_url,
        )

    # [3b] PERSISTE o contrato NOVO em MONTAGEM.
    contrato = criar_contrato_pipeline(
        conn,
        cliente_nome=cliente_nome,
        cpf=cpf_digitos,
        tipo_caso=tipo_caso,
        valor_honorarios_fmt=valor_fmt,
        template_id=template_id,
        person_id=person_id,
        lead_id=lead_id,
    )
    return await continuar_contrato(
        conn, asaas, zapsign,
        contrato_id=contrato["id"],
        cliente=cliente, escopo=escopo, tipo_caso=tipo_caso,
        valor_q=valor_q, valor_fmt=valor_fmt, valor_extenso=valor_extenso,
        template_id=template_id, signers_extra=signers_extra,
        due_date=due_date, base_url=base_url,
    )


async def continuar_contrato(
    conn: sqlite3.Connection,
    asaas: Any,
    zapsign: Any,
    *,
    contrato_id: int,
    cliente: dict[str, Any],
    escopo: dict[str, Any],
    tipo_caso: str,
    valor_q: float,
    valor_fmt: str,
    valor_extenso: str,
    template_id: str,
    signers_extra: list[dict[str, Any]],
    due_date: str,
    base_url: str = "",
) -> dict[str, Any]:
    """Retoma uma linha de contrato ABERTA (MONTAGEM/CRIANDO_DOC/PENDENTE_REVISAO)
    de onde parou, REUSANDO o mesmo external_ref=contrato-<id>.

    É o coração da idempotência: o mesmo external_ref garante que
    find_payment_by_external_reference reencontre a cobrança já criada (recupera
    órfã, não duplica) e que o create-doc seja retentado idempotente. Chamado
    tanto pelo caminho NOVO (contrato recém-criado em MONTAGEM) quanto pelo
    duplo-comando (contrato já aberto).
    """
    cliente_nome = cliente["nome_completo"]
    external_ref = f"contrato-{contrato_id}"

    # [4] ASAAS (DEDUPE obrigatório). O payment_id é persistido IMEDIATAMENTE
    # após create_payment, em passo próprio: separa a falha de persistência
    # (cobrança VIVA sem registro — alertar) da falha do Asaas. Falha → fica em
    # estado aberto (Mario retenta; o retry reusa este external_ref).
    try:
        existente = await asaas.find_payment_by_external_reference(external_ref)
    except Exception as exc:  # noqa: BLE001 — falha de cobrança não derruba o caller
        logger.exception("asaas find falhou (contrato=%s): %s", contrato_id, exc)
        return {"status": "erro_asaas", "contrato_id": contrato_id,
                "detalhe": type(exc).__name__}

    if existente:
        payment_id = existente.get("id")
        invoice_url = existente.get("invoiceUrl")
        customer_id = existente.get("customer")
        # Dedupe defensivo: existente parcial (sem invoiceUrl mas com id) →
        # completa via get_payment, em vez de cair em 'sem_invoice_url'. E não
        # sobrescreve customer_id válido com None.
        if payment_id and not invoice_url:
            with contextlib.suppress(Exception):
                completo = await asaas.get_payment(payment_id)
                invoice_url = completo.get("invoiceUrl") or invoice_url
                customer_id = completo.get("customer") or customer_id
        if customer_id is None:
            row = get_contrato(conn, contrato_id)
            if row is not None and row["asaas_customer_id"]:
                customer_id = row["asaas_customer_id"]
    else:
        try:
            customer_id = await asaas.get_or_create_customer(
                name=cliente_nome,
                cpf=_cpf_digitos(cliente.get("cpf")),
                email=cliente.get("email"),
                mobile_phone=cliente.get("celular"),
            )
            pay = await asaas.create_payment(
                customer_id=customer_id,
                value=valor_q,
                due_date=due_date,
                description=f"Honorários advocatícios - {tipo_caso}",
                external_reference=external_ref,
            )
        except Exception as exc:  # noqa: BLE001 — falha de cobrança não derruba o caller
            logger.exception("asaas falhou (contrato=%s): %s", contrato_id, exc)
            return {"status": "erro_asaas", "contrato_id": contrato_id,
                    "detalhe": type(exc).__name__}
        payment_id = pay.get("id")
        invoice_url = pay.get("invoiceUrl")
        # PERSISTE o payment_id IMEDIATAMENTE (passo próprio): a cobrança nasceu
        # VIVA — se gravar falhar, a cobrança é órfã. O retry reusa este
        # external_ref e o find a recupera (não duplica).
        try:
            registrar_cobranca(
                conn, contrato_id,
                customer_id=customer_id, payment_id=payment_id,
                invoice_url=invoice_url,
            )
        except Exception as exc:  # noqa: BLE001 — persistência falhou, cobrança VIVA
            logger.exception(
                "cobrança VIVA sem persistência (contrato=%s, ref=%s, "
                "payment_id=%s) — checar Asaas no retry: %s",
                contrato_id, external_ref, payment_id, exc,
            )
            return {"status": "erro_asaas", "contrato_id": contrato_id,
                    "detalhe": "persistencia_cobranca",
                    "cobranca_viva_sem_registro": True}
        return await _finalizar_apos_cobranca(
            conn, zapsign, contrato_id=contrato_id, cliente=cliente,
            escopo=escopo, valor_fmt=valor_fmt, valor_extenso=valor_extenso,
            invoice_url=invoice_url, template_id=template_id,
            signers_extra=signers_extra, base_url=base_url,
        )

    # Caminho do existente (dedupe encontrou): grava (preservando customer
    # válido) e segue. registrar_cobranca em passo próprio com try dedicado.
    try:
        registrar_cobranca(
            conn, contrato_id,
            customer_id=customer_id, payment_id=payment_id,
            invoice_url=invoice_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "falha ao registrar cobrança existente (contrato=%s): %s",
            contrato_id, exc,
        )
        return {"status": "erro_asaas", "contrato_id": contrato_id,
                "detalhe": "persistencia_cobranca"}

    return await _finalizar_apos_cobranca(
        conn, zapsign, contrato_id=contrato_id, cliente=cliente,
        escopo=escopo, valor_fmt=valor_fmt, valor_extenso=valor_extenso,
        invoice_url=invoice_url, template_id=template_id,
        signers_extra=signers_extra, base_url=base_url,
    )


async def _finalizar_apos_cobranca(
    conn: sqlite3.Connection,
    zapsign: Any,
    *,
    contrato_id: int,
    cliente: dict[str, Any],
    escopo: dict[str, Any],
    valor_fmt: str,
    valor_extenso: str,
    invoice_url: str | None,
    template_id: str,
    signers_extra: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    """Após a cobrança garantida (criada ou reusada), valida o invoice_url e
    dispara o create-doc silencioso."""
    if not invoice_url:
        logger.error("asaas sem invoiceUrl (contrato=%s)", contrato_id)
        return {"status": "erro_asaas", "contrato_id": contrato_id,
                "detalhe": "sem_invoice_url"}

    # [5] ZAPSIGN em SILÊNCIO (send_automatic_email=False FORÇADO).
    return await _criar_doc_silencioso(
        conn, zapsign,
        contrato=get_contrato(conn, contrato_id),
        cliente=cliente, escopo=escopo,
        valor_fmt=valor_fmt, valor_extenso=valor_extenso,
        invoice_url=invoice_url, template_id=template_id,
        signers_extra=signers_extra, base_url=base_url,
    )


async def _criar_doc_silencioso(
    conn: sqlite3.Connection,
    zapsign: Any,
    *,
    contrato: sqlite3.Row,
    cliente: dict[str, Any],
    escopo: dict[str, Any],
    valor_fmt: str,
    valor_extenso: str,
    invoice_url: str,
    template_id: str,
    signers_extra: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    """[5]/[6]: cria o doc em silêncio sob CLAIM atômico e devolve os links.

    INVARIANTE: send_automatic_email SEMPRE False em TODOS os signatários
    (cliente não recebe nada até a aprovação) — a flag por-signer é forçada
    aqui, defesa em profundidade que independe da precedência da ZapSign.
    Idempotência: se o contrato já tem zapsign_doc_token, NÃO re-cria
    (reconcilia o estado e devolve o que já existe).
    """
    contrato_id = contrato["id"]

    def _resultado(c: sqlite3.Row) -> dict[str, Any]:
        return {
            "status": "pendente_revisao",
            "contrato_id": contrato_id,
            "link_aprovacao": (
                link_aprovacao(base_url, c["aprovacao_token"]) if base_url else None
            ),
            "sign_url": c["sign_url"],
            "invoice_url": c["invoice_url"],
        }

    # Idempotência: doc já criado → reconcilia e sai (sem re-chamar a ZapSign).
    if contrato["zapsign_doc_token"]:
        if contrato["estado"] in (
            EstadoContrato.MONTAGEM, EstadoContrato.CRIANDO_DOC,
        ):
            transicao_contrato(
                conn, contrato_id, EstadoContrato.PENDENTE_REVISAO,
                motivo="reconciliação: doc já existia", ator="sistema",
            )
        return _resultado(get_contrato(conn, contrato_id))

    # Se o contrato já está em PENDENTE_REVISAO (retomada de duplo-comando com
    # o doc já criado num passo anterior), devolve os links sem re-criar.
    if contrato["estado"] == EstadoContrato.PENDENTE_REVISAO:
        return _resultado(contrato)

    data = montar_data_contrato(
        cliente, escopo,
        valor_fmt=valor_fmt, valor_extenso=valor_extenso,
        link_pagamento=invoice_url,
    )

    # Defesa: placeholder {{...}} residual num valor do data[] vaza CRAVADO no
    # PDF (a ZapSign não substitui placeholder DENTRO de um valor). Aborta.
    for par in data:
        if _PLACEHOLDER_RESIDUAL.search(par["para"]):
            logger.error(
                "placeholder residual no data[] (contrato=%s, de=%s): %r",
                contrato_id, par["de"], par["para"],
            )
            return {"status": "erro_placeholder_residual",
                    "contrato_id": contrato_id, "campo": par["de"]}

    # O create-doc-from-template só registra o signatário PRIMÁRIO via
    # signer_name (a API IGNORA qualquer array `signers`). O CLIENTE é o
    # primário (assina 1º). Escritório + testemunhas entram via add-signer
    # DEPOIS, na ORDEM de adição (= ordem de assinatura, signature_order_active).
    # A API não expõe CPF/order_group no primário — o CPF do cliente segue no
    # TEXTO do contrato (data[]); a ordem sai da sequência primário→add-signers.
    cliente_tel = re.sub(r"\D", "", str(cliente.get("celular") or ""))
    corpo: dict[str, Any] = {
        "template_id": template_id,
        "lang": "pt-br",
        "external_id": str(contrato_id),
        "signature_order_active": True,
        # INVARIANTE: silêncio total até a aprovação humana. NUNCA True.
        "send_automatic_email": False,
        "signer_name": cliente["nome_completo"],
        "data": data,
    }
    if cliente.get("email"):
        corpo["signer_email"] = cliente["email"]
    if cliente_tel:
        corpo["signer_phone_country"] = "55"
        corpo["signer_phone_number"] = cliente_tel

    # CLAIM atômico: MONTAGEM→CRIANDO_DOC sob lock (igual ao enviar_para_
    # assinatura). Só o vencedor (rowcount==1) chama a ZapSign.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE contrato SET estado = ?, atualizado_em = datetime('now') "
            "WHERE id = ? AND estado = ? AND zapsign_doc_token IS NULL",
            (EstadoContrato.CRIANDO_DOC, contrato_id, EstadoContrato.MONTAGEM),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return {"status": "em_andamento", "contrato_id": contrato_id}
        _inserir_transicao(
            conn, contrato_id, EstadoContrato.MONTAGEM,
            EstadoContrato.CRIANDO_DOC, motivo="claim create-doc (lock)",
            ator="sistema",
        )
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise

    # Fora da transação: o create-doc (POST não-idempotente). Falha → reverte
    # CRIANDO_DOC→MONTAGEM (Mario retenta sem novo toque).
    try:
        resp = await zapsign.create_doc_from_template(corpo)
    except Exception as exc:  # noqa: BLE001 — falha de rede não derruba o caller
        logger.exception("create-doc falhou (contrato=%s): %s", contrato_id, exc)
        transicao_contrato(
            conn, contrato_id, EstadoContrato.MONTAGEM,
            motivo=f"falha no create-doc: {type(exc).__name__}", ator="sistema",
        )
        return {"status": "erro_zapsign", "contrato_id": contrato_id,
                "detalhe": type(exc).__name__}

    doc_token = resp.get("token")
    if not doc_token:
        transicao_contrato(
            conn, contrato_id, EstadoContrato.MONTAGEM,
            motivo="resposta sem token", ator="sistema",
        )
        return {"status": "erro_zapsign", "contrato_id": contrato_id,
                "detalhe": "resposta_sem_token"}

    # Escritório + testemunhas via add-signer, NA ORDEM (= ordem de assinatura).
    # Se QUALQUER um falhar, o doc fica incompleto → apaga + reverte pra MONTAGEM
    # (Mario retenta e recria limpo). Por isso o token só é PERSISTIDO depois de
    # todos entrarem — assim o retry não acha um doc parcial e o marca pronto.
    try:
        for extra in signers_extra:
            await zapsign.add_signer(doc_token, _payload_add_signer(extra))
    except Exception as exc:  # noqa: BLE001 — add-signer parcial não pode "vazar"
        logger.exception(
            "add-signer falhou (contrato=%s, doc=%s): %s",
            contrato_id, doc_token, exc,
        )
        with contextlib.suppress(Exception):
            await zapsign.delete_doc(doc_token)
        transicao_contrato(
            conn, contrato_id, EstadoContrato.MONTAGEM,
            motivo=f"falha no add-signer: {type(exc).__name__}", ator="sistema",
        )
        return {"status": "erro_zapsign", "contrato_id": contrato_id,
                "detalhe": f"add_signer_{type(exc).__name__}"}

    signers = resp.get("signers") or []
    signer = signers[0] if signers else {}
    registrar_doc_preview(
        conn, contrato_id,
        doc_token=doc_token, sign_url=signer.get("sign_url"),
    )
    transicao_contrato(
        conn, contrato_id, EstadoContrato.PENDENTE_REVISAO,
        motivo="doc criado em silêncio (aguardando revisão)", ator="sistema",
    )
    return _resultado(get_contrato(conn, contrato_id))


async def aprovar_e_liberar(
    conn: sqlite3.Connection,
    zapsign: Any,
    *,
    token: str,
    ator: str = "mario",
) -> dict[str, Any]:
    """[7a] APROVA o PDF real e LIBERA a assinatura ao cliente. Idempotente.

    Acha o contrato por ``aprovacao_token``; só age se PENDENTE_REVISAO
    (clicar 2x não libera 2x). Carimba aprovado_em/aprovado_por (auditoria),
    CAS PENDENTE_REVISAO→LIBERANDO, resend_notifications_bulk (libera ao
    order_group 1 = cliente) → LIBERADO. Em falha do resend, reverte
    LIBERANDO→PENDENTE_REVISAO (Mario retenta).
    """
    contrato = conn.execute(
        "SELECT * FROM contrato WHERE aprovacao_token = ?", (token,),
    ).fetchone()
    if contrato is None:
        return {"status": "token_invalido"}
    contrato_id = contrato["id"]

    # Carimbo + CAS num único BEGIN IMMEDIATE (atômico): fecha a janela entre
    # carimbar e transicionar, e o CAS garante que 2 toques liberem UMA vez.
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT estado FROM contrato WHERE id = ?", (contrato_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return {"status": "token_invalido"}
        if row["estado"] != EstadoContrato.PENDENTE_REVISAO:
            # Idempotência: só libera quem está pendente de revisão.
            conn.execute("ROLLBACK")
            return {"status": "ja_processado", "contrato_id": contrato_id,
                    "estado": row["estado"]}
        conn.execute(
            "UPDATE contrato SET estado = ?, aprovado_em = datetime('now'), "
            "aprovado_por = ?, atualizado_em = datetime('now') "
            "WHERE id = ? AND estado = ?",
            (EstadoContrato.LIBERANDO, ator, contrato_id,
             EstadoContrato.PENDENTE_REVISAO),
        )
        _inserir_transicao(
            conn, contrato_id, EstadoContrato.PENDENTE_REVISAO,
            EstadoContrato.LIBERANDO, motivo="aprovação humana (1-toque)",
            ator=ator,
        )
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise

    # Guarda: sem doc_token NÃO há o que liberar (resend(None) é chamada
    # inválida à ZapSign). Reverte LIBERANDO→PENDENTE_REVISAO e sinaliza erro.
    doc_token = contrato["zapsign_doc_token"]
    if not doc_token:
        logger.error("aprovar sem doc_token (contrato=%s) — não libera", contrato_id)
        transicao_contrato(
            conn, contrato_id, EstadoContrato.PENDENTE_REVISAO,
            motivo="sem doc_token — não libera", ator="sistema",
        )
        return {"status": "erro_sem_doc_token", "contrato_id": contrato_id}

    try:
        await zapsign.resend_notifications_bulk(doc_token)
    except Exception as exc:  # noqa: BLE001 — falha de rede não derruba o caller
        logger.exception("resend falhou (contrato=%s): %s", contrato_id, exc)
        transicao_contrato(
            conn, contrato_id, EstadoContrato.PENDENTE_REVISAO,
            motivo=f"falha no resend: {type(exc).__name__}", ator="sistema",
        )
        return {"status": "erro_resend", "contrato_id": contrato_id,
                "detalhe": type(exc).__name__}

    transicao_contrato(
        conn, contrato_id, EstadoContrato.LIBERADO,
        motivo="assinatura liberada ao cliente", ator="sistema",
    )
    return {"status": "liberado", "contrato_id": contrato_id}


async def reprovar_contrato(
    conn: sqlite3.Connection,
    zapsign: Any,
    asaas: Any,
    *,
    token: str,
    motivo: str,
) -> dict[str, Any]:
    """[7b] REPROVA o PDF real: refuse no ZapSign + cancela a cobrança. Idempotente.

    Acha por ``reprovacao_token``; só age se PENDENTE_REVISAO. CAS →REPROVANDO.
    Ordem: refuse PRIMEIRO (barato/idempotente; o doc estava em silêncio, o
    cliente nunca soube), depois Asaas. Asaas: lê o status FRESCO (get_payment)
    antes de deletar — cobrança PENDING/OVERDUE é deletada; CONFIRMED/RECEIVED
    NÃO é deletada (dinheiro que entrou vira ESTORNO MANUAL — refund fica FORA
    do código). → REPROVADO.

    OBS (pós-LIBERADO): este caminho só age em PENDENTE_REVISAO. Cancelar a
    cobrança de um contrato JÁ LIBERADO é 100% MANUAL (não há via automática) —
    o retorno 'ja_processado' sinaliza que o estado já saiu da janela de revisão.

    Falha de get_payment/delete_payment NÃO é silenciosa: sinaliza
    ``cobranca_cancelamento_falhou=True`` + ``estorno_manual=True`` (por
    segurança, pode haver dinheiro pago não-lido) e usa motivo de verificação
    manual — pra o Mario conferir a cobrança no Asaas antes de encerrar.
    """
    contrato = conn.execute(
        "SELECT * FROM contrato WHERE reprovacao_token = ?", (token,),
    ).fetchone()
    if contrato is None:
        return {"status": "token_invalido"}
    contrato_id = contrato["id"]

    # CAS PENDENTE_REVISAO→REPROVANDO (idempotente: só reprova quem está pendente).
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT estado FROM contrato WHERE id = ?", (contrato_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return {"status": "token_invalido"}
        if row["estado"] != EstadoContrato.PENDENTE_REVISAO:
            conn.execute("ROLLBACK")
            return {"status": "ja_processado", "contrato_id": contrato_id,
                    "estado": row["estado"]}
        conn.execute(
            "UPDATE contrato SET estado = ?, atualizado_em = datetime('now') "
            "WHERE id = ? AND estado = ?",
            (EstadoContrato.REPROVANDO, contrato_id,
             EstadoContrato.PENDENTE_REVISAO),
        )
        _inserir_transicao(
            conn, contrato_id, EstadoContrato.PENDENTE_REVISAO,
            EstadoContrato.REPROVANDO, motivo=f"reprovação humana: {motivo}",
            ator="mario",
        )
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise

    # ZapSign: refuse primeiro (try/except benigno — refuse de doc já recusado
    # erra de forma inofensiva; o que importa é o doc ficar inassinável).
    doc_token = contrato["zapsign_doc_token"]
    if doc_token:
        with contextlib.suppress(Exception):
            await zapsign.refuse(doc_token, motivo)

    # Asaas: lê o status FRESCO antes de deletar. Cobrança paga NÃO é deletada.
    payment_id = contrato["asaas_payment_id"]
    estorno_manual = False
    cancelamento_falhou = False
    if payment_id:
        try:
            status = (await asaas.get_payment(payment_id)).get("status")
            if status in ("PENDING", "OVERDUE"):
                await asaas.delete_payment(payment_id)
            elif status in ("CONFIRMED", "RECEIVED"):
                # Dinheiro que entrou — NÃO deletar; vira estorno manual.
                estorno_manual = True
                logger.warning(
                    "contrato %s reprovado com cobrança PAGA (status=%s) — "
                    "ESTORNO MANUAL pro Mario", contrato_id, status,
                )
        except Exception as exc:  # noqa: BLE001 — não deixa o doc órfão meio-cancelado
            # NÃO é REPROVADO limpo: a cobrança pode estar VIVA (não lida/não
            # cancelada) ou até PAGA. Sinaliza alerta pro Mario conferir.
            cancelamento_falhou = True
            estorno_manual = True
            logger.exception(
                "asaas cancelamento falhou (contrato=%s, payment_id=%s) — "
                "cobrança possivelmente VIVA/PAGA, verificar manual: %s",
                contrato_id, payment_id, exc,
            )

    if cancelamento_falhou:
        motivo_trans = "reprovado — verificar cobrança manual (cancelamento falhou)"
    elif estorno_manual:
        motivo_trans = "estorno manual (cobrança paga)"
    else:
        motivo_trans = "contrato reprovado na revisão"
    transicao_contrato(
        conn, contrato_id, EstadoContrato.REPROVADO,
        motivo=motivo_trans, ator="sistema",
    )
    resultado: dict[str, Any] = {
        "status": "reprovado",
        "contrato_id": contrato_id,
        "estorno_manual": estorno_manual,
    }
    if cancelamento_falhou:
        resultado["cobranca_cancelamento_falhou"] = True
    return resultado


async def reconciliar_contratos_presos(
    conn: sqlite3.Connection,
    asaas: Any,
    zapsign: Any,
) -> list[dict[str, Any]]:
    """Destrava contratos presos em claims transitórios após crash entre o
    COMMIT do claim e a chamada externa. Re-executável manualmente (e por
    sweeper/scheduler quando existir). Retorna a lista de ações tomadas.

      - CRIANDO_DOC sem zapsign_doc_token → volta a MONTAGEM (nenhum doc foi
        de fato criado na ZapSign; Mario/retry recria).
      - LIBERANDO → re-tenta resend_notifications_bulk (idempotente); sucesso →
        LIBERADO; sem doc_token ou falha → volta a PENDENTE_REVISAO.
      - REPROVANDO → re-executa refuse + cancelamento Asaas (status fresco) →
        REPROVADO.
    """
    acoes: list[dict[str, Any]] = []

    # CRIANDO_DOC sem token → MONTAGEM.
    presos = conn.execute(
        "SELECT id FROM contrato WHERE estado = ? AND zapsign_doc_token IS NULL",
        (EstadoContrato.CRIANDO_DOC,),
    ).fetchall()
    for r in presos:
        cid = r["id"]
        transicao_contrato(
            conn, cid, EstadoContrato.MONTAGEM,
            motivo="reconciliação: CRIANDO_DOC sem token → MONTAGEM",
            ator="sistema",
        )
        acoes.append({"contrato_id": cid, "acao": "criando_doc_para_montagem"})

    # LIBERANDO → re-resend (idempotente) → LIBERADO; ou volta PENDENTE_REVISAO.
    liberando = conn.execute(
        "SELECT id, zapsign_doc_token FROM contrato WHERE estado = ?",
        (EstadoContrato.LIBERANDO,),
    ).fetchall()
    for r in liberando:
        cid = r["id"]
        doc_token = r["zapsign_doc_token"]
        if not doc_token:
            transicao_contrato(
                conn, cid, EstadoContrato.PENDENTE_REVISAO,
                motivo="reconciliação: LIBERANDO sem doc_token → revisão",
                ator="sistema",
            )
            acoes.append({"contrato_id": cid, "acao": "liberando_sem_token"})
            continue
        try:
            await zapsign.resend_notifications_bulk(doc_token)
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconciliação resend falhou (contrato=%s): %s", cid, exc)
            transicao_contrato(
                conn, cid, EstadoContrato.PENDENTE_REVISAO,
                motivo="reconciliação: resend falhou → revisão", ator="sistema",
            )
            acoes.append({"contrato_id": cid, "acao": "liberando_resend_falhou"})
            continue
        transicao_contrato(
            conn, cid, EstadoContrato.LIBERADO,
            motivo="reconciliação: resend re-tentado → LIBERADO", ator="sistema",
        )
        acoes.append({"contrato_id": cid, "acao": "liberado"})

    # REPROVANDO → re-refuse + re-cancela Asaas → REPROVADO.
    reprovando = conn.execute(
        "SELECT id, zapsign_doc_token, asaas_payment_id FROM contrato "
        "WHERE estado = ?",
        (EstadoContrato.REPROVANDO,),
    ).fetchall()
    for r in reprovando:
        cid = r["id"]
        doc_token = r["zapsign_doc_token"]
        payment_id = r["asaas_payment_id"]
        if doc_token:
            with contextlib.suppress(Exception):
                await zapsign.refuse(doc_token, "reprovação (reconciliação)")
        estorno_manual = False
        if payment_id:
            with contextlib.suppress(Exception):
                status = (await asaas.get_payment(payment_id)).get("status")
                if status in ("PENDING", "OVERDUE"):
                    await asaas.delete_payment(payment_id)
                elif status in ("CONFIRMED", "RECEIVED"):
                    estorno_manual = True
        transicao_contrato(
            conn, cid, EstadoContrato.REPROVADO,
            motivo=(
                "reconciliação: estorno manual (cobrança paga)" if estorno_manual
                else "reconciliação: reprovação finalizada"
            ),
            ator="sistema",
        )
        acoes.append({"contrato_id": cid, "acao": "reprovado",
                      "estorno_manual": estorno_manual})

    return acoes
