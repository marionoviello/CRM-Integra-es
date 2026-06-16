"""Tests for the contract-closing engine (contrato, roadmap 3.x).

O foco é a GARANTIA OAB: ``enviar_para_assinatura`` nunca chama o create-doc
sem o contrato estar em ``aprovado`` (teste keystone), e o valor de
honorários é sempre obrigatório (humano digita).
"""

import asyncio

import pytest

from noviello_funil.contrato import (
    EstadoContrato,
    aprovar,
    criar_contrato,
    criar_contrato_pipeline,
    enviar_para_assinatura,
    formatar_valor_brl,
    gerar_aprovacao_token,
    get_contrato,
    iniciar_contrato,
    link_aprovacao,
    montar_corpo_create_doc,
    montar_corpo_upload,
    montar_data_contrato,
    montar_minuta,
    montar_signer,
    registrar_envio,
    transicao_contrato,
)
from noviello_funil.db import connect, run_migrations

# Placeholders FICTÍCIOS (o template real do Mario tem nomes próprios).
PH = {
    "nome_cliente": "{{NOME}}",
    "objeto": "{{OBJETO}}",
    "valor_honorarios": "{{HONORARIOS}}",
}


def _db():
    conn = connect(":memory:")
    run_migrations(conn)
    return conn


class _FakeZap:
    """Cliente ZapSign falso — registra chamadas, pode levantar."""

    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls: list[dict] = []

    async def create_doc_from_template(self, corpo):
        self.calls.append(corpo)
        if self.exc:
            raise self.exc
        return self.resp


# --- montar_minuta -----------------------------------------------------------

def test_montar_minuta_omite_vazios():
    data = montar_minuta(
        placeholders=PH,
        valores={"nome_cliente": "Fulano Teste", "objeto": "",
                 "valor_honorarios": "R$ 1.000"},
    )
    assert {"de": "{{NOME}}", "para": "Fulano Teste"} in data
    assert {"de": "{{HONORARIOS}}", "para": "R$ 1.000"} in data
    # objeto vazio é omitido (placeholder fica em branco no doc)
    assert all(d["de"] != "{{OBJETO}}" for d in data)


def test_montar_minuta_exige_honorarios():
    with pytest.raises(ValueError):
        montar_minuta(
            placeholders=PH,
            valores={"nome_cliente": "Fulano Teste", "valor_honorarios": "   "},
        )


def test_montar_minuta_exige_placeholder_honorarios():
    """Valor preenchido mas o mapa não tem o placeholder de honorários →
    o valor humano não entraria no doc. Tem que levantar (revisão 15/jun)."""
    ph_sem_honorarios = {"nome_cliente": "{{NOME}}"}
    with pytest.raises(ValueError):
        montar_minuta(
            placeholders=ph_sem_honorarios,
            valores={"nome_cliente": "Fulano Teste",
                     "valor_honorarios": "R$ 5.000"},
        )


# --- montar_corpo_create_doc -------------------------------------------------

def test_montar_corpo_com_email():
    body = montar_corpo_create_doc(
        template_id="T", signer_name="Fulano Teste",
        signer_email="fulano@exemplo.com",
        data=[{"de": "{{H}}", "para": "R$ 1"}], external_id="5",
    )
    assert body["template_id"] == "T"
    assert body["external_id"] == "5"          # reconciliação/idempotência
    assert body["lang"] == "pt-br"
    assert body["send_automatic_email"] is True
    assert body["signer_email"] == "fulano@exemplo.com"


def test_montar_corpo_sem_email_nao_manda_automatico():
    body = montar_corpo_create_doc(
        template_id="T", signer_name="Fulano", signer_email=None,
        data=[], external_id="6",
    )
    assert body["send_automatic_email"] is False
    assert "signer_email" not in body


# --- montar_signer / montar_corpo_upload (caminho B, 4 assinaturas) ----------

