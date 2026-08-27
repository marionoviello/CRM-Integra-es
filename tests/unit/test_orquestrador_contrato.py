"""Tests for the contract-closing pipeline orchestrator (orquestrador_contrato).

Foco nas INVARIANTES DE SEGURANÇA (produção mexe com COBRANÇA):
  - send_automatic_email SEMPRE False no create-doc (cliente não recebe nada
    até a aprovação) → test_gerar_contrato_happy / test_send_automatic_email_false.
  - CONFLITO bloqueia ANTES de qualquer chamada externa → test_conflito_bloqueia.
  - DEDUPE: find_payment antes de create_payment (2 POSTs = 2 cobranças,
    proibido) → test_dedupe_nao_recria_cobranca.
  - reprovar lê o status FRESCO antes de delete; cobrança paga NÃO é deletada →
    test_reprovar_cobranca_paga_nao_deleta.

Fakes async (FakeAsaas/FakeZapSign) registram chamadas. Dados fictícios:
"Fulano Teste", cpf "00000000000", emails @exemplo.com.
"""

import asyncio

import pytest

from noviello_funil.contrato import (
    EstadoContrato,
    get_contrato,
    registrar_doc_preview,
    transicao_contrato,
)
from noviello_funil.db import connect, run_migrations
from noviello_funil.escopos import ESCOPOS
from noviello_funil.orquestrador_contrato import (
    _criar_doc_silencioso,
    aprovar_e_liberar,
    gerar_contrato,
    reconciliar_contratos_presos,
    reprovar_contrato,
)
from noviello_funil.politica_contrato import AUTOMATICO

TIPO = "urbanistico_iptu_regularizacao"   # único escopo já cadastrado

CLIENTE = {
    "nome_completo": "Fulano Teste",
    "cpf": "00000000000",
    "email": "fulano@exemplo.com",
    "celular": "11999990000",
    "nacionalidade": "brasileiro",
    "estado_civil": "solteiro",
    "profissao": "autônomo",
    "rg": "12.345.678-9",
    "orgao_emissor": "SSP/SP",
    "logradouro": "Rua Exemplo",
    "numero": "100",
    "bairro": "Centro",
    "cidade": "São Paulo",
    "uf": "SP",
    "cep": "01000-000",
}

SIGNERS_EXTRA = [
    {"name": "Escritório Teste", "email": "escritorio@exemplo.com",
     "order_group": 2, "qualification": "Contratado", "send_automatic_email": False},
    {"name": "Testemunha Teste", "email": "test@exemplo.com",
     "order_group": 3, "qualification": "Testemunha", "send_automatic_email": False},
]


def _db():
    conn = connect(":memory:")
    run_migrations(conn)
    return conn


class FakeAsaas:
    """Asaas falso (async) — registra chamadas, configurável por cenário.

    ``raise_on``: conjunto de nomes de método ('get_payment', 'delete_payment',
    'create_payment', 'find_payment') que devem levantar (injeção de falha).
    ``payment_value``: valor refletido no create_payment (default eco do value).
    """

    def __init__(self, *, existente=None, payment_status="PENDING",
                 raise_on=None):
        # ``existente`` força o que o find devolve (sobrepõe o registro real);
        # se None, o find usa o que o create_payment já criou (Asaas real
        # reencontra a cobrança pelo externalReference — base do dedupe).
        self.existente = existente
        self.payment_status = payment_status
        self.raise_on = set(raise_on or ())
        self.calls: list[tuple] = []
        # external_reference → payload da cobrança criada (modela o lado Asaas).
        self._por_ref: dict[str, dict] = {}

    async def find_payment_by_external_reference(self, ref):
        self.calls.append(("find_payment", ref))
        if "find_payment" in self.raise_on:
            raise RuntimeError("find boom")
        if self.existente is not None:
            return self.existente
        return self._por_ref.get(ref)

    async def get_or_create_customer(self, *, name, cpf, **kw):
        self.calls.append(("get_or_create_customer", cpf))
        return "cus_fake"

    async def create_payment(self, *, customer_id, value, due_date,
                             description, external_reference):
        self.calls.append(("create_payment", external_reference, value))
        if "create_payment" in self.raise_on:
            raise RuntimeError("create boom")
        pay = {
            "id": "pay_fake",
            "invoiceUrl": "https://asaas.com/i/fake",
            "customer": customer_id,
            "status": "PENDING",
        }
        self._por_ref[external_reference] = pay     # Asaas passa a reencontrar
        return pay

    async def get_payment(self, payment_id):
        self.calls.append(("get_payment", payment_id))
        if "get_payment" in self.raise_on:
            raise RuntimeError("get_payment boom")
        return {
            "id": payment_id,
            "status": self.payment_status,
            "invoiceUrl": "https://asaas.com/i/fake",
            "customer": "cus_fake",
        }

    async def delete_payment(self, payment_id):
        self.calls.append(("delete_payment", payment_id))
        if "delete_payment" in self.raise_on:
            raise RuntimeError("delete boom")
        return {"deleted": True, "id": payment_id}

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeZapSign:
    """ZapSign falso (async) — registra o corpo do create-doc e os tokens.

    ``slow``: cede o loop (await asyncio.sleep) dentro de resend/refuse pra
    forçar a corrida em testes de concorrência aprovar×reprovar.
    """

    def __init__(self, *, doc_token="doc_fake", sign_url="https://zap/s/fake",
                 slow=False):
        self.doc_token = doc_token
        self.sign_url = sign_url
        self.slow = slow
        self.create_calls: list[dict] = []
        self.resend_calls: list[str] = []
        self.refuse_calls: list[tuple] = []
        self.add_signer_calls: list[tuple] = []
        self.delete_calls: list[str] = []

    async def create_doc_from_template(self, corpo):
        self.create_calls.append(corpo)
        return {
            "token": self.doc_token,
            "signers": [{"token": "sg", "sign_url": self.sign_url}],
        }

    async def add_signer(self, doc_token, signer):
        self.add_signer_calls.append((doc_token, signer))
        return {"token": "added", "name": signer.get("name")}

    async def delete_doc(self, doc_token):
        self.delete_calls.append(doc_token)

    async def resend_notifications_bulk(self, doc_token):
        if self.slow:
            await asyncio.sleep(0.01)
        self.resend_calls.append(doc_token)
        return {"ok": True}

    async def refuse(self, doc_token, motivo):
        if self.slow:
            await asyncio.sleep(0.01)
        self.refuse_calls.append((doc_token, motivo))
        return {"ok": True}


