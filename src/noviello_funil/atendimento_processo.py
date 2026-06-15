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

Vínculo telefone→processo — AUTENTICAÇÃO POR person_id (id da ficha do
Juridiq, chave forte e única). O ``/lawSuit/`` traz cada parte como
``{id, name, personOrigin}``; o ``id`` do Cliente é o mesmo person_id que o
``/person/`` expõe e que o ``person_index`` mapeia pra telefone. Ligamos por
esse id — NÃO por nome (homônimo "João Silva" vazaria o processo de um pro
telefone do outro; a revisão adversarial de 15/jun pegou isso) e sem
depender de CPF (que o ``/lawSuit/`` não expõe no cliente — verificado
15/jun: campos só ``id/name/personOrigin``). CPF no nome, quando aparece,
serve de reforço. Cliente cuja ficha não tem telefone → atendimento humano.
Se um telefone resolve pra DUAS fichas distintas (contato compartilhado),
não autenticamos: escalamos. Índice ``cliente_processo`` repovoado de
madrugada junto do person_index (depende dele estar fresco).

Regra de marca: o bot fala "nossa equipe" / "um advogado", NUNCA "Dr.
Mario". Segredo, ambíguo e não-identificado vão SÓ pro canal interno.
"""

import logging
import re

import httpx

from noviello_funil.person_index import chaves_telefone

logger = logging.getLogger(__name__)

# Intenção "como está meu processo?". Exige uma PISTA DE STATUS perto de uma
# referência a processo/ação/caso — e uma guarda que derruba quem quer ABRIR
# um caso (lead novo), pra não sequestrar o funil.
_PROC = r"(processos?|a[çc][õo]es|a[çc][ãa]o|caso|invent[áa]rio|usucapi[ãa]o)"
_CUE = (
    r"(como\s+(est|t|and|v|sa)\w+|andamento|novidades?|not[íi]cias?|"
    r"atualiza[çc]\w+|status|movimenta[çc]\w+|posi[çc][ãa]o|"
    r"saiu|sa[íi]u|senten[çc]a|audi[êe]ncia|despacho|parad[oa]|"
    r"andou|parou|decis[ãa]o)"
)
_SEP = r"[^?.!\n]{0,18}"   # mesma frase: '?' '.' '!' e quebra são barreiras
_PERGUNTAS_STATUS = [
    rf"{_CUE}{_SEP}{_PROC}",
    rf"{_PROC}{_SEP}{_CUE}",
]
_RX_STATUS = [re.compile(p, re.IGNORECASE) for p in _PERGUNTAS_STATUS]

# Guarda: quem QUER ABRIR/contratar não é consulta de status (lead novo).
_RX_ABERTURA = re.compile(
    r"\b(quero|queria|gostaria|preciso|pretendo|posso|tenho\s+que)\b"
    r"[^?.!\n]{0,20}\b(abrir|entrar|processar|mover|ajuizar|propor|"
    r"dar\s+entrada|fazer)\b"
    r"|\bquanto\s+custa\b|\bcomo\s+funciona\b|\bcomo\s+fa[çc]o\s+pra\b"
    r"|\bvoc[êe]s?\s+(fazem|pegam|atendem|trabalham|cuidam)\b",
    re.IGNORECASE,
)

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
    """True se a mensagem é uma pergunta sobre o andamento de um processo.

    Conservador de propósito: quem quer ABRIR um caso (lead novo) NÃO casa,
    pra não tirar o lead do funil. Falso-negativo é barato (cai no funil e o
    Signal 1.5 ainda avisa que um cliente conhecido voltou).
    """
    t = str(texto or "")
    if _RX_ABERTURA.search(t):
        return False
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

    AUTENTICAÇÃO POR person_id: o ``id`` da parte Cliente no /lawSuit/ é o
    person_id da ficha; ligamos o processo aos telefones dessa ficha. CPF no
    nome (raro no cliente) entra como reforço, exigindo CPF único no índice.
    Sem id reconhecível nem CPF → nenhum vínculo automático (cai em humano).
    Idempotente em transação. Retorna nº de vínculos (telefone, processo).
    """
    doc_para_pid: dict[str, set[str]] = {}   # cpf → person_id(s)
    pid_tels: dict[str, set[str]] = {}       # person_id → telefones
    pid_nome: dict[str, str] = {}            # person_id → nome (exibição)
    for pid, nome, doc, tel in conn.execute(
        "SELECT person_id, nome, document, telefone_chave FROM person_index"
    ).fetchall():
        if tel:
            pid_tels.setdefault(pid, set()).add(tel)
        d = _so_digitos(doc)
        if d:
            doc_para_pid.setdefault(d, set()).add(pid)
        if nome:
            pid_nome[pid] = _nome_limpo(nome)

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
                # CHAVE FORTE: o id da parte Cliente é o person_id da ficha.
                pid = pessoa.get("id")
                match_tipo = "person_id"
                if not pid or pid not in pid_tels:
                    # Sem id reconhecível: tenta CPF no nome (raro), único.
                    doc = extrair_documento(pessoa.get("name"))
                    pids = doc_para_pid.get(doc) if doc else None
                    if not pids or len(pids) != 1:
                        continue               # sem id nem CPF único → humano
                    pid = next(iter(pids))
                    match_tipo = "cpf"
                nome_disp = pid_nome.get(pid) or _nome_limpo(pessoa.get("name"))
                for tel in pid_tels.get(pid, set()):
                    conn.execute(
                        "INSERT OR REPLACE INTO cliente_processo "
                        "(telefone_chave, person_id, process_number, is_secret, "
                        "last_movement_date, cliente_nome, match_tipo) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (tel, pid, num, is_secret, lmd, nome_disp, match_tipo),
                    )
                    n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    logger.info(
        "cliente_processo: %d vínculos (CPF) telefone↔processo de %d processos",
        n, len(processos),
    )
    return n


