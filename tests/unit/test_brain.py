"""Tests for the brain module — Claude prompting + parsing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.brain import (
    DecisaoInvalida,
    load_skill,
    parse_decisao,
    triagem,
)


def _text_block(text: str):
    """Mock de um content block de texto (type='text' + .text), como a API
    real devolve — o brain agora filtra por block.type == 'text'."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def test_load_skill_returns_nonempty_string():
    # atendente_geral é a skill VIVA (mono-skill dos 3 verticais). A antiga
    # saude_suplementar.md foi removida na auditoria 24/jun (G5, código morto).
    content = load_skill("atendente_geral")
    assert "Noviello" in content
    assert len(content) > 200


def test_parse_decisao_responder():
    raw = '{"acao": "responder", "mensagem": "olá maria"}'
    d = parse_decisao(raw)
    assert d.acao == "responder"
    assert d.mensagem == "olá maria"
    assert d.resumo_caso is None
    assert d.motivo_handoff is None


def test_parse_decisao_propor():
    raw = '{"acao": "propor", "mensagem": "proposta x", "resumo_caso": "plano negou"}'
    d = parse_decisao(raw)
    assert d.acao == "propor"
    assert d.resumo_caso == "plano negou"


def test_parse_decisao_handoff():
    raw = '{"acao": "handoff", "mensagem": "vou te passar", "motivo_handoff": "pediu humano"}'
    d = parse_decisao(raw)
    assert d.acao == "handoff"
    assert d.motivo_handoff == "pediu humano"


def test_parse_decisao_unknown_acao_raises():
    raw = '{"acao": "explodir", "mensagem": "..."}'
    with pytest.raises(DecisaoInvalida):
        parse_decisao(raw)


def test_parse_decisao_invalid_json_raises():
    with pytest.raises(DecisaoInvalida):
        parse_decisao("not json at all")


def test_parse_decisao_extracts_json_from_markdown_block():
    # Sometimes Claude wraps in ``` despite instructions
    raw = '```json\n{"acao": "responder", "mensagem": "oi"}\n```'
    d = parse_decisao(raw)
    assert d.acao == "responder"


def test_parse_decisao_extracts_json_from_prose_wrapped_fence():
    """Claude sometimes adds prose around the fence: 'Sure: ```{...}``` ok?'"""
    raw = (
        'Claro, aqui está a decisão:\n'
        '```json\n{"acao": "responder", "mensagem": "oi maria"}\n```\n'
        'Espero ter ajudado.'
    )
    d = parse_decisao(raw)
    assert d.acao == "responder"
    assert d.mensagem == "oi maria"


def test_parse_decisao_extracts_bare_json_from_prose():
    """No fence at all — just JSON embedded in conversational prose."""
    raw = (
        'Tudo bem! A decisão é {"acao": "handoff", "mensagem": "ok", '
        '"motivo_handoff": "fora escopo"} — pode prosseguir.'
    )
    d = parse_decisao(raw)
    assert d.acao == "handoff"
    assert d.motivo_handoff == "fora escopo"


@pytest.mark.asyncio
async def test_triagem_returns_decision_on_first_call():
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.content = [_text_block('{"acao":"responder","mensagem":"ok"}')]
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    decisao = await triagem(
        client=fake_client,
        model="claude-opus-4-8",
        skill_content="SKILL",
        conversation_transcript="Lead: oi",
    )

    assert decisao.acao == "responder"
    assert decisao.mensagem == "ok"
    fake_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_triagem_usa_structured_outputs_sem_retry():
    """A triagem manda output_config.format com o schema do Decisao e faz UMA
    chamada só (o antigo retry de JSON inválido foi removido — a API garante
    o JSON)."""
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.content = [_text_block('{"acao":"responder","mensagem":"ok"}')]
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    await triagem(
        client=fake_client,
        model="claude-opus-4-8",
        skill_content="SKILL",
        conversation_transcript="Lead: oi",
    )

    fake_client.messages.create.assert_called_once()
    fmt = fake_client.messages.create.call_args.kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert "acao" in fmt["schema"]["properties"]
    assert "responder" in fmt["schema"]["properties"]["acao"]["enum"]


@pytest.mark.asyncio
async def test_triagem_sem_bloco_de_texto_raises():
    """Refusal / truncamento por max_tokens → resposta sem bloco de texto →
    DecisaoInvalida (caminho que avisa o Mario), nunca IndexError mudo."""
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.content = []          # sem bloco de texto
    fake_response.stop_reason = "refusal"
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    with pytest.raises(DecisaoInvalida):
        await triagem(
            client=fake_client,
            model="claude-opus-4-8",
            skill_content="SKILL",
            conversation_transcript="Lead: oi",
        )


@pytest.mark.asyncio
async def test_followup_message_returns_text():
    from noviello_funil.brain import gerar_followup_msg

    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.content = [_text_block("Oi Maria, retomando nosso papo...")]
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    text = await gerar_followup_msg(
        client=fake_client,
        model="claude-sonnet-4-5",
        skill_content="SKILL",
        conversation_transcript="Lead: oi (há 2 dias)",
    )

    assert "Maria" in text
