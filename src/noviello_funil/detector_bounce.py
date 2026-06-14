"""Detector de bounce — fecha o loop de entrega de email.

O SMTP aceitar uma mensagem (que vira o 📧✅ do aniversário) NÃO garante
entrega: o servidor de destino pode devolver depois (endereço morto,
caixa cheia). Foi o caso real da Nayara em 14/jun — o parabéns saiu mas
o bol.com.br devolveu 3s depois, e tanto o envio quanto a devolução
caíram na lixeira do Mario, fora da busca padrão.

Este job lê a caixa via IMAP, acha as devoluções recentes (de qualquer
MAILER-DAEMON/postmaster, inclusive na lixeira), cruza com o que o
sistema registrou como enviado e avisa o Mario quando um email voltou.
O endereço é marcado como morto (``emails_mortos``) pra que os senders
não insistam.

Lição de 14/jun: bounce vem do servidor de DESTINO (não só do Google) e
pode estar na LIXEIRA — por isso varremos INBOX + Trash e casamos por
remetente OU assunto.

Execução: console script ``noviello-bounce`` via timer diário (08h45 BRT,
depois dos jobs de envio da manhã). Sem IMAP configurado → no-op.
"""

import email as emaillib
import imaplib
import logging
import re
from email.header import decode_header, make_header

logger = logging.getLogger(__name__)

_BOUNCE_REMETENTES = ("mailer-daemon", "postmaster")
_BOUNCE_ASSUNTOS = (
    "delivery status notification", "undelivered mail", "returned to sender",
    "mail delivery failed", "failure notice", "undeliverable",
    "não foi entregue", "nao foi entregue", "retorno ao remetente",
    "problema ao entregar",
)
_EMAIL_RE = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"


def eh_email_de_bounce(remetente: str, assunto: str) -> bool:
    """True se o email parece uma devolução (DSN)."""
    rem = (remetente or "").lower()
    if any(t in rem for t in _BOUNCE_REMETENTES):
        return True
    ass = (assunto or "").lower()
    return any(t in ass for t in _BOUNCE_ASSUNTOS)


