"""Integration tests for the contract HTTP shell (rotas_contrato).

Cobre a CASCA do pipeline de fechamento (a engine é testada em
test_orquestrador_contrato). Foco nas INVARIANTES da casca:

  (1) GET /contrato/aprovar/{token} é PREFETCHER-SAFE: zero efeito colateral
      (estado NÃO muda; nenhum resend/refuse) — só busca o PDF (get_doc).
  (2) os webhooks validam o segredo constant-time e REJEITAM o errado (401).
  (3) os webhooks respondem 200 mesmo com erro no processamento (fila não trava).
  (4) idempotência dos webhooks (2º POST do mesmo evento → no-op).

Fakes async (FakeZapSign/FakeAsaas) registram as chamadas. Dados fictícios:
"Fulano Teste", cpf "00000000000", emails @exemplo.com.
"""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from noviello_funil.contrato import EstadoContrato, get_contrato
from noviello_funil.db import connect, run_migrations
from noviello_funil.orquestrador_contrato import gerar_contrato
from noviello_funil.rotas_contrato import register_contrato_routes

ZAPSIGN_SECRET = "zaphook-secret-test"
ASAAS_TOKEN = "asaastoken-test"

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
]


class FakeSettings:
    """Settings mínimo que a casca lê (segredos dos webhooks + notify)."""

    zapsign_webhook_secret = ZAPSIGN_SECRET
    asaas_webhook_token = ASAAS_TOKEN
    mario_conversation_id = ""   # sem notify_mario nos testes


class FakeZapSign:
    """ZapSign falso — registra chamadas; ``get_doc`` é o novo método da casca.

    ``doc_status`` controla o status que o get_doc devolve (default 'signed'
    pros testes de webhook). ``get_doc_raises`` injeta falha de rede.
    """

    def __init__(self, *, doc_token="doc_fake", sign_url="https://zap/s/fake",
                 doc_status="signed", get_doc_raises=False):
        self.doc_token = doc_token
        self.sign_url = sign_url
        self.doc_status = doc_status
        self.get_doc_raises = get_doc_raises
        self.create_calls: list[dict] = []
        self.resend_calls: list[str] = []
        self.refuse_calls: list[tuple] = []
        self.get_doc_calls: list[str] = []
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

    async def get_doc(self, doc_token):
        self.get_doc_calls.append(doc_token)
        if self.get_doc_raises:
            raise RuntimeError("get_doc boom")
        return {
            "token": doc_token,
            "status": self.doc_status,
            "original_file": "https://zap/orig/fake.pdf",
            "signed_file": "https://zap/signed/fake.pdf",
        }

    async def resend_notifications_bulk(self, doc_token):
        self.resend_calls.append(doc_token)
        return {"ok": True}

    async def refuse(self, doc_token, motivo):
        self.refuse_calls.append((doc_token, motivo))
        return {"ok": True}


class FakeAsaas:
    """Asaas falso — o subset que a engine usa pra montar o contrato."""

    def __init__(self, *, payment_status="PENDING"):
        self.payment_status = payment_status
        self.calls: list[tuple] = []
        self._por_ref: dict[str, dict] = {}

    async def find_payment_by_external_reference(self, ref):
        self.calls.append(("find_payment", ref))
        return self._por_ref.get(ref)

    async def get_or_create_customer(self, *, name, cpf, **kw):
        self.calls.append(("get_or_create_customer", cpf))
        return "cus_fake"

    async def create_payment(self, *, customer_id, value, due_date,
                             description, external_reference):
        self.calls.append(("create_payment", external_reference, value))
        pay = {
            "id": "pay_fake",
            "invoiceUrl": "https://asaas.com/i/fake",
            "customer": customer_id,
            "status": "PENDING",
        }
        self._por_ref[external_reference] = pay
        return pay

    async def get_payment(self, payment_id):
        self.calls.append(("get_payment", payment_id))
        return {
            "id": payment_id,
            "status": self.payment_status,
            "invoiceUrl": "https://asaas.com/i/fake",
            "customer": "cus_fake",
        }

    async def delete_payment(self, payment_id):
        self.calls.append(("delete_payment", payment_id))
        return {"deleted": True, "id": payment_id}

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


def _db():
    conn = connect(":memory:")
    run_migrations(conn)
    return conn


def _build_app(conn, zapsign, asaas):
    app = FastAPI()
    register_contrato_routes(
        app,
        get_db=lambda: conn,
        settings=FakeSettings(),
        zapsign=zapsign,
        asaas=asaas,
        jurichat=None,
    )
    return app


