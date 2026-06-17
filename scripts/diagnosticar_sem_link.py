#!/usr/bin/env python3
"""Diagnostica os processos do Juridiq SEM link (``processUrl`` vazio): cruza
cada um com o DataJud (CNJ) pra separar os que DEVERIAM funcionar (acháveis no
tribunal → o link não vir é problema do Juridiq, vale re-ativar/esperar) dos
IMPOSSÍVEIS (segredo, número errado, ou tribunal fora do DataJud).

Categorias:
  - SEGREDO            segredo de justiça — não dá pra monitorar externamente
  - ARQUIVADO          arquivado — não precisa
  - DEVERIA_FUNCIONAR  achado no DataJud → o monitoramento DEVERIA pegar o link
  - NAO_ENCONTRADO     não achado no DataJud (número errado? segredo? fora?)
  - TRIBUNAL_FORA      tribunal não mapeado no DataJud (não dá pra confirmar)
  - ERRO_CONSULTA      erro http/timeout na consulta DataJud

Saída: resumo no stdout + ``diagnostico_sem_link.csv`` (com painel_url de cada).
Leva ~4-5 min (busca o detalhe de cada processo + consulta DataJud dos sem-link).

Uso (no VPS):
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/diagnosticar_sem_link.py
"""

from __future__ import annotations

import csv
import sys
import time
from collections import Counter

import httpx

from noviello_funil.config import Settings

_PAINEL = "https://dashboard.juridiq.com.br/law-suits"
_OUT = "diagnostico_sem_link.csv"
_TH_JQ = 0.2   # throttle do detalhe Juridiq
_TH_DJ = 0.6   # throttle DataJud (rate limit CNJ ~120/min)
_DJ_ACHADO = {"ok", "sem_movimentos"}


def _categoria(det: dict, dj_consultar) -> tuple[str, str]:
    """(categoria, dj_status) pra UM processo sem link."""
    mon = det.get("monitoringStatus") or "—"
    if det.get("isSecret") or mon == "SEGREDO":
        return "SEGREDO", ""
    if mon == "ARQUIVADO":
        return "ARQUIVADO", ""
    _, dj_status = dj_consultar(det.get("processNumber") or "")
    if dj_status in _DJ_ACHADO:
        return "DEVERIA_FUNCIONAR", dj_status
    if dj_status == "nao_encontrado":
        return "NAO_ENCONTRADO", dj_status
    if dj_status == "tribunal_nao_mapeado":
        return "TRIBUNAL_FORA", dj_status
    return "ERRO_CONSULTA", dj_status


def main() -> int:
    from auditar_processos_datajud import consultar_datajud  # reusa DataJud

    settings = Settings()
    if not settings.juridiq_api_key:
        print("ERRO: JURIDIQ_API_KEY ausente no .env", file=sys.stderr)
        return 2

    jq = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30,
    )
    dj = httpx.Client(timeout=30)
    linhas: list[dict] = []
    try:
        suits: list[dict] = []
        page = 1
        while True:
            d = jq.get("/lawSuit/", params={"page": page, "limit": 100}).json()
            suits.extend(d.get("data", []))
            if page >= int(d.get("totalPages") or 1):
                break
            page += 1
        total = len(suits)
        print(f"Total de processos: {total} — detalhe + DataJud dos sem-link...")

        def _dj(pn: str):
            r = consultar_datajud(dj, pn)
            time.sleep(_TH_DJ)
            return r

        for i, s in enumerate(suits, 1):
            pid = s.get("id") or ""
            try:
                det = jq.get(
                    f"/lawSuit/{pid}", params={"fullQuery": "true"},
                ).json()
            except Exception:  # noqa: BLE001 — não derruba o diagnóstico
                time.sleep(_TH_JQ)
                continue
            if (det.get("processUrl") or "").strip():
                time.sleep(_TH_JQ)
                continue  # tem link — fora do diagnóstico
            cat, dj_status = _categoria(det, _dj)
            linhas.append({
                "processNumber": det.get("processNumber") or "",
                "categoria": cat,
                "monitoringStatus": det.get("monitoringStatus") or "—",
                "dj_status": dj_status,
                "id": pid,
            })
            if i % 25 == 0:
                print(f"  {i}/{total}...")
            time.sleep(_TH_JQ)
    finally:
        jq.close()
        dj.close()

    print()
    cats = Counter(it["categoria"] for it in linhas)
    print(f"SEM link: {len(linhas)} — por categoria:")
    for cat, n in cats.most_common():
        print(f"  {cat}: {n}")
    print()

    with open(_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "categoria", "processNumber", "monitoringStatus",
            "dj_status", "painel_url",
        ])
        for it in sorted(linhas, key=lambda x: x["categoria"]):
            pid = it["id"]
            w.writerow([
                it["categoria"], it["processNumber"], it["monitoringStatus"],
                it["dj_status"], f"{_PAINEL}/{pid}" if pid else "",
            ])

    print(f"CSV gravado: {_OUT}")
    print("DEVERIA_FUNCIONAR → achável no tribunal: re-ativar/esperar o Juridiq.")
    print("NAO_ENCONTRADO/TRIBUNAL_FORA/SEGREDO → verificar número ou impossível.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
