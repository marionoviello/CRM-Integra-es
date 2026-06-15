"""Índice telefone→ficha do Juridiq (roadmap 0.1 — fundação).

Casar o telefone de quem manda WhatsApp com a ficha certa no Juridiq é
a peça que destrava reconhecer cliente existente (1.6), detectar
conflito de interesse (1.7) e responder "como está meu processo?" (2.4).
Hoje isso falha: o `/person/search` do Juridiq devolve 400 quando não
acha, e a listagem não filtra por telefone.

Solução: um índice local (SQLite) telefone→{id, nome, email, document},
repovoado de madrugada a partir do GET /person/ — que, ao contrário do
que assumíamos, JÁ traz phone/email/document na própria listagem
(verificado 2026-06-14), então o scan é rápido (~15 chamadas, segundos).

O matching tolera as duas variações que mais quebram telefone no Brasil:
o código de país (55) e o 9º dígito de celular. Cada pessoa é indexada
sob todas as variantes do seu número; a busca gera as variantes do
número procurado e casa por interseção.

Execução: console script ``noviello-indice`` via timer diário (04h BRT).
"""

import logging
import re

import httpx

logger = logging.getLogger(__name__)


def chaves_telefone(raw: object) -> set[str]:
    """Variantes canônicas (DDD+número) de um telefone BR.

    Normaliza removendo código de país (55) e formatação, e gera a
    variante com e sem o 9º dígito de celular — assim um cadastro com
    "9" casa com uma mensagem sem, e vice-versa. Número incompleto
    (< DDD + 8 dígitos) → conjunto vazio.
    """
    d = re.sub(r"\D", "", str(raw or ""))
    # tira o código de país brasileiro quando presente
    if len(d) > 11 and d.startswith("55"):
        d = d[2:]
    if len(d) < 10:  # menos que DDD(2) + 8 = inválido
        return set()
    if len(d) > 11:  # lixo / número internacional não-BR
        return set()
    ddd, num = d[:2], d[2:]
    chaves = {ddd + num}
    if len(num) == 9 and num[0] == "9":   # celular: adiciona variante sem o 9
        chaves.add(ddd + num[1:])
    if len(num) == 8:                       # 8 dígitos: adiciona variante com 9
        chaves.add(ddd + "9" + num)
    return chaves


def construir_indice(client: httpx.Client, conn) -> int:
    """Repovoa person_index a partir do GET /person/. Retorna nº de pessoas
    indexadas (com telefone válido). Idempotente: zera e reconstrói."""
    pessoas, page = [], 1
    while True:
        r = client.get("/person/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        pessoas.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1

    conn.execute("DELETE FROM person_index")
    indexadas = 0
    for p in pessoas:
        chaves = chaves_telefone(p.get("phone"))
        if not chaves:
            continue
        indexadas += 1
        for ch in chaves:
            conn.execute(
                "INSERT OR REPLACE INTO person_index "
                "(telefone_chave, person_id, nome, email, document) "
                "VALUES (?, ?, ?, ?, ?)",
                (ch, p.get("id"), p.get("name"), p.get("email"),
                 p.get("document")),
            )
    logger.info(
        "person_index: %d pessoas indexadas de %d (com telefone)",
        indexadas, len(pessoas),
    )
    return indexadas


def resolver_telefone(conn, telefone: object) -> dict | None:
    """Acha a ficha do Juridiq por telefone, ou None. Em caso de múltiplos
    matches (raro), retorna o primeiro — o caller decide o que fazer."""
    chaves = chaves_telefone(telefone)
    if not chaves:
        return None
    placeholders = ",".join("?" * len(chaves))
    row = conn.execute(
        f"SELECT person_id, nome, email, document FROM person_index "
        f"WHERE telefone_chave IN ({placeholders}) LIMIT 1",
        tuple(chaves),
    ).fetchone()
    if row is None:
        return None
    return {"person_id": row[0], "nome": row[1], "email": row[2],
            "document": row[3]}


def main() -> int:
    """Entry point do console script ``noviello-indice``."""
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations

    settings = Settings()
    logging.basicConfig(level=settings.log_level)
    if not settings.juridiq_api_key:
        logger.warning("person_index: JURIDIQ_API_KEY não configurada — pulando")
        return 0

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        n = construir_indice(client, conn)
        logger.info("person_index: índice reconstruído (%d pessoas)", n)
        # Mesmo job de madrugada também reconstrói o índice de partes
        # contrárias (conflito de interesse, roadmap 1.7) — reusa a
        # conexão/cliente, sem mais um timer.
        from noviello_funil.conflito import construir_indice_partes
        try:
            np = construir_indice_partes(client, conn)
            logger.info("person_index: índice de partes (%d) reconstruído", np)
        except Exception as exc:
            logger.exception("person_index: índice de partes falhou: %s", exc)
    finally:
        client.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
