"""Conferência e-mails (AASP + Recorte Digital OAB) × integração AASP.

O ``aasp_intimacoes`` grava as intimações da API AASP no Juridiq, mas
"total segurança" pede fonte de verificação INDEPENDENTE: os e-mails que
a AASP (intimacoes@info.aasp.org.br) e o Recorte Digital da OAB/SP
(oabsp@recortedigital.adv.br) mandam todo dia listam as mesmas
publicações por outro canal. Este job lê esses e-mails via IMAP (mesma
conta/senha de app do detector_bounce), extrai TODOS os números CNJ e
cruza com o que a integração processou (``aasp_intimacao_vista``).

Resultado no WhatsApp:
- dia COM intimação e tudo capturado → confirmação positiva curta
  ("N números conferidos ✓") — o silêncio nunca é ambíguo num dia com
  publicação;
- número no e-mail que a integração NÃO processou → alerta 🚨 com fonte
  e processo (pode ser atraso da API, falha do job, ou publicação que só
  o Recorte OAB cobre — ex.: DJU/federal fora do recorte AASP);
- nenhum e-mail com intimação na janela → silêncio (exit 0).

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


def conferir(
    achados: dict[str, dict], processados: set[str],
) -> list[dict]:
    """Números dos e-mails que a integração NÃO processou (faltantes)."""
    faltantes = []
    for digits, info in sorted(achados.items()):
        if digits in processados:
            continue
        faltantes.append({
            "processo": _mascarar(digits),
            "fonte": info.get("fonte") or "?",
            "data": info.get("data") or "?",
        })
    return faltantes


def montar_mensagem(faltantes: list[dict], total: int) -> str | None:
    """Mensagem WhatsApp. None = nenhum e-mail com intimação (silêncio)."""
    if not total:
        return None
    if not faltantes:
        return (
            f"🔎 *Conferência de intimações*: {total} número(s) nos e-mails "
            "AASP/Recorte OAB — todos capturados pela integração ✓"
        )
    blocos = [
        "🚨 *Conferência de intimações: DIVERGÊNCIA*",
        f"Dos {total} número(s) nos e-mails, {len(faltantes)} NÃO passou(aram) "
        "pela integração AASP→Juridiq:",
        "",
    ]
    for f in faltantes[:MAX_ITENS]:
        blocos.append(f"• {f['processo']} — {f['fonte']} ({f['data']})")
    if len(faltantes) > MAX_ITENS:
        blocos.append(f"… e mais {len(faltantes) - MAX_ITENS}.")
    blocos.append(
        "\nPossíveis causas: atraso da carga da AASP (a rodada das 14h pega), "
        "publicação fora do recorte AASP (ex.: DJU/federal, só no Recorte "
        "OAB) ou falha do job — conferir no painel do Juridiq."
    )
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

    faltantes = conferir(achados, processados)
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
