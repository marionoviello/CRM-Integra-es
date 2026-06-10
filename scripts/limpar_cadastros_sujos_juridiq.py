#!/usr/bin/env python3
"""Limpa cadastros do Juridiq com CPF/CNPJ colado no campo nome.

Problema (descoberto 2026-06-10): ~54 pessoas cadastradas manualmente
no Juridiq têm o documento embutido no nome e os campos próprios
vazios. Ex: 'WILSON ROBERTO MOREIRA - CPF: 676.474.568-49' com
document=NULL.

Pra cada um:
  1. Extrai o CPF/CNPJ do nome → preenche campo ``document`` (validado)
  2. Limpa o nome (remove ' - CPF: xxx' / ' - CNPJ: xxx')
  3. Title Case se o nome estava todo em CAIXA ALTA
  4. (opcional) cruza com planilha pra preencher phone/email/endereço

Uso:
    JURIDIQ_API_KEY=... uv run --with xlrd --with openpyxl --with httpx \
        python scripts/limpar_cadastros_sujos_juridiq.py [planilha.xlsx]
    # + --execute pra aplicar (default dry-run)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from importar_clientes_juridiq import (  # noqa: E402
    _norm_doc,
    _norm_name,
    _valida_cpf,
    ler_planilhas,
    montar_payload,
)
from enriquecer_clientes_juridiq import CAMPOS  # noqa: E402

BASE_URL = "https://api.juridiq.com.br"
THROTTLE_S = 0.3

# Captura '- CPF: 123.456.789-00' / '- CNPJ: 12.345...' no fim do nome.
_DOC_NO_NOME = re.compile(
    r"\s*[-–]\s*(?:CPF|C\.?P\.?F\.?|CNPJ|C\.?N\.?P\.?J\.?)\s*:?.*$",
    re.IGNORECASE,
)


def _limpar_nome(nome: str) -> str:
    limpo = _DOC_NO_NOME.sub("", nome).strip()
    # Title Case só se estava TODO em maiúsculas (preserva nomes mistos).
    if limpo and limpo == limpo.upper():
        limpo = limpo.title()
        # Conectivos minúsculos (de, da, do, dos, e)
        for w in (" De ", " Da ", " Do ", " Dos ", " Das ", " E "):
            limpo = limpo.replace(w, w.lower())
    return limpo


def _doc_do_nome(nome: str) -> str:
    """Extrai CPF/CNPJ do trecho após 'CPF:'/'CNPJ:'.

    SÓ retorna se for documento VÁLIDO (CPF com dígito verificador ou
    CNPJ de 14 dígitos). CPF mascarado ('129.XXX.XXX-XX') ou CNPJ
    incompleto vira '' — não preenchemos document com lixo.
    """
    m = re.search(r"(?:CPF|CNPJ)\s*:?\s*([\d.\-/]+)", nome, re.IGNORECASE)
    if not m:
        return ""
    doc = _norm_doc(m.group(1))
    if len(doc) == 11 and _valida_cpf(doc):
        return doc
    if len(doc) == 14:
        return doc
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("planilhas", nargs="*", help="planilhas pra cruzar dados (opcional)")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    api_key = os.environ.get("JURIDIQ_API_KEY", "").strip()
    if not api_key:
        print("ERRO: defina JURIDIQ_API_KEY", file=sys.stderr)
        return 2

    # Índice da planilha (por documento e nome) pra cruzar dados extras.
    plan_por_doc: dict[str, dict] = {}
    plan_por_nome: dict[str, dict] = {}
    if args.planilhas:
        for row in ler_planilhas(args.planilhas):
            body = montar_payload(row)
            if body.get("document"):
                plan_por_doc[body["document"]] = body
            plan_por_nome[_norm_name(body["name"])] = body

    client = httpx.Client(
        base_url=BASE_URL,
        headers={"x-juridiq-api-key": api_key},
        timeout=20.0,
    )

    # Baixa toda a base e seleciona os sujos.
    ppl, page = [], 1
    while True:
        d = client.get("/person/", params={"page": page, "limit": 100}).json()
        ppl.extend(d["data"])
        if page >= int(d.get("totalPages") or 1):
            break
        page += 1

    sujos = [
        p for p in ppl
        if re.search(r"CPF|C\.P\.F|CNPJ", (p.get("name") or ""), re.IGNORECASE)
    ]
    print(f"base: {len(ppl)} | cadastros sujos: {len(sujos)}")

    planejados = []  # (id, nome_atual, patch)
    for p in sujos:
        nome_atual = p["name"]
        patch = {}

        nome_limpo = _limpar_nome(nome_atual)
        if nome_limpo and nome_limpo != nome_atual:
            patch["name"] = nome_limpo

        doc_extraido = _doc_do_nome(nome_atual)
        doc_existente = "".join(c for c in str(p.get("document") or "") if c.isdigit())

        # Cruza com planilha (por doc extraído ou nome limpo).
        extra = plan_por_doc.get(doc_extraido) or plan_por_nome.get(
            _norm_name(nome_limpo)
        )
        # document final: planilha (mais confiável) > extraído do nome.
        doc_final = (extra.get("document") if extra else "") or doc_extraido
        if doc_final and not doc_existente:
            patch["document"] = doc_final

        if extra:
            det = client.get(f"/person/{p['id']}").json()
            det = det.get("data", det)
            for campo in CAMPOS:
                if campo == "document":
                    continue  # já tratado acima com prioridade
                novo = extra.get(campo)
                ex = patch.get(campo) or det.get(campo)
                if novo and not (ex and str(ex).strip()):
                    patch[campo] = novo
            time.sleep(0.1)

        if patch:
            planejados.append((p["id"], nome_atual, patch))

    print(f"a limpar: {len(planejados)}")
    print()
    print("=== amostra (10 primeiros) ===")
    for _, nome_atual, patch in planejados[:10]:
        novo_nome = patch.get("name", "(mantém)")
        outros = [k for k in patch if k != "name"]
        print(f"- {nome_atual!r}")
        print(f"    nome → {novo_nome!r} | + campos: {outros}")

    if not args.execute:
        print()
        print("Dry-run. Rode com --execute pra aplicar.")
        client.close()
        return 0

    print()
    ok, erros = 0, []
    for pid, nome_atual, patch in planejados:
        try:
            r = client.patch(f"/person/{pid}", json=patch)
            if r.status_code >= 400:
                erros.append((nome_atual, f"HTTP {r.status_code}: {r.text[:100]}"))
            else:
                ok += 1
        except Exception as exc:
            erros.append((nome_atual, str(exc)[:100]))
        time.sleep(THROTTLE_S)
    client.close()
    print(f"=== limpos: {ok} | erros: {len(erros)} ===")
    for nome, err in erros:
        print(f"  ERRO {nome!r}: {err}")
    return 0 if not erros else 1


if __name__ == "__main__":
    sys.exit(main())
