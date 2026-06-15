"""Atendimento "como está meu processo?" (roadmap 2.4).

Quando um cliente pergunta no WhatsApp sobre o andamento do processo dele,
o bot responde — mas com duas travas que o Mario definiu (decisão OAB):

  1. AUTENTICAÇÃO POR CADASTRO: só passa informação pro telefone que está
     no cadastro como PARTE CLIENTE daquele processo. Telefone não bate →
     não revela nada e avisa Mario + Hilde.
  2. SEGREDO DE JUSTIÇA: se o processo é sigiloso, o bot NÃO responde nada
     automático — avisa Mario + Hilde pra passarem a informação manual.

O que o bot informa (processo NÃO sigiloso, cliente verificado): número,
data e texto da última movimentação (fato processual público) + "equipe
acompanhando". SEM opinião jurídica, mérito ou prognóstico.

Vínculo telefone→processo: o ``person_index`` liga telefone↔ficha (com
CPF); o ``/lawSuit/`` traz ``persons`` com ``personOrigin`` e o nome do
cliente (com CPF colado no sufixo). Casamos por CPF (forte) e, na falta,
por nome completo normalizado. Índice ``cliente_processo`` repovoado de
madrugada junto do person_index (depende dele estar fresco).

Regra de marca: o bot fala "nossa equipe" / "um advogado", NUNCA "Dr.
Mario". Segredo e não-identificado vão SÓ pro canal interno.
"""

import logging
import re

import httpx

from noviello_funil.conflito import normalizar_nome
from noviello_funil.person_index import chaves_telefone

logger = logging.getLogger(__name__)

# Intenção "como está meu processo?". Exige uma referência a processo/ação/
# caso PERTO de uma pista de status — pra não sequestrar lead novo que só
# quer "abrir um processo" ou "entrar com uma ação".
_PROC = r"(processos?|a[çc][õo]es|a[çc][ãa]o|caso|invent[áa]rio|usucapi[ãa]o)"
_PERGUNTAS_STATUS = [
    rf"\bcomo\s+(est[áa]|t[áa]|anda|andam|v[ãa]o|vai)\b[^?]{{0,25}}{_PROC}",
    rf"\bandamento[s]?\b[^?]{{0,20}}{_PROC}",
    rf"{_PROC}[^?]{{0,20}}\bandamento",
    rf"\b(novidade|not[íi]cia|atualiza[çc][ãa]o|status|movimenta[çc][ãa]o|posi[çc][ãa]o)\b"
    rf"[^?]{{0,25}}{_PROC}",
    rf"\bmeu[s]?\s+{_PROC}\b[^?]{{0,30}}"
    rf"\b(como|novidade|andamento|atualiza|status|saiu|movimento|parado|anda|notici)",
    rf"{_PROC}[^?]{{0,25}}\b(teve|tem|saiu|houve)\s+(alguma\s+)?"
    rf"(novidade|movimenta|atualiza|not[íi]cia)",
]
_RX_STATUS = [re.compile(p, re.IGNORECASE) for p in _PERGUNTAS_STATUS]

# Mensagens ao cliente (neutras — não confirmam nem negam processo sigiloso).
MSG_SIGILOSO_CLIENTE = (
    "Recebi sua solicitação! 🙏 Sobre esse assunto, nossa equipe vai te "
    "retornar diretamente com as informações, tá bem?"
)
MSG_NAO_CADASTRADO_CLIENTE = (
    "Pra te passar informações de um processo, preciso confirmar seu "
    "cadastro primeiro. Já avisei nossa equipe pra te ajudar com isso. 🙏"
)


def detectar_pergunta_status(texto: object) -> bool:
    """True se a mensagem é uma pergunta sobre o andamento de um processo."""
    t = str(texto or "")
    return any(rx.search(t) for rx in _RX_STATUS)


def _so_digitos(s: object) -> str:
    return re.sub(r"\D", "", str(s or ""))


