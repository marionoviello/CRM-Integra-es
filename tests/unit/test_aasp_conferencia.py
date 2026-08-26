"""Tests da conferência e-mails (AASP/OAB) × integração AASP→Juridiq."""

import httpx
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


# --- numeros_no_painel (3ª fonte: o que o Juridiq realmente tem) --------------

def _client():
    return httpx.Client(
        base_url="https://api.juridiq.com.br",
        headers={"x-juridiq-api-key": "jq-test"},
    )


def test_numeros_no_painel_pagina_e_dedup(respx_mock):
    from noviello_funil.aasp_conferencia import numeros_no_painel
    paginas = [
        httpx.Response(200, json={
            "data": [
                {"processNumber": "1234567-08.2026.8.26.0100"},
                {"processNumber": "1234567-08.2026.8.26.0100"},
            ],
            "totalPages": 2,
        }),
        httpx.Response(200, json={
            "data": [{"processNumber": "7654321-09.2026.8.26.0000"}],
            "totalPages": 2,
        }),
    ]
    respx_mock.get("https://api.juridiq.com.br/publication/").mock(
        side_effect=paginas,
    )
    c = _client()
    try:
        nums = numeros_no_painel(c, dias=2)
    finally:
        c.close()
    assert nums == {"12345670820268260100", "76543210920268260000"}


def test_numeros_no_painel_fallback_regex_quando_sem_processnumber(respx_mock):
    """Juridiq às vezes devolve processNumber 'Não encontrado' — o número
    ainda está no teor da publicação e precisa contar."""
    from noviello_funil.aasp_conferencia import numeros_no_painel
    respx_mock.get("https://api.juridiq.com.br/publication/").mock(
        return_value=httpx.Response(200, json={
            "data": [{
                "processNumber": "Não encontrado",
                "content": "<p>Processo 1234567-08.2026.8.26.0100 - vista</p>",
            }],
            "totalPages": 1,
        }),
    )
    c = _client()
    try:
        nums = numeros_no_painel(c, dias=2)
    finally:
        c.close()
    assert nums == {"12345670820268260100"}


def test_numeros_no_painel_consulta_a_janela_de_datas(respx_mock):
    import datetime

    from noviello_funil.aasp_conferencia import numeros_no_painel
    rota = respx_mock.get("https://api.juridiq.com.br/publication/").mock(
        return_value=httpx.Response(200, json={"data": [], "totalPages": 1}),
    )
    c = _client()
    try:
        numeros_no_painel(c, dias=2, hoje=datetime.date(2026, 8, 26))
    finally:
        c.close()
    params = rota.calls[0].request.url.params
    assert params["start"] == "2026-08-24"
    assert params["end"] == "2026-08-26"


def test_numeros_no_painel_erro_devolve_none(respx_mock):
    """API fora do ar não pode virar 'o painel está vazio' (alerta falso)."""
    from noviello_funil.aasp_conferencia import numeros_no_painel
    respx_mock.get("https://api.juridiq.com.br/publication/").mock(
        return_value=httpx.Response(500, text="boom"),
    )
    c = _client()
    try:
        assert numeros_no_painel(c, dias=2) is None
    finally:
        c.close()


# --- conferir com a 3ª fonte --------------------------------------------------

def test_conferir_marca_o_que_o_painel_tem():
    from noviello_funil.aasp_conferencia import conferir
    achados = {
        "12345670820268260100": {"fonte": "AASP", "data": "26/08"},
        "76543210920268260000": {"fonte": "Recorte OAB", "data": "26/08"},
    }
    faltantes = conferir(
        achados,
        processados=set(),
        no_painel={"12345670820268260100"},
    )
    por_processo = {f["processo"]: f for f in faltantes}
    assert por_processo["1234567-08.2026.8.26.0100"]["no_painel"] is True
    assert por_processo["7654321-09.2026.8.26.0000"]["no_painel"] is False


