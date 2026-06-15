"""Alerta de publicações URGENTES não tratadas no Juridiq.

O Juridiq já manda TODAS as movimentações no WhatsApp do Mario. Mandar
a lista inteira de novo é ruído duplicado. Então este job faz o que o
Juridiq não faz: lê o TEOR de cada publicação ainda não tratada,
classifica com o Claude (urgente x rotina) e só fura o silêncio quando
algo exige ação num prazo (intimação, audiência, sentença, citação,
penhora/leilão). Nada urgente → não envia nada.

É um realce sobre o canal primário do Juridiq, não um segundo canal de
tudo. Fail-safe: se a classificação falhar, a publicação entra no
alerta marcada "conferir" — nunca suprime o que não entendeu.

Execução: console script ``noviello-publicacoes`` via systemd timer
diário (08h30 BRT). Quirk: o filtro ``isHandled=false`` do
GET /publication/ FUNCIONA (≠ /person/); o ``title`` é genérico
("Movimentação de processo") — o sinal de urgência está no ``content``.
"""

import asyncio
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

# Cap de itens detalhados na mensagem (o total sempre aparece no topo).
MAX_ITENS = 12
_RESUMO_CHARS = 90
# Teor enviado ao Claude por publicação. O tipo do ato + dispositivo
# cabem com folga; truncar segura custo e latência.
_TEOR_CHARS = 700

# processNumber quando o Juridiq não identifica o processo na publicação.
_SEM_PROCESSO = ("", "não encontrado", "nao encontrado")

_CLASSIFICADOR_SYSTEM = """\
Você assiste um advogado brasileiro. Para cada publicação de diário \
oficial / movimentação processual, decida se ela é URGENTE (exige ação \
do advogado dentro de um prazo, ou comunica risco/constrição) ou ROTINA \
(mero andamento, não exige ato imediato).

URGENTE:
- Intimação que abre prazo (manifestação, contestação, impugnação, \
recurso, embargos, cumprimento de sentença, emenda à inicial)
- Audiência ou perícia designada (data marcada)
- Sentença ou acórdão (abre prazo recursal)
- Decisão sobre liminar/tutela, ou despacho que determina providência \
da parte com prazo
- Citação (cite-se)
- Penhora, bloqueio/Sisbajud, arresto, sequestro, leilão/praça/hasta

ROTINA:
- Mero expediente, juntada de petição, vista ao MP/perito/contadoria
- Ciência de ato sem prazo para a parte, conclusão, remessa, certidão
- Publicação meramente informativa
- Homologação/decisão que não exige ato (acordo já cumprido, baixa)

Responda APENAS com um array JSON, um objeto por publicação, na MESMA \
ordem recebida:
[{"id":"<id>","urgente":true|false,"motivo":"<= 8 palavras","prazo":\
"<prazo se houver, ex: 15 dias / 20/06; senão vazio>"}]"""


def _limpar_teor(html: str) -> str:
    """HTML do teor → texto plano colapsado."""
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", txt).strip()


