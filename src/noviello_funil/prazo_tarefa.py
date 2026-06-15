"""Publicação urgente vira TAREFA rastreável no Juridiq (roadmap 1.1).

O ``publicacoes.py`` já lê as publicações não tratadas, classifica urgência
e EXTRAI o prazo (texto do Claude: "15 dias" / "20/06"). Mas hoje isso só
vira alerta no WhatsApp — efêmero. Este módulo fecha o loop: monta a tarefa
(título, prazo SUGERIDO com folga, descrição) pra criar no painel via
``POST /task/``, e marca a publicação como tratada. É a defesa direta contra
o erro mais caro do escritório: perder prazo.

⚠️ A data é uma ESTIMATIVA da IA — a tarefa sai marcada "prazo sugerido,
conferir a contagem no painel", com folga (buffer antes da data real). Dias
úteis × corridos, suspensões e feriados forenses variam; jamais apresentar
como cálculo oficial. As funções aqui são puras/testáveis; as chamadas à API
(criar tarefa, marcar lida) ficam no juridiq_client.

Idempotência por publication_id (tabela tarefa_publicacao): uma publicação
gera UMA tarefa, e só marcamos a publicação como tratada DEPOIS da tarefa
criada — uma falha no meio não perde o prazo.
"""

import datetime
import logging
import re

import httpx

logger = logging.getLogger(__name__)

# Folga (dias) antes da data real estimada — a tarefa vence ANTES pra dar
# margem. Conservador de propósito (perder prazo é o pior caso).
BUFFER_DIAS = 3


def _data_base(iso: object) -> datetime.date | None:
    """Data da publicação (DD/MM/AAAA ou ISO) → date. None se não parseável."""
    s = str(iso or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def calcular_prazo_sugerido(
    prazo_texto: object,
    data_pub: object,
    buffer_dias: int = BUFFER_DIAS,
) -> str | None:
    """Estima a data-limite (ISO 'YYYY-MM-DD') a partir do prazo extraído.

    - Data explícita ('20/06' ou '20/06/2026') → essa data − buffer.
    - 'N dias' → data da publicação + N − buffer (corridos, conservador).
    - Nada parseável → None (a tarefa fica sem data, marcada "conferir").

    SEMPRE uma sugestão com folga — não é cálculo oficial de prazo.
    """
    t = str(prazo_texto or "").strip().lower()
    if not t:
        return None
    base = _data_base(data_pub)

    # Data explícita DD/MM[/AAAA].
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", t)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        if m.group(3):
            ano = int(m.group(3))
        elif base:
            ano = base.year
        else:
            return None
        try:
            alvo = datetime.date(ano, mes, dia)
        except ValueError:
            return None
        # sem ano e a data "já passou" relativa à publicação → ano seguinte
        if not m.group(3) and base and alvo < base:
            try:
                alvo = alvo.replace(year=alvo.year + 1)
            except ValueError:
                return None
        return (alvo - datetime.timedelta(days=buffer_dias)).isoformat()

    # "N dias".
    m = re.search(r"\b(\d{1,3})\s*dias?\b", t)
    if m and base:
        n = int(m.group(1))
        alvo = base + datetime.timedelta(days=n)
        return (alvo - datetime.timedelta(days=buffer_dias)).isoformat()

    return None


def montar_titulo(motivo: object, processo: object) -> str:
    """'PRAZO: <motivo> — <processo>' (cap no título)."""
    mot = re.sub(r"\s+", " ", str(motivo or "").strip()) or "ato com prazo"
    proc = str(processo or "").strip()
    base = f"PRAZO: {mot}"
    if proc:
        base += f" — {proc}"
    return base[:120]


def montar_descricao(
    motivo: object, prazo_texto: object, teor: object, data_pub: object,
) -> str:
    """Descrição da tarefa: teor + o aviso de prazo SUGERIDO."""
    partes = ["⚠️ Prazo SUGERIDO pela IA — confira a contagem no painel "
              "(dias úteis × corridos, feriados forenses, suspensões)."]
    if prazo_texto and str(prazo_texto).strip():
        partes.append(f"Prazo extraído: {str(prazo_texto).strip()}")
    if data_pub and str(data_pub).strip():
        partes.append(f"Publicação: {str(data_pub).strip()}")
    if motivo and str(motivo).strip():
        partes.append(f"Motivo: {str(motivo).strip()}")
    teor_s = re.sub(r"\s+", " ", str(teor or "").strip())
    if teor_s:
        partes.append(f"\nTeor: {teor_s[:1500]}")
    return "\n".join(partes)


def deve_criar_tarefa(pub: dict) -> bool:
    """Só vira tarefa a publicação URGENTE com processo identificado (sem
    processo não dá pra vincular no painel — fica só no alerta)."""
    return bool(pub.get("urgente")) and bool(str(pub.get("processo") or "").strip())


def ja_criada(conn, publication_id: object) -> bool:
    """A publicação já gerou tarefa? (idempotência)."""
    pid = str(publication_id or "")
    if not pid:
        return True   # sem id não cria (evita duplicar cego)
    row = conn.execute(
        "SELECT 1 FROM tarefa_publicacao WHERE publication_id = ?", (pid,),
    ).fetchone()
    return row is not None


def marcar_criada(conn, publication_id: object, processo: object, task_id: object) -> None:
    """Registra que a publicação virou tarefa (após criar). Idempotente."""
    conn.execute(
        "INSERT OR IGNORE INTO tarefa_publicacao "
        "(publication_id, process_number, task_id) VALUES (?, ?, ?)",
        (str(publication_id or ""), str(processo or ""), str(task_id or "")),
    )


def montar_corpo_tarefa(
    *,
    titulo: str,
    descricao: str,
    final_date: str | None,
    law_suit_id: str,
    board: str,
    column: str,
    priority: str,
) -> dict:
    """Corpo do POST /task/ (campos espelham a estrutura do GET /task/:
    title, description, priority, board, column, lawSuitId, finalDate)."""
    body: dict = {
        "title": titulo,
        "description": descricao,
        "priority": priority,
        "board": board,
        "column": column,
        "lawSuitId": law_suit_id,
    }
    if final_date:
        body["finalDate"] = final_date
    return body


def criar_tarefa(
    client: httpx.Client, corpo: dict,
) -> tuple[str | None, str]:
    """POST /task/ → (task_id | None, detalhe). Erro não levanta — devolve o
    motivo pro caller decidir (e logar o corpo da resposta na 1ª validação)."""
    try:
        r = client.post("/task/", json=corpo)
    except httpx.HTTPError as exc:
        return None, f"erro_{type(exc).__name__}"
    if r.status_code >= 400:
        return None, f"http_{r.status_code}: {r.text[:400]}"
    try:
        data = r.json()
    except ValueError:
        return None, "resposta_nao_json"
    obj = data.get("data") if isinstance(data, dict) and "data" in data else data
    tid = obj.get("id") if isinstance(obj, dict) else None
    return (str(tid) if tid else None), "ok"