async def _gerar(conn, asaas, zap, **over):
    kwargs = dict(
        cliente=CLIENTE, tipo_caso=TIPO, valor_honorarios=3500.0,
        valor_extenso="três mil e quinhentos reais",
        template_id="T1", signers_extra=SIGNERS_EXTRA, due_date="2026-07-01",
        base_url="https://funil.x",
    )
    kwargs.update(over)
    return await gerar_contrato(conn, asaas, zap, **kwargs)


def _escopo_resolvido():
    """Escopo já resolvido (sem {{...}} pendente) — como chega ao
    _criar_doc_silencioso em produção (resolver_escopo já substituiu)."""
    from noviello_funil.escopos import resolver_escopo
    return resolver_escopo(
        TIPO,
        substituicoes={
            "{{VALOR_HONORARIOS}}": "3.500,00",
            "{{VALOR_HONORARIOS_EXTENSO}}": "três mil e quinhentos reais",
        },
    )


# --- gerar_contrato happy path -----------------------------------------------

@pytest.mark.asyncio
async def test_gerar_contrato_happy():
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap)

    assert out["status"] == "pendente_revisao"
    cid = out["contrato_id"]
    # cobrança criada UMA vez (dedupe encontrou nada → criou)
    assert len(asaas.called("create_payment")) == 1
    # valor float vai cru pro Asaas (a IA não precifica; o Mario digitou)
    assert asaas.called("create_payment")[0][2] == 3500.0
    # doc criado UMA vez
    assert len(zap.create_calls) == 1
    # links de saída
    assert out["invoice_url"] == "https://asaas.com/i/fake"
    assert out["sign_url"] == "https://zap/s/fake"
    assert out["link_aprovacao"].endswith(
        get_contrato(conn, cid)["aprovacao_token"]
    )
    # estado final
    fresh = get_contrato(conn, cid)
    assert fresh["estado"] == EstadoContrato.PENDENTE_REVISAO
    assert fresh["zapsign_doc_token"] == "doc_fake"
    assert fresh["asaas_payment_id"] == "pay_fake"
    conn.close()


@pytest.mark.asyncio
async def test_send_automatic_email_false():
    """INVARIANTE: o create-doc do pipeline FORÇA send_automatic_email=False
    (cliente não recebe nada até a aprovação do Mario)."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    await _gerar(conn, asaas, zap)

    corpo = zap.create_calls[0]
    assert corpo["send_automatic_email"] is False
    assert corpo["signature_order_active"] is True
    # cliente é o signatário PRIMÁRIO (signer_name) — assina 1º; extras entram
    # via add-signer (ordem de adição = ordem de assinatura).
    assert corpo["signer_name"]
    assert "signers" not in corpo   # NÃO manda array signers (a API ignora)
    assert zap.add_signer_calls     # escritório + testemunhas via add-signer
    # o LINK_PAGAMENTO (invoiceUrl) entra no data[]
    pares = {d["de"]: d["para"] for d in corpo["data"]}
    assert pares["{{LINK_PAGAMENTO}}"] == "https://asaas.com/i/fake"
    conn.close()


@pytest.mark.asyncio
async def test_multi_signatario_primario_mais_add_signer_em_ordem():
    """create-doc-from-template registra SÓ o cliente (signer_name, primário).
    Escritório + testemunhas entram via add-signer DEPOIS, no mesmo doc e NA
    ORDEM (= ordem de assinatura). O corpo NÃO manda array `signers` (a API
    ignora). É o fix do bug do multi-signatário (diagnóstico 18/jun)."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    await _gerar(conn, asaas, zap)

    # 1 create-doc, com o cliente primário e SEM array signers.
    assert len(zap.create_calls) == 1
    assert "signers" not in zap.create_calls[0]
    assert zap.create_calls[0]["signer_name"]
    # 1 add-signer por extra, todos no doc criado, na ordem dos signers_extra.
    assert len(zap.add_signer_calls) == len(SIGNERS_EXTRA)
    assert all(tok == "doc_fake" for tok, _ in zap.add_signer_calls)
    assert [p["name"] for _, p in zap.add_signer_calls] == \
        [s["name"] for s in SIGNERS_EXTRA]
    conn.close()


