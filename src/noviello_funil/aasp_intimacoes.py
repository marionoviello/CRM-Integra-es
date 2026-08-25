"""Intimações do recorte AASP → andamento manual no Juridiq.

O monitoramento nativo do Juridiq falha em silêncio (status CADASTRADO
defasado — auditoria jun/2026) e NÃO cobre 2ª instância. O recorte da
AASP é fonte independente: este job busca as intimações do dia na API da
AASP, casa o número CNJ com a carteira e grava cada uma como andamento
manual (`POST /lawSuit/movements`, prefixo [AASP], privado — não vai pro
cliente no Jurichat). Intimação nova passa pelo classificador de urgência
das publicações; urgente vira TAREFA de prazo no painel. Intimação de
processo FORA da carteira vira alerta grave (cadastrar o processo!).

Quirks de projeto:
- O schema do item da AASP é DESCONHECIDO (doc não documenta; recorte
  contratado em 24/08/2026, ainda sem publicações). Parser defensivo com
  variantes de nome de campo + payload bruto salvo em `aasp_raw` antes de
  qualquer parse — item que o parser não entender não se perde.
- NÃO usamos `diferencial=true` da AASP: o flag deles é consumido na
  leitura; se o job morrer no meio, perderíamos intimação. Consultamos por
  data explícita (janela de `aasp_dias_janela` dias) e deduplicamos local
  (`aasp_intimacao_vista`).
- Só marcamos como vista DEPOIS do andamento criado (casadas) ou do alerta
  enviado (não-casadas) — falha no meio = retry no próximo run.

Execução: console script ``noviello-aasp`` via systemd timer diário
(10:45 UTC = 07:45 BRT, antes do noviello-publicacoes 08:30 BRT).
"""

import datetime
import hashlib
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

MAX_ITENS = 12                # cap de itens detalhados na mensagem
_TEOR_ANDAMENTO_CHARS = 4000  # teor no andamento do Juridiq
_RESUMO_CHARS = 90

# Nomes de campo do item da AASP. Schema REAL mapeado em 25/08/2026 com a
# 1ª intimação de verdade: numeroUnicoProcesso (nº CNJ mascarado),
# textoPublicacao (teor, texto plano), titulo, cabecalho,
# codigoRelacionamento (id único AASP) e jornal como OBJETO
# {nomeJornal, dataDisponibilizacao_Publicacao, ...} — a data da intimação
# só existe dentro dele. As variantes extras ficam como defesa.
_CAMPOS_PROCESSO = ("numeroUnicoProcesso", "numeroProcesso",
                    "numeroProcessoMascara", "processo", "numProcesso")
_CAMPOS_TEOR = ("textoPublicacao", "conteudo", "despacho", "texto", "teor",
                "publicacao")
_CAMPOS_DATA = ("dataDisponibilizacao", "dataPublicacao", "dataDivulgacao",
                "data")
_CAMPOS_JORNAL = ("jornal", "nomeJornal", "descricaoJornal", "diario",
                  "caderno", "titulo")
_CAMPOS_JORNAL_DICT = ("nomeJornal", "descricaoJornal", "nome")
_CAMPOS_DATA_JORNAL = ("dataDisponibilizacao_Publicacao",
                       "dataDisponibilizacao", "dataPublicacao")


def _so_digitos(s: object) -> str:
    return re.sub(r"\D", "", str(s or ""))


def formatar_cnj(numero: object) -> str:
    """20 dígitos → máscara CNJ. Qualquer outra coisa → ''."""
    d = _so_digitos(numero)
    if len(d) != 20:
        return ""
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def instancia_sugerida(digits: str) -> int | None:
    """Origem 0000 = processo de 2º grau (TJSP/TRF) → instance 2.

    Heurística conservadora: só afirma quando a origem é o marcador
    inequívoco de 2ª instância; caso contrário deixa a API usar a
    instância atual do processo (omitir).
    """
    if len(digits) == 20 and digits[16:] == "0000":
        return 2
    return None


def _limpar_html(html: object) -> str:
    txt = re.sub(r"<[^>]+>", " ", str(html or ""))
    txt = re.sub(r"\s+", " ", txt).strip()
    # Tag removida no meio da frase deixa espaço órfão antes da pontuação
    # ("autora .") — o andamento vai pro painel, então vale o polimento.
    return re.sub(r"\s+([.,;:!?])", r"\1", txt)


