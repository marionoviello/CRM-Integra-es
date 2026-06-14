"""Tests for the daily unhandled-publications urgency alert (publicacoes)."""

import httpx
import pytest

from noviello_funil.publicacoes import (
    _data_curta,
    _parse_veredictos,
    buscar_nao_tratadas,
    montar_mensagem,
)

# --- _data_curta --------------------------------------------------------------

def test_data_curta_formatos():
    assert _data_curta("11/06/2026") == "11/06"
    assert _data_curta("2026-06-11") == "11/06"
    assert _data_curta("2026-06-11T00:00:00.000Z") == "11/06"
    assert _data_curta("") == "?"
    assert _data_curta(None) == "?"
    assert _data_curta("lixo") == "lixo"


# --- buscar_nao_tratadas -------------------------------------------------------

@pytest.mark.asyncio
async def test_buscar_pagina_normaliza_e_extrai_teor(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/publication/").mock(
        return_value=httpx.Response(200, json={
            "data": [{
                "id": "PUB1",
                "descriptionSmall": "Diário Oficial do Estado de São Paulo",
                "title": "Nova citação",
                "content": "<p>PODER JUDICIÁRIO <b>CITAÇÃO</b> Cite-se o réu.</p>",
                "processNumber": "Não encontrado",
                "publicationDate": "11/06/2026",
                "officialDiary": "Diário Oficial do Estado de São Paulo",
                "isHandled": False,
            }],
            "totalPages": 1, "totalResults": 1,
        }),
    )
    client = httpx.Client(
        base_url="https://api.juridiq.com.br",
        headers={"x-juridiq-api-key": "jq-test"},
    )
    try:
        pubs = buscar_nao_tratadas(client)
    finally:
        client.close()

    assert len(pubs) == 1
    p = pubs[0]
    assert p["processo"] == ""               # "Não encontrado" → vazio
    assert p["resumo"] == "Nova citação"     # title (descriptionSmall == diário descartado)
    # teor = content sem HTML, colapsado
    assert "CITAÇÃO" in p["teor"]
    assert "<" not in p["teor"]


# --- _parse_veredictos (robustez + fail-safe) --------------------------------

def _pubs(n):
    return [{"id": f"P{i}", "processo": f"000000{i}-11.2026.8.26.0053",
             "resumo": f"Ato {i}", "data": "10/06/2026", "diario": "DJE",
             "teor": f"teor {i}"} for i in range(1, n + 1)]


def test_parse_veredictos_mapeia_por_id():
    pubs = _pubs(2)
    raw = (
        '[{"id":"P1","urgente":true,"motivo":"intimação 15 dias","prazo":"15 dias"},'
        '{"id":"P2","urgente":false,"motivo":"mero expediente","prazo":""}]'
    )
    out = _parse_veredictos(raw, pubs)
    assert out[0]["urgente"] is True
    assert out[0]["motivo"] == "intimação 15 dias"
    assert out[0]["prazo"] == "15 dias"
    assert out[1]["urgente"] is False


def test_parse_veredictos_failsafe_json_invalido_marca_urgente():
    """LLM devolveu lixo → NÃO suprime: tudo vira urgente p/ Mario conferir."""
    pubs = _pubs(2)
    out = _parse_veredictos("desculpe, não consegui", pubs)
    assert all(v["urgente"] for v in out)
    assert all("classificar" in v["motivo"].lower() for v in out)


def test_parse_veredictos_id_ausente_no_retorno_vira_urgente():
    """Publicação que o LLM esqueceu de classificar → fail-safe urgente."""
    pubs = _pubs(2)
    raw = '[{"id":"P1","urgente":false,"motivo":"juntada"}]'
    out = _parse_veredictos(raw, pubs)
    by_id = {v["id"]: v for v in out}
    assert by_id["P1"]["urgente"] is False
    assert by_id["P2"]["urgente"] is True  # ausente → fail-safe


# --- montar_mensagem (só urgentes) -------------------------------------------

def test_mensagem_urgentes_singular_e_motivo():
    urg = [{**_pubs(1)[0], "urgente": True, "motivo": "audiência designada",
            "prazo": "20/06"}]
    msg = montar_mensagem(urg)
    assert msg.startswith("⚠️ *1 publicação que parece urgente*")
    assert "10/06 — 0000001-11.2026.8.26.0053" in msg
    assert "audiência designada" in msg
    assert "20/06" in msg
    assert "<" not in msg  # WhatsApp-safe


def test_mensagem_urgentes_plural_ordena_mais_antiga_primeiro():
    a = {**_pubs(1)[0], "data": "11/06/2026", "urgente": True, "motivo": "x"}
    b = {**_pubs(1)[0], "data": "09/06/2026", "urgente": True, "motivo": "y"}
    msg = montar_mensagem([a, b])
    assert msg.startswith("⚠️ *2 publicações que parecem urgentes*")
    assert msg.index("09/06") < msg.index("11/06")


def test_mensagem_menciona_juridiq_como_fonte_primaria():
    urg = [{**_pubs(1)[0], "urgente": True, "motivo": "sentença"}]
    msg = montar_mensagem(urg)
    # rodapé deixa claro que o Juridiq já manda o resto (sem duplicar)
    assert "Juridiq" in msg
