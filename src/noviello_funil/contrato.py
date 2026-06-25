"""Fechamento de contrato de honorários por assinatura eletrônica (3.x).

Fluxo 1-TOQUE (decisões 15/jun):
  1. Mario COMANDA "gerar contrato do Fulano" (gatilho 100% humano).
  2. O bot checa CONFLITO (bloqueante) e monta a minuta → estado
     ``pendente_aprovacao``. Nenhuma chamada à ZapSign ainda.
  3. Mario APROVA com 1 toque (link tokenizado) → estado ``aprovado``.
  4. SÓ ENTÃO o create-doc é chamado → estado ``enviado``; a ZapSign manda
     o link de assinatura ao cliente.
  5. Webhook ``doc_signed`` confirma → estado ``assinado`` → intake/arquivo.

Garantia OAB (Prov. 205/2021, mandato personalíssimo): ``enviar_para_assinatura``
só chama o create-doc se o contrato está em ``aprovado`` E carrega o CARIMBO de
aprovação humana (``aprovado_em``/``aprovado_por``, que SÓ ``aprovar()`` grava) —
o estado sozinho seria um proxy fraco, já que ``transicao_contrato`` é uma
primitiva genérica. O envio é um CLAIM atômico (CAS ``aprovado``→``enviando`` sob
``BEGIN IMMEDIATE``): dois toques concorrentes geram UM só documento, e um POST
não-idempotente nunca é retentado às cegas. valor_honorarios é SEMPRE digitado
pelo Mario (a IA nunca precifica). Toda transição é auditada em
``contrato_transicao`` — a trilha que torna o 1-toque defensável.

Este módulo é o ÚNICO que lê/escreve as tabelas ``contrato``/
``contrato_transicao`` (mesma disciplina do ``state.py``).
"""

import contextlib
import logging
import re
import secrets
import sqlite3
from typing import Any, Final

from .conflito import checar_conflito

logger = logging.getLogger(__name__)


class EstadoContrato:
    """Estados do contrato. TEXT puro no banco (sem CHECK, migrações leves)."""
    PENDENTE_APROVACAO: Final = "contrato_pendente_aprovacao"
    APROVADO: Final = "contrato_aprovado"
    ENVIANDO: Final = "contrato_enviando"   # claim transitório (create-doc em voo)
    ENVIADO: Final = "contrato_enviado"
    ASSINADO: Final = "contrato_assinado"
    RECUSADO: Final = "contrato_recusado"
    EXPIRADO: Final = "contrato_expirado"
    # Pipeline NOVO (escopos→Asaas→ZapSign silencioso→gate sobre o PDF real).
    # O doc é criado EM SILÊNCIO (send_automatic_email=False) e fica em
    # PENDENTE_REVISAO até o Mario aprovar (LIBERANDO→LIBERADO) ou reprovar
    # (REPROVANDO→REPROVADO). MONTAGEM/CRIANDO_DOC são claims transitórios.
    MONTAGEM: Final = "contrato_montagem"
    CRIANDO_DOC: Final = "contrato_criando_doc"      # claim (create-doc em voo)
    PENDENTE_REVISAO: Final = "contrato_pendente_revisao"
    LIBERANDO: Final = "contrato_liberando"          # claim (resend em voo)
    LIBERADO: Final = "contrato_liberado"
    REPROVANDO: Final = "contrato_reprovando"        # claim (refuse+cancela em voo)
    REPROVADO: Final = "contrato_reprovado"


# --- Montagem da minuta (puro/testável) ----------------------------------