def _primeiro_campo(raw: dict, campos: tuple[str, ...]) -> str:
    def _ok(v: object) -> bool:
        # dict/list nunca viram valor — str({...}) no meio do andamento.
        return bool(v) and not isinstance(v, (dict, list)) and str(v).strip()

    for c in campos:
        v = raw.get(c)
        if _ok(v):
            return str(v).strip()
    lower = {str(k).lower(): v for k, v in raw.items()}
    for c in campos:
        v = lower.get(c.lower())
        if _ok(v):
            return str(v).strip()
    return ""


def _extrair_jornal_e_data(raw: dict) -> tuple[str, str]:
    """Nome do jornal + data da intimação, cobrindo o `jornal` OBJETO.

    Data preferida: a do objeto jornal (dataDisponibilizacao_Publicacao,
    ISO → só YYYY-MM-DD). Fallback: campos de data no topo do item.
    """
    jdict = raw.get("jornal")
    jdict = jdict if isinstance(jdict, dict) else {}
    jornal = (
        _primeiro_campo(jdict, _CAMPOS_JORNAL_DICT)
        or _primeiro_campo(raw, _CAMPOS_JORNAL)
    )
    data = (
        _primeiro_campo(jdict, _CAMPOS_DATA_JORNAL)
        or _primeiro_campo(raw, _CAMPOS_DATA)
    )
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", data)
    if m:
        data = m.group(1)
    return jornal, data


def normalizar_item(raw: dict) -> dict:
    """Item bruto da AASP → dict normalizado com chave de dedup.

    chave = sha256(dígitos do processo | data | teor) — estável entre runs
    e independente de campos cosméticos que a AASP mude.
    """
    processo_raw = _primeiro_campo(raw, _CAMPOS_PROCESSO)
    digits = _so_digitos(processo_raw)
    teor = _limpar_html(_primeiro_campo(raw, _CAMPOS_TEOR))
    jornal, data = _extrair_jornal_e_data(raw)
    # codigoRelacionamento (id único da AASP) ancora a dedup quando existe;
    # o hash de conteúdo continua no fallback e protege contra reemissão.
    cod = _primeiro_campo(raw, ("codigoRelacionamento",))
    chave = hashlib.sha256(f"{cod}|{digits}|{data}|{teor}".encode()).hexdigest()
    return {
        "chave": chave,
        "processo_raw": processo_raw,
        "processo_digitos": digits,
        "processo": formatar_cnj(digits) or processo_raw,
        "teor": teor,
        "data": data,
        "jornal": jornal,
    }


def buscar_intimacoes(
    client: httpx.Client, chave_api: str, data: datetime.date,
) -> list[dict]:
    """GET /api/Associado/intimacao/json de UM dia. Levanta em erro.

    `erro: true` com HTTP 200 é o jeito da AASP sinalizar falha (chave
    inválida etc.) — vira exceção pra o run falhar visível no journal,
    nunca "zero intimações" silencioso.
    """
    r = client.get(
        "/api/Associado/intimacao/json",
        params={"chave": chave_api, "data": data.isoformat()},
    )
    r.raise_for_status()
    corpo = r.json()
    if corpo.get("erro"):
        raise RuntimeError(f"AASP retornou erro: {corpo.get('status')!r}")
    return [i for i in (corpo.get("intimacoes") or []) if isinstance(i, dict)]


def salvar_raw(conn, item: dict, data_consulta: str) -> None:
    """Payload bruto → aasp_raw (dedup por hash do JSON canônico)."""
    payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO aasp_raw (hash, payload, data_consulta) "
        "VALUES (?, ?, ?)",
        (h, payload, data_consulta),
    )


def ja_vista(conn, chave: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM aasp_intimacao_vista WHERE chave = ?", (chave,),
    ).fetchone()
    return row is not None


def marcar_vista(conn, chave: str, processo: str, law_suit_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO aasp_intimacao_vista "
        "(chave, processo, law_suit_id) VALUES (?, ?, ?)",
        (chave, processo, law_suit_id),
    )