# --- CONFLITO bloqueia ANTES de qualquer chamada externa ---------------------

@pytest.mark.asyncio
async def test_conflito_bloqueia():
    """INVARIANTE: suspeita de conflito → nenhuma cobrança/doc, nada criado."""
    conn = _db()
    conn.execute(
        "INSERT INTO parte_contraria (nome_norm, processo, papel) "
        "VALUES (?, ?, ?)",
        ("fulano teste", "1234567-00.2024.8.26.0100", "Requerido"),
    )
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap)

    assert out["status"] == "bloqueado_conflito"
    assert len(out["conflitos"]) == 1
    # NADA externo foi chamado
    assert asaas.calls == []
    assert zap.create_calls == []
    # nenhum contrato criado
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM contrato"
    ).fetchone()["c"] == 0
    conn.close()


# --- DEDUPE: find antes de create_payment ------------------------------------

@pytest.mark.asyncio
async def test_dedupe_nao_recria_cobranca():
    """INVARIANTE: find_payment_by_external_reference encontra existente →
    create_payment NÃO é chamado (2 POSTs = 2 cobranças, proibido)."""
    conn = _db()
    asaas = FakeAsaas(existente={
        "id": "pay_existente",
        "invoiceUrl": "https://asaas.com/i/exist",
        "customer": "cus_old",
    })
    zap = FakeZapSign()

    out = await _gerar(conn, asaas, zap)

    assert out["status"] == "pendente_revisao"
    assert asaas.called("create_payment") == []     # NÃO recriou
    assert asaas.called("get_or_create_customer") == []
    assert out["invoice_url"] == "https://asaas.com/i/exist"
    assert get_contrato(conn, out["contrato_id"])["asaas_payment_id"] == \
        "pay_existente"
    conn.close()


# --- escopo inexistente ------------------------------------------------------

@pytest.mark.asyncio
async def test_escopo_indisponivel():
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap, tipo_caso="tipo_que_nao_existe")

    assert out["status"] == "escopo_indisponivel"
    assert asaas.calls == [] and zap.create_calls == []
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM contrato"
    ).fetchone()["c"] == 0
    conn.close()


# --- aprovar_e_liberar -------------------------------------------------------

@pytest.mark.asyncio
async def test_aprovar_e_liberar():
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    token = get_contrato(conn, cid)["aprovacao_token"]

    r = await aprovar_e_liberar(conn, zap, token=token, ator="mario")

    assert r["status"] == "liberado"
    assert zap.resend_calls == ["doc_fake"]          # liberou a assinatura
    fresh = get_contrato(conn, cid)
    assert fresh["estado"] == EstadoContrato.LIBERADO
    assert fresh["aprovado_por"] == "mario"          # carimbo gravado
    assert fresh["aprovado_em"]
    conn.close()


@pytest.mark.asyncio
async def test_aprovar_e_liberar_idempotente():
    """Clicar 2x não chama o resend 2x (só libera UMA vez)."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    token = get_contrato(conn, out["contrato_id"])["aprovacao_token"]

    await aprovar_e_liberar(conn, zap, token=token)
    r2 = await aprovar_e_liberar(conn, zap, token=token)

    assert r2["status"] == "ja_processado"
    assert len(zap.resend_calls) == 1                # NÃO reenviou
    conn.close()


# --- reprovar_contrato -------------------------------------------------------

@pytest.mark.asyncio
async def test_reprovar_cobranca_pendente_deleta():
    conn = _db()
    asaas = FakeAsaas(payment_status="PENDING")
    zap = FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    token = get_contrato(conn, cid)["reprovacao_token"]

    r = await reprovar_contrato(
        conn, zap, asaas, token=token, motivo="Revisão interna",
    )

    assert r["status"] == "reprovado"
    assert r["estorno_manual"] is False
    # refuse PRIMEIRO, depois delete da cobrança PENDING
    assert zap.refuse_calls == [("doc_fake", "Revisão interna")]
    assert asaas.called("delete_payment") == [("delete_payment", "pay_fake")]
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.REPROVADO
    conn.close()


@pytest.mark.asyncio
async def test_reprovar_cobranca_paga_nao_deleta():
    """INVARIANTE: cobrança RECEIVED → refuse chamado, mas delete NÃO
    (dinheiro que entrou vira estorno manual; NUNCA refund automático)."""
    conn = _db()
    asaas = FakeAsaas(payment_status="RECEIVED")
    zap = FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    token = get_contrato(conn, cid)["reprovacao_token"]

    r = await reprovar_contrato(
        conn, zap, asaas, token=token, motivo="Revisão interna",
    )

    assert r["status"] == "reprovado"
    assert r["estorno_manual"] is True
    # leu o status FRESCO antes de decidir
    assert asaas.called("get_payment") == [("get_payment", "pay_fake")]
    # refuse SIM, delete NÃO (não apaga dinheiro que entrou)
    assert zap.refuse_calls == [("doc_fake", "Revisão interna")]
    assert asaas.called("delete_payment") == []
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.REPROVADO
    conn.close()


@pytest.mark.asyncio
async def test_escopo_realmente_cadastrado():
    """Sanidade: o tipo usado nos testes existe na biblioteca de escopos."""
    assert TIPO in ESCOPOS


# ===========================================================================
# INVARIANTES CRÍTICAS (revisão adversarial 16/jun) — cada uma com teste
# nomeado que a prova.
# ===========================================================================


# --- INVARIANTE (1): signer send_automatic_email=False em TODOS ---------------

@pytest.mark.asyncio
async def test_invariante_signer_send_automatic_email_false():
    """INVARIANTE (1): silêncio total até a aprovação. O create-doc tem
    send_automatic_email=False no GLOBAL (cobre o cliente, que é o signatário
    PRIMÁRIO via signer_name), e CADA add-signer (escritório + testemunhas) entra
    com send_automatic_email=False. Sem isso, o link de assinatura vazaria antes
    da aprovação."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    await _gerar(conn, asaas, zap)

    corpo = zap.create_calls[0]
    # Global = silêncio (cobre o cliente, primário via signer_name).
    assert corpo["send_automatic_email"] is False
    assert corpo["signer_email"] == "fulano@exemplo.com"   # email do cliente preservado
    # Cada add-signer (escritório + testemunhas) também em silêncio.
    assert zap.add_signer_calls, "deve adicionar os signatários extras"
    assert all(p["send_automatic_email"] is False for _, p in zap.add_signer_calls)
    conn.close()


