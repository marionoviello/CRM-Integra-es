"""Tests for the ZapSign client (roadmap 3.x)."""

import json as _json

import httpx
import pytest

from noviello_funil.zapsign_client import ZapSignClient

_BASE = "https://api.zapsign.com.br/api/v1"


@pytest.mark.asyncio
async def test_create_doc_envia_bearer_e_corpo(respx_mock):
    route = respx_mock.post(f"{_BASE}/models/create-doc/").mock(
        return_value=httpx.Response(200, json={
            "token": "doc-1", "status": "pending",
            "signers": [{"token": "s1",
                         "sign_url": "https://app.zapsign.com.br/verificar/s1"}],
        }),
    )
    client = ZapSignClient("zs-test")
    try:
        resp = await client.create_doc_from_template(
            {"template_id": "T", "signer_name": "Fulano Teste"},
        )
    finally:
        await client.aclose()

    assert resp["token"] == "doc-1"
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer zs-test"
    body = _json.loads(req.read())
    assert body["template_id"] == "T"


@pytest.mark.asyncio
async def test_create_doc_nao_retenta_em_timeout(respx_mock):
    """POST não-idempotente: um timeout NÃO pode virar 2 documentos. O
    create-doc faz UMA tentativa só (sem with_retry)."""
    route = respx_mock.post(f"{_BASE}/models/create-doc/").mock(
        side_effect=httpx.ReadTimeout("timeout"),
    )
    client = ZapSignClient("zs-test")
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.create_doc_from_template({"template_id": "T"})
    finally:
        await client.aclose()

    assert route.call_count == 1        # uma tentativa, não 3


@pytest.mark.asyncio
async def test_create_doc_from_pdf(respx_mock):
    route = respx_mock.post(f"{_BASE}/docs/").mock(
        return_value=httpx.Response(200, json={
            "token": "doc-7", "status": "pending",
            "signers": [{"token": "s1",
                         "sign_url": "https://app.zapsign.com.br/verificar/s1"}],
        }),
    )
    client = ZapSignClient("zs-test")
    try:
        resp = await client.create_doc_from_pdf(
            {"name": "Contrato", "base64_pdf": "JVBERi0x", "signers": []},
        )
    finally:
        await client.aclose()

    assert resp["token"] == "doc-7"
    assert route.calls.last.request.headers["authorization"] == "Bearer zs-test"


@pytest.mark.asyncio
async def test_create_doc_from_pdf_nao_retenta_em_timeout(respx_mock):
    """POST /docs/ também é não-idempotente — um timeout não vira 2 documentos."""
    route = respx_mock.post(f"{_BASE}/docs/").mock(
        side_effect=httpx.ReadTimeout("timeout"),
    )
    client = ZapSignClient("zs-test")
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.create_doc_from_pdf({"name": "Contrato"})
    finally:
        await client.aclose()

    assert route.call_count == 1


@pytest.mark.asyncio
async def test_get_doc_status_autoritativo(respx_mock):
    respx_mock.get(f"{_BASE}/docs/doc-1/").mock(
        return_value=httpx.Response(200, json={
            "token": "doc-1", "status": "signed",
            "signed_file": "https://zapsign.s3.amazonaws.com/pdf/abc.pdf",
        }),
    )
    client = ZapSignClient("zs-test")
    try:
        doc = await client.get_doc("doc-1")
    finally:
        await client.aclose()

    assert doc["status"] == "signed"
    assert doc["signed_file"].endswith(".pdf")


@pytest.mark.asyncio
async def test_download_signed_file(respx_mock):
    url = "https://zapsign.s3.amazonaws.com/pdf/abc.pdf"
    respx_mock.get(url).mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7 fake"),
    )
    client = ZapSignClient("zs-test")
    try:
        data = await client.download_signed_file(url)
    finally:
        await client.aclose()

    assert data.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_register_webhook_inclui_header_secreto(respx_mock):
    route = respx_mock.post(f"{_BASE}/user/company/webhook/").mock(
        return_value=httpx.Response(200, json={"id": 1}),
    )
    client = ZapSignClient("zs-test")
    try:
        await client.register_webhook(
            url="https://funil.noviello.adv.br/webhooks/zapsign",
            event_type="doc_signed",
            secret_value="s3cr3t-bem-longo",
        )
    finally:
        await client.aclose()

    body = _json.loads(route.calls.last.request.read())
    assert body["url"].endswith("/webhooks/zapsign")
    assert body["type"] == "doc_signed"
    # header secreto (substitui a falta de HMAC da ZapSign)
    assert body["headers"][0]["value"] == "s3cr3t-bem-longo"


# --- Caminho A: ler modelos + verificação humana -----------------------------

@pytest.mark.asyncio
async def test_list_templates(respx_mock):
    respx_mock.get(f"{_BASE}/templates/").mock(
        return_value=httpx.Response(200, json={"results": [
            {"token": "tpl-1", "name": "Contrato plano de saúde"},
            {"token": "tpl-2", "name": "Modelo Variável"},
        ]}),
    )
    client = ZapSignClient("zs-test")
    try:
        tpls = await client.list_templates()
    finally:
        await client.aclose()
    assert [t["name"] for t in tpls] == [
        "Contrato plano de saúde", "Modelo Variável",
    ]


@pytest.mark.asyncio
async def test_get_template_inputs(respx_mock):
    respx_mock.get(f"{_BASE}/templates/tpl-2/").mock(
        return_value=httpx.Response(200, json={
            "token": "tpl-2", "name": "Modelo Variável",
            "inputs": [
                {"variable": "{{NOME}}", "label": "Nome", "required": True},
                {"variable": "{{HONORARIOS}}", "label": "Honorários",
                 "required": True},
            ],
        }),
    )
    client = ZapSignClient("zs-test")
    try:
        tpl = await client.get_template("tpl-2")
    finally:
        await client.aclose()
    variaveis = [i["variable"] for i in tpl["inputs"]]
    assert "{{NOME}}" in variaveis and "{{HONORARIOS}}" in variaveis


@pytest.mark.asyncio
async def test_resend_notifications_bulk(respx_mock):
    route = respx_mock.post(
        f"{_BASE}/docs/doc-9/resend-notifications-bulk/"
    ).mock(return_value=httpx.Response(200, json={
        "success": True, "total_signers": 4, "sent_count": 1,
    }))
    client = ZapSignClient("zs-test")
    try:
        r = await client.resend_notifications_bulk("doc-9")
    finally:
        await client.aclose()
    assert r["sent_count"] == 1                     # só o cliente (order 1)
    assert route.calls.last.request.headers["authorization"] == "Bearer zs-test"


@pytest.mark.asyncio
async def test_refuse(respx_mock):
    route = respx_mock.post(f"{_BASE}/refuse/").mock(
        return_value=httpx.Response(200, json={"status": "refused"}),
    )
    client = ZapSignClient("zs-test")
    try:
        r = await client.refuse("doc-9", "valor de honorários errado")
    finally:
        await client.aclose()
    assert r["status"] == "refused"
    body = _json.loads(route.calls.last.request.read())
    assert body["doc_token"] == "doc-9"
    assert "honorários" in body["rejected_reason"]
