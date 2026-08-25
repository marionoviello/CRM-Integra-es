"""Tests da conferência e-mails (AASP/OAB) × integração AASP→Juridiq."""

import pytest

from noviello_funil.db import connect, run_migrations


@pytest.fixture()
def conn():
    c = connect(":memory:")
    run_migrations(c)
    yield c
    c.close()


# --- extrair_numeros_cnj ------------------------------------------------------

def test_extrair_numeros_cnj_mascarados_e_dedup():
    from noviello_funil.aasp_conferencia import extrair_numeros_cnj
    texto = (
        "Intimação Processo: 1234567-08.2026.8.26.0100 blá blá.\n"
        "PROCESSO: 1234567-08.2026.8.26.0100 - repetido |\n"
        "Agravo Nº 7654321-09.2026.8.26.0000/SP despacho\n"
        "telefone 11 99999-9999 e CNPJ 12.345.678/0001-99 não contam"
    )
    nums = extrair_numeros_cnj(texto)
    assert nums == {"12345670820268260100", "76543210920268260000"}


def test_extrair_numeros_cnj_texto_vazio():
    from noviello_funil.aasp_conferencia import extrair_numeros_cnj
    assert extrair_numeros_cnj("") == set()
    assert extrair_numeros_cnj(None) == set()


# --- numeros_processados ------------------------------------------------------

def test_numeros_processados_janela(conn):
    from noviello_funil.aasp_conferencia import numeros_processados
    conn.execute(
        "INSERT INTO aasp_intimacao_vista (chave, processo, law_suit_id) "
        "VALUES ('c1', '1234567-08.2026.8.26.0100', 'u1')",
    )
    # antiga (fora da janela de 3 dias)
    conn.execute(
        "INSERT INTO aasp_intimacao_vista (chave, processo, law_suit_id, criado_em) "
        "VALUES ('c2', '7654321-09.2026.8.26.0000', 'u2', "
        "datetime('now', '-10 days'))",
    )
    nums = numeros_processados(conn, dias=3)
    assert nums == {"12345670820268260100"}


# --- conferir -----------------------------------------------------------------

def test_conferir_tudo_capturado():
    from noviello_funil.aasp_conferencia import conferir
    achados = {
        "12345670820268260100": {"fonte": "AASP", "data": "25/08"},
    }
    faltantes = conferir(achados, {"12345670820268260100"})
    assert faltantes == []


def test_conferir_faltante_vira_alerta():
    from noviello_funil.aasp_conferencia import conferir
    achados = {
        "12345670820268260100": {"fonte": "AASP", "data": "25/08"},
        "76543210920268260000": {"fonte": "Recorte OAB", "data": "25/08"},
    }
    faltantes = conferir(achados, {"12345670820268260100"})
    assert len(faltantes) == 1
    f = faltantes[0]
    assert f["processo"] == "7654321-09.2026.8.26.0000"
    assert f["fonte"] == "Recorte OAB"


# --- montar_mensagem ----------------------------------------------------------

def test_mensagem_silencio_sem_intimacao():
    from noviello_funil.aasp_conferencia import montar_mensagem
    assert montar_mensagem([], 0) is None


def test_mensagem_ok_positivo():
    from noviello_funil.aasp_conferencia import montar_mensagem
    txt = montar_mensagem([], 3)
    assert "3" in txt
    assert "✓" in txt or "capturad" in txt.lower()


def test_mensagem_alerta_faltantes():
    from noviello_funil.aasp_conferencia import montar_mensagem
    txt = montar_mensagem(
        [{"processo": "7654321-09.2026.8.26.0000", "fonte": "Recorte OAB",
          "data": "25/08"}],
        total=2,
    )
    assert "🚨" in txt
    assert "7654321-09.2026.8.26.0000" in txt
    assert "Recorte OAB" in txt


# --- classificar e-mail por remetente ----------------------------------------

def test_fonte_do_remetente():
    from noviello_funil.aasp_conferencia import fonte_do_remetente
    assert fonte_do_remetente(
        "AASP Intimações <intimacoes@info.aasp.org.br>",
        "intimacoes@info.aasp.org.br,oabsp@recortedigital.adv.br",
    ) == "AASP"
    assert fonte_do_remetente(
        "oabsp@recortedigital.adv.br",
        "intimacoes@info.aasp.org.br,oabsp@recortedigital.adv.br",
    ) == "Recorte OAB"
    assert fonte_do_remetente(
        "alguem@outro.com",
        "intimacoes@info.aasp.org.br,oabsp@recortedigital.adv.br",
    ) is None
