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