# --- INVARIANTE (2): duplo-comando NÃO duplica cobrança -----------------------

@pytest.mark.asyncio
async def test_invariante_duplo_comando_nao_duplica_cobranca():
    """INVARIANTE (2): chamar gerar_contrato 2x pro MESMO cliente/caso →
    create_payment 1x, create-doc 1x, COUNT(contrato)==1. O 2º comando RETOMA o
    contrato aberto (find reusa o external_ref) em vez de criar um 2º."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out1 = await _gerar(conn, asaas, zap)
    out2 = await _gerar(conn, asaas, zap)

    assert out1["status"] == "pendente_revisao"
    # o 2º comando retoma (já em PENDENTE_REVISAO) e devolve os links
    assert out2["status"] == "pendente_revisao"
    assert out2["contrato_id"] == out1["contrato_id"]
    # cobrança UMA vez, doc UMA vez
    assert len(asaas.called("create_payment")) == 1
    assert len(zap.create_calls) == 1
    # UM só contrato no banco
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM contrato"
    ).fetchone()["c"] == 1
    conn.close()


# --- INVARIANTE (3): validações de entrada barram cedo ------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("over", "status_esperado"),
    [
        ({"valor_honorarios": 0.0}, "valor_invalido"),
        ({"valor_honorarios": -3500.0}, "valor_invalido"),
        ({"valor_extenso": ""}, "erro_valor_extenso"),
        ({"valor_extenso": "   "}, "erro_valor_extenso"),
        ({"cliente": {**CLIENTE, "cpf": "123"}}, "cpf_invalido"),
        ({"cliente": {**CLIENTE, "cpf": ""}}, "cpf_invalido"),
        (
            {"cliente": {**{k: v for k, v in CLIENTE.items()
                           if k not in ("email", "celular")}}},
            "sem_canal_contato",
        ),
    ],
)
async def test_invariante_validacoes_entrada_barram_cedo(over, status_esperado):
    """INVARIANTE (3): valor<=0, valor_extenso vazio, cpf inválido e cliente
    sem canal barram ANTES de tocar Asaas/ZapSign — nada criado."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap, **over)

    assert out["status"] == status_esperado
    assert asaas.calls == []
    assert zap.create_calls == []
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM contrato"
    ).fetchone()["c"] == 0
    conn.close()


# --- INVARIANTE (4): reprovar com cancelamento falho sinaliza alerta ----------

@pytest.mark.asyncio
async def test_invariante_reprovar_cancelamento_falho_sinaliza_alerta():
    """INVARIANTE (4): get_payment lança → reprovar NÃO vai a REPROVADO limpo:
    sinaliza cobranca_cancelamento_falhou + estorno_manual e usa motivo de
    verificação manual (não silencioso)."""
    conn = _db()
    asaas = FakeAsaas()
    zap = FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    token = get_contrato(conn, cid)["reprovacao_token"]
    # agora injeta a falha no get_payment do cancelamento
    asaas.raise_on = {"get_payment"}

    r = await reprovar_contrato(
        conn, zap, asaas, token=token, motivo="Revisão interna",
    )

    assert r["status"] == "reprovado"
    assert r["cobranca_cancelamento_falhou"] is True
    assert r["estorno_manual"] is True             # por segurança (pode estar paga)
    # NÃO deletou às cegas
    assert asaas.called("delete_payment") == []
    # transição com motivo de verificação manual (não "reprovado na revisão")
    motivo = conn.execute(
        "SELECT motivo FROM contrato_transicao WHERE contrato_id = ? "
        "AND estado_novo = ?",
        (cid, EstadoContrato.REPROVADO),
    ).fetchone()["motivo"]
    assert "verificar cobrança manual" in motivo
    conn.close()