def montar_minuta(
    *,
    placeholders: dict[str, str],
    valores: dict[str, str],
) -> list[dict[str, str]]:
    """Array ``data`` do create-doc: ``[{"de": "{{X}}", "para": valor}]``.

    ``placeholders`` mapeia campo-semântico → string EXATA do template do
    Mario (ex.: ``{"valor_honorarios": "{{HONORÁRIOS}}"}``) — injetado em
    runtime, nunca hardcodado (cada template tem nomes próprios).
    ``valores`` mapeia campo-semântico → valor real.

    ``valores['valor_honorarios']`` é OBRIGATÓRIO e não-vazio: o valor é
    SEMPRE digitado por humano (decisão 15/jun) — a IA não precifica.
    Campos sem valor são omitidos (o placeholder fica em branco no doc).
    """
    if not (valores.get("valor_honorarios") or "").strip():
        raise ValueError("valor_honorarios obrigatório (humano sempre digita)")
    if "valor_honorarios" not in placeholders:
        raise ValueError(
            "placeholder de honorários ausente no mapa — o valor digitado pelo "
            "Mario não entraria no documento"
        )
    data: list[dict[str, str]] = []
    for chave, placeholder in placeholders.items():
        val = (valores.get(chave) or "").strip()
        if val:
            data.append({"de": placeholder, "para": val})
    return data


def montar_corpo_create_doc(
    *,
    template_id: str,
    signer_name: str,
    signer_email: str | None,
    data: list[dict[str, str]],
    external_id: str,
    send_automatic_email: bool = True,
) -> dict[str, Any]:
    """Corpo do ``POST /models/create-doc/``.

    ``external_id`` = id interno do contrato (reconciliação + idempotência).
    ``send_automatic_email`` só liga se houver email (a ZapSign manda o link
    de assinatura ao cliente). Sem email, o link sai pelo sign_url (a equipe
    repassa) — nunca quebra.
    """
    body: dict[str, Any] = {
        "template_id": template_id,
        "signer_name": signer_name,
        "data": data,
        "external_id": external_id,
        "lang": "pt-br",
        "send_automatic_email": bool(send_automatic_email and signer_email),
    }
    if signer_email:
        body["signer_email"] = signer_email
    return body


def montar_signer(
    *,
    name: str,
    email: str | None = None,
    telefone: str | None = None,
    qualification: str | None = None,
    order_group: int | None = None,
    cpf: str | None = None,
) -> dict[str, Any]:
    """Um signatário do POST /docs/ (caminho B).

    ``send_automatic_email`` só liga se houver email (a ZapSign manda o link
    por email). ``cpf`` (só dígitos) entra como validação no ato da assinatura;
    é PII — vem de config (.env), nunca de código. ``qualification`` aparece no
    relatório ('Assinou como testemunha'). ``order_group`` controla a ordem
    quando ``signature_order_active`` está ligado no documento.
    """
    s: dict[str, Any] = {"name": name, "send_automatic_email": bool(email)}
    if email:
        s["email"] = email
    if telefone:
        digitos = re.sub(r"\D", "", telefone)
        s["phone_country"] = "55"
        s["phone_number"] = digitos
    if qualification:
        s["qualification"] = qualification
    if order_group is not None:
        s["order_group"] = order_group
    if cpf:
        s["cpf"] = re.sub(r"\D", "", cpf)
    return s


def montar_corpo_upload(
    *,
    name: str,
    base64_pdf: str,
    signers: list[dict[str, Any]],
    external_id: str,
    signature_order: bool = True,
) -> dict[str, Any]:
    """Corpo do ``POST /docs/`` (caminho B: PDF que NÓS geramos).

    ``signers`` já vem na ordem (cliente order_group 1 → escritório 2 →
    testemunhas 3). ``signature_order`` liga a assinatura SEQUENCIAL (cliente
    assina, depois o escritório, depois as testemunhas). ``base64_pdf`` sem o
    prefixo ``data:...``; ``external_id`` = id interno do contrato.
    """
    return {
        "name": name[:255],
        "base64_pdf": base64_pdf,
        "lang": "pt-br",
        "external_id": external_id,
        "signature_order_active": bool(signature_order),
        "signers": signers,
    }


