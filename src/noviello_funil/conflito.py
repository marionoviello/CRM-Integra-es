"""Detecção de conflito de interesse (impedimento, roadmap 1.7).

Um lead que aparece como PARTE CONTRÁRIA de um cliente do escritório é
um impedimento ético (Código de Ética da OAB) — hoje isso depende da
memória do advogado. Este módulo indexa as partes não-cliente de todos
os processos e, quando um lead novo bate com um desses nomes, LEVANTA
SUSPEITA ao Mario.

Regras de ouro (do roadmap):
- O sistema só SUSPEITA — a decisão de impedimento é HUMANA (homonímia
  gera falso positivo).
- NUNCA revelar ao lead que ele apareceu como parte contrária — a
  suspeita vai SÓ pro canal interno.

Fonte: GET /lawSuit/ já traz ``persons`` com ``personOrigin`` (Cliente,
Requerido, Requerida, Autor, …). Indexamos quem NÃO é "Cliente".

Match conservador: nome completo normalizado, exato, com ≥2 palavras
(um primeiro nome sozinho casaria meio mundo). Prefere perder um caso
duvidoso a inundar o Mario de falsos positivos.
"""

import logging
import re
import unicodedata

import httpx

logger = logging.getLogger(__name__)


def normalizar_nome(nome: object) -> str:
    """Nome → comparável: tira sufixo '- CPF/CNPJ ...', acentos, caixa."""
    s = str(nome or "")
    # corta sufixo de documento que o Juridiq cola no nome
    s = re.split(r"\s*-\s*(?:cpf|cnpj|rg)\b", s, flags=re.IGNORECASE)[0]
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def construir_indice_partes(client: httpx.Client, conn) -> int:
    """Repovoa parte_contraria do GET /lawSuit/. Retorna nº de partes
    indexadas. Idempotente (zera e reconstrói)."""
    processos, page = [], 1
    while True:
        r = client.get("/lawSuit/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        processos.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1

    # 1ª passada: nomes que são CLIENTE em QUALQUER processo. Um cliente
    # do escritório nunca pode virar "adversário" — mesmo que apareça com
    # outro papel noutro processo (defesa do bug de revisão 15/jun).
    clientes = set()
    for p in processos:
        for pessoa in p.get("persons") or []:
            if (pessoa.get("personOrigin") or "").strip().lower() == "cliente":
                clientes.add(normalizar_nome(pessoa.get("name")))

    # Transação: leitores (poll cycle) nunca veem o índice parcial entre o
    # DELETE e os INSERTs (mesmo motivo do person_index — bug revisão 15/jun).
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM parte_contraria")
        n = 0
        for p in processos:
            num = p.get("processNumber") or ""
            for pessoa in p.get("persons") or []:
                origem = (pessoa.get("personOrigin") or "").strip().lower()
                if not origem or origem == "cliente":
                    continue
                nome_norm = normalizar_nome(pessoa.get("name"))
                if len(nome_norm.split()) < 2:   # ignora nomes/instituições de 1 palavra
                    continue
                if nome_norm in clientes:         # é cliente noutro processo → não é adversário
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO parte_contraria "
                    "(nome_norm, processo, papel) VALUES (?, ?, ?)",
                    (nome_norm, num, pessoa.get("personOrigin")),
                )
                n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    logger.info("conflito: %d partes contrárias indexadas de %d processos",
                n, len(processos))
    return n


def checar_conflito(conn, nome_lead: object) -> list[dict]:
    """Lead bate com alguma parte contrária? Retorna [{processo, papel}].

    Match exato por nome completo normalizado (≥2 palavras). Vazio = sem
    suspeita.
    """
    alvo = normalizar_nome(nome_lead)
    if len(alvo.split()) < 2:
        return []
    rows = conn.execute(
        "SELECT processo, papel FROM parte_contraria WHERE nome_norm = ?",
        (alvo,),
    ).fetchall()
    return [{"processo": r[0], "papel": r[1]} for r in rows]
