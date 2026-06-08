#!/usr/bin/env bash
# Smoke test: send a fake webhook and verify the server processes it.
#
# Pre-req: the service must be running locally (uvicorn or systemd).
# Run:   bash scripts/smoke.sh

set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== smoke 1: lead inicial ==="
uv run python scripts/smoke_send_webhook.py \
    --conversation-id "C-SMOKE-$(date +%s)" \
    --lead-id "L-SMOKE-$(date +%s)" \
    --text "Olá, plano negou minha cirurgia"

echo
echo "Now check:"
echo "  - The server log (journalctl -u noviello-funil -f) for processing"
echo "  - The local sqlite DB:"
echo "      sqlite3 data/noviello.db 'SELECT id, estado, turnos FROM leads;'"