def gerar_aprovacao_token() -> str:
    """Token urlsafe do link de aprovação 1-toque (imprevisível)."""
    return secrets.token_urlsafe(32)


def link_aprovacao(base_url: str, token: str) -> str:
    """Link de aprovação 1-toque que vai pro WhatsApp do Mario. Cai numa
    PÁGINA de confirmação (GET, sem efeito) — a aprovação real é o POST do
    botão, pra prefetcher de link não 'tapar' sozinho."""
    return f"{base_url.rstrip('/')}/zapsign/aprovar/{token}"


# --- Pipeline NOVO: montagem do data[] sobre o template real (puro) -------

# Mapa EXATO {{VAR}} do template → chave do dict ``cliente`` (spec do Mario).
# São os dados pessoais que o cliente forneceu; PII vem do comando/ficha,
# nunca de código.
_VARS_CLIENTE: Final[dict[str, str]] = {
    "{{NOME_COMPLETO}}": "nome_completo",
    "{{NACIONALIDADE}}": "nacionalidade",
    "{{ESTADO_CIVIL}}": "estado_civil",
    "{{PROFISSAO}}": "profissao",
    "{{RG}}": "rg",
    "{{ORGAO_EMISSOR}}": "orgao_emissor",
    "{{CPF}}": "cpf",
    "{{LOGRADOURO}}": "logradouro",
    "{{NUMERO}}": "numero",
    "{{COMPLEMENTO}}": "complemento",
    "{{BAIRRO}}": "bairro",
    "{{CIDADE}}": "cidade",
    "{{UF}}": "uf",
    "{{CEP}}": "cep",
    "{{CELULAR}}": "celular",
    "{{EMAIL}}": "email",
}

# Mapa {{VAR}} → chave do dict ``escopo`` (texto curado, vetado à IA).
_VARS_ESCOPO: Final[dict[str, str]] = {
    "{{AREA_ATUACAO}}": "area_atuacao",
    "{{OBJETO_CONTRATO}}": "objeto_contrato",
    "{{CONTEXTO_NORMATIVO}}": "contexto_normativo",
    "{{DESCRICAO_HONORARIOS}}": "descricao_honorarios",
}


def montar_data_contrato(
    cliente: dict[str, Any],
    escopo: dict[str, Any],
    *,
    valor_fmt: str,
    valor_extenso: str,
    link_pagamento: str,
) -> list[dict[str, str]]:
    """Array ``data`` do create-doc: ``[{"de": "{{VAR}}", "para": valor}]``.

    Mapeamento EXATO do template real (spec do Mario): dados pessoais de
    ``cliente``, texto curado de ``escopo``, e os 3 valores monetários/link
    passados à parte (o valor SEMPRE vem do humano, a IA nunca precifica).
    Pares com valor vazio/None são OMITIDOS (o placeholder fica em branco no
    doc) — mesma disciplina de ``montar_minuta``.
    """
    # COMPLEMENTO carrega a própria vírgula: no template o trecho é
    # "nº {{NUMERO}}{{COMPLEMENTO}}, {{BAIRRO}}" (SEM vírgula antes do
    # placeholder). Assim endereço sem complemento sai "nº 100, Bairro" em vez
    # de "nº 100, , Bairro"; com complemento sai "nº 100, apto 12, Bairro".
    _compl = str(cliente.get("complemento") or "").strip()
    cliente = {**cliente, "complemento": f", {_compl}" if _compl else ""}
    fontes: list[tuple[dict[str, str], dict[str, Any]]] = [
        (_VARS_CLIENTE, cliente),
        (_VARS_ESCOPO, escopo),
    ]
    extras: dict[str, Any] = {
        "{{VALOR_HONORARIOS}}": valor_fmt,
        "{{VALOR_HONORARIOS_EXTENSO}}": valor_extenso,
        "{{LINK_PAGAMENTO}}": link_pagamento,
    }
    data: list[dict[str, str]] = []
    for mapa, origem in fontes:
        for var, chave in mapa.items():
            val = str(origem.get(chave) or "").strip()
            if val:
                data.append({"de": var, "para": val})
    for var, valor in extras.items():
        val = str(valor or "").strip()
        if val:
            data.append({"de": var, "para": val})
    return data