def consultar_processos_do_telefone(conn, telefone: object) -> list[dict]:
    """Processos em que este telefone é PARTE CLIENTE. [] se nenhum.

    Cada item carrega o ``person_id`` — o classificar detecta telefone que
    bate com mais de uma ficha (ambíguo) e recusa autenticar.
    """
    chaves = chaves_telefone(telefone)
    if not chaves:
        return []
    ph = ",".join("?" * len(chaves))
    rows = conn.execute(
        f"SELECT DISTINCT person_id, process_number, is_secret, last_movement_date, "
        f"cliente_nome FROM cliente_processo WHERE telefone_chave IN ({ph})",
        tuple(chaves),
    ).fetchall()
    return [
        {
            "person_id": r[0],
            "process_number": r[1],
            "is_secret": bool(r[2]),
            "last_movement_date": r[3],
            "cliente_nome": r[4],
        }
        for r in rows
    ]


def classificar_atendimento(processos: list[dict]) -> dict:
    """Decide a ação a partir dos processos do telefone.

    - sem processo               → 'nao_cadastrado' (não revela, avisa interno)
    - bate com 2+ fichas         → 'ambiguo' (não autentica, escala humano)
    - tem algum NÃO sigiloso     → 'responder' (responde os públicos; sigilosos,
                                    se houver, vão escalados — nunca citados)
    - só sigiloso(s)             → 'sigiloso' (neutro ao cliente, escala interno)
    """
    if not processos:
        return {"acao": "nao_cadastrado", "publicos": [], "sigilosos": []}
    pids = {p.get("person_id") for p in processos}
    if len(pids) > 1:   # telefone aponta pra mais de uma pessoa → inseguro
        return {"acao": "ambiguo", "publicos": [], "sigilosos": []}
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


def alerta_ambiguo(telefone: object, ultima_msg: object) -> str:
    """Alerta interno: o telefone bate com mais de uma ficha (homônimo)."""
    msg = re.sub(r"\s+", " ", str(ultima_msg or "")).strip()[:160]
    return (
        "⚠️ *Consulta de processo — telefone AMBÍGUO*\n"
        f"O número {telefone} bate com MAIS DE UM cadastro (homônimo ou contato "
        "compartilhado). Não autentiquei nem revelei nada — confirmem quem é "
        f'antes de responder.\nMensagem: "{msg}"'
    )


async def ultimo_movimento_datajud(
    process_number: str, datajud_api_key: str,
) -> dict | None:
    """Última movimentação do processo no DataJud (texto+data), ou None.

    Best-effort e fora do event loop (asyncio.to_thread) — só roda quando um
    cliente verificado pergunta. Falha/timeout → None (cai pra data do Juridiq).
    Timeout curto (8s): a degradação pra data-only já é graciosa.
    """
    if not datajud_api_key or not process_number:
        return None
    import asyncio

    from noviello_funil.triagem_financeira import consultar_movimentos_datajud

    def _fetch() -> dict | None:
        with httpx.Client(timeout=8.0) as c:
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
