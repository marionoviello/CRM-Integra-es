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


# --- indexar_carteira / criar_andamento --------------------------------------

def _jq_client():
    return httpx.Client(
        base_url="https://api.juridiq.com.br",
        headers={"x-juridiq-api-key": "jq-test"},
    )


def test_indexar_carteira_paginada(respx_mock):
    from noviello_funil.aasp_intimacoes import indexar_carteira
    url = "https://api.juridiq.com.br/lawSuit/"
    respx_mock.get(url, params={"page": 1, "limit": 100}).mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "uuid-1",
                      "processNumber": "1234567-08.2026.8.26.0100"}],
            "totalPages": 2,
        }),
    )
    respx_mock.get(url, params={"page": 2, "limit": 100}).mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "uuid-2", "processNumber": ""}],  # sem número: fora
            "totalPages": 2,
        }),
    )
    c = _jq_client()
    try:
        idx = indexar_carteira(c)
    finally:
        c.close()
    assert idx == {"12345670820268260100": "uuid-1"}


def test_montar_conteudo():
    from noviello_funil.aasp_intimacoes import montar_conteudo
    txt = montar_conteudo({
        "jornal": "DJE SP", "data": "20/08/2026", "teor": "Intime-se.",
    })
    assert txt.startswith("[AASP] Intimação — DJE SP — 20/08/2026")
    assert "Intime-se." in txt
    sem_teor = montar_conteudo({"jornal": "", "data": "", "teor": ""})
    assert "[AASP]" in sem_teor and "conferir" in sem_teor


def test_criar_andamento_ok_e_erro(respx_mock):
    from noviello_funil.aasp_intimacoes import criar_andamento
    respx_mock.post("https://api.juridiq.com.br/lawSuit/movements").mock(
        return_value=httpx.Response(201, json={"id": "mv-1"}),
    )
    c = _jq_client()
    try:
        ok, det = criar_andamento(c, "uuid-1", "[AASP] x", instance=2)
        assert (ok, det) == (True, "ok")
        body = json.loads(respx_mock.calls.last.request.content)
        assert body == {"lawSuitId": "uuid-1", "content": "[AASP] x",
                        "instance": 2}

        respx_mock.post("https://api.juridiq.com.br/lawSuit/movements").mock(
            return_value=httpx.Response(400, json={"message": "ruim"}),
        )
        ok, det = criar_andamento(c, "uuid-1", "x")
        assert ok is False and det.startswith("http_400")
    finally:
        c.close()


# --- buscar_intimacoes / raw / vista -----------------------------------------

def _aasp_client():
    return httpx.Client(base_url="https://intimacaoapi.aasp.org.br")


def test_buscar_intimacoes_ok(respx_mock):
    from noviello_funil.aasp_intimacoes import buscar_intimacoes
    respx_mock.get(
        "https://intimacaoapi.aasp.org.br/api/Associado/intimacao/json",
    ).mock(
        return_value=httpx.Response(200, json={
            "intimacoes": [{"numeroProcesso": "1"}, "lixo-nao-dict"],
            "erro": False, "status": "Sucesso",
        }),
    )
    c = _aasp_client()
    try:
        itens = buscar_intimacoes(c, "chave-teste", datetime.date(2026, 8, 20))
    finally:
        c.close()
    assert itens == [{"numeroProcesso": "1"}]   # não-dict filtrado
    req = respx_mock.calls.last.request
    assert "chave=chave-teste" in str(req.url)
    assert "data=2026-08-20" in str(req.url)


def test_buscar_intimacoes_erro_da_api_levanta(respx_mock):
    from noviello_funil.aasp_intimacoes import buscar_intimacoes
    respx_mock.get(
        "https://intimacaoapi.aasp.org.br/api/Associado/intimacao/json",
    ).mock(
        return_value=httpx.Response(200, json={
            "intimacoes": [], "erro": True, "status": "Chave inválida",
        }),
    )
    c = _aasp_client()
    try:
        with pytest.raises(RuntimeError, match="Chave inv"):
            buscar_intimacoes(c, "chave-ruim", datetime.date(2026, 8, 20))
    finally:
        c.close()


def test_salvar_raw_dedup(conn):
    from noviello_funil.aasp_intimacoes import salvar_raw
    item = {"numeroProcesso": "1", "conteudo": "X"}
    salvar_raw(conn, item, "2026-08-20")
    salvar_raw(conn, item, "2026-08-21")   # mesmo payload → não duplica
    n = conn.execute("SELECT COUNT(*) FROM aasp_raw").fetchone()[0]
    assert n == 1


def test_vista_roundtrip(conn):
    from noviello_funil.aasp_intimacoes import ja_vista, marcar_vista
    assert not ja_vista(conn, "abc")
    marcar_vista(conn, "abc", "1234567-08.2026.8.26.0100", "uuid-1")
    assert ja_vista(conn, "abc")
    marcar_vista(conn, "abc", "x", "y")   # idempotente, não levanta


def test_chave_dedup_estavel_e_distinta():
    from noviello_funil.aasp_intimacoes import normalizar_item
    a = {"numeroProcesso": "1", "conteudo": "X",
         "dataDisponibilizacao": "20/08/2026"}
    assert normalizar_item(a)["chave"] == normalizar_item(dict(a))["chave"]
    b = dict(a, conteudo="Y")
    assert normalizar_item(a)["chave"] != normalizar_item(b)["chave"]
