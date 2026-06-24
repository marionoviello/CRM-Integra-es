"""Tests for the typed settings loader."""

import pytest
from pydantic import ValidationError

from noviello_funil.config import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("JURICHAT_API_KEY", "jk-test")
    monkeypatch.setenv("JURICHAT_WEBHOOK_SECRET", "whsec-test")
    monkeypatch.setenv("NOTIFICACAO_TELEFONE", "5511999999999")
    monkeypatch.setenv("MARIO_CONVERSATION_ID", "C-MARIO")
    monkeypatch.setenv("JURICHAT_INBOX_ID", "inbox-test")

    s = Settings()

    assert s.anthropic_api_key == "sk-test"
    assert s.jurichat_api_key == "jk-test"
    assert s.jurichat_webhook_secret == "whsec-test"
    assert s.notificacao_telefone == "5511999999999"
    assert s.jurichat_base_url == "https://api.jurichat.com"
    assert s.max_turnos_por_lead == 20
    assert s.followup_1_apos_horas == 48
    assert s.followup_2_apos_horas == 72
    assert s.encerramento_apos_horas == 24
    assert s.anthropic_model.startswith("claude-")


def test_modelos_triagem_e_followup_separados():
    """A triagem (decisão + resposta ao lead) roda no modelo mais capaz; o
    follow-up automático roda num modelo mais leve. Os dois NÃO podem apontar
    pro mesmo campo — senão o split de precisão/custo é só decorativo.

    ``_env_file=None`` isola o teste do ``.env`` real (testa os defaults do
    código, não a config de produção)."""
    s = Settings(
        _env_file=None,
        anthropic_api_key="sk-test",
        jurichat_api_key="jk-test",
        jurichat_webhook_secret="whsec-test",
        notificacao_telefone="5511999999999",
        mario_conversation_id="C-MARIO",
        jurichat_inbox_id="inbox-test",
    )
    assert s.anthropic_model == "claude-opus-4-8"
    assert s.anthropic_model_followup == "claude-sonnet-4-6"
    assert s.anthropic_model != s.anthropic_model_followup


def test_settings_missing_required_fails(monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "JURICHAT_API_KEY",
        "JURICHAT_WEBHOOK_SECRET",
        "NOTIFICACAO_TELEFONE",
        "MARIO_CONVERSATION_ID",
        "JURICHAT_INBOX_ID",
    ]:
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ValidationError):
        Settings()
