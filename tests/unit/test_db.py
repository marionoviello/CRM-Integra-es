"""Tests for sqlite connection and migrations."""

import sqlite3

from noviello_funil.db import connect, run_migrations


def test_connect_returns_row_factory():
    conn = connect(":memory:")
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_migrations_create_three_tables(db_conn):
    cursor = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [r["name"] for r in cursor.fetchall()]
    assert "leads" in tables
    assert "transicoes" in tables
    assert "webhooks_recebidos" in tables


def test_migrations_are_idempotent(db_conn):
    # Running migrations again should not raise
    run_migrations(db_conn)
    run_migrations(db_conn)


def test_leads_table_has_required_columns(db_conn):
    cursor = db_conn.execute("PRAGMA table_info(leads)")
    cols = {r["name"] for r in cursor.fetchall()}
    expected = {
        "id", "jurichat_lead_id", "jurichat_conversation_id",
        "contato_telefone", "contato_nome", "estado", "turnos",
        "ultima_msg_lead_em", "proxima_acao_em", "erro_atual",
        "criado_em", "atualizado_em",
    }
    assert expected.issubset(cols)