def test_montar_signer_email_cpf_qualificacao():
    s = montar_signer(
        name="Fulano Teste", email="f@exemplo.com", qualification="Contratante",
        order_group=1, cpf="004.833.679-31",
    )
    assert s["email"] == "f@exemplo.com"
    assert s["send_automatic_email"] is True
    assert s["qualification"] == "Contratante"
    assert s["order_group"] == 1
    assert s["cpf"] == "00483367931"          # só dígitos (PII, vem do .env)


def test_montar_signer_sem_email_nao_manda_automatico():
    s = montar_signer(
        name="Testemunha Teste", telefone="(11) 99999-8888",
        order_group=3, qualification="Testemunha",
    )
    assert s["send_automatic_email"] is False
    assert "email" not in s
    assert s["phone_country"] == "55" and s["phone_number"] == "11999998888"


def test_montar_corpo_upload_ordem_sequencial():
    signers = [
        montar_signer(name="Cliente", email="c@x.com", order_group=1,
                      qualification="Contratante"),
        montar_signer(name="Escritório", email="e@x.com", order_group=2,
                      qualification="Contratado"),
        montar_signer(name="Hilde Teste", email="h@x.com", order_group=3,
                      qualification="Testemunha"),
        montar_signer(name="Marcio Teste", email="m@x.com", order_group=3,
                      qualification="Testemunha"),
    ]
    body = montar_corpo_upload(
        name="Contrato Fulano Teste", base64_pdf="JVBERi0x",
        signers=signers, external_id="7",
    )
    assert body["base64_pdf"] == "JVBERi0x"
    assert body["external_id"] == "7"
    assert body["lang"] == "pt-br"
    assert body["signature_order_active"] is True
    assert [s["order_group"] for s in body["signers"]] == [1, 2, 3, 3]
    assert body["signers"][2]["qualification"] == "Testemunha"


def test_token_unico():
    a, b = gerar_aprovacao_token(), gerar_aprovacao_token()
    assert a != b and len(a) > 20


# --- criar_contrato ----------------------------------------------------------

def test_criar_contrato_pendente_com_trilha():
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 5.000",
        template_id="T1",
    )
    assert c["estado"] == EstadoContrato.PENDENTE_APROVACAO
    assert c["aprovacao_token"]
    rows = conn.execute(
        "SELECT estado_novo, ator FROM contrato_transicao WHERE contrato_id = ?",
        (c["id"],),
    ).fetchall()
    assert rows[0]["estado_novo"] == EstadoContrato.PENDENTE_APROVACAO
    conn.close()


def test_criar_contrato_exige_honorarios():
    conn = _db()
    with pytest.raises(ValueError):
        criar_contrato(
            conn, cliente_nome="Fulano Teste", valor_honorarios="",
            template_id="T1",
        )
    conn.close()


# --- iniciar_contrato (gatilho + conflito bloqueante) ------------------------

def test_link_aprovacao():
    assert link_aprovacao("https://funil.x/", "tok123") == \
        "https://funil.x/zapsign/aprovar/tok123"


def test_iniciar_contrato_bloqueia_conflito():
    """Lead é parte contrária de um cliente → NÃO cria contrato (1.7)."""
    conn = _db()
    conn.execute(
        "INSERT INTO parte_contraria (nome_norm, processo, papel) "
        "VALUES (?, ?, ?)",
        ("fulano teste adversario", "1234567-00.2024.8.26.0100", "Requerido"),
    )
    contrato, link, conflitos = iniciar_contrato(
        conn, cliente_nome="Fulano Teste Adversario",
        valor_honorarios="R$ 5.000", template_id="T", base_url="https://funil.x",
    )
    assert contrato is None and link is None
    assert len(conflitos) == 1
    # NADA foi criado — nem o contrato pendente
    assert conn.execute("SELECT COUNT(*) AS c FROM contrato").fetchone()["c"] == 0
    conn.close()


