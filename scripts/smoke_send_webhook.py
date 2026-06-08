"""Send a fake Jurichat webhook to the local server.

Usage:
  uv run python scripts/smoke_send_webhook.py [--text "msg do lead"]

Reads JURICHAT_WEBHOOK_SECRET from .env, signs the payload with HMAC-SHA256,
posts to http://127.0.0.1:8000/webhooks/jurichat.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="Olá, vi que vocês trabalham com plano de saúde?")
    parser.add_argument("--conversation-id", default="C-SMOKE-1")
    parser.add_argument("--lead-id", default="L-SMOKE-1")
    parser.add_argument("--from-me", action="store_true", help="Simulate Mario sending")
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/jurichat")
    args = parser.parse_args()

    secret = os.environ.get("JURICHAT_WEBHOOK_SECRET")
    if not secret:
        # Try loading from .env
        env_path = ".env"
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("JURICHAT_WEBHOOK_SECRET="):
                        secret = line.split("=", 1)[1].strip()
                        break
    if not secret:
        print("ERROR: JURICHAT_WEBHOOK_SECRET not set", file=sys.stderr)
        return 1

    payload = {
        "event": "chat.conversation.updated",
        "id": f"evt-smoke-{os.urandom(4).hex()}",
        "conversation_id": args.conversation_id,
        "lead_id": args.lead_id,
        "contact": {"phone": "5511988887777", "name": "Lead Smoke"},
        "message": {"text": args.text, "from_me": args.from_me},
    }

    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    resp = httpx.post(
        args.url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-JuriChat-Signature": sig,
        },
        timeout=10.0,
    )
    print(f"status={resp.status_code} body={resp.text}")
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