class _FakeJurichatRoute:
    """Jurichat falso pra rota de no-show — registra envios."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.starts: list[str] = []

    async def start_human_support(self, conv_id, **kw):
        self.starts.append(conv_id)
        return {"success": True}

    async def send_message(self, conv_id, text, **kw):
        self.sent.append((conv_id, text))
        return {"id": "m"}


def _insert_lead_noshow(conn, *, token):
    conn.execute(
        """INSERT INTO leads (jurichat_lead_id, jurichat_conversation_id,
           contato_telefone, contato_nome, estado, reuniao_em, noshow_token)
           VALUES ('L-9','C-9','5511999990000','Pedro Teste','aguardando_humano',
                   '2026-06-22T10:00:00+00:00', ?)""",
        (token,),
    )
    return conn.execute(
        "SELECT id FROM leads WHERE jurichat_lead_id='L-9'"
    ).fetchone()["id"]


def test_post_cancelar_reuniao_noshow_limpa_e_oferece_remarcacao():
    """POST /reuniao/cancelar/{token}: limpa a reunião, oferece remarcação ao
    lead e consome o token (idempotente — 2º POST = link inválido)."""
    conn = _db()
    token = "tok-noshow-abc"
    lead_id = _insert_lead_noshow(conn, token=token)
    jurichat = _FakeJurichatRoute()
    app = FastAPI()
    register_contrato_routes(
        app, get_db=lambda: conn, settings=FakeSettings(),
        zapsign=None, asaas=None, jurichat=jurichat,
    )
    client = TestClient(app)

    resp = client.post(f"/reuniao/cancelar/{token}")
    assert resp.status_code == 200
    assert "cancelada" in resp.text.lower()
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["reuniao_em"] is None
    assert lead["noshow_token"] is None
    assert any(c == "C-9" for c, _ in jurichat.sent)
    assert any("remarca" in t.lower() for _, t in jurichat.sent)

    # Token consumido → 2º POST devolve "link inválido".
    resp2 = client.post(f"/reuniao/cancelar/{token}")
    txt = resp2.text.lower()
    assert "inválido" in txt or "expirou" in txt


async def _criar_contrato_pendente(conn, asaas, zapsign):
    """Gera um contrato real em PENDENTE_REVISAO via a engine."""
    out = await gerar_contrato(
        conn, asaas, zapsign,
        cliente=CLIENTE, tipo_caso=TIPO, valor_honorarios=3500.0,
        valor_extenso="três mil e quinhentos reais",
        template_id="T1", signers_extra=SIGNERS_EXTRA, due_date="2026-07-01",
        base_url="https://funil.x",
    )
    assert out["status"] == "pendente_revisao"
    return out["contrato_id"]


# --- (1) GET de aprovação é PREFETCHER-SAFE (sem efeito colateral) ---------

def test_get_aprovar_is_prefetcher_safe():
    """INVARIANTE: o GET da página NÃO muda o estado e NÃO chama resend/refuse
    (prefetcher de link não pode aprovar). Só busca o PDF via get_doc."""
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))
    token = get_contrato(conn, cid)["aprovacao_token"]
    estado_antes = get_contrato(conn, cid)["estado"]

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.get(f"/contrato/aprovar/{token}")

    assert r.status_code == 200
    # estado NÃO mudou
    assert get_contrato(conn, cid)["estado"] == estado_antes
    assert estado_antes == EstadoContrato.PENDENTE_REVISAO
    # nenhuma liberação/recusa disparada
    assert zapsign.resend_calls == []
    assert zapsign.refuse_calls == []
    # PDF foi buscado FRESCO (get_doc chamado)
    assert zapsign.get_doc_calls == ["doc_fake"]
    # a página mostra o PDF e os valores de conferência
    assert "Fulano Teste" in r.text
    assert "iframe" in r.text
    conn.close()


def test_get_aprovar_token_invalido_404_like():
    """Token desconhecido → página de 'não encontrado', sem efeito."""
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.get("/contrato/aprovar/token-que-nao-existe")
    assert r.status_code == 200
    assert "não encontrado" in r.text.lower()
    assert zapsign.get_doc_calls == []
    conn.close()


# --- POST aprovar → libera (resend chamado, estado LIBERADO) --------------

def test_post_aprovar_libera():
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))
    token = get_contrato(conn, cid)["aprovacao_token"]

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(f"/contrato/aprovar/{token}")

    assert r.status_code == 200
    assert zapsign.resend_calls == ["doc_fake"]
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.LIBERADO
    conn.close()


# --- POST reprovar → reprova (refuse chamado) -----------------------------

def test_post_reprovar_reprova():
    conn = _db()
    asaas, zapsign = FakeAsaas(payment_status="PENDING"), FakeZapSign()
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))
    token = get_contrato(conn, cid)["reprovacao_token"]

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            f"/contrato/reprovar/{token}", data={"motivo": "Revisão interna"},
        )

    assert r.status_code == 200
    assert zapsign.refuse_calls == [("doc_fake", "Revisão interna")]
    assert asaas.called("delete_payment") == [("delete_payment", "pay_fake")]
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.REPROVADO
    conn.close()


# --- (2) webhook valida o segredo constant-time e rejeita o errado --------

def test_webhook_zapsign_secret_errado_401():
    """INVARIANTE: header secreto errado → 401, NADA processado."""
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/zapsign",
            json={"event_type": "doc_signed", "token": "doc_fake"},
            headers={"X-Zapsign-Secret": "ERRADO"},
        )
    assert r.status_code == 401
    assert zapsign.get_doc_calls == []
    conn.close()


def test_webhook_asaas_token_errado_401():
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/asaas",
            json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_fake"}},
            headers={"asaas-access-token": "ERRADO"},
        )
    assert r.status_code == 401
    conn.close()


# --- webhook zapsign: assinatura confirmada → ASSINADO --------------------

def test_webhook_zapsign_signed_marks_assinado():
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign(doc_status="signed")
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/zapsign",
            json={"event_type": "doc_signed", "token": "doc_fake"},
            headers={"X-Zapsign-Secret": ZAPSIGN_SECRET},
        )
    assert r.status_code == 200
    fresh = get_contrato(conn, cid)
    assert fresh["estado"] == EstadoContrato.ASSINADO
    assert fresh["signed_file_url"] == "https://zap/signed/fake.pdf"
    conn.close()


def test_webhook_zapsign_nao_signed_nao_marca():
    """Doc ainda não totalmente assinado (status != signed) → NÃO transiciona."""
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign(doc_status="pending")
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/zapsign",
            json={"event_type": "doc_signed", "token": "doc_fake"},
            headers={"X-Zapsign-Secret": ZAPSIGN_SECRET},
        )
    assert r.status_code == 200
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.PENDENTE_REVISAO
    conn.close()


# --- (4) idempotência do webhook zapsign ----------------------------------

def test_webhook_zapsign_idempotente():
    """2º POST do mesmo evento → no-op (duplicated), processa só 1x."""
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign(doc_status="signed")
    asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))

    app = _build_app(conn, zapsign, asaas)
    payload = {"event_type": "doc_signed", "token": "doc_fake"}
    headers = {"X-Zapsign-Secret": ZAPSIGN_SECRET}
    with TestClient(app) as c:
        r1 = c.post("/webhooks/zapsign", json=payload, headers=headers)
        r2 = c.post("/webhooks/zapsign", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicated") is True
    # get_doc só foi chamado UMA vez (2º POST nem entrou no background task)
    assert zapsign.get_doc_calls == ["doc_fake"]
    conn.close()


# --- webhook asaas: PAYMENT_RECEIVED → cobranca_paga_em -------------------

def test_webhook_asaas_received_marca_paga():
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))
    assert get_contrato(conn, cid)["cobranca_paga_em"] is None

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/asaas",
            json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_fake"}},
            headers={"asaas-access-token": ASAAS_TOKEN},
        )
    assert r.status_code == 200
    assert get_contrato(conn, cid)["cobranca_paga_em"] is not None
    conn.close()


def test_webhook_asaas_idempotente():
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))

    app = _build_app(conn, zapsign, asaas)
    payload = {"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_fake"}}
    headers = {"asaas-access-token": ASAAS_TOKEN}
    with TestClient(app) as c:
        r1 = c.post("/webhooks/asaas", json=payload, headers=headers)
        r2 = c.post("/webhooks/asaas", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicated") is True
    assert get_contrato(conn, cid)["cobranca_paga_em"] is not None
    conn.close()


# --- (3) webhook responde 200 mesmo com erro no processamento -------------

def test_webhook_zapsign_erro_processamento_ainda_200():
    """INVARIANTE: erro no BackgroundTask (get_doc explode) NÃO vaza —
    o webhook responde 200 (senão a fila do ZapSign re-tenta para sempre)."""
    conn = _db()
    asaas = FakeAsaas()
    zapsign = FakeZapSign(get_doc_raises=True)
    cid = asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/zapsign",
            json={"event_type": "doc_signed", "token": "doc_fake"},
            headers={"X-Zapsign-Secret": ZAPSIGN_SECRET},
        )
    assert r.status_code == 200
    # estado intacto (o processamento falhou mas não quebrou nada)
    assert get_contrato(conn, cid)["estado"] == EstadoContrato.PENDENTE_REVISAO
    conn.close()


def test_webhook_asaas_erro_processamento_ainda_200():
    """Payment sem contrato correspondente / erro → 200 (fila não trava)."""
    conn = _db()
    asaas, zapsign = FakeAsaas(), FakeZapSign()
    asyncio.run(_criar_contrato_pendente(conn, asaas, zapsign))

    app = _build_app(conn, zapsign, asaas)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/asaas",
            json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_inexistente"}},
            headers={"asaas-access-token": ASAAS_TOKEN},
        )
    assert r.status_code == 200
    conn.close()


# --- rotas seguras quando a feature está off (clientes None) --------------

def test_post_aprovar_sem_zapsign_responde_seguro():
    """Sem ZapSign (feature off) → 200 com aviso, sem quebrar."""
    conn = _db()
    app = _build_app(conn, zapsign=None, asaas=None)
    with TestClient(app) as c:
        r = c.post("/contrato/aprovar/qualquer-token")
    assert r.status_code == 200
    assert "habilitad" in r.text.lower()
    conn.close()
