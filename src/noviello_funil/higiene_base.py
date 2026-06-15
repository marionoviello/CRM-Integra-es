"""Higienização da base de pessoas — relatório de qualidade (roadmap 2.3).

A base do Juridiq (~1465 pessoas) está suja: ~78% sem email, ~80% sem
telefone, dezenas de duplicatas (mesmo CPF em 2+ fichas) e nomes quase
iguais por erro de digitação ("Carla Donati" × "Donatti"). Isso limita
TODOS os automatismos (aniversário não dispara sem data; reconhecer
cliente não casa sem telefone). Este job mede a qualidade e manda um
relatório acionável ao Mario — sem mexer em nada (merge é decisão
humana, no painel).

Sinais, do mais forte ao mais fraco:
1. Mesmo CPF/CNPJ em 2+ fichas = duplicata CERTA (merge).
2. Nome quase idêntico = provável duplicata por digitação.
3. Email/telefone compartilhado = relacionadas (pode ser intermediário
   ou duplicata — conferir).
4. Completude agregada (quantos faltam email/telefone/doc).

Execução: console script ``noviello-higiene`` via timer semanal (terça
08h BRT — dia diferente dos outros relatórios). Base limpa → silêncio.
"""

import asyncio
import logging
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

import httpx

logger = logging.getLogger(__name__)

SIMILARIDADE_MIN = 0.88   # ratio acima disso = nomes "quase iguais"
MAX_AMOSTRA = 10          # exemplos por categoria na mensagem


def _digitos(s: object) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _tel_canonico(s: object) -> str | None:
    """Chave única de telefone (tolera 55 e 9º dígito — reusa person_index)."""
    from noviello_funil.person_index import chaves_telefone
    chaves = chaves_telefone(s)
    return max(chaves, key=len) if chaves else None


