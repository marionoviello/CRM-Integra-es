"""Tests for the Asaas client (cobrança de honorários, pipeline 3.x)."""

import json as _json

import httpx
import pytest

from noviello_funil.asaas import AsaasClient

_BASE = "https://api-sandbox.asaas.com"


@pytest.mark.asyncio
async def test_find_customer_encontrado(respx_mock):
    respx_mock.get(f"{_BASE}/v3/customers").mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "cus_123", "name": "Fulano Teste"}], "totalCount": 1,
        }),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        cid = await client.find_customer_by_cpf("000.000.000-00")
    finally:
        await client.aclose()
    assert cid == "cus_123"


@pytest.mark.asyncio
async def test_find_customer_nao_existe(respx_mock):
    respx_mock.get(f"{_BASE}/v3/customers").mock(
        return_value=httpx.Response(200, json={"data": [], "totalCount": 0}),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        cid = await client.find_customer_by_cpf("000.000.000-00")
    finally:
        await client.aclose()
    assert cid is None


@pytest.mark.asyncio
async def test_create_customer_header_e_cpf_digitos(respx_mock):
    route = respx_mock.post(f"{_BASE}/v3/customers").mock(
        return_value=httpx.Response(200, json={"id": "cus_new"}),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        cid = await client.create_customer(
            name="Fulano Teste", cpf="000.000.000-00",
            email="f@x.com", mobile_phone="(11) 99999-8888",
            external_reference="contrato-5", notification_disabled=True,
        )
    finally:
        await client.aclose()
    assert cid == "cus_new"
    req = route.calls.last.request
    assert req.headers["access_token"] == "$aact_hmlg_test"   # não é Bearer
    body = _json.loads(req.read())
    assert body["cpfCnpj"] == "00000000000"                   # só dígitos
    assert body["mobilePhone"] == "11999998888"
    assert body["notificationDisabled"] is True


@pytest.mark.asyncio
async def test_get_or_create_reusa_existente(respx_mock):
    respx_mock.get(f"{_BASE}/v3/customers").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "cus_ja"}]}),
    )
    create = respx_mock.post(f"{_BASE}/v3/customers").mock(
        return_value=httpx.Response(200, json={"id": "cus_nunca"}),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        cid = await client.get_or_create_customer(
            name="Fulano Teste", cpf="000.000.000-00",
        )
    finally:
        await client.aclose()
    assert cid == "cus_ja"
    assert not create.called                                  # não duplicou


@pytest.mark.asyncio
async def test_create_payment_undefined_devolve_invoice(respx_mock):
    route = respx_mock.post(f"{_BASE}/v3/payments").mock(
        return_value=httpx.Response(200, json={
            "id": "pay_1", "status": "PENDING",
            "invoiceUrl": "https://www.asaas.com/i/abc123",
        }),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        pg = await client.create_payment(
            customer_id="cus_123", value=3500.00, due_date="2026-06-20",
            description="Honorários advocatícios", external_reference="contrato-5",
        )
    finally:
        await client.aclose()
    assert pg["invoiceUrl"].endswith("abc123")
    assert pg["id"] == "pay_1"
    body = _json.loads(route.calls.last.request.read())
    assert body["billingType"] == "UNDEFINED"
    assert body["value"] == 3500.00
    assert body["externalReference"] == "contrato-5"


@pytest.mark.asyncio
async def test_find_payment_por_external_reference(respx_mock):
    respx_mock.get(f"{_BASE}/v3/payments").mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "pay_1", "status": "PENDING"}],
        }),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        pg = await client.find_payment_by_external_reference("contrato-5")
    finally:
        await client.aclose()
    assert pg["id"] == "pay_1"


@pytest.mark.asyncio
async def test_get_payment_status(respx_mock):
    respx_mock.get(f"{_BASE}/v3/payments/pay_1").mock(
        return_value=httpx.Response(200, json={"id": "pay_1", "status": "RECEIVED"}),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        pg = await client.get_payment("pay_1")
    finally:
        await client.aclose()
    assert pg["status"] == "RECEIVED"


@pytest.mark.asyncio
async def test_register_webhook(respx_mock):
    route = respx_mock.post(f"{_BASE}/v3/webhooks").mock(
        return_value=httpx.Response(200, json={"id": "wh_1", "enabled": True}),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        r = await client.register_webhook(
            url="https://funil.exemplo/webhooks/asaas",
            auth_token="segredo-longo-aleatorio-do-env",
            email="escritorio@exemplo.com",
        )
    finally:
        await client.aclose()
    assert r["id"] == "wh_1"
    req = route.calls.last.request
    assert req.headers["access_token"] == "$aact_hmlg_test"      # não é Bearer
    body = _json.loads(req.read())
    assert body["url"] == "https://funil.exemplo/webhooks/asaas"
    assert body["authToken"] == "segredo-longo-aleatorio-do-env"
    assert body["sendType"] == "SEQUENTIALLY"
    assert body["enabled"] is True
    assert body["events"] == ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"]


@pytest.mark.asyncio
async def test_delete_payment(respx_mock):
    respx_mock.delete(f"{_BASE}/v3/payments/pay_1").mock(
        return_value=httpx.Response(200, json={"deleted": True, "id": "pay_1"}),
    )
    client = AsaasClient("$aact_hmlg_test")
    try:
        r = await client.delete_payment("pay_1")
    finally:
        await client.aclose()
    assert r["deleted"] is True
