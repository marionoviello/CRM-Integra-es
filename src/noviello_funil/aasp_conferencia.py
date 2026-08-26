"""Conferência tripla: e-mails (AASP + Recorte OAB) × integração × painel.

O ``aasp_intimacoes`` grava as intimações da API AASP no Juridiq, mas
"total segurança" pede fonte de verificação INDEPENDENTE: os e-mails que
a AASP (intimacoes@info.aasp.org.br) e o Recorte Digital da OAB/SP
(oabsp@recortedigital.adv.br) mandam todo dia listam as mesmas
publicações por outro canal. Este job lê esses e-mails via IMAP (mesma
conta/senha de app do detector_bounce), extrai TODOS os números CNJ e
cruza com DUAS referências:

1. o que a nossa integração processou (``aasp_intimacao_vista``, SQLite);
2. o que o painel do Juridiq REALMENTE tem (``GET /publication/?start&end``)
   — 3ª fonte, que o item 1 não substitui: a tabela local registra o que
   o NOSSO job achou que gravou, não o que o Juridiq recebeu.

A comparação é assimétrica de propósito: só interessa o que chegou por
e-mail e não chegou ao destino. AASP e Recorte OAB têm coberturas
legitimamente diferentes (recorte AASP × DJE-SP pela OAB), então exigir
que as três fontes coincidam produziria divergência todo santo dia.

Resultado no WhatsApp:
- dia COM intimação e tudo capturado → confirmação positiva curta
  ("N números conferidos ✓") — o silêncio nunca é ambíguo num dia com
  publicação;
- número do e-mail fora da integração E fora do painel → alerta 🚨 (é o
  caso que pode passar batido de verdade);
- número fora da integração mas presente no painel → aviso ⚠️ (o
  processo não sumiu, só não veio pelo nosso job);
- nenhum e-mail com intimação na janela → silêncio (exit 0).

Sem ``JURIDIQ_API_KEY``, ou com a API fora do ar, a 3ª fonte fica de
fora e o job volta ao comportamento de 2 fontes — nunca o contrário
(painel indisponível jamais vira "sumiu do Juridiq").

Limite conhecido (v1): a conferência é por NÚMERO de processo, não por
quantidade de atos — duas intimações do mesmo processo no mesmo dia com
uma só capturada não disparam alerta.

Execução: console script ``noviello-aasp-conferencia`` via timer diário
18:15 UTC = 15:15 BRT (depois da 2ª rodada da API às 14h BRT).
"""

import email as emaillib
import imaplib
import logging
import re
from email.header import decode_header, make_header

logger = logging.getLogger(__name__)

MAX_ITENS = 12

# Nº CNJ mascarado (NNNNNNN-DD.AAAA.J.TR.OOOO). Só a forma mascarada:
# procurar 20 dígitos soltos em e-mail geraria falso positivo (códigos de
# barras, protocolos).
_CNJ_RE = re.compile(r"\b(\d{7})-(\d{2})\.(\d{4})\.(\d)\.(\d{2})\.(\d{4})\b")


def _mascarar(digits: str) -> str:
    d = digits
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def extrair_numeros_cnj(texto: object) -> set[str]:
    """Todos os números CNJ (como 20 dígitos) de um texto. Dedup."""
    return {"".join(m.groups()) for m in _CNJ_RE.finditer(str(texto or ""))}


def fonte_do_remetente(remetente: str, remetentes_cfg: str) -> str | None:
    """Rótulo da fonte a partir do From do e-mail; None se não é fonte
    monitorada. AASP ganha rótulo próprio; o resto vira "Recorte OAB"
    (ou o domínio, se um dia entrar um 3º remetente)."""
    rem = (remetente or "").lower()
    for cfg in (r.strip().lower() for r in remetentes_cfg.split(",") if r.strip()):
        if cfg in rem:
            if "aasp" in cfg:
                return "AASP"
            if "recortedigital" in cfg or "oab" in cfg:
                return "Recorte OAB"
            return cfg
    return None


def numeros_processados(conn, dias: int) -> set[str]:
    """Números (20 dígitos) processados pela integração na janela."""
    rows = conn.execute(
        "SELECT processo FROM aasp_intimacao_vista "
        "WHERE criado_em >= datetime('now', ?)",
        (f"-{int(dias)} days",),
    ).fetchall()
    nums = set()
    for (proc,) in rows:
        d = re.sub(r"\D", "", str(proc or ""))
        if len(d) == 20:
            nums.add(d)
    return nums