def test_iniciar_contrato_livre_cria_e_devolve_link():
    conn = _db()
    contrato, link, conflitos = iniciar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 5.000 em 5x",
        template_id="T1", base_url="https://funil.x", cliente_email="f@x.com",
    )
    assert conflitos == []
    assert contrato["estado"] == EstadoContrato.PENDENTE_APROVACAO
    assert link.startswith("https://funil.x/zapsign/aprovar/")
    assert link.endswith(contrato["aprovacao_token"])
    conn.close()


# --- aprovar (1-toque) -------------------------------------------------------

def test_aprovar_token_invalido_none():
    conn = _db()
    assert aprovar(conn, "nao-existe") is None
    conn.close()


def test_aprovar_move_pendente_para_aprovado():
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 1",
        template_id="T",
    )
    a = aprovar(conn, c["aprovacao_token"], ator="mario")
    assert a["estado"] == EstadoContrato.APROVADO
    assert a["aprovado_por"] == "mario"
    assert a["aprovado_em"]
    conn.close()


def test_aprovar_idempotente_nao_reabre():
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 1",
        template_id="T",
    )
    aprovar(conn, c["aprovacao_token"])
    a2 = aprovar(conn, c["aprovacao_token"])      # 2ª vez
    assert a2["estado"] == EstadoContrato.APROVADO
    # só UMA transição pra aprovado (clicar 2x não dispara 2 envios)
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM contrato_transicao "
        "WHERE contrato_id = ? AND estado_novo = ?",
        (c["id"], EstadoContrato.APROVADO),
    ).fetchone()["c"]
    assert n == 1
    conn.close()


def test_transicao_id_inexistente_levanta():
    conn = _db()
    with pytest.raises(ValueError):
        transicao_contrato(
            conn, 999, EstadoContrato.ENVIADO, motivo="x", ator="sistema",
        )
    conn.close()


# --- O GATE (keystone) -------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_nao_envia_sem_aprovacao():
    """KEYSTONE OAB: contrato pendente NUNCA chama o create-doc."""
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 5.000",
        template_id="T1",
    )
    assert c["estado"] == EstadoContrato.PENDENTE_APROVACAO
    zap = _FakeZap(resp={"token": "doc-1", "signers": [
        {"token": "s1", "sign_url": "u"}]})

    doc_token, detalhe = await enviar_para_assinatura(
        zap, conn, c, placeholders=PH,
    )

    assert doc_token is None
    assert "gate" in detalhe
    assert zap.calls == []          # create-doc NUNCA chamado
    # estado intacto
    assert get_contrato(conn, c["id"])["estado"] == \
        EstadoContrato.PENDENTE_APROVACAO
    conn.close()


@pytest.mark.asyncio
async def test_aprovado_envia_e_transiciona():
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", cliente_email="fulano@exemplo.com",
        valor_honorarios="R$ 5.000 em 5x", objeto="Inventário",
        template_id="T1",
    )
    aprovado = aprovar(conn, c["aprovacao_token"], ator="mario")
    zap = _FakeZap(resp={
        "token": "doc-9", "status": "pending",
        "signers": [{"token": "sg-9",
                     "sign_url": "https://app.zapsign.com.br/verificar/sg-9"}],
    })

    doc_token, detalhe = await enviar_para_assinatura(
        zap, conn, aprovado, placeholders=PH,
    )

    assert detalhe == "ok" and doc_token == "doc-9"
    assert len(zap.calls) == 1
    body = zap.calls[0]
    assert body["template_id"] == "T1"
    assert body["external_id"] == str(c["id"])
    assert body["signer_email"] == "fulano@exemplo.com"
    assert {"de": "{{HONORARIOS}}", "para": "R$ 5.000 em 5x"} in body["data"]
    fresh = get_contrato(conn, c["id"])
    assert fresh["estado"] == EstadoContrato.ENVIADO
    assert fresh["zapsign_doc_token"] == "doc-9"
    assert fresh["sign_url"].endswith("sg-9")
    conn.close()