def extrair_destinatario_falho(corpo: str) -> str | None:
    """Extrai o endereço que falhou de um corpo de bounce.

    Prioriza o campo canônico ``Final-Recipient`` (DSN, RFC 3464); cai
    para os textos em PT/EN que os provedores usam. Retorna lowercase
    ou None.
    """
    texto = corpo or ""
    # 1. DSN canônico (mais confiável)
    m = re.search(
        rf"(?:Final|Original)-Recipient:\s*rfc822;\s*({_EMAIL_RE})",
        texto, re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    # 2. Postfix (BOL e muitos outros): o endereço que falhou aparece no
    #    relatório técnico, sem Final-Recipient. Confiáveis e específicos.
    for pat in (
        rf"RCPT TO:\s*<({_EMAIL_RE})>",          # comando SMTP rejeitado
        rf"<({_EMAIL_RE})>:\s*host\b",            # "<email>: host ... said"
        rf"<({_EMAIL_RE})>\s+(?:User unknown|does not exist)",
    ):
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    # 3. textos PT/EN de provedores
    for pat in (
        rf"não foi entregue\s+(?:a|para)\s+({_EMAIL_RE})",
        rf"nao foi entregue\s+(?:a|para)\s+({_EMAIL_RE})",
        rf"could ?n['o]t be delivered to\s+({_EMAIL_RE})",
        rf"delivery to the following recipient[^\n]*?({_EMAIL_RE})",
        rf"to\s+({_EMAIL_RE})\s+(?:because|porque|failed)",
    ):
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return None


def cruzar_com_enviados(conn, emails_bounced: list[str]) -> list[dict]:
    """Cruza endereços que bouncearam com o que o sistema enviou.

    Retorna só os que batem com um envio registrado (em emails_aniversario)
    — esses são os que importam pro Mario. Marca cada um como morto.
    """
    alvo = {e.lower() for e in emails_bounced if e}
    if not alvo:
        return []
    placeholders = ",".join("?" * len(alvo))
    rows = conn.execute(
        f"SELECT DISTINCT person_id, email FROM emails_aniversario "
        f"WHERE lower(email) IN ({placeholders})",
        tuple(alvo),
    ).fetchall()
    casados = []
    for person_id, email_addr in rows:
        casados.append({"person_id": person_id, "email": email_addr})
        conn.execute(
            "INSERT OR IGNORE INTO emails_mortos (email, motivo) VALUES (?, ?)",
            (email_addr.lower(), "bounce detectado"),
        )
    return casados


def _decode(valor: str) -> str:
    try:
        return str(make_header(decode_header(valor or "")))
    except Exception:
        return valor or ""


def buscar_bounces_imap(
    *, host: str, port: int, user: str, password: str, dias: int = 2,
) -> list[str]:
    """Conecta via IMAP e devolve os endereços que bouncearam nos últimos
    ``dias``. Varre INBOX + lixeira. Erro de IMAP → lista vazia (no-op)."""
    import datetime

    desde = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=dias)
    ).strftime("%d-%b-%Y")
    bounced: set[str] = set()
    try:
        M = imaplib.IMAP4_SSL(host, port)
        M.login(user, password)
    except Exception as exc:
        logger.warning("detector_bounce: IMAP indisponível (%s) — no-op", exc)
        return []
    try:
        # Descobre a pasta de lixeira via SPECIAL-USE (\Trash), independe
        # de locale. Fallback nos nomes comuns.
        pastas = ["INBOX"]
        try:
            for raw in M.list()[1]:
                linha = raw.decode(errors="ignore") if isinstance(raw, bytes) else raw
                if "\\Trash" in linha or "/Lixeira" in linha or "/Trash" in linha:
                    nome = linha.split(' "/" ')[-1].strip().strip('"')
                    pastas.append(nome)
        except Exception:
            pastas += ["[Gmail]/Lixeira", "[Gmail]/Trash"]

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
                    msg = emaillib.message_from_bytes(msg_data[0][1])
                    remetente = _decode(msg.get("From", ""))
                    assunto = _decode(msg.get("Subject", ""))
                    if not eh_email_de_bounce(remetente, assunto):
                        continue
                    corpo = _corpo_texto(msg)
                    alvo = extrair_destinatario_falho(corpo)
                    if alvo:
                        bounced.add(alvo)
            except Exception as exc:
                logger.debug("detector_bounce: pasta %s falhou: %s", pasta, exc)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return sorted(bounced)


def _corpo_texto(msg) -> str:
    partes = []
    if msg.is_multipart():
        for p in msg.walk():
            ct = p.get_content_type()
            if ct in ("text/plain", "message/delivery-status", "message/rfc822"):
                try:
                    payload = p.get_payload(decode=True)
                    if payload:
                        partes.append(payload.decode(errors="ignore"))
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                partes.append(payload.decode(errors="ignore"))
        except Exception:
            pass
    return "\n".join(partes)


def main() -> int:
    """Entry point do console script ``noviello-bounce``."""
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.smtp_user or not settings.smtp_password:
        logger.warning("detector_bounce: SMTP/IMAP não configurado — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("detector_bounce: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    bounced = buscar_bounces_imap(
        host=settings.imap_host,
        port=settings.imap_port,
        user=settings.smtp_user,
        password=settings.smtp_password,
        dias=2,
    )
    logger.info("detector_bounce: %d endereço(s) com bounce recente", len(bounced))
    if not bounced:
        return 0

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        casados = cruzar_com_enviados(conn, bounced)
    finally:
        conn.close()

    if not casados:
        logger.info("detector_bounce: nenhum bounce bate com envio nosso")
        return 0

    linhas = [f"• {c['email']}" for c in casados]
    texto = (
        "⚠️ *Email(s) de parabéns que VOLTARAM* (não chegaram ao cliente):\n\n"
        + "\n".join(linhas)
        + "\n\nEndereço inválido/desativado — vale corrigir ou marcar no "
        "Juridiq. Não vou tentar de novo nesses."
    )
    logger.info("detector_bounce:\n%s", texto)

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
