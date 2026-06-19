#!/usr/bin/env python3
"""Audita a carteira do Juridiq: lista os processos SEM link do tribunal
(campo ``processUrl`` vazio) e/ou SEM monitoramento — candidatos a ligar o
acompanhamento no painel.

IMPORTANTE: ``processUrl`` NÃO vem na listagem (``GET /lawSuit/``), só no
DETALHE (``GET /lawSuit/{id}``). Por isso este script lista todos e busca o
detalhe de CADA processo (284 ≈ 1-2 min com throttle). Lê ``processUrl`` +
``monitoringId`` + ``monitoringStatus``.

A API do Juridiq NÃO tem endpoint pra FORÇAR/LIGAR monitoramento (é ação do
PAINEL). Este script é READ-ONLY: grava um CSV com o ``painel_url`` de cada
processo sem link, pra você (ou uma automação de navegador) ligar o
monitoramento.

Uso (no VPS, com o .env do funil):
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/auditar_links_juridiq.py
"""

from __future__ import annotations

import csv
import sys
import time
from collections import Counter

import httpx

from noviello_funil.config import Settings

_PAINEL_BASE = "https://dashboard.juridiq.com.br/law-suits"
_OUT_CSV = "processos_sem_link.csv"
_OUT_CSV_SUB = "sem_link_sem_monitoramento.csv"  # subset: sem link E sem monitoringId
_THROTTLE_S = 0.2


def _listar_processos(client: httpx.Client) -> list[dict]:
    """GET /lawSuit/ paginado (traz id + monitoringStatus, mas NÃO processUrl)."""
    procs: list[dict] = []
    page = 1
    while True:
        r = client.get("/lawSuit/", params={"page": page, "limit": 100})
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
        total = len(procs)
        print(f"Total de processos no Juridiq: {total}")
        print("Buscando o detalhe de cada (pra ler o processUrl real)...")

        sem_link: list[dict] = []
        com_link = 0
        falhas = 0
        for i, p in enumerate(procs, 1):
            pid = p.get("id") or ""
            try:
                r = client.get(f"/lawSuit/{pid}", params={"fullQuery": "true"})
                r.raise_for_status()
                d = r.json()
            except Exception as exc:  # noqa: BLE001 — não derruba a auditoria
                falhas += 1
                print(f"  ! falha no detalhe {pid}: {exc}", file=sys.stderr)
                continue
            url = (d.get("processUrl") or "").strip()
            if url:
                com_link += 1
            else:
                sem_link.append({
                    "processNumber": d.get("processNumber") or "",
                    "status": d.get("status") or "",
                    "monitoringStatus": d.get("monitoringStatus")
                    or p.get("monitoringStatus") or "—",
                    "monitoringId": d.get("monitoringId"),
                    "court": d.get("court") or "",
                    "createdByAutomation": d.get("createdByAutomation"),
                    "id": pid,
                })
            if i % 25 == 0:
                print(f"  {i}/{total}...")
            time.sleep(_THROTTLE_S)

    print()
    print(f"COM link de tribunal (processUrl): {com_link}")
    print(f"SEM link de tribunal (processUrl vazio): {len(sem_link)}")
    if falhas:
        print(f"Falhas ao buscar detalhe: {falhas}")
    print()

    if sem_link:
        por_status = Counter(p["monitoringStatus"] for p in sem_link)
        print("Sem-link por monitoringStatus:")
        for st, n in por_status.most_common():
            print(f"  {st}: {n}")
        sem_mon_id = sum(1 for p in sem_link if not p["monitoringId"])
        print(f"Sem-link E sem monitoringId (nenhum job): {sem_mon_id}")
        print()

    with open(_OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "processNumber", "status", "monitoringStatus", "tem_monitoringId",
            "court", "createdByAutomation", "id", "painel_url",
        ])
        for p in sem_link:
            pid = p["id"]
            w.writerow([
                p["processNumber"], p["status"], p["monitoringStatus"],
                "sim" if p["monitoringId"] else "nao",
                p["court"], p["createdByAutomation"], pid,
                f"{_PAINEL_BASE}/{pid}" if pid else "",
            ])

    print(f"CSV gravado: {_OUT_CSV} ({len(sem_link)} processos sem link)")

    # Subset ACIONÁVEL: sem link E sem monitoringId (monitoramento NUNCA
    # configurado) → é a lista pra LIGAR o monitoramento no painel. Diferente
    # de "tem monitoringId mas link não gerou" (sync/credencial travada).
    sub = [p for p in sem_link if not p["monitoringId"]]
    with open(_OUT_CSV_SUB, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "processNumber", "status", "monitoringStatus", "court",
            "id", "painel_url",
        ])
        for p in sub:
            pid = p["id"]
            w.writerow([
                p["processNumber"], p["status"], p["monitoringStatus"],
                p["court"], pid, f"{_PAINEL_BASE}/{pid}" if pid else "",
            ])
    print(f"CSV gravado: {_OUT_CSV_SUB} ({len(sub)} SEM link E SEM monitoramento)")
    print("A API NÃO liga monitoramento — use o painel_url do CSV pra ligar no")
    print("painel, ou peça a automação de navegador (Chrome).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
