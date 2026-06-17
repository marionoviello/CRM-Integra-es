#!/usr/bin/env python3
"""Audita a carteira do Juridiq: lista os processos SEM link do tribunal
(campo ``processUrl`` vazio) — candidatos a ter o monitoramento ligado.

A API do Juridiq NÃO tem endpoint pra FORÇAR/LIGAR monitoramento (isso é ação
do PAINEL). Este script é READ-ONLY: baixa todos os lawSuits, marca os que
estão sem ``processUrl`` e grava um CSV com o link do painel de cada um — daí
você (ou uma automação de navegador) liga o monitoramento processo a processo.

Uso (no VPS, com o .env do funil):
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/auditar_links_juridiq.py
"""

from __future__ import annotations

import csv
import sys
from collections import Counter

import httpx

from noviello_funil.config import Settings

_PAINEL_BASE = "https://dashboard.juridiq.com.br/law-suits"
_OUT_CSV = "processos_sem_link.csv"


def _listar_processos(client: httpx.Client) -> list[dict]:
    """GET /lawSuit/ paginado, fullQuery pra trazer o campo processUrl."""
    procs: list[dict] = []
    page = 1
    while True:
        r = client.get(
            "/lawSuit/",
            params={"page": page, "limit": 100, "fullQuery": "true"},
        )
        r.raise_for_status()
        data = r.json()
        procs.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return procs


def main() -> int:
    settings = Settings()
    if not settings.juridiq_api_key:
        print("ERRO: JURIDIQ_API_KEY ausente no .env", file=sys.stderr)
        return 2

    with httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30,
    ) as client:
        procs = _listar_processos(client)

    sem_link = [p for p in procs if not (p.get("processUrl") or "").strip()]
    com_link = len(procs) - len(sem_link)

    print(f"Total de processos no Juridiq: {len(procs)}")
    print(f"COM link de tribunal (processUrl): {com_link}")
    print(f"SEM link de tribunal (processUrl vazio): {len(sem_link)}")
    print()

    if sem_link:
        por_status = Counter(p.get("monitoringStatus") or "—" for p in sem_link)
        print("Sem-link por monitoringStatus:")
        for st, n in por_status.most_common():
            print(f"  {st}: {n}")
        print()

    with open(_OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "processNumber", "status", "monitoringStatus", "court",
            "createdByAutomation", "id", "painel_url",
        ])
        for p in sem_link:
            pid = p.get("id") or ""
            w.writerow([
                p.get("processNumber") or "",
                p.get("status") or "",
                p.get("monitoringStatus") or "",
                p.get("court") or "",
                p.get("createdByAutomation"),
                pid,
                f"{_PAINEL_BASE}/{pid}" if pid else "",
            ])

    print(f"CSV gravado: {_OUT_CSV} ({len(sem_link)} processos sem link)")
    print("A API NÃO liga monitoramento — use o painel_url do CSV pra ligar no")
    print("painel, ou peça a automação de navegador (Chrome).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