# --- Cobrança órfã: registrar_cobranca falha → retry NÃO duplica --------------

@pytest.mark.asyncio
async def test_cobranca_orfa_retry_nao_duplica(monkeypatch):
    """high#1/med#1/med#5: create_payment OK mas registrar_cobranca falha →
    cobrança VIVA órfã. O retry (mesmo cliente/caso) reusa o external_ref e
    NÃO cria 2ª cobrança."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    # injeta falha de persistência APÓS o create_payment bem-sucedido
    import noviello_funil.orquestrador_contrato as orq
    chamou = {"n": 0}
    real = orq.registrar_cobranca

    def boom(*a, **k):
        chamou["n"] += 1
        if chamou["n"] == 1:
            raise RuntimeError("DB locked")
        return real(*a, **k)

    monkeypatch.setattr(orq, "registrar_cobranca", boom)

    out1 = await _gerar(conn, asaas, zap)
    assert out1["status"] == "erro_asaas"
    assert out1.get("cobranca_viva_sem_registro") is True
    # cobrança criada 1x, mas órfã (não persistida)
    assert len(asaas.called("create_payment")) == 1

    # RETRY: a cobrança existe no Asaas → o find a reencontra (mesmo external_ref).
    asaas.existente = {
        "id": "pay_fake",
        "invoiceUrl": "https://asaas.com/i/fake",
        "customer": "cus_fake",
    }
    out2 = await _gerar(conn, asaas, zap)

    assert out2["status"] == "pendente_revisao"
    assert out2["contrato_id"] == out1["contrato_id"]      # mesmo contrato
    # NÃO criou 2ª cobrança (o find reusou)
    assert len(asaas.called("create_payment")) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM contrato"
    ).fetchone()["c"] == 1
    conn.close()


# --- Retry após erro_zapsign reusa a cobrança --------------------------------

@pytest.mark.asyncio
async def test_retry_apos_erro_zapsign_reusa_cobranca():
    """med#1: create-doc falha na 1ª → retry do mesmo cliente reusa a cobrança
    (1 só create_payment) e o contrato é reaproveitado."""
    conn = _db()
    asaas = FakeAsaas()

    class _ZapFalha:
        def __init__(self):
            self.create_calls: list[dict] = []
            self.falhar = True

        async def add_signer(self, doc_token, signer):
            return {"token": "added"}

        async def create_doc_from_template(self, corpo):
            self.create_calls.append(corpo)
            if self.falhar:
                raise RuntimeError("zap down")
            return {"token": "doc_fake",
                    "signers": [{"token": "sg", "sign_url": "u"}]}

    zap = _ZapFalha()
    out1 = await _gerar(conn, asaas, zap)
    assert out1["status"] == "erro_zapsign"
    # cobrança já viva; contrato voltou a MONTAGEM
    assert len(asaas.called("create_payment")) == 1
    cid = out1["contrato_id"]
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.MONTAGEM

    # o find passa a reencontrar a cobrança no retry
    asaas.existente = {
        "id": "pay_fake",
        "invoiceUrl": "https://asaas.com/i/fake",
        "customer": "cus_fake",
    }
    zap.falhar = False
    out2 = await _gerar(conn, asaas, zap)

    assert out2["status"] == "pendente_revisao"
    assert out2["contrato_id"] == cid
    assert len(asaas.called("create_payment")) == 1       # NÃO duplicou
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM contrato"
    ).fetchone()["c"] == 1
    conn.close()


# --- erro_asaas deixa exatamente 1 contrato em MONTAGEM -----------------------

@pytest.mark.asyncio
async def test_erro_asaas_deixa_um_contrato_em_montagem():
    """med#5: create_payment lança → status erro_asaas, exatamente 1 contrato
    em MONTAGEM, nada no ZapSign."""
    conn = _db()
    asaas = FakeAsaas(raise_on={"create_payment"})
    zap = FakeZapSign()

    out = await _gerar(conn, asaas, zap)

    assert out["status"] == "erro_asaas"
    assert zap.create_calls == []
    rows = conn.execute(
        "SELECT estado FROM contrato"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["estado"] == EstadoContrato.MONTAGEM
    conn.close()


# --- placeholder residual aborta o create-doc --------------------------------

@pytest.mark.asyncio
async def test_placeholder_residual_aborta_create_doc():
    """med#6: um {{...}} não-resolvido num valor do data[] vazaria CRAVADO no
    PDF. Detecta e aborta antes do create-doc."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    # escopo cujo objeto carrega um placeholder esquecido (chega ao data[])
    cliente_placeholder = {**CLIENTE, "profissao": "{{PROFISSAO_ESQUECIDA}}"}

    out = await _gerar(conn, asaas, zap, cliente=cliente_placeholder)

    assert out["status"] == "erro_placeholder_residual"
    assert zap.create_calls == []                  # NÃO chamou o create-doc
    conn.close()


# --- quantização do valor: Asaas e contrato batem nos centavos ----------------

