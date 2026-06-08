# Noviello Funil Saúde

Atendente IA para leads de plano de saúde no WhatsApp via Jurichat e Claude.

**Spec:** [`docs/superpowers/specs/2026-06-03-noviello-funil-saude-design.md`](docs/superpowers/specs/2026-06-03-noviello-funil-saude-design.md)
**Plano:** [`docs/superpowers/plans/2026-06-03-noviello-funil-saude.md`](docs/superpowers/plans/2026-06-03-noviello-funil-saude.md)

## Stack

Python 3.11 · FastAPI · SQLite · `httpx` · `anthropic` SDK · `pydantic-settings` · `pytest` · `uv`

## Dev — Windows / macOS / Linux

```bash
# 1. Install uv if needed: https://docs.astral.sh/uv/getting-started/installation/
# 2. Sync deps:
uv sync --all-extras

# 3. Copy env template and fill in secrets:
cp .env.example .env
# Edit .env with real values from C:\Users\mario\.secrets\noviello-automacao.env

# 4. Run tests:
uv run pytest -v

# 5. Run the server locally:
uv run uvicorn noviello_funil.main:app --reload --port 8000

# 6. Smoke test (in another shell):
uv run python scripts/smoke_send_webhook.py --text "oi, vocês trabalham com plano de saúde?"
```

## Deploy — VPS Hostinger Ubuntu 22.04

Prereqs on the VPS:
- Python 3.11 (or install via deadsnakes)
- `uv` installed system-wide (or for the `noviello` user)
- nginx, certbot
- A subdomain pointing to the VPS IP (e.g., `funil.noviello.adv.br`)

```bash
# As root (one-time setup)
useradd --system --create-home --shell /bin/bash noviello
mkdir -p /opt/noviello-funil-saude
chown noviello:noviello /opt/noviello-funil-saude

# As noviello user
sudo -iu noviello
cd /opt/noviello-funil-saude
git clone <your-repo-url> .
uv sync --no-dev
cp .env.example .env
# Edit .env with production values
mkdir -p data

# Back as root: install systemd units
cp deploy/noviello-funil.service /etc/systemd/system/
cp deploy/noviello-followup.service /etc/systemd/system/
cp deploy/noviello-followup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now noviello-funil.service
systemctl enable --now noviello-followup.timer

# Install nginx config
cp deploy/nginx.conf /etc/nginx/sites-available/noviello-funil
ln -s /etc/nginx/sites-available/noviello-funil /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Get TLS cert
certbot --nginx -d funil.noviello.adv.br

# Verify
curl https://funil.noviello.adv.br/health   # should return {"ok":true}
systemctl status noviello-funil.service
systemctl list-timers noviello-followup.timer
```

## Operations

```bash
# Tail the live log
journalctl -u noviello-funil.service -f

# Inspect the DB
sqlite3 /opt/noviello-funil-saude/data/noviello.db
sqlite> .mode column
sqlite> .headers on
sqlite> SELECT id, contato_nome, estado, turnos, erro_atual FROM leads;
sqlite> SELECT * FROM transicoes ORDER BY criado_em DESC LIMIT 20;

# Force a follow-up cycle right now (instead of waiting for the timer)
systemctl start noviello-followup.service

# Reload after code change
git pull
uv sync --no-dev
systemctl restart noviello-funil.service
```

## Pre-production checklist (per spec §15)

- [ ] Validate that Jurichat fires webhook per message (not only per CRM stage change)
- [ ] Map exact webhook payload shape and confirm `message.from_me` semantics
- [ ] Confirm Jurichat endpoint for listing lead tags
- [ ] Rotate Jurichat API key (the previous one was exposed in chat — see spec §15.5)
- [ ] Pick the actual subdomain for the webhook (currently `funil.noviello.adv.br` is a placeholder)
- [ ] Define `MARIO_CONVERSATION_ID` — the Jurichat conversation that receives notifications
- [ ] Establish daily backup of `data/noviello.db` (rsync to another disk or off-VPS)
