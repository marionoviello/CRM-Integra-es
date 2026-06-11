#!/usr/bin/env python3
"""Enriquece pessoas JÁ EXISTENTES no Juridiq com dados de planilhas.

Problema que resolve (2026-06-10): a base pré-existente do Juridiq tem
muita pessoa cadastrada SÓ com nome. A importação inicial pulou essas
(dedupe correto — não duplica), mas os dados ricos da planilha (email,
telefone, endereço, documento) nunca chegaram lá.

Política CONSERVADORA: preenche apenas campos VAZIOS no Juridiq.
NUNCA sobrescreve um valor já existente.

Uso:
    # Dry-run (default):
    JURIDIQ_API_KEY=... uv run --with xlrd --with httpx python \
        scripts/enriquecer_clientes_juridiq.py planilha1.xls ...

    # Valendo:
    ... --execute
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from importar_clientes_juridiq import (  # noqa: E402
    _norm_doc,
    _norm_name,
    ler_planilhas,
    montar_payload,
)

BASE_URL = "https://api.juridiq.com.br"
THROTTLE_S = 0.3

# Campos que o enriquecimento pode preencher (planilha → Juridiq vazio).
CAMPOS = [
    "email", "phone", "document", "rg", "birthDate", "personType",
    "zipCode", "state", "city", "neighborhood", "streetAndNumber",
    "addressComplement", "annotation",
]


def baixar_indice(client: httpx.Client) -> dict[str, str]:
    """Todas as pessoas → {chave: person_id} (chave = doc norm e nome norm)."""
    indice: dict[str, str] = {}
    page = 1
    while True:
        resp = client.get("/person/", params={"page": page, "limit": 100})
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("data", []):
            pid = p["id"]
            if p.get("document"):
                indice.setdefault(_norm_doc(p["document"]), pid)
            if p.get("name"):
                indice.setdefault(_norm_name(p["name"]), pid)
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return indice


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("planilhas", nargs="+")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    api_key = os.environ.get("JURIDIQ_API_KEY", "").strip()
    if not api_key:
        print("ERRO: defina JURIDIQ_API_KEY", file=sys.stderr)
        return 2

    registros = ler_planilhas(args.planilhas)
    print(f"linhas lidas: {len(registros)}")

    client = httpx.Client(
        base_url=BASE_URL,
        headers={"x-juridiq-api-key": api_key},
        timeout=20.0,
    )

    print("baixando índice de pessoas do Juridiq...")
    indice = baixar_indice(client)
    print(f"índice: {len(indice)} chaves")

    # Pra cada linha: acha a pessoa, compara campo a campo.
    planejados: list[tuple[str, str, dict]] = []  # (person_id, nome, patch)
    nao_encontrados: list[str] = []
    vistos: set[str] = set()

    for row in registros:
        body = montar_payload(row)
        doc = body.get("document", "")
        pid = indice.get(doc) if doc else None
        match_por_nome = False
        if not pid:
            pid = indice.get(_norm_name(body["name"]))
            match_por_nome = pid is not None
        if not pid:
            nao_encontrados.append(body["name"])
            continue
        if pid in vistos:  # mesma pessoa em 2 planilhas
            continue
        vistos.add(pid)

        # Detalhe atual da pessoa no Juridiq.
        resp = client.get(f"/person/{pid}")
        if resp.status_code >= 400:
            print(f"  AVISO: GET {pid} falhou ({resp.status_code}) — pulando")
            continue
        atual = resp.json()
        atual = atual.get("data", atual)

        # Guarda contra HOMÔNIMO (auditoria 2026-06-11): match por NOME
        # é fraco — duas pessoas com o mesmo nome são comuns. Nesse
        # caso: (a) se o alvo tem documento DIFERENTE do da planilha,
        # é outra pessoa → pula; (b) nunca gravar dados identitários
        # (document/rg/birthDate) via match por nome.
        campos_permitidos = list(CAMPOS)
        if match_por_nome:
            doc_alvo = str(atual.get("document") or "").strip()
            if doc and doc_alvo and doc_alvo != doc:
                print(
                    f"  AVISO: {body['name']!r} — homônimo com documento "
                    f"divergente no Juridiq, pulando"
                )
                continue
            campos_permitidos = [
                c for c in CAMPOS if c not in ("document", "rg", "birthDate")
            ]

        patch = {}
        for campo in campos_permitidos:
            novo = body.get(campo)
            existente = atual.get(campo)
            if novo and not (existente and str(existente).strip()):
                patch[campo] = novo
        if patch:
            planejados.append((pid, body["name"], patch))
        time.sleep(0.1)  # gentileza nos GETs

    print()
    print(f"=== RESUMO {'(DRY-RUN)' if not args.execute else '(EXECUTANDO)'} ===")
    print(f"pessoas a enriquecer: {len(planejados)}")
    print(f"não encontradas no Juridiq: {len(nao_encontrados)}")
    from collections import Counter
    contagem = Counter(c for _, _, p in planejados for c in p)
    print("campos a preencher:", dict(contagem))
    print()
    print("=== amostra (5 primeiras) ===")
    for pid, nome, patch in planejados[:5]:
        campos = ", ".join(f"{k}={str(v)[:30]!r}" for k, v in patch.items()
                           if k != "annotation")
        extra = " +annotation" if "annotation" in patch else ""
        print(f"- {nome}: {campos}{extra}")

    if not args.execute:
        print()
        print("Dry-run concluído. Rode com --execute pra aplicar.")
        client.close()
        return 0

    print()
    ok, erros = 0, []
    for pid, nome, patch in planejados:
        try:
            resp = client.patch(f"/person/{pid}", json=patch)
            if resp.status_code >= 400:
                erros.append((nome, f"HTTP {resp.status_code}: {resp.text[:100]}"))
            else:
                ok += 1
                if ok % 25 == 0:
                    print(f"  ...{ok} atualizados")
        except Exception as exc:
            erros.append((nome, str(exc)[:100]))
        time.sleep(THROTTLE_S)

    client.close()
    print()
    print("=== RELATÓRIO FINAL ===")
    print(f"enriquecidos: {ok}")
    print(f"erros: {len(erros)}")
    for nome, err in erros:
        print(f"  ERRO {nome!r}: {err}")
    return 0 if not erros else 1


if __name__ == "__main__":
    sys.exit(main())