def _data_curta(raw: object) -> str:
    """'11/06/2026' ou '2026-06-11[...]' → '11/06'. Vazio → '?'."""
    s = str(raw or "").strip()
    if not s:
        return "?"
    m = re.match(r"^(\d{2})/(\d{2})/\d{4}", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.match(r"^\d{4}-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(2)}/{m.group(1)}"
    return s


def _data_ordenavel(raw: object) -> str:
    """Chave de ordenação ISO-like; datas não parseáveis vão pro fim."""
    s = str(raw or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return "9999-99-99"


def buscar_nao_tratadas(client: httpx.Client) -> list[dict]:
    """GET /publication/?isHandled=false paginado, normalizado.

    Cada item: {id, processo, resumo, data, diario, teor}.
    """
    brutas, page = [], 1
    while True:
        resp = client.get(
            "/publication/",
            params={"page": page, "limit": 100, "isHandled": "false"},
        )
        resp.raise_for_status()
        data = resp.json()
        brutas.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1

    pubs = []
    for p in brutas:
        processo = str(p.get("processNumber") or "").strip()
        if processo.lower() in _SEM_PROCESSO:
            processo = ""
        diario = (p.get("officialDiary") or "").strip()
        # title traz o tipo do ato; descriptionSmall costuma só repetir o
        # diário (verificado 2026-06-11) — candidato igual ao diário cai.
        resumo = next(
            (
                c.strip()
                for c in (p.get("title"), p.get("descriptionSmall"))
                if c and c.strip() and c.strip() != diario
            ),
            "",
        )
        pubs.append({
            "id": p.get("id") or "",
            "processo": processo,
            # lawsuitId (minúsculo) liga a publicação ao processo p/ a tarefa
            # (1.1) — vem direto na publicação, sem resolver processNumber.
            "lawsuitId": p.get("lawsuitId") or "",
            "resumo": resumo,
            "data": p.get("publicationDate") or p.get("availabilityDate"),
            "diario": diario,
            "teor": _limpar_teor(p.get("content") or ""),
        })
    return pubs


def _parse_veredictos(raw: str, pubs: list[dict]) -> list[dict]:
    """Mescla o veredicto do Claude em cada publicação. Fail-safe urgente.

    - JSON inválido → TODAS viram urgentes ("conferir").
    - publicação sem veredicto no retorno → urgente (fail-safe).
    Nunca suprime silenciosamente uma publicação que não foi classificada.
    """
    por_id: dict[str, dict] = {}
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        text = fenced.group(1).strip()
    i, j = text.find("["), text.rfind("]")
    if i != -1 and j > i:
        try:
            for v in json.loads(text[i : j + 1]):
                if isinstance(v, dict) and v.get("id"):
                    por_id[str(v["id"])] = v
        except (json.JSONDecodeError, TypeError):
            por_id = {}

    out = []
    for p in pubs:
        v = por_id.get(p["id"])
        if v is None:
            out.append({**p, "urgente": True,
                        "motivo": "não foi possível classificar — conferir",
                        "prazo": ""})
        else:
            out.append({**p, "urgente": bool(v.get("urgente")),
                        "motivo": (v.get("motivo") or "").strip(),
                        "prazo": (v.get("prazo") or "").strip()})
    return out


async def classificar_urgencia(
    anthropic_client, model: str, pubs: list[dict],
) -> list[dict]:
    """Classifica cada publicação como urgente/rotina via Claude.

    Retorna a lista de pubs enriquecida com {urgente, motivo, prazo}.
    Erro de API → fail-safe: todas urgentes ("conferir").
    """
    if not pubs:
        return []
    itens = [
        {"id": p["id"], "tipo": p["resumo"] or "(movimentação)",
         "teor": p["teor"][:_TEOR_CHARS]}
        for p in pubs
    ]
    user_text = (
        "Classifique as publicações abaixo. Responda só o array JSON.\n\n"
        + json.dumps(itens, ensure_ascii=False)
    )
    try:
        resp = await anthropic_client.messages.create(
            model=model,
            max_tokens=1024,
            system=_CLASSIFICADOR_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
        raw = resp.content[0].text
    except Exception as exc:
        logger.exception("publicacoes: classificação falhou: %s", exc)
        raw = ""  # fail-safe: _parse marca tudo como urgente
    return _parse_veredictos(raw, pubs)


def montar_mensagem(urgentes: list[dict], n_tarefas: int = 0) -> str:
    """Mensagem WhatsApp-ready com SÓ as publicações urgentes.

    ``n_tarefas`` (1.1): quantas viraram TAREFA no painel — vira uma linha de
    confirmação no topo (os ✅ não precisam mais ser anotados à mão)."""
    n = len(urgentes)
    plural = (
        "publicações que parecem urgentes" if n > 1
        else "publicação que parece urgente"
    )
    cab = f"⚠️ *{n} {plural}*\n"
    if n_tarefas:
        cab += (
            f"✅ {n_tarefas} virou tarefa no painel (prazo SUGERIDO — confira a "
            "contagem).\n"
        )

    ordenadas = sorted(urgentes, key=lambda p: _data_ordenavel(p.get("data")))
    linhas = []
    for p in ordenadas[:MAX_ITENS]:
        ref = p["processo"] or p["diario"] or "(sem referência)"
        linha = f"• {_data_curta(p.get('data'))} — {ref}"
        motivo = (p.get("motivo") or "").strip()
        prazo = (p.get("prazo") or "").strip()
        detalhe = motivo
        if prazo:
            detalhe = f"{motivo} (prazo: {prazo})" if motivo else f"prazo: {prazo}"
        if detalhe:
            linha += f"\n   _{detalhe[:_RESUMO_CHARS]}_"
        linhas.append(linha)

    extra = ""
    if n > MAX_ITENS:
        extra = f"\n… e mais {n - MAX_ITENS}.\n"
    rodape = (
        "\nO Juridiq segue mandando todas as movimentações; aqui vão só "
        "as que parecem ter prazo. Trate no painel pra silenciar."
    )
    return cab + "\n" + "\n".join(linhas) + "\n" + extra + rodape


def _criar_tarefas_de_prazo(settings, urgentes: list[dict]) -> int:
    """Cria TAREFA no Juridiq pras publicações urgentes com processo (1.1).

    Idempotente por publication_id. Gated por publicacoes_criar_tarefa +
    task_column_id. Erro por publicação não derruba as outras. Retorna nº
    criadas. (sync — chamado dentro do _run; são poucas tarefas.)
    """
    import datetime

    from noviello_funil.db import connect, run_migrations
    from noviello_funil.prazo_tarefa import (
        calcular_prazo_sugerido,
        criar_tarefa,
        deve_criar_tarefa,
        ja_criada,
        marcar_criada,
        montar_corpo_tarefa,
        montar_descricao,
        montar_titulo,
    )

    if not (settings.publicacoes_criar_tarefa and settings.task_column_id):
        return 0

    conn = connect(settings.database_path)
    run_migrations(conn)
    cli = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    hoje = datetime.date.today().isoformat()
    n = 0
    try:
        for pub in urgentes:
            if not (deve_criar_tarefa(pub) and pub.get("lawsuitId")):
                continue
            if ja_criada(conn, pub["id"]):
                continue
            corpo = montar_corpo_tarefa(
                titulo=montar_titulo(
                    pub.get("motivo") or pub.get("resumo"), pub["processo"],
                ),
                descricao=montar_descricao(
                    pub.get("motivo"), pub.get("prazo"), pub.get("teor"),
                    pub.get("data"),
                ),
                final_date=calcular_prazo_sugerido(pub.get("prazo"), pub.get("data")),
                initial_date=hoje,
                law_suit_id=pub["lawsuitId"],
                column_id=settings.task_column_id,
                priority=settings.task_priority,
            )
            tid, det = criar_tarefa(cli, corpo)
            if tid:
                marcar_criada(conn, pub["id"], pub["processo"], tid)
                n += 1
            else:
                logger.error(
                    "publicacoes: criar tarefa falhou pub=%s: %s", pub["id"], det,
                )
    finally:
        cli.close()
        conn.close()
    return n


def main() -> int:
    """Entry point do console script ``noviello-publicacoes``.

    Zero publicações urgentes → exit 0 silencioso (nenhum envio).
    """
    from anthropic import AsyncAnthropic

    from noviello_funil.config import Settings
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("publicacoes: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("publicacoes: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        pubs = buscar_nao_tratadas(client)
    finally:
        client.close()

    logger.info("publicacoes: %d não tratada(s)", len(pubs))
    if not pubs:
        return 0

    async def _run() -> None:
        anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
        classificadas = await classificar_urgencia(
            anthropic, settings.anthropic_model, pubs,
        )
        urgentes = [p for p in classificadas if p["urgente"]]
        logger.info(
            "publicacoes: %d urgente(s) de %d não tratada(s)",
            len(urgentes), len(pubs),
        )
        if not urgentes:
            return

        # 1.1: publicação urgente com processo vira TAREFA rastreável no painel
        # (não some no alerta). Gated/idempotente — ver _criar_tarefas_de_prazo.
        n_tarefas = _criar_tarefas_de_prazo(settings, urgentes)
        if n_tarefas:
            logger.info("publicacoes: %d tarefa(s) de prazo criada(s)", n_tarefas)

        texto = montar_mensagem(urgentes, n_tarefas)
        logger.info("publicacoes:\n%s", texto)
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

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