def test_conferir_sem_painel_consultado_marca_none():
    from noviello_funil.aasp_conferencia import conferir
    achados = {"12345670820268260100": {"fonte": "AASP", "data": "26/08"}}
    faltantes = conferir(achados, processados=set(), no_painel=None)
    assert faltantes[0]["no_painel"] is None


# --- severidade da mensagem ---------------------------------------------------

def test_mensagem_so_no_painel_nao_e_alerta_grave():
    from noviello_funil.aasp_conferencia import montar_mensagem
    txt = montar_mensagem(
        [{"processo": "1234567-08.2026.8.26.0100", "fonte": "AASP",
          "data": "26/08", "no_painel": True}],
        total=1,
    )
    assert "🚨" not in txt
    assert "⚠️" in txt
    assert "Juridiq" in txt


def test_mensagem_brandos_viram_contagem_nao_lista():
    """O Juridiq já manda essas movimentações no WhatsApp por conta própria:
    repetir a lista aqui é ruído duplicado. Só a contagem; detalhe no log."""
    from noviello_funil.aasp_conferencia import montar_mensagem
    txt = montar_mensagem(
        [{"processo": "1234567-08.2026.8.26.0100", "fonte": "AASP",
          "data": "26/08", "no_painel": True},
         {"processo": "7654321-09.2026.8.26.0000", "fonte": "AASP",
          "data": "26/08", "no_painel": True}],
        total=5,
    )
    assert "1234567-08.2026.8.26.0100" not in txt
    assert "7654321-09.2026.8.26.0000" not in txt
    assert "2" in txt                       # a contagem aparece
    assert "log" in txt.lower()             # e diz onde achar o detalhe


def test_mensagem_ausente_em_tudo_e_alerta_grave():
    from noviello_funil.aasp_conferencia import montar_mensagem
    txt = montar_mensagem(
        [{"processo": "7654321-09.2026.8.26.0000", "fonte": "Recorte OAB",
          "data": "26/08", "no_painel": False}],
        total=1,
    )
    assert "🚨" in txt
    assert "7654321-09.2026.8.26.0000" in txt


def test_mensagem_separa_os_dois_baldes():
    from noviello_funil.aasp_conferencia import montar_mensagem
    txt = montar_mensagem(
        [
            {"processo": "1234567-08.2026.8.26.0100", "fonte": "AASP",
             "data": "26/08", "no_painel": True},
            {"processo": "7654321-09.2026.8.26.0000", "fonte": "Recorte OAB",
             "data": "26/08", "no_painel": False},
        ],
        total=3,
    )
    assert "🚨" in txt and "⚠️" in txt
    grave, brando = txt.index("🚨"), txt.index("⚠️")
    assert grave < brando          # o que sumiu vem primeiro
    # o grave é listado item a item; o brando, só contado
    assert "7654321-09.2026.8.26.0000" in txt
    assert "1234567-08.2026.8.26.0100" not in txt


# --- fonte_painel: degradação quando o Juridiq não está configurado -----------

class _Cfg:
    def __init__(self, api_key):
        self.juridiq_api_key = api_key
        self.juridiq_base_url = "https://api.juridiq.com.br"
        self.aasp_conferencia_dias = 2


def test_fonte_painel_sem_api_key_nao_consulta():
    """Sem JURIDIQ_API_KEY o job degrada pro comportamento antigo (2 fontes),
    não para de rodar nem inventa painel vazio."""
    from noviello_funil.aasp_conferencia import fonte_painel
    assert fonte_painel(_Cfg("")) is None


def test_fonte_painel_com_api_key_consulta_o_juridiq(respx_mock):
    from noviello_funil.aasp_conferencia import fonte_painel
    rota = respx_mock.get("https://api.juridiq.com.br/publication/").mock(
        return_value=httpx.Response(200, json={
            "data": [{"processNumber": "1234567-08.2026.8.26.0100"}],
            "totalPages": 1,
        }),
    )
    assert fonte_painel(_Cfg("jq-test")) == {"12345670820268260100"}
    assert rota.calls[0].request.headers["x-juridiq-api-key"] == "jq-test"
