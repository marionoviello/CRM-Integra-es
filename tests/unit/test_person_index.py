"""Tests for the phone→Juridiq-person index (person_index, roadmap 0.1)."""

import httpx
import pytest

from noviello_funil.db import connect, run_migrations
from noviello_funil.person_index import (
    chaves_telefone,
    construir_indice,
    resolver_telefone,
)

# --- chaves_telefone (normalização BR: 55, 9º dígito) ------------------------

def test_celular_completo_gera_variantes_com_e_sem_9():
    # 55 11 97642-5232
    ch = chaves_telefone("5511976425232")
    assert "11976425232" in ch   # com 9
    assert "1176425232" in ch     # sem o 9º dígito


def test_sem_codigo_pais_normaliza_igual():
    assert chaves_telefone("11976425232") & chaves_telefone("5511976425232")


def test_fixo_8_digitos_gera_variante_com_9():
    ch = chaves_telefone("1132514567")  # 11 3251-4567 (fixo)
    assert "1132514567" in ch
    assert "1193251456" not in ch  # fixo não vira celular cego (só prefixa 9)
    assert "11932514567" in ch     # variante com 9 prefixado


def test_formatacao_e_lixo_sao_ignorados():
    assert chaves_telefone("(11) 9 7642-5232") == chaves_telefone("5511976425232")


def test_numero_incompleto_retorna_vazio():
    assert chaves_telefone("12345") == set()
    assert chaves_telefone("") == set()
    assert chaves_telefone(None) == set()


def test_match_cruzado_com_sem_nono_digito():
    # cliente cadastrado com 9; mensagem chega sem o 9 → ainda casa
    cadastro = chaves_telefone("5511976425232")
    inbound = chaves_telefone("551176425232")
    assert cadastro & inbound


# --- construir_indice + resolver_telefone ------------------------------------

@pytest.mark.asyncio
async def test_indice_resolve_pessoa(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/person/").mock(
        return_value=httpx.Response(200, json={
            "data": [
                {"id": "P1", "name": "Fulano Teste", "phone": "5511976425232",
                 "email": "fulano@x.com", "document": "12345678900"},
                {"id": "P2", "name": "Sem Telefone", "phone": "",
                 "email": "semtel@x.com", "document": ""},
            ],
            "totalPages": 1, "totalResults": 2,
        }),
    )
    conn = connect(":memory:")
    run_migrations(conn)
    client = httpx.Client(
        base_url="https://api.juridiq.com.br",
        headers={"x-juridiq-api-key": "jq"},
    )
    try:
        n = construir_indice(client, conn)
    finally:
        client.close()
    assert n == 1  # só P1 tem telefone indexável

    # resolve com o número exato
    achado = resolver_telefone(conn, "5511976425232")
    assert achado is not None
    assert achado["person_id"] == "P1"
    assert achado["nome"] == "Fulano Teste"

    # resolve sem o 9º dígito (variação comum no WhatsApp)
    achado2 = resolver_telefone(conn, "551176425232")
    assert achado2 is not None and achado2["person_id"] == "P1"

    # telefone desconhecido → None
    assert resolver_telefone(conn, "5511000000000") is None
    conn.close()


@pytest.mark.asyncio
async def test_reconstruir_indice_e_idempotente(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/person/").mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "P1", "name": "A", "phone": "5511976425232"}],
            "totalPages": 1, "totalResults": 1,
        }),
    )
    conn = connect(":memory:")
    run_migrations(conn)
    client = httpx.Client(base_url="https://api.juridiq.com.br",
                          headers={"x-juridiq-api-key": "jq"})
    try:
        construir_indice(client, conn)
        construir_indice(client, conn)  # 2ª vez não duplica nem quebra
    finally:
        client.close()
    rows = conn.execute("SELECT COUNT(*) FROM person_index").fetchone()[0]
    assert rows == 2  # P1 gera 2 chaves (com e sem 9), sem duplicar
    conn.close()