@pytest.mark.asyncio
async def test_valor_quantizado_asaas_e_contrato_batem():
    """med#7: 1234.999 → quantiza a 1235.0. O Asaas recebe 1235.0 e o
    {{VALOR_HONORARIOS}} do contrato mostra 1.235,00 (batem)."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    await _gerar(conn, asaas, zap, valor_honorarios=1234.999)

    # Asaas recebeu o valor quantizado
    assert asaas.called("create_payment")[0][2] == 1235.0
    # o contrato mostra o mesmo (1.235,00)
    corpo = zap.create_calls[0]
    pares = {d["de"]: d["para"] for d in corpo["data"]}
    assert pares["{{VALOR_HONORARIOS}}"] == "1.235,00"
    conn.close()


# --- aprovar_e_liberar com doc_token None (low#2) -----------------------------

@pytest.mark.asyncio
async def test_aprovar_sem_doc_token_nao_chama_resend():
    """low#2: contrato em PENDENTE_REVISAO sem zapsign_doc_token → guarda:
    reverte LIBERANDO→PENDENTE_REVISAO, NÃO chama resend(None)."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    # zera o doc_token (simula contrato em revisão sem token, p.ex. migração)
    conn.execute(
        "UPDATE contrato SET zapsign_doc_token = NULL WHERE id = ?", (cid,),
    )
    token = get_contrato(conn, cid)["aprovacao_token"]

    r = await aprovar_e_liberar(conn, zap, token=token)

    assert r["status"] == "erro_sem_doc_token"
    assert zap.resend_calls == []                  # NÃO chamou resend(None)
    # NÃO virou LIBERADO; voltou a PENDENTE_REVISAO
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.PENDENTE_REVISAO
    conn.close()


# --- Idempotência _criar_doc_silencioso: reconciliação por token (low#3) ------

@pytest.mark.asyncio
async def test_criar_doc_silencioso_reconcilia_por_token():
    """low#3 (a): contrato que JÁ tem zapsign_doc_token re-entrando → NÃO
    re-chama a ZapSign; reconcilia pra PENDENTE_REVISAO."""
    conn = _db()
    from noviello_funil.contrato import criar_contrato_pipeline
    c = criar_contrato_pipeline(
        conn, cliente_nome="Fulano Teste", cpf="00000000000",
        tipo_caso=TIPO, valor_honorarios_fmt="3.500,00", template_id="T1",
    )
    registrar_doc_preview(
        conn, c["id"], doc_token="doc_ja_existe", sign_url="https://zap/s/ja",
    )
    zap = FakeZapSign()

    out = await _criar_doc_silencioso(
        conn, zap,
        contrato=get_contrato(conn, c["id"]),
        cliente=CLIENTE, escopo=_escopo_resolvido(),
        valor_fmt="3.500,00", valor_extenso="três mil e quinhentos reais",
        invoice_url="https://asaas.com/i/x", template_id="T1",
        signers_extra=SIGNERS_EXTRA, base_url="https://funil.x",
    )

    assert out["status"] == "pendente_revisao"
    assert zap.create_calls == []                  # NÃO re-criou
    assert get_contrato(conn, c["id"])["estado"] == EstadoContrato.PENDENTE_REVISAO
    conn.close()


@pytest.mark.asyncio
async def test_criar_doc_silencioso_cas_perdido():
    """low#3 (b): contrato já fora de MONTAGEM (sem token) → CAS perde,
    status 'em_andamento', a ZapSign NÃO é chamada."""
    conn = _db()
    from noviello_funil.contrato import criar_contrato_pipeline
    c = criar_contrato_pipeline(
        conn, cliente_nome="Fulano Teste", cpf="00000000000",
        tipo_caso=TIPO, valor_honorarios_fmt="3.500,00", template_id="T1",
    )
    # força CRIANDO_DOC (sem token) — o claim MONTAGEM→CRIANDO_DOC vai perder
    transicao_contrato(
        conn, c["id"], EstadoContrato.CRIANDO_DOC, motivo="seed", ator="teste",
    )
    zap = FakeZapSign()

    out = await _criar_doc_silencioso(
        conn, zap,
        contrato=get_contrato(conn, c["id"]),
        cliente=CLIENTE, escopo=_escopo_resolvido(),
        valor_fmt="3.500,00", valor_extenso="três mil e quinhentos reais",
        invoice_url="https://asaas.com/i/x", template_id="T1",
        signers_extra=SIGNERS_EXTRA, base_url="https://funil.x",
    )

    assert out["status"] == "em_andamento"
    assert out["contrato_id"] == c["id"]
    assert zap.create_calls == []
    conn.close()


# --- dedupe defensivo: existente parcial não trava nem grava null (low#4) -----

@pytest.mark.asyncio
async def test_dedupe_existente_sem_invoice_completa_via_get_payment():
    """low#4: existente sem invoiceUrl mas com id → get_payment completa
    (não trava em 'sem_invoice_url')."""
    conn = _db()
    asaas = FakeAsaas(existente={"id": "pay_parcial"})   # SEM invoiceUrl/customer
    zap = FakeZapSign()

    out = await _gerar(conn, asaas, zap)

    assert out["status"] == "pendente_revisao"
    # completou via get_payment
    assert asaas.called("get_payment") == [("get_payment", "pay_parcial")]
    assert out["invoice_url"] == "https://asaas.com/i/fake"
    fresh = get_contrato(conn, out["contrato_id"])
    assert fresh["asaas_customer_id"] == "cus_fake"       # não gravou None
    conn.close()