def formatar_valor_brl(valor: float) -> str:
    """3500.0 → ``"3.500,00"`` (milhar com ponto, decimal com vírgula).

    Formata pro corpo do contrato e pro Asaas value (que recebe o float cru).
    """
    inteiro = f"{valor:,.2f}"           # 1234567.5 → '1,234,567.50' (locale en)
    # troca os separadores en→pt: vírgula↔ponto.
    return inteiro.replace(",", "_").replace(".", ",").replace("_", ".")


# --- Pipeline NOVO: persistência da cobrança e do doc-preview -------------

def registrar_cobranca(
    conn: sqlite3.Connection,
    contrato_id: int,
    *,
    customer_id: str | None,
    payment_id: str | None,
    invoice_url: str | None,
) -> None:
    """Salva a cobrança Asaas no contrato (após create/find do payment)."""
    conn.execute(
        "UPDATE contrato SET asaas_customer_id = ?, asaas_payment_id = ?, "
        "invoice_url = ?, atualizado_em = datetime('now') WHERE id = ?",
        (customer_id, payment_id, invoice_url, contrato_id),
    )


def registrar_doc_preview(
    conn: sqlite3.Connection,
    contrato_id: int,
    *,
    doc_token: str | None,
    sign_url: str | None,
) -> None:
    """Salva o doc-preview da ZapSign (criado em silêncio, antes da revisão)."""
    conn.execute(
        "UPDATE contrato SET zapsign_doc_token = ?, sign_url = ?, "
        "atualizado_em = datetime('now') WHERE id = ?",
        (doc_token, sign_url, contrato_id),
    )


def criar_contrato_pipeline(
    conn: sqlite3.Connection,
    *,
    cliente_nome: str,
    cpf: str,
    tipo_caso: str,
    valor_honorarios_fmt: str,
    template_id: str,
    person_id: str | None = None,
    lead_id: int | None = None,
) -> sqlite3.Row:
    """Cria o contrato do pipeline NOVO em estado MONTAGEM.

    Dois tokens DISTINTOS: ``aprovacao_token`` (link 1-toque de APROVAR e
    liberar a assinatura) e ``reprovacao_token`` (link 1-toque de REPROVAR).
    valor_honorarios_fmt já formatado (humano sempre digitou). Registra a
    transição inicial na trilha de auditoria.
    """
    if not (valor_honorarios_fmt or "").strip():
        raise ValueError("valor_honorarios obrigatório (humano sempre digita)")
    aprovacao_token = gerar_aprovacao_token()
    reprovacao_token = gerar_aprovacao_token()
    cur = conn.execute(
        """
        INSERT INTO contrato (
            person_id, lead_id, cliente_nome, cpf, tipo_caso,
            valor_honorarios, estado, template_id,
            aprovacao_token, reprovacao_token
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            person_id, lead_id, cliente_nome, re.sub(r"\D", "", cpf), tipo_caso,
            valor_honorarios_fmt, EstadoContrato.MONTAGEM, template_id,
            aprovacao_token, reprovacao_token,
        ),
    )
    contrato_id = cur.lastrowid
    _inserir_transicao(
        conn, contrato_id, None, EstadoContrato.MONTAGEM,
        motivo="contrato em montagem (pipeline)", ator="sistema",
    )
    return get_contrato(conn, contrato_id)


def _valores_do_contrato(contrato: sqlite3.Row) -> dict[str, str]:
    """Campos semânticos a partir da linha do contrato (pra montar_minuta)."""
    return {
        "nome_cliente": contrato["cliente_nome"] or "",
        "email": contrato["cliente_email"] or "",
        "telefone": contrato["cliente_telefone"] or "",
        "objeto": contrato["objeto"] or "",
        "valor_honorarios": contrato["valor_honorarios"] or "",
    }


# --- Repositório (lê/escreve as tabelas) ---------------------------------

def criar_contrato(
    conn: sqlite3.Connection,
    *,
    cliente_nome: str,
    valor_honorarios: str,
    template_id: str,
    cliente_email: str | None = None,
    cliente_telefone: str | None = None,
    objeto: str | None = None,
    person_id: str | None = None,
    lead_id: int | None = None,
) -> sqlite3.Row:
    """Cria o contrato em ``pendente_aprovacao`` com token de aprovação.

    valor_honorarios é obrigatório e não-vazio aqui também (defesa em
    profundidade: a IA nunca cria contrato sem o valor que o Mario digitou).
    """
    if not (valor_honorarios or "").strip():
        raise ValueError("valor_honorarios obrigatório (humano sempre digita)")
    token = gerar_aprovacao_token()
    cur = conn.execute(
        """
        INSERT INTO contrato (
            person_id, lead_id, cliente_nome, cliente_email, cliente_telefone,
            objeto, valor_honorarios, estado, template_id, aprovacao_token
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            person_id, lead_id, cliente_nome, cliente_email, cliente_telefone,
            objeto, valor_honorarios, EstadoContrato.PENDENTE_APROVACAO,
            template_id, token,
        ),
    )
    contrato_id = cur.lastrowid
    _inserir_transicao(
        conn, contrato_id, None, EstadoContrato.PENDENTE_APROVACAO,
        motivo="minuta montada (aguardando aprovação)", ator="sistema",
    )
    return get_contrato(conn, contrato_id)


