"""Tests for the Juridiq client + intake helper."""

import httpx
import pytest

from noviello_funil.juridiq_client import JuridiqClient, intake_lead_agendado


@pytest.mark.asyncio
async def test_search_person_encontrada(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/person/search").mock(
        return_value=httpx.Response(200, json={
            "id": "P-1", "name": "Franklin Crespo",
            "phone": "5514991817005",
        }),
    )

    client = JuridiqClient("jq-test")
    try:
        person = await client.search_person_by_phone("5514991817005")
    finally:
        await client.aclose()

    assert person["id"] == "P-1"


@pytest.mark.asyncio
async def test_search_person_404_retorna_none(respx_mock):
    """API retorna 4xx quando não acha — tratamos como None, sem raise."""
    respx_mock.get("https://api.juridiq.com.br/person/search").mock(
        return_value=httpx.Response(404, json={
            "message": "not found", "statusCode": 404,
            "translateMessage": "Pessoa não encontrada",
        }),
    )

    client = JuridiqClient("jq-test")
    try:
        person = await client.search_person_by_phone("5500000000000")
    finally:
        await client.aclose()

    assert person is None


@pytest.mark.asyncio
async def test_create_person_envia_header_e_campos(respx_mock):
    route = respx_mock.post("https://api.juridiq.com.br/person/").mock(
        return_value=httpx.Response(201, json={"id": "P-NEW", "name": "Maria"}),
    )

    client = JuridiqClient("jq-test")
    try:
        person = await client.create_person(
            name="Maria Silva",
            phone="5511999998888",
            email="maria@exemplo.com",
            annotation="Lead qualificado",
        )
    finally:
        await client.aclose()

    assert person["id"] == "P-NEW"
    req = route.calls.last.request
    assert req.headers["x-juridiq-api-key"] == "jq-test"
    import json as _json
    body = _json.loads(req.read())
    assert body["name"] == "Maria Silva"
    assert body["phone"] == "5511999998888"
    assert body["email"] == "maria@exemplo.com"
    assert body["personOrigin"] == "lead"
    assert "Julia" in body["clientDiscoverOffice"]
    assert body["annotation"] == "Lead qualificado"


# --- intake_lead_agendado --------------------------------------------------

@pytest.mark.asyncio
async def test_intake_cria_pessoa_nova(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/person/search").mock(
        return_value=httpx.Response(404, json={
            "message": "x", "statusCode": 404, "translateMessage": "x",
        }),
    )
    route = respx_mock.post("https://api.juridiq.com.br/person/").mock(
        return_value=httpx.Response(201, json={"id": "P-77"}),
    )

    client = JuridiqClient("jq-test")
    try:
        pid = await intake_lead_agendado(
            client,
            nome="Franklin Crespo",
            telefone="5514991817005",
            email="franklin@gmail.com",
            resumo_caso="Inventário extrajudicial SP, ~R$800k",
            horario_humano="qua (10/jun) às 14h",
            meet_link="https://meet.google.com/qzf-nzef-aex",
        )
    finally:
        await client.aclose()

    assert pid == "P-77"
    import json as _json
    body = _json.loads(route.calls.last.request.read())
    # Annotation carrega resumo + agendamento + Meet
    assert "Inventário extrajudicial" in body["annotation"]
    assert "qua (10/jun) às 14h" in body["annotation"]
    assert "meet.google.com" in body["annotation"]
    assert "Julia" in body["annotation"]


@pytest.mark.asyncio
async def test_intake_idempotente_pessoa_ja_existe(respx_mock):
    """Telefone já cadastrado → NÃO duplica, retorna id existente."""
    respx_mock.get("https://api.juridiq.com.br/person/search").mock(
        return_value=httpx.Response(200, json={
            "id": "P-EXISTENTE", "name": "Franklin",
        }),
    )
    create_route = respx_mock.post("https://api.juridiq.com.br/person/").mock(
        return_value=httpx.Response(201, json={"id": "P-NUNCA"}),
    )

    client = JuridiqClient("jq-test")
    try:
        pid = await intake_lead_agendado(
            client,
            nome="Franklin", telefone="5514991817005", email=None,
            resumo_caso="x", horario_humano="x", meet_link="",
        )
    finally:
        await client.aclose()

    assert pid == "P-EXISTENTE"
    assert not create_route.called


@pytest.mark.asyncio
async def test_intake_nunca_levanta_em_falha(respx_mock, caplog):
    """API do Juridiq fora do ar → intake retorna None, agendamento
    não pode ser afetado."""
    respx_mock.get("https://api.juridiq.com.br/person/search").mock(
        return_value=httpx.Response(503),
    )

    client = JuridiqClient("jq-test")
    try:
        pid = await intake_lead_agendado(
            client,
            nome="X", telefone="55", email=None,
            resumo_caso="x", horario_humano="x", meet_link="",
        )  # MUST NOT raise
    finally:
        await client.aclose()

    assert pid is None
    assert any("intake juridiq falhou" in r.message for r in caplog.records)
