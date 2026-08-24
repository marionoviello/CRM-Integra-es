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


# --- normalizar_item / helpers -----------------------------------------------

def test_formatar_cnj():
    from noviello_funil.aasp_intimacoes import formatar_cnj
    assert formatar_cnj("12345670820268260100") == "1234567-08.2026.8.26.0100"
    assert formatar_cnj("1234567-08.2026.8.26.0100") == "1234567-08.2026.8.26.0100"
    assert formatar_cnj("123") == ""
    assert formatar_cnj("") == ""


def test_instancia_sugerida():
    from noviello_funil.aasp_intimacoes import instancia_sugerida
    assert instancia_sugerida("22345670820268260000") == 2   # origem 0000 = 2º grau
    assert instancia_sugerida("12345670820268260100") is None
    assert instancia_sugerida("123") is None


def test_normalizar_item_campos_padrao():
    from noviello_funil.aasp_intimacoes import normalizar_item
    item = normalizar_item({
        "numeroProcesso": "1234567-08.2026.8.26.0100",
        "conteudo": "<p>Intime-se a parte <b>autora</b>.</p>",
        "dataDisponibilizacao": "20/08/2026",
        "jornal": "DJE - Caderno Judicial",
    })
    assert item["processo"] == "1234567-08.2026.8.26.0100"
    assert item["processo_digitos"] == "12345670820268260100"
    assert item["teor"] == "Intime-se a parte autora."
    assert item["data"] == "20/08/2026"
    assert item["jornal"] == "DJE - Caderno Judicial"
    assert len(item["chave"]) == 64


def test_normalizar_item_variantes_e_case_insensitive():
    from noviello_funil.aasp_intimacoes import normalizar_item
    item = normalizar_item({
        "Processo": "12345670820268260100",
        "Despacho": "Vistos.",
        "DataPublicacao": "2026-08-20",
        "NomeJornal": "DJE SP",
    })
    assert item["processo"] == "1234567-08.2026.8.26.0100"  # máscara aplicada
    assert item["teor"] == "Vistos."
    assert item["data"] == "2026-08-20"
    assert item["jornal"] == "DJE SP"


def test_normalizar_item_sem_processo_nao_quebra():
    from noviello_funil.aasp_intimacoes import normalizar_item
    item = normalizar_item({"conteudo": "Edital genérico."})
    assert item["processo"] == ""
    assert item["processo_digitos"] == ""
    assert item["teor"] == "Edital genérico."


def test_chave_dedup_estavel_e_distinta():
    from noviello_funil.aasp_intimacoes import normalizar_item
    a = {"numeroProcesso": "1", "conteudo": "X",
         "dataDisponibilizacao": "20/08/2026"}
    assert normalizar_item(a)["chave"] == normalizar_item(dict(a))["chave"]
    b = dict(a, conteudo="Y")
    assert normalizar_item(a)["chave"] != normalizar_item(b)["chave"]
