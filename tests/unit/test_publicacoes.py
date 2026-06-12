"""Tests for the daily unhandled-publications alert (publicacoes)."""

import httpx
import pytest

from noviello_funil.publicacoes import (
    _data_curta,
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
async def test_buscar_pagina_e_normaliza(respx_mock):
    rota = respx_mock.get("https://api.juridiq.com.br/publication/").mock(
        side_effect=[
            httpx.Response(200, json={
                "data": [{
                    "id": "PUB1",
                    "descriptionSmall": "Intimação para audiência",
                    "processNumber": "1234567-89.2026.8.26.0000",
                    "publicationDate": "10/06/2026",
                    "officialDiary": "DJE-SP",
                    "isHandled": False,
                }],
                "totalPages": 2, "totalResults": 2,
            }),
            httpx.Response(200, json={
                "data": [{
                    "id": "PUB2",
                    # Caso real 11/jun: descriptionSmall repete o diário
                    # (inútil); title traz o tipo do ato.
                    "descriptionSmall": "Diário Oficial do Estado de São Paulo",
                    "title": "Nova citação",
                    "processNumber": "Não encontrado",
                    "publicationDate": "11/06/2026",
                    "officialDiary": "Diário Oficial do Estado de São Paulo",
                    "isHandled": False,
                }],
                "totalPages": 2, "totalResults": 2,
            }),
        ],
    )

    client = httpx.Client(
        base_url="https://api.juridiq.com.br",
        headers={"x-juridiq-api-key": "jq-test"},
    )
    try:
        pubs = buscar_nao_tratadas(client)
    finally:
        client.close()

    # Filtro isHandled=false na query de TODAS as chamadas
    for call in rota.calls:
        assert call.request.url.params["isHandled"] == "false"

    assert len(pubs) == 2
    assert pubs[0]["processo"] == "1234567-89.2026.8.26.0000"
    assert pubs[0]["resumo"] == "Intimação para audiência"
    # "Não encontrado" → sem processo; descriptionSmall == diário é
    # descartado e o title (tipo do ato) vira o resumo
    assert pubs[1]["processo"] == ""
    assert pubs[1]["resumo"] == "Nova citação"
    assert pubs[1]["diario"] == "Diário Oficial do Estado de São Paulo"


# --- montar_mensagem -----------------------------------------------------------

def _pub(i: int, data: str = "10/06/2026") -> dict:
    return {
        "id": f"P{i}",
        "processo": f"000000{i}-11.2026.8.26.0000",
        "resumo": f"Intimação {i}",
        "data": data,
        "diario": "DJE-SP",
    }


def test_mensagem_singular_e_conteudo():
    msg = montar_mensagem([_pub(1)])
    assert msg.startswith("📌 *1 publicação não tratada no Juridiq*")
    assert "10/06 — 0000001-11.2026.8.26.0000" in msg
    assert "Intimação 1" in msg
    assert "<" not in msg  # WhatsApp-safe


def test_mensagem_plural_e_ordenacao_mais_antiga_primeiro():
    msg = montar_mensagem([
        _pub(2, data="11/06/2026"),
        _pub(1, data="09/06/2026"),
    ])
    assert msg.startswith("📌 *2 publicações não tratadas no Juridiq*")
    # Mais antiga (09/06) vem antes da mais nova (11/06)
    assert msg.index("09/06") < msg.index("11/06")


def test_mensagem_sem_processo_mostra_diario():
    pub = _pub(1)
    pub["processo"] = ""
    msg = montar_mensagem([pub])
    assert "10/06 — DJE-SP" in msg


def test_mensagem_cap_de_itens():
    msg = montar_mensagem([_pub(i) for i in range(1, 13)])
    assert msg.count("•") == 10
    assert "e mais 2" in msg


def test_mensagem_trunca_resumo_longo():
    pub = _pub(1)
    pub["resumo"] = "x" * 500
    msg = montar_mensagem([pub])
    linha = next(l for l in msg.split("\n") if l.startswith("•"))
    assert len(linha) < 200