def extrair_documento(nome: object) -> str:
    """CPF/CNPJ colado no nome do cliente pelo Juridiq → só dígitos. '' se não."""
    m = re.search(
        r"-\s*(?:cpf|cnpj|rg)\s*:?\s*([\d.\-/]+)", str(nome or ""), re.IGNORECASE,
    )
    return _so_digitos(m.group(1)) if m else ""


def _nome_limpo(nome: object) -> str:
    """Nome sem o sufixo '- CPF/CNPJ ...', preservando a caixa (p/ exibição)."""
    return re.split(
        r"\s*-\s*(?:cpf|cnpj|rg)\b", str(nome or ""), flags=re.IGNORECASE,
    )[0].strip()


def _fmt_data(iso: object) -> str:
    """'2026-06-10' → '10/06/2026'. Mantém o original se não bater."""
    s = str(iso or "")[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def _listar_processos(client: httpx.Client) -> list[dict]:
    procs, page = [], 1
    while True:
        r = client.get("/lawSuit/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        procs.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return procs


def construir_indice_cliente_processo(client: httpx.Client, conn) -> int:
    """Repovoa cliente_processo cruzando /lawSuit/ (clientes) com person_index.

    Casa por CPF (forte) e, na falta, por nome completo normalizado (≥2
    palavras). Idempotente: zera e reconstrói em transação (leitor do poll
    nunca vê índice parcial). Retorna nº de vínculos (telefone, processo).
    """
    # 1. person_index → por_doc / por_nome → telefones do cliente.
    por_doc: dict[str, set[str]] = {}
    por_nome: dict[str, set[str]] = {}
    for _pid, nome, doc, tel in conn.execute(
        "SELECT person_id, nome, document, telefone_chave FROM person_index"
    ).fetchall():
        if not tel:
            continue
        d = _so_digitos(doc)
        if d:
            por_doc.setdefault(d, set()).add(tel)
        nn = normalizar_nome(nome)
        if len(nn.split()) >= 2:
            por_nome.setdefault(nn, set()).add(tel)

    processos = _listar_processos(client)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM cliente_processo")
        n = 0
        for p in processos:
            num = p.get("processNumber") or ""
            if not num:
                continue
            is_secret = 1 if p.get("isSecret") else 0
            lmd = str(p.get("lastMovementDate") or "")[:10]
            for pessoa in p.get("persons") or []:
                if (pessoa.get("personOrigin") or "").strip().lower() != "cliente":
                    continue
                nome = pessoa.get("name")
                doc = extrair_documento(nome)
                telefones: set[str] = set()
                if doc and doc in por_doc:          # CPF é a chave forte
                    telefones = por_doc[doc]
                else:                               # fallback: nome completo
                    telefones = por_nome.get(normalizar_nome(nome), set())
                for tel in telefones:
                    conn.execute(
                        "INSERT OR REPLACE INTO cliente_processo "
                        "(telefone_chave, process_number, is_secret, "
                        "last_movement_date, cliente_nome) VALUES (?, ?, ?, ?, ?)",
                        (tel, num, is_secret, lmd, _nome_limpo(nome)),
                    )
                    n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    logger.info(
        "cliente_processo: %d vínculos telefone↔processo de %d processos",
        n, len(processos),
    )
    return n


def consultar_processos_do_telefone(conn, telefone: object) -> list[dict]:
    """Processos em que este telefone é PARTE CLIENTE. [] se nenhum."""
    chaves = chaves_telefone(telefone)
    if not chaves:
        return []
    ph = ",".join("?" * len(chaves))
    rows = conn.execute(
        f"SELECT DISTINCT process_number, is_secret, last_movement_date, cliente_nome "
        f"FROM cliente_processo WHERE telefone_chave IN ({ph})",
        tuple(chaves),
    ).fetchall()
    return [
        {
            "process_number": r[0],
            "is_secret": bool(r[1]),
            "last_movement_date": r[2],
            "cliente_nome": r[3],
        }
        for r in rows
    ]


def classificar_atendimento(processos: list[dict]) -> dict:
    """Decide a ação a partir dos processos do telefone.

    - sem processo            → 'nao_cadastrado' (não revela, avisa interno)
    - tem algum NÃO sigiloso  → 'responder' (responde os públicos; sigilosos
                                 — se houver — vão escalados, nunca citados)
    - só sigiloso(s)          → 'sigiloso' (neutro ao cliente, escala interno)
    """
    if not processos:
        return {"acao": "nao_cadastrado", "publicos": [], "sigilosos": []}
    publicos = [p for p in processos if not p["is_secret"]]
    sigilosos = [p for p in processos if p["is_secret"]]
    if publicos:
        return {"acao": "responder", "publicos": publicos, "sigilosos": sigilosos}
    return {"acao": "sigiloso", "publicos": [], "sigilosos": sigilosos}


def montar_resposta_cliente(
    publicos: list[dict], movimentos: dict | None = None,
) -> str:
    """Resposta ao cliente verificado (processos NÃO sigilosos).

    ``movimentos`` (opcional) = {process_number: {'data', 'nome'}} do DataJud,
    pra incluir o TEXTO da última movimentação. Sem ele, usa só a data do
    Juridiq. Fato processual puro — sem opinião jurídica.
    """
    movimentos = movimentos or {}
    cab = "Olá! 👋 Sobre o seu processo:" if len(publicos) == 1 else \
        "Olá! 👋 Sobre os seus processos:"
    linhas = [cab]
    for p in publicos:
        num = p["process_number"]
        mov = movimentos.get(num) or {}
        data = mov.get("data") or p.get("last_movement_date") or ""
        texto = mov.get("nome")
        linha = f"• Nº {num}"
        if data:
            linha += f" — última movimentação em {_fmt_data(data)}"
            if texto:
                linha += f": {texto}"
        linhas.append(linha)
    linhas.append(
        "\nNossa equipe está acompanhando de perto. Se precisar de algum "
        "detalhe específico, posso pedir pra um advogado te retornar, tá? 🙏"
    )
    return "\n".join(linhas)


def alerta_sigiloso(nome: object, telefone: object, sigilosos: list[dict]) -> str:
    """Alerta interno (Mario + Hilde): cliente pediu info de processo sigiloso."""
    refs = ", ".join(p["process_number"] for p in sigilosos[:5])
    quem = _nome_limpo(nome) or (sigilosos[0].get("cliente_nome") if sigilosos else "") \
        or "Cliente"
    return (
        "🔒 *Cliente pediu info de processo em SEGREDO de justiça*\n"
        f"{quem} ({telefone}) perguntou sobre o andamento de {refs}. "
        "Não passei nada automático — *respondam manualmente* (você e a Hilde)."
    )


def alerta_nao_identificado(telefone: object, ultima_msg: object) -> str:
    """Alerta interno: alguém perguntou de processo mas o telefone não casa."""
    msg = re.sub(r"\s+", " ", str(ultima_msg or "")).strip()[:160]
    return (
        "❓ *Consulta de processo não identificada*\n"
        f"O número {telefone} perguntou sobre 'meu processo', mas não bate com "
        "nenhum cadastro. Pode ser cliente com número novo.\n"
        f'Mensagem: "{msg}"'
    )


async def ultimo_movimento_datajud(
    process_number: str, datajud_api_key: str,
) -> dict | None:
    """Última movimentação do processo no DataJud (texto+data), ou None.

    Best-effort e fora do event loop (asyncio.to_thread) — só roda quando um
    cliente verificado pergunta. Falha/timeout → None (cai pra data do Juridiq).
    """
    if not datajud_api_key or not process_number:
        return None
    import asyncio

    from noviello_funil.triagem_financeira import consultar_movimentos_datajud

    def _fetch() -> dict | None:
        with httpx.Client(timeout=15.0) as c:
            movs, _ = consultar_movimentos_datajud(c, process_number, datajud_api_key)
        if not movs:
            return None
        m = max(movs, key=lambda x: str(x.get("data") or ""))
        return {
            "data": str(m.get("data") or "")[:10],
            "nome": (str(m.get("nome") or "").strip() or None),
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:  # noqa: BLE001 — best-effort, nunca derruba a resposta
        logger.warning("ultimo_movimento_datajud falhou (%s): %s", process_number, exc)
        return None