# --- concorrência aprovar × reprovar (med#2) ---------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("ordem", ["aprovar_primeiro", "reprovar_primeiro"])
async def test_concorrencia_aprovar_reprovar_um_so_vence(ordem):
    """med#2: aprovar_e_liberar e reprovar_contrato competem do MESMO
    PENDENTE_REVISAO. Prova: estado final LIBERADO XOR REPROVADO; NUNCA resend E
    refuse no mesmo contrato; cobrança não deletada se aprovou; perdedor
    'ja_processado'."""
    conn = _db()
    asaas = FakeAsaas()
    zap = FakeZapSign(slow=True)
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    fresh = get_contrato(conn, cid)
    aprov_tok = fresh["aprovacao_token"]
    reprov_tok = fresh["reprovacao_token"]

    coros = [
        aprovar_e_liberar(conn, zap, token=aprov_tok),
        reprovar_contrato(conn, zap, asaas, token=reprov_tok, motivo="x"),
    ]
    if ordem == "reprovar_primeiro":
        coros = list(reversed(coros))
    r1, r2 = await asyncio.gather(*coros)

    estado = get_contrato(conn, cid)["estado"]
    # (1) LIBERADO XOR REPROVADO
    assert (estado == EstadoContrato.LIBERADO) ^ (estado == EstadoContrato.REPROVADO)
    # (2) NÃO houve resend E refuse no mesmo contrato
    assert not (len(zap.resend_calls) == 1 and len(zap.refuse_calls) == 1)
    # (3) cobrança não deletada quando o vencedor foi aprovar
    if estado == EstadoContrato.LIBERADO:
        assert asaas.called("delete_payment") == []
    # (4) exatamente um perdedor 'ja_processado'
    status = {r1["status"], r2["status"]}
    assert "ja_processado" in status
    conn.close()


# --- reconciliação de claims presos (med#3) ----------------------------------

@pytest.mark.asyncio
async def test_reconciliar_criando_doc_sem_token_volta_montagem():
    """med#3 (a): CRIANDO_DOC sem token → MONTAGEM."""
    conn = _db()
    from noviello_funil.contrato import criar_contrato_pipeline
    c = criar_contrato_pipeline(
        conn, cliente_nome="Fulano Teste", cpf="00000000000",
        tipo_caso=TIPO, valor_honorarios_fmt="3.500,00", template_id="T1",
    )
    transicao_contrato(
        conn, c["id"], EstadoContrato.CRIANDO_DOC, motivo="seed", ator="teste",
    )
    asaas, zap = FakeAsaas(), FakeZapSign()

    acoes = await reconciliar_contratos_presos(conn, asaas, zap)

    assert get_contrato(conn, c["id"])["estado"] == EstadoContrato.MONTAGEM
    assert any(a["acao"] == "criando_doc_para_montagem" for a in acoes)
    conn.close()


@pytest.mark.asyncio
async def test_reconciliar_liberando_re_resend_para_liberado():
    """med#3 (b): LIBERANDO com token → re-resend (idempotente) → LIBERADO."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    transicao_contrato(
        conn, cid, EstadoContrato.LIBERANDO, motivo="seed crash", ator="teste",
    )

    acoes = await reconciliar_contratos_presos(conn, asaas, zap)

    assert get_contrato(conn, cid)["estado"] == EstadoContrato.LIBERADO
    assert zap.resend_calls == ["doc_fake"]
    assert any(a["acao"] == "liberado" for a in acoes)
    conn.close()


@pytest.mark.asyncio
async def test_reconciliar_reprovando_re_refuse_e_cancela():
    """med#3 (c): REPROVANDO → re-refuse + cancela Asaas (PENDING) → REPROVADO."""
    conn = _db()
    asaas = FakeAsaas(payment_status="PENDING")
    zap = FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    transicao_contrato(
        conn, cid, EstadoContrato.REPROVANDO, motivo="seed crash", ator="teste",
    )

    acoes = await reconciliar_contratos_presos(conn, asaas, zap)

    assert get_contrato(conn, cid)["estado"] == EstadoContrato.REPROVADO
    assert zap.refuse_calls == [("doc_fake", "reprovação (reconciliação)")]
    assert asaas.called("delete_payment") == [("delete_payment", "pay_fake")]
    assert any(a["acao"] == "reprovado" for a in acoes)
    conn.close()


# --- reprovar pós-LIBERADO: cancelamento é manual (low#1) ---------------------