def iniciar_contrato(
    conn: sqlite3.Connection,
    *,
    cliente_nome: str,
    valor_honorarios: str,
    template_id: str,
    base_url: str,
    cliente_email: str | None = None,
    cliente_telefone: str | None = None,
    objeto: str | None = None,
    person_id: str | None = None,
    lead_id: int | None = None,
) -> tuple[sqlite3.Row | None, str | None, list[dict]]:
    """Frente do fluxo (gatilho do Mario): CONFLITO bloqueante → cria contrato
    pendente → devolve o link de aprovação 1-toque.

    Retorna ``(contrato|None, link|None, conflitos)``. Se ``conflitos`` não é
    vazio, NÃO cria nada — o lead aparece como parte contrária de um cliente
    (impedimento ético, roadmap 1.7); o caller alerta o Mario pelo canal
    INTERNO e o fluxo para. NUNCA revelar o conflito ao lead.

    A checagem usa a tabela ``parte_contraria`` (repovoada de madrugada do
    GET /lawSuit/). Match conservador por nome completo (≥2 palavras) —
    homonímia pode dar falso positivo; por isso é o Mario que decide, não
    o código.
    """
    conflitos = checar_conflito(conn, cliente_nome)
    if conflitos:
        logger.warning(
            "contrato BLOQUEADO por conflito (cliente=%r, %d processo(s))",
            cliente_nome, len(conflitos),
        )
        return None, None, conflitos
    contrato = criar_contrato(
        conn,
        cliente_nome=cliente_nome,
        valor_honorarios=valor_honorarios,
        template_id=template_id,
        cliente_email=cliente_email,
        cliente_telefone=cliente_telefone,
        objeto=objeto,
        person_id=person_id,
        lead_id=lead_id,
    )
    link = link_aprovacao(base_url, contrato["aprovacao_token"])
    return contrato, link, []


def get_contrato(conn: sqlite3.Connection, contrato_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contrato WHERE id = ?", (contrato_id,),
    ).fetchone()


def get_contrato_by_aprovacao_token(
    conn: sqlite3.Connection, token: str,
) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM contrato WHERE aprovacao_token = ?", (token,),
    ).fetchone()