@pytest.mark.asyncio
async def test_idempotencia_doc_ja_existe_nao_recria():
    """Retry após falha pós-create (estado aprovado + doc_token já salvo):
    não re-chama o create-doc e reconcilia pra enviado."""
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 1",
        template_id="T",
    )
    aprovado = aprovar(conn, c["aprovacao_token"])
    # simula o estado meio-feito: doc criado na ZapSign, transição não rolou
    registrar_envio(
        conn, c["id"], doc_token="doc-x", signer_token="s", sign_url="u",
    )
    aprovado = get_contrato(conn, c["id"])      # ainda APROVADO, com doc_token
    zap = _FakeZap(resp={"token": "NOVO", "signers": []})

    doc_token, detalhe = await enviar_para_assinatura(
        zap, conn, aprovado, placeholders=PH,
    )

    assert detalhe == "ja_enviado" and doc_token == "doc-x"
    assert zap.calls == []          # não criou um 2º documento
    assert get_contrato(conn, c["id"])["estado"] == EstadoContrato.ENVIADO
    conn.close()


@pytest.mark.asyncio
async def test_reconcilia_preso_em_enviando():
    """Crash entre registrar_envio e a transição final deixa 'enviando' +
    doc_token. A reentrada reconcilia pra 'enviado' sem re-chamar a ZapSign."""
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 1",
        template_id="T",
    )
    aprovar(conn, c["aprovacao_token"])
    # simula o meio-feito: claim moveu pra 'enviando' e o doc_token foi salvo
    transicao_contrato(
        conn, c["id"], EstadoContrato.ENVIANDO, motivo="claim", ator="sistema",
    )
    registrar_envio(
        conn, c["id"], doc_token="doc-z", signer_token="s", sign_url="u",
    )
    preso = get_contrato(conn, c["id"])
    zap = _FakeZap(resp={"token": "NOVO", "signers": []})

    doc_token, detalhe = await enviar_para_assinatura(
        zap, conn, preso, placeholders=PH,
    )

    assert detalhe == "ja_enviado" and doc_token == "doc-z"
    assert zap.calls == []
    assert get_contrato(conn, c["id"])["estado"] == EstadoContrato.ENVIADO
    conn.close()


@pytest.mark.asyncio
async def test_falha_no_create_mantem_aprovado_pra_retry():
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 1",
        template_id="T",
    )
    aprovado = aprovar(conn, c["aprovacao_token"])
    zap = _FakeZap(exc=RuntimeError("boom"))

    doc_token, detalhe = await enviar_para_assinatura(
        zap, conn, aprovado, placeholders=PH,
    )

    assert doc_token is None and detalhe.startswith("erro_")
    # fica aprovado — um retry pode reenviar sem novo toque do Mario
    assert get_contrato(conn, c["id"])["estado"] == EstadoContrato.APROVADO
    conn.close()


@pytest.mark.asyncio
async def test_resposta_sem_token():
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 1",
        template_id="T",
    )
    aprovado = aprovar(conn, c["aprovacao_token"])
    zap = _FakeZap(resp={"signers": []})        # resposta sem 'token'

    doc_token, detalhe = await enviar_para_assinatura(
        zap, conn, aprovado, placeholders=PH,
    )

    assert doc_token is None and detalhe == "resposta_sem_token"
    # reverte o claim: volta a aprovado pra retry deliberado
    assert get_contrato(conn, c["id"])["estado"] == EstadoContrato.APROVADO
    conn.close()


@pytest.mark.asyncio
async def test_gate_exige_carimbo_humano():
    """PORTA LATERAL FECHADA (defesa-em-profundidade, revisão 15/jun):
    se algum caminho futuro forçar 'aprovado' via transicao_contrato (sem
    passar por aprovar(), logo SEM carimbo), o gate recusa o create-doc."""
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 5.000",
        template_id="T1",
    )
    # força 'aprovado' pela primitiva genérica — pula aprovar(), não carimba
    transicao_contrato(
        conn, c["id"], EstadoContrato.APROVADO,
        motivo="porta lateral (teste)", ator="sistema",
    )
    forjado = get_contrato(conn, c["id"])
    assert forjado["estado"] == EstadoContrato.APROVADO
    assert forjado["aprovado_por"] is None      # sem carimbo humano
    zap = _FakeZap(resp={"token": "doc-x", "signers": []})

    doc_token, detalhe = await enviar_para_assinatura(
        zap, conn, forjado, placeholders=PH,
    )

    assert doc_token is None
    assert "carimbo" in detalhe
    assert zap.calls == []                       # create-doc NÃO chamado
    conn.close()