@pytest.mark.asyncio
async def test_reprovar_apos_liberado_ja_processado():
    """low#1: pós-LIBERADO reprovar retorna 'ja_processado' (cancelamento da
    cobrança é manual — documentado na docstring)."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    out = await _gerar(conn, asaas, zap)
    cid = out["contrato_id"]
    aprov_tok = get_contrato(conn, cid)["aprovacao_token"]
    await aprovar_e_liberar(conn, zap, token=aprov_tok)
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.LIBERADO

    reprov_tok = get_contrato(conn, cid)["reprovacao_token"]
    r = await reprovar_contrato(conn, zap, asaas, token=reprov_tok, motivo="x")

    assert r["status"] == "ja_processado"
    assert r["estado"] == EstadoContrato.LIBERADO
    # NÃO tocou no Asaas (não há delete às cegas pós-liberação)
    assert asaas.called("delete_payment") == []
    conn.close()


# --- montar_signers_padrao (signatários fixos da config) ---------------------

def test_montar_signers_padrao_ordem_e_qualificacao():
    from types import SimpleNamespace

    from noviello_funil.orquestrador_contrato import montar_signers_padrao
    s = SimpleNamespace(
        contrato_escritorio_nome="Mario Noviello",
        contrato_escritorio_email="mario@exemplo.com",
        contrato_escritorio_cpf="111",
        contrato_testemunha_1_nome="Hilde Teste",
        contrato_testemunha_1_email="h@exemplo.com",
        contrato_testemunha_1_cpf="222",
        contrato_testemunha_2_nome="Marcio Teste",
        contrato_testemunha_2_email="m@exemplo.com",
        contrato_testemunha_2_cpf="333",
    )
    signers = montar_signers_padrao(s)
    assert [x["order_group"] for x in signers] == [2, 3, 3]
    assert signers[0]["qualification"] == "Contratado"
    assert signers[1]["qualification"] == "Testemunha"
    assert signers[0]["email"] == "mario@exemplo.com"


def test_montar_signers_padrao_omite_sem_email():
    from types import SimpleNamespace

    from noviello_funil.orquestrador_contrato import montar_signers_padrao
    s = SimpleNamespace(
        contrato_escritorio_nome="", contrato_escritorio_email="",
        contrato_escritorio_cpf="",
        contrato_testemunha_1_nome="Hilde", contrato_testemunha_1_email="h@x.com",
        contrato_testemunha_1_cpf="",
        contrato_testemunha_2_nome="", contrato_testemunha_2_email="",
        contrato_testemunha_2_cpf="",
    )
    signers = montar_signers_padrao(s)
    assert len(signers) == 1 and signers[0]["qualification"] == "Testemunha"


# --- política de liberação por tipo de caso ----------------------------------

@pytest.mark.asyncio
async def test_tipo_sem_politica_continua_no_gate_humano():
    """REGRESSÃO: sem config de política, tudo se comporta como antes."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap)

    assert out["status"] == "pendente_revisao"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_politica_automatica_libera_e_chama_resend():
    """SIGNERS_EXTRA já traz o escritório em order_group 2 — a contra-assinatura
    existe, então o freio ético não dispara."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap, politicas={TIPO: AUTOMATICO})

    assert out["status"] == "liberado_automatico"
    assert out["motivo_liberacao"] == "politica_automatica"
    assert zap.resend_calls == [zap.doc_token]


@pytest.mark.asyncio
async def test_sem_escritorio_na_lista_nao_libera():
    """FREIO ESTRUTURAL. Sem ninguém em order_group 2 no documento, não há
    contra-assinatura — e é ela que sustenta o fundamento do modo automático.
    O freio lê a lista REAL de signatários, não a config: config e documento
    poderiam divergir, e aí o freio protegeria o fato errado."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    so_testemunha = [s for s in SIGNERS_EXTRA if s.get("order_group") != 2]

    out = await _gerar(
        conn, asaas, zap,
        politicas={TIPO: AUTOMATICO},
        signers_extra=so_testemunha,
    )

    assert out["status"] == "pendente_revisao"
    assert out["motivo_liberacao"] == "sem_contra_assinante"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_lista_de_signatarios_vazia_nao_libera():
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(
        conn, asaas, zap, politicas={TIPO: AUTOMATICO}, signers_extra=[],
    )

    assert out["status"] == "pendente_revisao"
    assert out["motivo_liberacao"] == "sem_contra_assinante"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_politica_automatica_acima_do_teto_nao_libera():
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(
        conn, asaas, zap,
        politicas={TIPO: AUTOMATICO},
        teto_automatico=100.0,
    )

    assert out["status"] == "pendente_revisao"
    assert out["motivo_liberacao"] == "acima_do_teto"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_send_automatic_email_continua_false_mesmo_no_automatico():
    """INVARIANTE: o doc SEMPRE nasce em silêncio. Criar e liberar seguem
    sendo duas chamadas — é o que permite mudar de política sem reescrever
    o pipeline."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    await _gerar(conn, asaas, zap, politicas={TIPO: AUTOMATICO})

    assert zap.create_calls[0]["send_automatic_email"] is False


@pytest.mark.asyncio
async def test_auditoria_registra_que_nao_houve_aprovacao_humana():
    """A trilha de auditoria existe pra responder QUEM liberou. Um contrato
    liberado por política não foi aprovado por ninguém, e a linha de
    transição não pode dizer que foi."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap, politicas={TIPO: AUTOMATICO})

    linhas = conn.execute(
        "SELECT ator, motivo FROM contrato_transicao WHERE contrato_id = ?",
        (out["contrato_id"],),
    ).fetchall()
    liberacao = [r for r in linhas if r["ator"] == "sistema"]
    assert liberacao, f"nenhuma transição do sistema em {[dict(r) for r in linhas]}"
    assert all("humana" not in (r["motivo"] or "") for r in liberacao)
