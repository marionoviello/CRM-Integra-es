"""Tests for the conflict-of-interest check (conflito, roadmap 1.7)."""

import httpx
import pytest

from noviello_funil.conflito import (
    checar_conflito,
    construir_indice_partes,
    normalizar_nome,
)
from noviello_funil.db import connect, run_migrations

# --- normalizar_nome ---------------------------------------------------------

def test_remove_sufixo_documento_e_acentos():
    assert normalizar_nome("EVELYN ELOIZA CAMARGO GRAVA - CPF: 084.XXX.XXX-XX") \
        == "evelyn eloiza camargo grava"
    assert normalizar_nome("José da Silva") == "jose da silva"
    assert normalizar_nome("  Maria   Santos  ") == "maria santos"


def test_nome_vazio():
    assert normalizar_nome("") == ""
    assert normalizar_nome(None) == ""


# --- construir_indice_partes -------------------------------------------------

@pytest.mark.asyncio
async def test_indexa_so_partes_contrarias(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/lawSuit/").mock(
        return_value=httpx.Response(200, json={
            "data": [{
                "processNumber": "1234567-89.2026.8.26.0100",
                "persons": [
                    {"name": "Maria Cliente Silva", "personOrigin": "Cliente"},
                    {"name": "João Réu Souza", "personOrigin": "Requerido"},
                    {"name": "INSS", "personOrigin": "Requerida"},
                ],
            }],
            "totalPages": 1, "totalResults": 1,
        }),
    )
    conn = connect(":memory:")
    run_migrations(conn)
    client = httpx.Client(base_url="https://api.juridiq.com.br",
                          headers={"x-juridiq-api-key": "jq"})
    try:
        n = construir_indice_partes(client, conn)
    finally:
        client.close()
    # só "João Réu Souza" entra: cliente fica de fora, e "INSS"
    # (instituição de 1 palavra) é filtrada pra não casar com leads
    assert n == 1
    nomes = {r[0] for r in conn.execute("SELECT nome_norm FROM parte_contraria")}
    assert "joao reu souza" in nomes
    assert "maria cliente silva" not in nomes  # é cliente, não conta
    assert "inss" not in nomes                  # 1 palavra → filtrada
    conn.close()


@pytest.mark.asyncio
async def test_cliente_em_outro_processo_nao_vira_adversario(respx_mock):
    # mesma pessoa: Cliente no proc A, Requerido no proc B → NÃO indexar
    # como adversário (não pode disparar conflito contra cliente da casa)
    respx_mock.get("https://api.juridiq.com.br/lawSuit/").mock(
        return_value=httpx.Response(200, json={
            "data": [
                {"processNumber": "1-1", "persons": [
                    {"name": "Carlos Multipapel Dias", "personOrigin": "Cliente"},
                ]},
                {"processNumber": "2-2", "persons": [
                    {"name": "Carlos Multipapel Dias", "personOrigin": "Requerido"},
                ]},
            ],
            "totalPages": 1, "totalResults": 2,
        }),
    )
    conn = connect(":memory:")
    run_migrations(conn)
    client = httpx.Client(base_url="https://api.juridiq.com.br",
                          headers={"x-juridiq-api-key": "jq"})
    try:
        construir_indice_partes(client, conn)
    finally:
        client.close()
    assert checar_conflito(conn, "Carlos Multipapel Dias") == []  # é cliente, não adversário
    conn.close()


# --- checar_conflito ---------------------------------------------------------

def test_lead_que_e_parte_contraria_dispara():
    conn = connect(":memory:")
    run_migrations(conn)
    conn.execute(
        "INSERT INTO parte_contraria (nome_norm, processo, papel) VALUES (?,?,?)",
        ("joao reu souza", "1234567-89.2026.8.26.0100", "Requerido"),
    )
    hits = checar_conflito(conn, "João Réu Souza")
    assert len(hits) == 1
    assert hits[0]["processo"] == "1234567-89.2026.8.26.0100"
    assert hits[0]["papel"] == "Requerido"
    conn.close()


def test_lead_desconhecido_nao_dispara():
    conn = connect(":memory:")
    run_migrations(conn)
    conn.execute(
        "INSERT INTO parte_contraria (nome_norm, processo, papel) VALUES (?,?,?)",
        ("joao reu souza", "1-1", "Requerido"),
    )
    assert checar_conflito(conn, "Maria Lead Nova") == []
    conn.close()


def test_nome_curto_demais_nao_casa():
    # match exige nome com ao menos 2 palavras — evita flood por 1º nome
    conn = connect(":memory:")
    run_migrations(conn)
    conn.execute(
        "INSERT INTO parte_contraria (nome_norm, processo, papel) VALUES (?,?,?)",
        ("joao reu souza", "1-1", "Requerido"),
    )
    assert checar_conflito(conn, "João") == []
    assert checar_conflito(conn, "") == []
    conn.close()
