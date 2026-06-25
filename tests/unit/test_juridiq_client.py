"""Tests for the Juridiq client + intake helper."""

import os
import sys

import httpx
import pytest

from noviello_funil.juridiq_client import JuridiqClient, intake_lead_agendado

# Os testes de _norm_doc importam do script de migração em scripts/.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
)


@pytest.mark.asyncio
async def test_search_person_encontrada(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/person/search").mock(
        return_value=httpx.Response(200, json={
            "id": "P-1", "name": "Fulano Teste",
            "phone": "5500000000001",
        }),
    )

    client = JuridiqClient("jq-test")
    try:
        person = await client.search_person_by_phone("5500000000001")
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
            nome="Fulano Teste",
            telefone="5500000000001",
            email="fulano@exemplo.com",
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
            "id": "P-EXISTENTE", "name": "Fulano",
        }),
    )
    create_route = respx_mock.post("https://api.juridiq.com.br/person/").mock(
        return_value=httpx.Response(201, json={"id": "P-NUNCA"}),
    )

    client = JuridiqClient("jq-test")
    try:
        pid = await intake_lead_agendado(
            client,
            nome="Fulano", telefone="5500000000001", email=None,
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


# --- _norm_doc: correção de CPF do CRM antigo (2026-06-10) ----------------

def test_norm_doc_cpf_11_digitos_intacto():
    from importar_clientes_juridiq import _norm_doc
    assert _norm_doc("07561830815") == "07561830815"


def test_norm_doc_cpf_12_digitos_com_lixo_no_fim():
    """CRM antigo exportou CPF com 1 dígito extra. Trunca pro CPF válido."""
    from importar_clientes_juridiq import _norm_doc
    # 549979658040 → 54997965804 (CPF com dígito verificador correto)
    assert _norm_doc("549979658040") == "54997965804"
    assert _norm_doc("236989238230") == "23698923823"


def test_norm_doc_remove_sufixo_excel():
    from importar_clientes_juridiq import _norm_doc
    assert _norm_doc("41107268834.0") == "41107268834"


def test_norm_doc_cnpj_14_intacto():
    from importar_clientes_juridiq import _norm_doc
    assert _norm_doc("27340554000194") == "27340554000194"


def test_norm_doc_12_digitos_nao_cpf_vira_cnpj():
    """12 dígitos que NÃO validam como CPF → CNPJ que perdeu zeros."""
    from importar_clientes_juridiq import _norm_doc
    # 000000000000 não é CPF válido → zfill 14
    assert _norm_doc("123456789012") == "00123456789012"


@pytest.mark.asyncio
async def test_intake_cliente_assinado_reusa_dedup_cria_e_best_effort():
    """#36 (25/jun): reusa person_id do contrato; senão dedupe por telefone;
    senão cria; falha vira None (fire-and-forget — ASSINADO é fato consumado)."""
    from unittest.mock import AsyncMock, MagicMock

    from noviello_funil.juridiq_client import intake_cliente_assinado

    # 1. person_id já no contrato → reusa, sem tocar a API.
    j = MagicMock()
    j.search_person_by_phone = AsyncMock()
    j.create_person = AsyncMock()
    pid = await intake_cliente_assinado(
        j, person_id="p-existe", nome="X", telefone="5511", email=None,
        tipo_caso="inventario",
    )
    assert pid == "p-existe"
    j.search_person_by_phone.assert_not_awaited()
    j.create_person.assert_not_awaited()

    # 2. sem person_id, telefone casa → reusa o encontrado.
    j2 = MagicMock()
    j2.search_person_by_phone = AsyncMock(return_value={"id": "p-found"})
    j2.create_person = AsyncMock()
    pid = await intake_cliente_assinado(
        j2, person_id=None, nome="X", telefone="5511", email=None, tipo_caso=None,
    )
    assert pid == "p-found"
    j2.create_person.assert_not_awaited()

    # 3. sem person_id, telefone NÃO casa → cria.
    j3 = MagicMock()
    j3.search_person_by_phone = AsyncMock(return_value=None)
    j3.create_person = AsyncMock(return_value={"id": "p-new"})
    pid = await intake_cliente_assinado(
        j3, person_id=None, nome="X", telefone="5511", email="x@x.com",
        tipo_caso="usucapiao",
    )
    assert pid == "p-new"
    j3.create_person.assert_awaited_once()

    # 4. API levanta → fire-and-forget, retorna None.
    j4 = MagicMock()
    j4.search_person_by_phone = AsyncMock(side_effect=RuntimeError("boom"))
    pid = await intake_cliente_assinado(
        j4, person_id=None, nome="X", telefone="5511", email=None, tipo_caso=None,
    )
    assert pid is None
