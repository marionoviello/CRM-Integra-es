"""Test that the wired-up app exposes a health check and registers webhooks."""

from fastapi.testclient import TestClient


def test_app_starts_and_serves_health(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("JURICHAT_API_KEY", "jk-test")
    monkeypatch.setenv("JURICHAT_WEBHOOK_SECRET", "wh-test")
    monkeypatch.setenv("NOTIFICACAO_TELEFONE", "5511999999999")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("MARIO_CONVERSATION_ID", "C-MARIO")

    from noviello_funil.main import create_app

    app = create_app()
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
