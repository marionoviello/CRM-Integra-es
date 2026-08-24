"""Tests do job de intimações AASP → Juridiq (aasp_intimacoes)."""

import datetime
import json

import httpx
import pytest

from noviello_funil.db import connect, run_migrations


@pytest.fixture()
def conn():
    c = connect(":memory:")
    run_migrations(c)
    yield c
    c.close()


def _settings(**overrides):
    from noviello_funil.config import Settings
    base = dict(
        _env_file=None,
        anthropic_api_key="sk-test",
        jurichat_api_key="jk-test",
        jurichat_webhook_secret="whsec-test",
        notificacao_telefone="5511999999999",
        mario_conversation_id="C-MARIO",
        jurichat_inbox_id="inbox-test",
    )
    base.update(overrides)
    from noviello_funil.config import Settings
    return Settings(**base)


# --- config / migrations ------------------------------------------------------

def test_config_aasp_defaults():
    s = _settings()
    assert s.aasp_chave == ""
    assert s.aasp_base_url == "https://intimacaoapi.aasp.org.br"
    assert s.aasp_dias_janela == 3
    assert s.aasp_criar_tarefa is True


def test_migrations_criam_tabelas_aasp(conn):
    for tabela in ("aasp_raw", "aasp_intimacao_vista"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (tabela,),
        ).fetchone()
        assert row is not None, tabela
