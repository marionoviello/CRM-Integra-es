"""Tests for publication→task (prazo_tarefa, roadmap 1.1)."""

from noviello_funil.db import connect, run_migrations
from noviello_funil.prazo_tarefa import (
    calcular_prazo_sugerido,
    criar_tarefa,
    deve_criar_tarefa,
    ja_criada,
    marcar_criada,
    montar_corpo_tarefa,
    montar_descricao,
    montar_titulo,
)

PUB = "11/06/2026"


# --- calcular_prazo_sugerido -------------------------------------------------

def test_prazo_n_dias_com_buffer():
    # 15 dias a partir de 11/06 = 26/06; buffer 3 → 23/06.
    assert calcular_prazo_sugerido("15 dias", PUB, buffer_dias=3) == "2026-06-23"


def test_prazo_data_explicita():
    # 20/06 (mesmo ano da publicação) − buffer 3 → 17/06.
    assert calcular_prazo_sugerido("20/06", PUB, buffer_dias=3) == "2026-06-17"
    assert calcular_prazo_sugerido("20/06/2026", PUB, buffer_dias=0) == "2026-06-20"


def test_prazo_data_sem_ano_que_passou_vai_pro_proximo_ano():
    # publicação 11/06/2026, prazo "05/01" → já passou no ano → 2027.
    assert calcular_prazo_sugerido("05/01", PUB, buffer_dias=0) == "2027-01-05"


def test_prazo_vazio_ou_nao_parseavel():
    assert calcular_prazo_sugerido("", PUB) is None
    assert calcular_prazo_sugerido("a ver", PUB) is None
    assert calcular_prazo_sugerido("10 dias", "data ruim") is None   # sem base


# --- titulo / descricao ------------------------------------------------------

def test_montar_titulo():
    t = montar_titulo("contestação", "1000000-00.2024.8.26.0100")
    assert t.startswith("PRAZO: contestação")
    assert "1000000-00.2024.8.26.0100" in t


def test_montar_titulo_sem_processo():
    assert montar_titulo("recurso", "") == "PRAZO: recurso"


def test_montar_descricao_avisa_sugerido():
    d = montar_descricao("intimação", "15 dias", "<b>Teor</b> do ato", PUB)
    assert "SUGERIDO" in d
    assert "15 dias" in d
    assert "Teor" in d


# --- regra de criação --------------------------------------------------------

def test_deve_criar_so_urgente_com_processo():
    assert deve_criar_tarefa({"urgente": True, "processo": "1-1"}) is True
    assert deve_criar_tarefa({"urgente": True, "processo": ""}) is False
    assert deve_criar_tarefa({"urgente": False, "processo": "1-1"}) is False


# --- idempotência ------------------------------------------------------------

def test_idempotencia_uma_tarefa_por_publicacao():
    conn = connect(":memory:")
    run_migrations(conn)
    assert ja_criada(conn, "pub1") is False
    marcar_criada(conn, "pub1", "1-1", "task-99")
    assert ja_criada(conn, "pub1") is True
    # re-marcar não duplica
    marcar_criada(conn, "pub1", "1-1", "task-99")
    assert ja_criada(conn, "pub1") is True
    conn.close()


def test_sem_id_nao_cria():
    conn = connect(":memory:")
    run_migrations(conn)
    assert ja_criada(conn, "") is True   # sem id → trata como já criada (não duplica cego)
    conn.close()


# --- corpo + POST ------------------------------------------------------------

def test_montar_corpo_tarefa():
    c = montar_corpo_tarefa(
        titulo="PRAZO: x", descricao="d", final_date="2026-06-20",
        initial_date="2026-06-15", law_suit_id="uuid-1", column_id="col-uuid",
        priority="Alta",
    )
    assert c["title"] == "PRAZO: x"
    assert c["lawSuitId"] == "uuid-1"
    assert c["finalDate"] == "2026-06-20"
    assert c["columnId"] == "col-uuid"          # UUID, não nome
    assert c["initialDate"] == "2026-06-15"
    assert c["priority"] == "Alta"
    # sem prazo → sem a chave finalDate (mas columnId/initialDate ficam)
    c2 = montar_corpo_tarefa(
        titulo="t", descricao="d", final_date=None, initial_date="2026-06-15",
        law_suit_id="u", column_id="c", priority="Alta",
    )
    assert "finalDate" not in c2
    assert c2["columnId"] == "c" and c2["initialDate"] == "2026-06-15"


class _FakeResp:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.body = None

    def post(self, path, json=None):
        self.body = json
        return self._resp


def test_criar_tarefa_sucesso():
    cli = _FakeClient(_FakeResp(201, {"data": {"id": "task-123"}}))
    tid, det = criar_tarefa(cli, {"title": "x"})
    assert tid == "task-123" and det == "ok"
    assert cli.body == {"title": "x"}


def test_criar_tarefa_erro_http_devolve_corpo():
    cli = _FakeClient(_FakeResp(400, text="column not found"))
    tid, det = criar_tarefa(cli, {})
    assert tid is None
    assert "http_400" in det and "column not found" in det