def numeros_no_painel(
    client, dias: int, hoje: object = None,
) -> set[str] | None:
    """Números (20 dígitos) que o painel do Juridiq REALMENTE tem na janela.

    3ª fonte, independente do nosso SQLite: ``aasp_intimacao_vista`` só
    registra o que a NOSSA integração achou que gravou; isto pergunta ao
    Juridiq o que ele de fato tem, via ``GET /publication/?start&end``
    (paginado). Quando o ``processNumber`` vem vazio ou "Não encontrado",
    o número ainda está no teor — daí o fallback pelo regex CNJ.

    Erro/indisponibilidade → ``None`` (≠ ``set()``): painel não
    consultado nunca pode virar "sumiu do Juridiq".
    """
    import datetime

    fim = hoje or datetime.datetime.now(datetime.UTC).date()
    inicio = fim - datetime.timedelta(days=int(dias))
    nums: set[str] = set()
    page = 1
    try:
        while True:
            resp = client.get(
                "/publication/",
                params={
                    "page": page,
                    "limit": 100,
                    "start": inicio.isoformat(),
                    "end": fim.isoformat(),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            for pub in data.get("data", []):
                digits = re.sub(r"\D", "", str(pub.get("processNumber") or ""))
                if len(digits) == 20:
                    nums.add(digits)
                    continue
                nums |= extrair_numeros_cnj(
                    f"{pub.get('title') or ''} {pub.get('content') or ''}",
                )
            if page >= int(data.get("totalPages") or 1):
                break
            page += 1
    except Exception as exc:
        logger.warning(
            "aasp_conferencia: painel Juridiq indisponível (%s) — 3ª fonte "
            "fica de fora desta rodada", exc,
        )
        return None
    return nums


def fonte_painel(settings) -> set[str] | None:
    """3ª fonte a partir das settings. ``None`` = painel não consultado
    (sem ``JURIDIQ_API_KEY`` o job degrada pro modo 2 fontes de antes)."""
    if not getattr(settings, "juridiq_api_key", ""):
        logger.info(
            "aasp_conferencia: JURIDIQ_API_KEY ausente — conferência sem a "
            "3ª fonte (painel)",
        )
        return None
    import httpx

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    try:
        return numeros_no_painel(client, settings.aasp_conferencia_dias)
    finally:
        client.close()


def conferir(
    achados: dict[str, dict],
    processados: set[str],
    no_painel: set[str] | None = None,
) -> list[dict]:
    """Números dos e-mails que a integração NÃO processou (faltantes).

    ``no_painel`` é a 3ª fonte (o que o Juridiq tem). Cada faltante sai
    com a chave ``no_painel``: ``True`` (está no painel, só não veio pela
    integração), ``False`` (não está em lugar nenhum) ou ``None`` (painel
    não consultado).
    """
    faltantes = []
    for digits, info in sorted(achados.items()):
        if digits in processados:
            continue
        faltantes.append({
            "processo": _mascarar(digits),
            "fonte": info.get("fonte") or "?",
            "data": info.get("data") or "?",
            "no_painel": None if no_painel is None else digits in no_painel,
        })
    return faltantes


def _linhas_itens(itens: list[dict]) -> list[str]:
    linhas = [
        f"• {f['processo']} — {f['fonte']} ({f['data']})"
        for f in itens[:MAX_ITENS]
    ]
    if len(itens) > MAX_ITENS:
        linhas.append(f"… e mais {len(itens) - MAX_ITENS}.")
    return linhas


def montar_mensagem(faltantes: list[dict], total: int) -> str | None:
    """Mensagem WhatsApp. None = nenhum e-mail com intimação (silêncio).

    Dois baldes de severidade: o que não está NEM na integração NEM no
    painel do Juridiq (🚨, publicação em risco de passar batida) e o que
    está no painel mas fora da integração (⚠️, o processo não sumiu).
    """
    if not total:
        return None
    graves = [f for f in faltantes if not f.get("no_painel")]
    brandos = [f for f in faltantes if f.get("no_painel")]

    # Balde brando = só a contagem. O Juridiq já manda essas movimentações
    # no WhatsApp por conta própria (ver publicacoes.py): repetir a lista
    # aqui é o ruído duplicado que aquele job existe pra evitar. O detalhe
    # item a item vai pro log, pra investigar quando interessar.
    aviso = (
        f"\n⚠️ Outro(s) {len(brandos)} número(s) não passaram pela integração "
        "AASP→Juridiq, mas ESTÃO no painel (nada se perdeu — detalhe no log)."
    ) if brandos else ""

    if not graves:
        estado = "presentes no Juridiq ✓" if brandos else "capturados pela integração ✓"
        return (
            f"🔎 *Conferência de intimações*: {total} número(s) nos e-mails "
            f"AASP/Recorte OAB — todos {estado}{aviso}"
        )

    blocos: list[str] = []
    if graves:
        confirmado = any(f.get("no_painel") is False for f in graves)
        blocos.append("🚨 *Conferência de intimações: DIVERGÊNCIA*")
        blocos.append(
            f"Dos {total} número(s) nos e-mails, {len(graves)} NÃO passou(aram) "
            "pela integração AASP→Juridiq"
            + (" nem aparece(m) no painel do Juridiq:" if confirmado else ":")
        )
        blocos.append("")
        blocos.extend(_linhas_itens(graves))
        blocos.append(
            "\nPossíveis causas: atraso da carga da AASP (a rodada das 14h pega), "
            "publicação fora do recorte AASP (ex.: DJU/federal, só no Recorte "
            "OAB) ou falha do job — conferir no painel do Juridiq."
        )
    if brandos:
        blocos.append(aviso)
    return "\n".join(blocos)


def _decode(valor: str) -> str:
    try:
        return str(make_header(decode_header(valor or "")))
    except Exception:
        return valor or ""


def _corpo_texto(msg) -> str:
    """Corpo do e-mail como texto (text/plain + text/html sem tags)."""
    partes = []
    payloads = msg.walk() if msg.is_multipart() else [msg]
    for p in payloads:
        ct = p.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            payload = p.get_payload(decode=True)
            if not payload:
                continue
            texto = payload.decode(errors="ignore")
            if ct == "text/html":
                texto = re.sub(r"<[^>]+>", " ", texto)
            partes.append(texto)
        except Exception:
            continue
    return "\n".join(partes)


def buscar_numeros_emails(
    *, host: str, port: int, user: str, password: str,
    remetentes: str, dias: int,
) -> dict[str, dict]:
    """IMAP → {digits20: {fonte, data}} das fontes monitoradas na janela.

    Varre INBOX e a pasta \\All (arquivados contam — o Mario organiza a
    caixa). Erro de IMAP → dict vazio com warning (o job não pode
    inventar divergência por indisponibilidade de e-mail).
    """
    import datetime

    desde = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=dias)
    ).strftime("%d-%b-%Y")
    achados: dict[str, dict] = {}
    try:
        M = imaplib.IMAP4_SSL(host, port)
        M.login(user, password)
    except Exception as exc:
        logger.warning("aasp_conferencia: IMAP indisponível (%s) — no-op", exc)
        return {}
    try:
        pastas = ["INBOX"]
        try:
            for raw in M.list()[1]:
                linha = raw.decode(errors="ignore") if isinstance(raw, bytes) else raw
                if "\\All" in linha:
                    nome = linha.split(' "/" ')[-1].strip().strip('"')
                    pastas.append(nome)
        except Exception:
            pastas += ["[Gmail]/Todos os e-mails", "[Gmail]/All Mail"]

        vistos_ids: set[bytes] = set()
        for pasta in pastas:
            try:
                if M.select(f'"{pasta}"', readonly=True)[0] != "OK":
                    continue
                typ, dados = M.search(None, "SINCE", desde)
                if typ != "OK":
                    continue
                for num in dados[0].split():
                    typ, msg_data = M.fetch(num, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw_bytes = msg_data[0][1]
                    msg = emaillib.message_from_bytes(raw_bytes)
                    mid = (msg.get("Message-ID") or "").encode()
                    if mid and mid in vistos_ids:
                        continue          # mesma msg no INBOX e no All Mail
                    if mid:
                        vistos_ids.add(mid)
                    fonte = fonte_do_remetente(
                        _decode(msg.get("From", "")), remetentes,
                    )
                    if not fonte:
                        continue
                    data_email = _decode(msg.get("Date", ""))[:22]
                    for digits in extrair_numeros_cnj(_corpo_texto(msg)):
                        achados.setdefault(
                            digits, {"fonte": fonte, "data": data_email},
                        )
            except Exception as exc:
                logger.debug("aasp_conferencia: pasta %s falhou: %s", pasta, exc)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return achados


def main() -> int:
    """Entry point do console script ``noviello-aasp-conferencia``."""
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.smtp_user or not settings.smtp_password:
        logger.warning("aasp_conferencia: SMTP/IMAP não configurado — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning(
            "aasp_conferencia: MARIO_CONVERSATION_ID não configurado — pulando",
        )
        return 0

    achados = buscar_numeros_emails(
        host=settings.imap_host,
        port=settings.imap_port,
        user=settings.smtp_user,
        password=settings.smtp_password,
        remetentes=settings.aasp_conferencia_remetentes,
        dias=settings.aasp_conferencia_dias,
    )
    logger.info(
        "aasp_conferencia: %d número(s) nos e-mails da janela", len(achados),
    )

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        processados = numeros_processados(
            conn, settings.aasp_conferencia_dias + 1,
        )
    finally:
        conn.close()

    no_painel = fonte_painel(settings)
    if no_painel is not None:
        logger.info(
            "aasp_conferencia: %d publicação(ões) no painel do Juridiq na janela",
            len(no_painel),
        )

    faltantes = conferir(achados, processados, no_painel)

    # O WhatsApp só recebe a contagem dos que estão no painel; o detalhe
    # fica aqui pra quando o Mario quiser ver a cobertura do recorte AASP.
    for f in faltantes:
        if f.get("no_painel"):
            logger.info(
                "aasp_conferencia: %s (%s, %s) está no painel do Juridiq mas "
                "não passou pela integração AASP",
                f["processo"], f["fonte"], f["data"],
            )

    texto = montar_mensagem(faltantes, total=len(achados))
    if texto is None:
        return 0
    logger.info("aasp_conferencia:\n%s", texto)

    import asyncio

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
