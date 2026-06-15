"""Tests for publication→task (prazo_tarefa, roadmap 1.1)."""

from noviello_funil.db import connect, run_migrations
from noviello_funil.prazo_tarefa import (
    calcular_prazo_sugerido,
    deve_criar_tarefa,
    ja_criada,
    marcar_criada,
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
