#!/usr/bin/env python3
"""Setup one-time pra obter o refresh_token do Google Calendar.

Uso:

    1. Crie um OAuth Client ID (Desktop app) em
       https://console.cloud.google.com/apis/credentials
       — copie ``Client ID`` e ``Client Secret``.

    2. Rode:

        export GOOGLE_OAUTH_CLIENT_ID="...apps.googleusercontent.com"
        export GOOGLE_OAUTH_CLIENT_SECRET="GOCSPX-..."
        uv run python scripts/google_oauth_setup.py

    3. O script abre URL no browser, você autoriza com a conta
       ``mario@noviello.adv.br``, copia o código que aparece, cola no
       terminal. Sai o ``refresh_token`` — adicione no ``.env``:

        GOOGLE_OAUTH_REFRESH_TOKEN=1//0g...

Sem dependências extras: usa só stdlib + httpx (já no projeto). Roda
com flow "loopback localhost" (recomendado pelo Google pra Desktop apps
desde 2022; out-of-band/manual copy-paste foi descontinuado).
"""

from __future__ import annotations

import http.server
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    """Captura ``?code=...`` no redirect e sinaliza o thread principal."""

    received: dict[str, Any] = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _CodeHandler.received["code"] = params["code"][0]
            _CodeHandler.received["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<h1>Autorização recebida</h1>"
                "<p>Pode fechar esta aba e voltar pro terminal.</p>"
                .encode("utf-8")
            )
        elif "error" in params:
            _CodeHandler.received["error"] = params["error"][0]
            self.send_response(400)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):
        pass  # silencia logs HTTP no terminal


def main() -> int:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "ERRO: defina GOOGLE_OAUTH_CLIENT_ID e GOOGLE_OAUTH_CLIENT_SECRET "
            "como variáveis de ambiente antes de rodar.",
            file=sys.stderr,
        )
        return 2

    port = _pick_free_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(16)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # ESSENCIAL pra receber refresh_token
        "prompt": "consent",         # força tela de consent pra garantir refresh
        "state": state,
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    server = http.server.HTTPServer(("127.0.0.1", port), _CodeHandler)
    server_thread = threading.Thread(
        target=server.handle_request, daemon=True,
    )
    server_thread.start()

    print(f"Abrindo browser pra autorização ({redirect_uri})...")
    print("Se não abrir sozinho, cole esta URL no navegador:")
    print(auth_url)
    print()
    webbrowser.open(auth_url)

    server_thread.join(timeout=300)  # 5 min pra autorizar
    if not _CodeHandler.received.get("code"):
        err = _CodeHandler.received.get("error", "timeout ou nada recebido")
        print(f"ERRO: autorização falhou — {err}", file=sys.stderr)
        return 1
    if _CodeHandler.received.get("state") != state:
        print("ERRO: state mismatch — possível CSRF", file=sys.stderr)
        return 1

    code = _CodeHandler.received["code"]

    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15.0,
    )
    if resp.status_code >= 400:
        print(f"ERRO {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    data = resp.json()
    refresh = data.get("refresh_token")
    if not refresh:
        print(
            "ERRO: Google não retornou refresh_token. Tente:\n"
            "  1. Revogar acesso em https://myaccount.google.com/permissions\n"
            "  2. Rodar de novo (o prompt=consent vai pedir tudo de novo)\n",
            file=sys.stderr,
        )
        print(f"resposta: {data}", file=sys.stderr)
        return 1

    print()
    print("=" * 60)
    print("REFRESH TOKEN OBTIDO COM SUCESSO")
    print("=" * 60)
    print()
    print("Adicione esta linha no .env do servidor:")
    print()
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh}")
    print()
    print("(o access_token de 1h tá no campo data['access_token'], mas o")
    print("noviello-funil renova sozinho quando precisar — só guarde o refresh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