def indexar_carteira(client: httpx.Client) -> dict[str, str]:
    """GET /lawSuit/ paginado → {dígitos do nº CNJ: lawSuitId}.

    Comparação por dígitos (não máscara): imune a diferença de formatação
    entre AASP e Juridiq. Processo sem número fica de fora (não casável).
    """
    idx: dict[str, str] = {}
    page = 1
    while True:
        r = client.get("/lawSuit/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        for p in data.get("data", []):
            digits = _so_digitos(p.get("processNumber"))
            if digits and p.get("id"):
                idx[digits] = p["id"]
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return idx


def montar_conteudo(item: dict) -> str:
    """Texto do andamento: cabeçalho [AASP] reconhecível + teor."""
    cab = "[AASP] Intimação"
    if item.get("jornal"):
        cab += f" — {item['jornal']}"
    if item.get("data"):
        cab += f" — {item['data']}"
    teor = (item.get("teor") or "").strip()
    if not teor:
        teor = "(sem teor no retorno da AASP — conferir no portal)"
    return f"{cab}\n\n{teor[:_TEOR_ANDAMENTO_CHARS]}"


def criar_andamento(
    client: httpx.Client, law_suit_id: str, content: str,
    instance: int | None = None,
) -> tuple[bool, str]:
    """POST /lawSuit/movements → (ok, detalhe). Não levanta — o caller
    decide (uma falha não pode derrubar as outras intimações)."""
    body: dict = {"lawSuitId": law_suit_id, "content": content}
    if instance:
        body["instance"] = instance
    try:
        r = client.post("/lawSuit/movements", json=body)
    except httpx.HTTPError as exc:
        return False, f"erro_{type(exc).__name__}"
    if r.status_code >= 400:
        return False, f"http_{r.status_code}: {r.text[:400]}"
    return True, "ok"


def montar_mensagem(
    casadas: list[dict], fora_carteira: list[dict], n_tarefas: int,
) -> str | None:
    """Resumo WhatsApp do run. None = nada novo (silêncio)."""
    total = len(casadas) + len(fora_carteira)
    if not total:
        return None
    plural = "intimações novas" if total > 1 else "intimação nova"
    blocos = [f"📨 *AASP: {total} {plural} no recorte*"]
    ok = sum(1 for c in casadas if c.get("andamento_ok"))
    if ok:
        blocos.append(f"✅ {ok} registrada(s) como andamento [AASP] no Juridiq.")
    falhas = len(casadas) - ok
    if falhas:
        blocos.append(
            f"⚠️ {falhas} falhou(aram) ao gravar — nova tentativa no próximo run."
        )
    if n_tarefas:
        blocos.append(
            f"✅ {n_tarefas} virou tarefa no painel (prazo SUGERIDO — confira "
            "a contagem)."
        )

    urgentes = [c for c in casadas if c.get("urgente")]
    if urgentes:
        blocos.append("\n⚠️ *Urgentes:*")
        for c in urgentes[:MAX_ITENS]:
            linha = f"• {c.get('data') or '?'} — {c.get('processo') or '(sem nº)'}"
            motivo = (c.get("motivo") or "").strip()
            prazo = (c.get("prazo") or "").strip()
            detalhe = motivo
            if prazo:
                detalhe = f"{motivo} (prazo: {prazo})" if motivo else f"prazo: {prazo}"
            if detalhe:
                linha += f"\n   _{detalhe[:_RESUMO_CHARS]}_"
            blocos.append(linha)

    if fora_carteira:
        blocos.append(
            "\n🚨 *Fora da carteira* (intimação de processo que NÃO está no "
            "Juridiq — cadastrar):"
        )
        for f in fora_carteira[:MAX_ITENS]:
            ref = f.get("processo") or f.get("jornal") or "(sem referência)"
            blocos.append(f"• {f.get('data') or '?'} — {ref}")

    blocos.append(
        "\nFonte: recorte AASP. Andamentos entram privados (não vão pro "
        "cliente no Jurichat)."
    )
    return "\n".join(blocos)


def processar_novas(
    jq: httpx.Client, conn, novas: list[dict], idx: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Cria o andamento das casadas; separa as fora-da-carteira.

    try/except POR intimação (padrão publicacoes.py): uma falha não
    derruba as demais. Vista só é marcada após 201 — retry natural.
    Fora-da-carteira NÃO é marcada aqui (só depois do alerta enviado,
    no main — senão um crash antes do alerta silenciaria pra sempre).
    """
    casadas, fora = [], []
    for item in novas:
        try:
            law_suit_id = idx.get(item["processo_digitos"] or "—")
            if not law_suit_id:
                fora.append(item)
                continue
            ok, det = criar_andamento(
                jq, law_suit_id, montar_conteudo(item),
                instance=instancia_sugerida(item["processo_digitos"]),
            )
            item["law_suit_id"] = law_suit_id
            item["andamento_ok"] = ok
            if ok:
                marcar_vista(conn, item["chave"], item["processo"], law_suit_id)
            else:
                logger.error(
                    "aasp: andamento falhou processo=%s: %s",
                    item["processo"], det,
                )
            casadas.append(item)
        except Exception as exc:
            logger.exception(
                "aasp: erro na intimação %s: %s", item.get("chave"), exc,
            )
    return casadas, fora


def _criar_tarefas(settings, conn, jq: httpx.Client, casadas: list[dict]) -> int:
    """Urgente + andamento gravado → TAREFA de prazo (reuso prazo_tarefa).

    Idempotência pela MESMA tabela das publicações (tarefa_publicacao),
    com publication_id prefixado "aasp:". Falha em tarefa não derruba
    nada (o alerta é o canal fail-safe).
    """
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

    if not (settings.aasp_criar_tarefa and settings.task_column_id):
        return 0
    hoje = datetime.date.today()
    n = 0
    for item in casadas:
        try:
            if not (deve_criar_tarefa(item) and item.get("andamento_ok")):
                continue
            pid = f"aasp:{item['chave']}"
            if ja_criada(conn, pid):
                continue
            corpo = montar_corpo_tarefa(
                titulo=montar_titulo(
                    item.get("motivo") or "intimação AASP", item["processo"],
                ),
                descricao=montar_descricao(
                    item.get("motivo"), item.get("prazo"), item.get("teor"),
                    item.get("data"),
                ),
                final_date=calcular_prazo_sugerido(
                    item.get("prazo"), item.get("data"), hoje=hoje,
                ),
                initial_date=hoje.isoformat(),
                law_suit_id=item["law_suit_id"],
                column_id=settings.task_column_id,
                priority=settings.task_priority,
            )
            tid, det = criar_tarefa(jq, corpo)
            if not tid:
                logger.error("aasp: tarefa falhou %s: %s", item["processo"], det)
                continue
            try:
                marcar_criada(conn, pid, item["processo"], tid)
            except Exception as exc:
                logger.error(
                    "aasp: tarefa %s CRIADA mas marcar_criada falhou (órfã): %s",
                    tid, exc,
                )
            n += 1
        except Exception as exc:
            logger.exception(
                "aasp: erro na tarefa de %s: %s", item.get("chave"), exc,
            )
    return n


def main() -> int:
    """Entry point do console script ``noviello-aasp``.

    Nada novo no recorte → exit 0 silencioso.
    """
    import asyncio

    from anthropic import AsyncAnthropic

    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario
    from noviello_funil.publicacoes import classificar_urgencia

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    for campo in ("aasp_chave", "juridiq_api_key"):
        if not getattr(settings, campo):
            logger.warning("aasp: %s não configurada — pulando", campo.upper())
            return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("aasp: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    hoje = datetime.date.today()
    aasp = httpx.Client(base_url=settings.aasp_base_url, timeout=30.0)
    brutos: list[tuple[str, dict]] = []
    try:
        for i in range(settings.aasp_dias_janela):
            d = hoje - datetime.timedelta(days=i)
            for raw in buscar_intimacoes(aasp, settings.aasp_chave, d):
                brutos.append((d.isoformat(), raw))
    finally:
        aasp.close()
    logger.info(
        "aasp: %d itens na janela de %d dias",
        len(brutos), settings.aasp_dias_janela,
    )

    conn = connect(settings.database_path)
    run_migrations(conn)
    jq = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        novas = []
        for data_consulta, raw in brutos:
            salvar_raw(conn, raw, data_consulta)
            item = normalizar_item(raw)
            if not ja_vista(conn, item["chave"]):
                novas.append(item)
        logger.info("aasp: %d nova(s)", len(novas))
        if not novas:
            return 0

        idx = indexar_carteira(jq)
        casadas, fora = processar_novas(jq, conn, novas, idx)

        async def _run() -> None:
            anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
            # Classifica TODAS as novas (casadas e fora) de uma vez; o
            # id/resumo que o classificador espera vem do adapter abaixo.
            para_classificar = [
                {**it, "id": it["chave"],
                 "resumo": f"Intimação AASP — {it['jornal'] or 'diário'}"}
                for it in casadas + fora
            ]
            classificadas = await classificar_urgencia(
                anthropic, settings.anthropic_model, para_classificar,
            )
            por_chave = {c["chave"]: c for c in classificadas}
            for it in casadas + fora:
                v = por_chave.get(it["chave"], {})
                it["urgente"] = bool(v.get("urgente"))
                it["motivo"] = v.get("motivo") or ""
                it["prazo"] = v.get("prazo") or ""

            n_tarefas = 0
            try:
                n_tarefas = _criar_tarefas(settings, conn, jq, casadas)
            except Exception as exc:
                logger.exception(
                    "aasp: criação de tarefas falhou (alerta segue): %s", exc,
                )

            texto = montar_mensagem(casadas, fora, n_tarefas)
            if texto is None:
                return
            logger.info("aasp:\n%s", texto)
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
            # Alerta enviado → agora sim as fora-da-carteira estão tratadas.
            for it in fora:
                marcar_vista(conn, it["chave"], it["processo"], "")

        asyncio.run(_run())
    finally:
        jq.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
