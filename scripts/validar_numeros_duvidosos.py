#!/usr/bin/env python3
"""Valida os processos "duvidosos" (NAO_ENCONTRADO + ERRO_CONSULTA do
diagnostico_sem_link.csv): pra cada um (1) valida o dígito verificador do
número CNJ — pega typo/número errado SEM scraping — e (2) RE-consulta o
DataJud (os ERRO falharam por rate-limit; re-checando, a maioria aparece).

Novas categorias:
  - NUMERO_INVALIDO       dígito CNJ não bate → número errado, CORRIGIR
  - ACHADO_NA_RECHECAGEM  apareceu no DataJud agora → era transitório, ok
  - VALIDO_MAS_AUSENTE    número ok mas não no DataJud → segredo/lacuna/novo
  - TRIBUNAL_FORA         tribunal não coberto pelo DataJud
  - ERRO_PERSISTENTE      DataJud seguiu falhando (re-rodar mais tarde)

Lê ./diagnostico_sem_link.csv (rode diagnosticar_sem_link.py antes).
Saída: resumo + validar_duvidosos.csv (com painel_url de cada).

Uso (no VPS):
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/validar_numeros_duvidosos.py
"""

from __future__ import annotations

import csv
import re
import sys
import time
from collections import Counter

import httpx

_IN = "diagnostico_sem_link.csv"
_OUT = "validar_duvidosos.csv"
_ALVO = {"NAO_ENCONTRADO", "ERRO_CONSULTA"}
_ACHADO = {"ok", "sem_movimentos"}
_CNJ_RE = re.compile(r"^(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})$")


def validar_cnj(numero: str) -> bool:
    """True se o dígito verificador do número CNJ confere (ISO 7064 mod 97)."""
    m = _CNJ_RE.match((numero or "").strip())
    if not m:
        return False
    seq, dv, ano, j, tr, orig = m.groups()
    base = seq + ano + j + tr + orig + "00"
    return (98 - int(base) % 97) == int(dv)


def main() -> int:
    from auditar_processos_datajud import consultar_datajud  # reusa DataJud

    try:
        with open(_IN, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("categoria") in _ALVO]
    except FileNotFoundError:
        print(f"ERRO: rode diagnosticar_sem_link.py antes ({_IN} não existe)",
              file=sys.stderr)
        return 2

    print(f"Duvidosos a validar: {len(rows)}")
    dj = httpx.Client(timeout=30)
    saida: list[dict] = []
    try:
        for i, r in enumerate(rows, 1):
            pn = r.get("processNumber") or ""
            valido = validar_cnj(pn)
            dj_status = "nao_checado"
            if valido:
                # re-checa DataJud com 1 retry (os ERRO eram rate-limit)
                for _ in range(2):
                    _, dj_status = consultar_datajud(dj, pn)
                    if not (dj_status.startswith("http_")
                            or dj_status.startswith("erro_")):
                        break
                    time.sleep(1.5)
                time.sleep(0.8)

            if not valido:
                cat = "NUMERO_INVALIDO"
            elif dj_status in _ACHADO:
                cat = "ACHADO_NA_RECHECAGEM"
            elif dj_status == "nao_encontrado":
                cat = "VALIDO_MAS_AUSENTE"
            elif dj_status == "tribunal_nao_mapeado":
                cat = "TRIBUNAL_FORA"
            else:
                cat = "ERRO_PERSISTENTE"

            saida.append({
                "novo_cat": cat,
                "numero_valido": "sim" if valido else "NAO",
                "processNumber": pn,
                "recheck": dj_status,
                "categoria_antiga": r.get("categoria") or "",
                "painel_url": r.get("painel_url") or "",
            })
            if i % 10 == 0:
                print(f"  {i}/{len(rows)}...")
    finally:
        dj.close()

    print()
    cats = Counter(o["novo_cat"] for o in saida)
    print("Por categoria nova:")
    for cat, n in cats.most_common():
        print(f"  {cat}: {n}")
    print()

    with open(_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "novo_cat", "numero_valido", "processNumber", "recheck",
            "categoria_antiga", "painel_url",
        ])
        for o in sorted(saida, key=lambda x: x["novo_cat"]):
            w.writerow([
                o["novo_cat"], o["numero_valido"], o["processNumber"],
                o["recheck"], o["categoria_antiga"], o["painel_url"],
            ])

    print(f"CSV gravado: {_OUT}")
    print("NUMERO_INVALIDO → corrigir o número no Juridiq.")
    print("ACHADO_NA_RECHECAGEM → era só erro transitório; vira DEVERIA_FUNCIONAR.")
    print("VALIDO_MAS_AUSENTE → número ok mas fora do DataJud (segredo/lacuna).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