@pytest.mark.asyncio
async def test_envio_concorrente_gera_um_doc():
    """Dois toques concorrentes (ex.: Mario clica 2x o link) → UM documento.
    O CAS aprovado→enviando serializa: só o vencedor chama a ZapSign."""
    conn = _db()
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 1",
        template_id="T",
    )
    aprovado = aprovar(conn, c["aprovacao_token"])

    class _SlowZap:
        def __init__(self):
            self.calls: list[dict] = []

        async def create_doc_from_template(self, corpo):
            self.calls.append(corpo)
            await asyncio.sleep(0.01)            # cede o loop p/ forçar corrida
            return {"token": "doc-1",
                    "signers": [{"token": "s", "sign_url": "u"}]}

    zap = _SlowZap()
    r1, r2 = await asyncio.gather(
        enviar_para_assinatura(zap, conn, aprovado, placeholders=PH),
        enviar_para_assinatura(zap, conn, aprovado, placeholders=PH),
    )

    assert len(zap.calls) == 1                    # só UM create-doc
    detalhes = {r1[1], r2[1]}
    assert "ok" in detalhes                       # um venceu
    assert any("andamento" in d or "não aprovado" in d for d in detalhes)
    assert get_contrato(conn, c["id"])["estado"] == EstadoContrato.ENVIADO
    conn.close()


# --- Pipeline NOVO: montar_data_contrato (puro) ------------------------------

_CLIENTE = {
    "nome_completo": "Fulano Teste",
    "nacionalidade": "brasileiro",
    "estado_civil": "casado",
    "profissao": "engenheiro",
    "rg": "12.345.678-9",
    "orgao_emissor": "SSP/SP",
    "cpf": "00000000000",
    "logradouro": "Rua Exemplo",
    "numero": "100",
    "complemento": "",                 # vazio → omitido
    "bairro": "Centro",
    "cidade": "São Paulo",
    "uf": "SP",
    "cep": "01000-000",
    "celular": "11999990000",
    "email": "fulano@exemplo.com",
}
_ESCOPO = {
    "area_atuacao": "Direito Imobiliário",
    "objeto_contrato": "Prestação de serviços advocatícios.",
    "contexto_normativo": "Lei nº 12.016/2009.",
    "descricao_honorarios": "R$ 3.500,00 em parcela única.",
}


def test_montar_data_contrato_mapeia_certo():
    data = montar_data_contrato(
        _CLIENTE, _ESCOPO,
        valor_fmt="3.500,00", valor_extenso="três mil e quinhentos reais",
        link_pagamento="https://asaas.com/i/abc",
    )
    pares = {d["de"]: d["para"] for d in data}
    assert pares["{{NOME_COMPLETO}}"] == "Fulano Teste"
    assert pares["{{CPF}}"] == "00000000000"
    assert pares["{{EMAIL}}"] == "fulano@exemplo.com"
    assert pares["{{OBJETO_CONTRATO}}"] == "Prestação de serviços advocatícios."
    assert pares["{{AREA_ATUACAO}}"] == "Direito Imobiliário"
    assert pares["{{VALOR_HONORARIOS}}"] == "3.500,00"
    assert pares["{{VALOR_HONORARIOS_EXTENSO}}"] == "três mil e quinhentos reais"
    assert pares["{{LINK_PAGAMENTO}}"] == "https://asaas.com/i/abc"


