"""Tests do boletim diário de andamento da integração AASP."""

import datetime

import pytest

from noviello_funil.db import connect, run_migrations


@pytest.fixture()
def conn():
    c = connect(":memory:")
    run_migrations(c)
    yield c
    c.close()


def test_dentro_da_janela():
    from noviello_funil.aasp_boletim import dentro_da_janela
    hoje = datetime.date(2026, 8, 26)
    assert dentro_da_janela("2026-08-30", hoje) is True
    assert dentro_da_janela("2026-08-26", hoje) is True     # inclusive
    assert dentro_da_janela("2026-08-25", hoje) is False    # expirou
    assert dentro_da_janela("", hoje) is True               # vazio = sempre
    assert dentro_da_janela("lixo", hoje) is True           # inválido = sempre


def test_coletar_do_dia_e_acumulado(conn):
    from noviello_funil.aasp_boletim import coletar
    conn.execute(
        "INSERT INTO aasp_intimacao_vista (chave, processo, law_suit_id) "
        "VALUES ('c1', '1234567-08.2026.8.26.0100', 'u1')",
    )
    conn.execute(
        "INSERT INTO aasp_intimacao_vista (chave, processo, law_suit_id) "
        "VALUES ('c2', '7654321-09.2026.8.26.0000', '')",   # fora da carteira
    )
    conn.execute(
        "INSERT INTO aasp_intimacao_vista (chave, processo, law_suit_id, criado_em) "
        "VALUES ('c3', '1111111-11.2020.8.26.0100', 'u3', "
        "datetime('now', '-5 days'))",
    )
    conn.execute(
        "INSERT INTO tarefa_publicacao (publication_id, process_number, task_id) "
        "VALUES ('aasp:c1', '1234567-08.2026.8.26.0100', 't1')",
    )
    conn.execute(
        "INSERT INTO tarefa_publicacao (publication_id, process_number, task_id) "
        "VALUES ('pub-normal', 'x', 't2')",   # publicação nativa: não conta
    )
    d = coletar(conn)
    assert d["hoje_total"] == 2
    assert d["hoje_andamentos"] == 1
    assert d["hoje_fora"] == 1
    assert d["hoje_tarefas"] == 1
    assert d["acumulado"] == 3
    assert "1234567-08.2026.8.26.0100" in d["hoje_processos"]


def test_montar_boletim_com_e_sem_movimento():
    from noviello_funil.aasp_boletim import montar_boletim
    txt = montar_boletim({
        "hoje_total": 2, "hoje_andamentos": 1, "hoje_fora": 1,
        "hoje_tarefas": 1, "acumulado": 5,
        "hoje_processos": ["1234567-08.2026.8.26.0100"],
    }, hoje=datetime.date(2026, 8, 26))
    assert "26/08" in txt
    assert "2" in txt and "1" in txt and "5" in txt
    assert "1234567-08.2026.8.26.0100" in txt

    vazio = montar_boletim({
        "hoje_total": 0, "hoje_andamentos": 0, "hoje_fora": 0,
        "hoje_tarefas": 0, "acumulado": 5, "hoje_processos": [],
    }, hoje=datetime.date(2026, 8, 26))
    assert "sem intima" in vazio.lower() or "0" in vazio