def _norm_nome(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def _grupos_por_chave(pessoas: list[dict], keyfn) -> list[dict]:
    """Agrupa pessoas por uma chave; retorna grupos com 2+ membros."""
    d: dict[str, list[str]] = defaultdict(list)
    for p in pessoas:
        k = keyfn(p)
        if k:
            d[k].append(p.get("name") or "(sem nome)")
    return [
        {"chave": k, "nomes": nomes}
        for k, nomes in d.items() if len(nomes) > 1
    ]


def _pares_similares(pessoas: list[dict]) -> list[tuple[str, str]]:
    """Pares de nomes quase idênticos (provável duplicata por digitação).

    Só compara dentro do mesmo PRIMEIRO nome (corte barato) e exige nome
    completo (>=2 palavras) pra não parear gente diferente de 1º nome igual.
    """
    por_primeiro: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for p in pessoas:
        nm = _norm_nome(p.get("name"))
        if nm and len(nm.split()) >= 2:
            por_primeiro[nm.split()[0]].append((nm, p.get("name") or ""))
    pares = []
    for grupo in por_primeiro.values():
        vistos = sorted(set(grupo))
        for i in range(len(vistos)):
            for j in range(i + 1, len(vistos)):
                a, b = vistos[i], vistos[j]
                if a[0] != b[0] and \
                        SequenceMatcher(None, a[0], b[0]).ratio() >= SIMILARIDADE_MIN:
                    pares.append((a[1], b[1]))
    return pares


def analisar_base(pessoas: list[dict]) -> dict:
    """Diagnóstico de qualidade da base. Tudo read-only."""
    total = len(pessoas)
    sem_email = sum(1 for p in pessoas if not (p.get("email") or "").strip())
    sem_telefone = sum(1 for p in pessoas if len(_digitos(p.get("phone"))) < 10)
    sem_documento = sum(1 for p in pessoas if len(_digitos(p.get("document"))) < 11)

    dup_documento = _grupos_por_chave(
        pessoas,
        lambda p: _digitos(p.get("document")) if len(_digitos(p.get("document"))) >= 11 else None,
    )
    dup_email = _grupos_por_chave(
        pessoas, lambda p: (p.get("email") or "").strip().lower() or None,
    )
    dup_telefone = _grupos_por_chave(pessoas, lambda p: _tel_canonico(p.get("phone")))
    nomes_similares = _pares_similares(pessoas)

    return {
        "total": total,
        "sem_email": sem_email,
        "sem_telefone": sem_telefone,
        "sem_documento": sem_documento,
        "dup_documento": dup_documento,
        "dup_email": dup_email,
        "dup_telefone": dup_telefone,
        "nomes_similares": nomes_similares,
    }


def montar_mensagem(diag: dict) -> str | None:
    """Relatório WhatsApp-ready, ou None se não há nada acionável.

    "Acionável" = existe duplicata (doc/email/telefone) OU nomes
    similares. Incompletude sozinha não dispara (é crônica; vai junto
    como contexto quando há o que limpar)."""
    tem_dup = (
        diag["dup_documento"] or diag["dup_email"]
        or diag["dup_telefone"] or diag["nomes_similares"]
    )
    if not tem_dup:
        return None

    t = diag["total"] or 1
    blocos = [
        f"🧹 *Higiene da base* ({diag['total']} pessoas)",
        f"\nCompletude: {diag['sem_email']} sem email "
        f"({100 * diag['sem_email'] // t}%), {diag['sem_telefone']} sem telefone, "
        f"{diag['sem_documento']} sem documento.",
    ]

    if diag["dup_documento"]:
        blocos.append(
            f"\n🔁 *{len(diag['dup_documento'])} CPF/CNPJ em 2+ fichas* "
            "(mesma pessoa duplicada — fundir no painel):"
        )
        for g in diag["dup_documento"][:MAX_AMOSTRA]:
            blocos.append(f"• {' = '.join(g['nomes'][:4])}")
        if len(diag["dup_documento"]) > MAX_AMOSTRA:
            blocos.append(f"… e mais {len(diag['dup_documento']) - MAX_AMOSTRA}.")

    if diag["nomes_similares"]:
        blocos.append(
            f"\n✏️ *{len(diag['nomes_similares'])} nomes quase iguais* "
            "(provável duplicata por digitação):"
        )
        for a, b in diag["nomes_similares"][:MAX_AMOSTRA]:
            blocos.append(f"• {a}  ~  {b}")
        if len(diag["nomes_similares"]) > MAX_AMOSTRA:
            blocos.append(f"… e mais {len(diag['nomes_similares']) - MAX_AMOSTRA}.")

    compartilhados = len(diag["dup_email"]) + len(diag["dup_telefone"])
    if compartilhados:
        blocos.append(
            f"\n📎 {len(diag['dup_email'])} emails e {len(diag['dup_telefone'])} "
            "telefones compartilhados por 2+ fichas (intermediário ou duplicata)."
        )

    blocos.append("\nTudo só sinalização — fundir/corrigir é no painel do Juridiq.")
    return "\n".join(blocos)


def _listar_pessoas(client: httpx.Client) -> list[dict]:
    pessoas, page = [], 1
    while True:
        r = client.get("/person/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        pessoas.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return pessoas


def main() -> int:
    """Entry point do console script ``noviello-higiene``."""
    from noviello_funil.config import Settings
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("higiene: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("higiene: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        pessoas = _listar_pessoas(client)
    finally:
        client.close()

    diag = analisar_base(pessoas)
    logger.info(
        "higiene: %d pessoas, %d dup-doc, %d nomes similares",
        diag["total"], len(diag["dup_documento"]), len(diag["nomes_similares"]),
    )

    texto = montar_mensagem(diag)
    if texto is None:
        return 0
    logger.info("higiene:\n%s", texto)

    async def _send() -> None:
        jurichat = JurichatClient(
            api_key=settings.jurichat_api_key,
            base_url=settings.jurichat_base_url,
            bot_user_id=settings.jurichat_bot_user_id,
        )
        try:
            await notify_mario(
                jurichat,
                mario_conversation_id=settings.mario_conversation_id,
                mensagem=texto,
            )
        finally:
            await jurichat.aclose()

    asyncio.run(_send())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