def get_contrato_by_doc_token(
    conn: sqlite3.Connection, doc_token: str,
) -> sqlite3.Row | None:
    if not doc_token:
        return None
    return conn.execute(
        "SELECT * FROM contrato WHERE zapsign_doc_token = ?", (doc_token,),
    ).fetchone()


def _inserir_transicao(
    conn: sqlite3.Connection,
    contrato_id: int,
    estado_anterior: str | None,
    estado_novo: str,
    *,
    motivo: str,
    ator: str,
) -> None:
    conn.execute(
        """
        INSERT INTO contrato_transicao
            (contrato_id, estado_anterior, estado_novo, motivo, ator)
        VALUES (?, ?, ?, ?, ?)
        """,
        (contrato_id, estado_anterior, estado_novo, motivo, ator),
    )


def transicao_contrato(
    conn: sqlite3.Connection,
    contrato_id: int,
    estado_novo: str,
    *,
    motivo: str,
    ator: str,
) -> str:
    """Transição atômica: UPDATE estado + INSERT na trilha de auditoria.

    Sempre via esta função — nunca UPDATE estado solto. Retorna o estado
    anterior. BEGIN IMMEDIATE evita corrida no SELECT-then-UPDATE (mesma
    disciplina do ``state.transicao``).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT estado FROM contrato WHERE id = ?", (contrato_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise ValueError(f"Contrato {contrato_id} não encontrado")
        anterior = row["estado"]
        conn.execute(
            "UPDATE contrato SET estado = ?, atualizado_em = datetime('now') "
            "WHERE id = ?",
            (estado_novo, contrato_id),
        )
        _inserir_transicao(
            conn, contrato_id, anterior, estado_novo, motivo=motivo, ator=ator,
        )
        conn.execute("COMMIT")
        return anterior
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise


def aprovar(
    conn: sqlite3.Connection, token: str, *, ator: str = "mario",
) -> sqlite3.Row | None:
    """O 1-TOQUE: valida o token e move pendente→aprovado. Idempotente.

    - Token inválido/inexistente → None.
    - Já aprovado/enviado/assinado → devolve o contrato sem reabrir
      (clicar 2x não dispara 2 envios; o gate de envio também protege).
    - Recusado/expirado → não reabre (None-safe via estado).

    Carimba aprovado_em/aprovado_por (auditoria). NÃO chama a ZapSign —
    o envio é um passo separado (``enviar_para_assinatura``), pra manter o
    gate explícito e o endpoint de aprovação rápido.
    """
    contrato = get_contrato_by_aprovacao_token(conn, token)
    if contrato is None:
        return None
    contrato_id = contrato["id"]
    # Carimbo + estado + auditoria num ÚNICO BEGIN IMMEDIATE (atômico): fecha
    # a janela de crash entre carimbar e transicionar, e o CAS (WHERE estado=
    # pendente) garante que 2 toques concorrentes só aprovam UMA vez.
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT estado FROM contrato WHERE id = ?", (contrato_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["estado"] != EstadoContrato.PENDENTE_APROVACAO:
            # Idempotência: só aprova quem está pendente. Qualquer outro estado
            # (já aprovado, enviado, recusado…) volta como está, sem efeito.
            conn.execute("ROLLBACK")
            return get_contrato(conn, contrato_id)
        conn.execute(
            "UPDATE contrato SET estado = ?, aprovado_em = datetime('now'), "
            "aprovado_por = ?, atualizado_em = datetime('now') "
            "WHERE id = ? AND estado = ?",
            (EstadoContrato.APROVADO, ator, contrato_id,
             EstadoContrato.PENDENTE_APROVACAO),
        )
        _inserir_transicao(
            conn, contrato_id, EstadoContrato.PENDENTE_APROVACAO,
            EstadoContrato.APROVADO, motivo="aprovação humana (1-toque)",
            ator=ator,
        )
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise
    return get_contrato(conn, contrato_id)


def registrar_envio(
    conn: sqlite3.Connection,
    contrato_id: int,
    *,
    doc_token: str,
    signer_token: str | None,
    sign_url: str | None,
) -> None:
    """Salva os tokens da ZapSign após o create-doc (antes de transicionar)."""
    conn.execute(
        "UPDATE contrato SET zapsign_doc_token = ?, zapsign_signer_token = ?, "
        "sign_url = ?, atualizado_em = datetime('now') WHERE id = ?",
        (doc_token, signer_token, sign_url, contrato_id),
    )


def registrar_assinatura(
    conn: sqlite3.Connection, contrato_id: int, *, signed_file_url: str | None,
) -> None:
    """Salva a URL do PDF assinado (webhook doc_signed)."""
    conn.execute(
        "UPDATE contrato SET signed_file_url = ?, atualizado_em = datetime('now') "
        "WHERE id = ?",
        (signed_file_url, contrato_id),
    )


# #36 (25/jun): marcação por-passo do pós-assinatura. Carimba o timestamp SÓ após
# o passo dar certo (espelha tarefa_publicacao) → passo que falha/crasha fica NULL
# e re-tenta. Whitelist (nomes vêm do código, não de input — defesa em profundidade).
_POS_TIMESTAMP_COLS: Final = frozenset(
    {"intake_juridiq_em", "arquivo_pdf_em", "tarefa_abertura_em"}
)
_POS_REF_COLS: Final = frozenset(
    {"person_id", "signed_file_path", "juridiq_task_id"}
)


def marcar_passo_pos_assinatura(
    conn: sqlite3.Connection,
    contrato_id: int,
    *,
    passo_em: str,
    ref_col: str | None = None,
    ref_valor: str | None = None,
) -> None:
    """#36 (25/jun): carimba um sub-passo do pós-assinatura como FEITO (timestamp
    now) + grava a ref (person_id/signed_file_path/juridiq_task_id), atômico. Só
    chamar APÓS sucesso do passo."""
    if passo_em not in _POS_TIMESTAMP_COLS:
        raise ValueError(f"passo_em inválido: {passo_em!r}")
    sets = [f"{passo_em} = datetime('now')"]
    params: list[Any] = []
    if ref_col is not None:
        if ref_col not in _POS_REF_COLS:
            raise ValueError(f"ref_col inválida: {ref_col!r}")
        sets.append(f"{ref_col} = ?")
        params.append(ref_valor)
    sets.append("atualizado_em = datetime('now')")
    conn.execute(
        f"UPDATE contrato SET {', '.join(sets)} WHERE id = ?",
        (*params, contrato_id),
    )


# --- O GATE: envio à assinatura (única porta pro create-doc) --------------

async def enviar_para_assinatura(
    client: Any,
    conn: sqlite3.Connection,
    contrato: sqlite3.Row,
    *,
    placeholders: dict[str, str],
) -> tuple[str | None, str]:
    """Chama o create-doc da ZapSign — a ÚNICA porta de saída pro envio.

    GATE OAB (re-lê o estado AUTORITATIVO do banco, ignora o Row do caller
    que pode estar stale): só envia se o contrato está em ``aprovado`` E
    carrega o carimbo de aprovação humana (``aprovado_em``/``aprovado_por``).
    O estado sozinho seria furável por ``transicao_contrato`` (primitiva
    genérica); o carimbo só ``aprovar()`` grava. Retorna (doc_token|None,
    detalhe).

    Concorrência: o envio é um CLAIM atômico (CAS ``aprovado``→``enviando``
    sob BEGIN IMMEDIATE) — 2 toques concorrentes geram UM só documento. Em
    falha do create-doc, reverte ``enviando``→``aprovado`` (retry deliberado
    sem novo toque). Idempotência: se já há ``zapsign_doc_token``, reconcilia
    e não re-chama (o POST é não-idempotente).
    """
    contrato_id = contrato["id"]
    fresco = get_contrato(conn, contrato_id)
    if fresco is None:
        return None, "nao_encontrado"

    # Idempotência: doc já criado → reconcilia o estado e sai (sem montar
    # minuta nem chamar a ZapSign de novo). Trata tanto o meio-feito vindo de
    # ``aprovado`` quanto de ``enviando`` (crash entre registrar_envio e a
    # transição final) — sem regredir um contrato já assinado/recusado.
    if fresco["zapsign_doc_token"]:
        if fresco["estado"] in (
            EstadoContrato.APROVADO, EstadoContrato.ENVIANDO,
        ):
            transicao_contrato(
                conn, contrato_id, EstadoContrato.ENVIADO,
                motivo="reconciliação: doc já existia", ator="sistema",
            )
        return fresco["zapsign_doc_token"], "ja_enviado"

    # GATE (sobre dado fresco): aprovado + carimbo humano.
    if fresco["estado"] != EstadoContrato.APROVADO:
        return None, (
            f"gate: estado={fresco['estado']} (não aprovado) — NÃO envia"
        )
    if not (fresco["aprovado_em"] and fresco["aprovado_por"]):
        return None, "gate: sem carimbo de aprovação humana — NÃO envia"

    # Minuta (pura). Um erro de config (placeholder faltando) levanta AQUI,
    # antes do claim — o estado não fica preso em ``enviando``.
    data = montar_minuta(
        placeholders=placeholders, valores=_valores_do_contrato(fresco),
    )
    corpo = montar_corpo_create_doc(
        template_id=fresco["template_id"],
        signer_name=fresco["cliente_nome"],
        signer_email=fresco["cliente_email"],
        data=data,
        external_id=str(contrato_id),
    )

    # CLAIM atômico: aprovado→enviando sob lock. O WHERE re-valida estado +
    # carimbo + ausência de doc_token; só o vencedor (rowcount==1) chama a
    # ZapSign. Toques concorrentes / reentrância param aqui.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE contrato SET estado = ?, atualizado_em = datetime('now') "
            "WHERE id = ? AND estado = ? AND aprovado_por IS NOT NULL "
            "AND zapsign_doc_token IS NULL",
            (EstadoContrato.ENVIANDO, contrato_id, EstadoContrato.APROVADO),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return None, "gate: envio já em andamento ou estado mudou — NÃO reenvia"
        _inserir_transicao(
            conn, contrato_id, EstadoContrato.APROVADO, EstadoContrato.ENVIANDO,
            motivo="claim de envio (lock)", ator="sistema",
        )
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise

    # Fora da transação: o create-doc. O client NÃO retenta (POST não-
    # idempotente) — falha sobe e a gente reverte o claim pra ``aprovado``.
    try:
        resp = await client.create_doc_from_template(corpo)
    except Exception as exc:  # noqa: BLE001 — falha de rede não pode derrubar o caller
        logger.exception("create-doc falhou (contrato=%s): %s", contrato_id, exc)
        transicao_contrato(
            conn, contrato_id, EstadoContrato.APROVADO,
            motivo=f"falha no create-doc: {type(exc).__name__}", ator="sistema",
        )
        return None, f"erro_{type(exc).__name__}"

    doc_token = resp.get("token")
    if not doc_token:
        transicao_contrato(
            conn, contrato_id, EstadoContrato.APROVADO,
            motivo="resposta sem token", ator="sistema",
        )
        return None, "resposta_sem_token"
    signers = resp.get("signers") or []
    signer = signers[0] if signers else {}
    registrar_envio(
        conn, contrato_id,
        doc_token=doc_token,
        signer_token=signer.get("token"),
        sign_url=signer.get("sign_url"),
    )
    transicao_contrato(
        conn, contrato_id, EstadoContrato.ENVIADO,
        motivo="create-doc enviado à ZapSign", ator="sistema",
    )
    return doc_token, "ok"