def test_montar_data_contrato_omite_vazios():
    data = montar_data_contrato(
        _CLIENTE, _ESCOPO,
        valor_fmt="3.500,00", valor_extenso="", link_pagamento="x",
    )
    des = {d["de"] for d in data}
    # complemento do cliente é vazio → omitido
    assert "{{COMPLEMENTO}}" not in des
    # valor_extenso vazio → omitido
    assert "{{VALOR_HONORARIOS_EXTENSO}}" not in des


def test_montar_data_contrato_placeholder_residual_fica_cravado():
    """med#6: se a descrição de honorários do escopo carrega um placeholder
    NÃO-resolvido, montar_data_contrato o entrega CRAVADO no valor (a ZapSign
    não substitui placeholder DENTRO de um valor). É por isso que o orquestrador
    varre o data[] por \\{\\{...\\}\\} antes do create-doc e aborta — ver
    test_placeholder_residual_aborta_create_doc."""
    escopo_furado = {
        **_ESCOPO,
        "descricao_honorarios": "R$ {{VALOR_HONORARIOS}} ({{VALOR_HONORARIOS_EXTENSO}}).",
    }
    data = montar_data_contrato(
        _CLIENTE, escopo_furado,
        valor_fmt="3.500,00", valor_extenso="", link_pagamento="x",
    )
    pares = {d["de"]: d["para"] for d in data}
    # o placeholder fica cravado no valor — gap real que o orquestrador defende
    assert "{{VALOR_HONORARIOS_EXTENSO}}" in pares["{{DESCRICAO_HONORARIOS}}"]


# --- Pipeline NOVO: formatar_valor_brl (puro) --------------------------------

def test_formatar_valor_brl():
    assert formatar_valor_brl(3500.0) == "3.500,00"
    assert formatar_valor_brl(1234567.5) == "1.234.567,50"
    assert formatar_valor_brl(0.0) == "0,00"


def test_formatar_valor_brl_terceira_casa_arredonda():
    """med#7: a função formata com .2f (arredonda a 3ª casa). A coerência com o
    Asaas é garantida pela QUANTIZAÇÃO no orquestrador (ambos batem nos
    centavos); aqui só documentamos o arredondamento da formatação."""
    assert formatar_valor_brl(1234.999) == "1.235,00"
    assert formatar_valor_brl(999.996) == "1.000,00"


def test_formatar_valor_brl_negativo_documenta():
    """med#8: formatar_valor_brl NÃO rejeita negativo (formata '-'); a
    validação valor>0 fica na ENTRADA (gerar_contrato) — ver
    test_invariante_validacoes_entrada_barram_cedo no orquestrador."""
    assert formatar_valor_brl(-3500.0) == "-3.500,00"


# --- Pipeline NOVO: criar_contrato_pipeline ----------------------------------

def test_criar_contrato_pipeline_montagem_tokens_distintos():
    conn = _db()
    c = criar_contrato_pipeline(
        conn, cliente_nome="Fulano Teste", cpf="000.000.000-00",
        tipo_caso="urbanistico_iptu_regularizacao",
        valor_honorarios_fmt="3.500,00", template_id="T1",
    )
    assert c["estado"] == EstadoContrato.MONTAGEM
    assert c["tipo_caso"] == "urbanistico_iptu_regularizacao"
    assert c["cpf"] == "00000000000"               # só dígitos
    assert c["aprovacao_token"] and c["reprovacao_token"]
    assert c["aprovacao_token"] != c["reprovacao_token"]
    rows = conn.execute(
        "SELECT estado_novo FROM contrato_transicao WHERE contrato_id = ?",
        (c["id"],),
    ).fetchall()
    assert rows[0]["estado_novo"] == EstadoContrato.MONTAGEM
    conn.close()


def test_criar_contrato_pipeline_exige_valor():
    conn = _db()
    with pytest.raises(ValueError):
        criar_contrato_pipeline(
            conn, cliente_nome="Fulano Teste", cpf="00000000000",
            tipo_caso="x", valor_honorarios_fmt="", template_id="T1",
        )
    conn.close()
