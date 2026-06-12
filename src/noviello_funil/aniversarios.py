"""Aniversariantes do dia — substitui o email diário do Juridiq.

Em vez da lista por email (Juridiq manda às 05h), o bot varre a base
de pessoas do Juridiq e manda no WhatsApp do Mario (canal de alertas)
a lista de aniversariantes do dia com link wa.me pronto + texto de
parabéns sugerido pra colar. Parabenizar pessoalmente vale mais que
mensagem automática de bot — o sistema só elimina o trabalho de
descobrir QUEM e digitar.

Execução: console script ``noviello-aniversarios`` via systemd timer
diário (08h BRT). Sem aniversariantes → não envia nada (sem ruído).

Nota de arquitetura: a LISTAGEM GET /person/ do Juridiq não retorna
birthDate (verificado 2026-06-11) — só o GET /person/{id}. O job faz
o scan completo da base (~1.5k pessoas, throttle 0.15s ≈ 4 min) uma
vez por dia, de madrugada. Batch burro e robusto > cache esperto.
"""

import asyncio
import datetime
import logging
import smtplib
import time
import unicodedata
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

# Artes do cartão de aniversário (decisão Mario 2026-06-11):
# clara → mulheres, escura → homens. Roteadas por heurística de nome
# (Juridiq não tem campo de gênero).
ASSETS_DIR = Path(__file__).parent / "assets"
ARTE_CLARA = ASSETS_DIR / "aniversario_clara.png"
ARTE_ESCURA = ASSETS_DIR / "aniversario_escura.png"

# Nomes BR que terminam em 'a' mas são masculinos (exceções comuns).
_MASCULINOS_EM_A = frozenset({
    "luca", "joca", "juca", "nicola", "garcia", "jonata", "mustafa",
    "akira",
})
# Nomes que NÃO terminam em 'a' mas são femininos.
_FEMININOS_SEM_A = frozenset({
    "isabel", "raquel", "rachel", "ester", "esther", "ruth", "rute",
    "carmen", "miriam", "ingrid", "kelly", "joice", "joyce", "iris",
    "ines", "agnes", "beatriz", "liz", "muriel",
    "gisele", "michele", "michelle", "danielle", "daniele", "nicole",
    "alice", "clarice", "denise", "elaine", "eliane", "simone",
    "ivone", "marlene", "arlete", "ivete", "odete", "salete",
    "solange", "edith", "edite", "mercedes",
    "dolores", "lourdes", "jaqueline",
    "jacqueline", "aline", "karine", "nadine", "celine", "pauline",
})


def _primeiro_nome_norm(nome: str) -> str:
    s = unicodedata.normalize("NFKD", (nome or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    partes = s.lower().split()
    return partes[0] if partes else ""


def escolher_arte(nome: str) -> Path:
    """Clara pra mulheres, escura pra homens (heurística de nome BR).

    Sufixo 'a' → feminino, com listas de exceções. Na dúvida → escura.
    Custo de erro ~zero: as artes diferem só na cor de fundo.
    """
    p = _primeiro_nome_norm(nome)
    if not p:
        return ARTE_ESCURA
    if p in _FEMININOS_SEM_A:
        return ARTE_CLARA
    if p in _MASCULINOS_EM_A:
        return ARTE_ESCURA
    return ARTE_CLARA if p.endswith("a") else ARTE_ESCURA

JURIDIQ_BASE = "https://api.juridiq.com.br"
THROTTLE_S = 0.15
TZ_BR = ZoneInfo("America/Sao_Paulo")


def eh_aniversariante_hoje(
    birth_date: object, hoje: datetime.date,
) -> bool:
    """True se ``birth_date`` (ISO 'YYYY-MM-DD[...]') cai hoje.

    Regra 29/fev: em ano não-bissexto, celebra em 28/fev.
    """
    s = str(birth_date or "")[:10]
    try:
        ano, mes, dia = (int(x) for x in s.split("-"))
    except (ValueError, AttributeError):
        return False
    if not (1 <= mes <= 12 and 1 <= dia <= 31):
        return False
    if (mes, dia) == (hoje.month, hoje.day):
        return True
    # 29/fev em ano sem 29/fev → celebra 28/fev
    if (mes, dia) == (2, 29) and (hoje.month, hoje.day) == (2, 28):
        try:
            datetime.date(hoje.year, 2, 29)
            return False  # ano bissexto tem o dia real
        except ValueError:
            return True
    return False


def buscar_aniversariantes(
    client: httpx.Client, hoje: datetime.date,
) -> list[dict]:
    """Varre a base do Juridiq e retorna os aniversariantes de hoje.

    Cada item: {nome, telefone, email, person_id}.
    """
    pessoas, page = [], 1
    while True:
        resp = client.get("/person/", params={"page": page, "limit": 100})
        resp.raise_for_status()
        data = resp.json()
        pessoas.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1

    aniversariantes = []
    for p in pessoas:
        pid = p.get("id")
        if not pid:
            continue
        try:
            det = client.get(f"/person/{pid}")
            if det.status_code >= 400:
                continue
            d = det.json()
            d = d.get("data", d)
        except httpx.HTTPError as exc:
            logger.warning("detalhe %s falhou: %s", pid, exc)
            continue
        if eh_aniversariante_hoje(d.get("birthDate"), hoje):
            aniversariantes.append({
                "nome": d.get("name") or "(sem nome)",
                "telefone": d.get("phone") or "",
                "email": d.get("email") or "",
                "person_id": pid,
            })
        time.sleep(THROTTLE_S)
    return aniversariantes


_DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
_MESES = [
    "jan", "fev", "mar", "abr", "mai", "jun",
    "jul", "ago", "set", "out", "nov", "dez",
]


# --- Email de parabéns -------------------------------------------------------
#
# Conteúdo 100% RELACIONAMENTO, zero captação/CTA comercial — email de
# aniversário com oferta de serviço violaria o tom do Provimento
# 205/2021 da OAB (publicidade sóbria, sem mercantilização).

def montar_email_parabens(
    nome: str, *, com_arte: bool = False,
) -> tuple[str, str, str]:
    """Retorna (assunto, corpo_texto, corpo_html) do parabéns.

    ``com_arte=True`` insere o cartão da marca no topo (referenciado
    por CID ``cid:arte`` — o caller embute o MIMEImage correspondente).
    O texto encurta pra não duplicar o que a arte já diz.
    """
    primeiro_nome = nome.strip().split()[0].title() if nome.strip() else "amigo(a)"
    assunto = f"Feliz aniversário, {primeiro_nome}! 🎉"
    texto = (
        f"Olá, {primeiro_nome}!\n\n"
        "Hoje é um dia especial e não poderíamos deixar passar em "
        "branco: feliz aniversário!\n\n"
        "Que este novo ciclo venha cheio de saúde, conquistas e bons "
        "momentos ao lado de quem você ama.\n\n"
        "Um grande abraço,\n\n"
        "Mario Noviello\n"
        "Noviello Advocacia\n"
        "www.noviello.adv.br"
    )
    arte_html = (
        '<img src="cid:arte" alt="A Noviello Advocacia deseja feliz '
        'aniversário!" width="560" '
        'style="display: block; width: 100%; max-width: 560px; '
        'height: auto; border-radius: 4px; margin-bottom: 20px;">'
        if com_arte else ""
    )
    saudacao_extra = "" if com_arte else """
      <p style="font-size: 16px; line-height: 1.6;">
        Hoje é um dia especial e não poderíamos deixar passar em branco:
        <strong>feliz aniversário!</strong> 🎉
      </p>
      <p style="font-size: 16px; line-height: 1.6;">
        Que este novo ciclo venha cheio de saúde, conquistas e bons
        momentos ao lado de quem você ama.
      </p>"""
    mensagem_curta = """
      <p style="font-size: 16px; line-height: 1.6;">
        Hoje o dia é seu — <strong>feliz aniversário!</strong> 🎉
        Conte sempre com a gente.
      </p>""" if com_arte else ""
    html = f"""\
<html>
  <body style="font-family: Georgia, 'Times New Roman', serif; color: #2b2b2b;
               max-width: 560px; margin: 0 auto; padding: 24px;">
    {arte_html}
    <div style="border-top: 4px solid #68192E; padding-top: 24px;">
      <p style="font-size: 17px;">Olá, <strong>{primeiro_nome}</strong>!</p>
      {saudacao_extra}{mensagem_curta}
      <p style="font-size: 16px;">Um grande abraço,</p>
      <p style="margin-top: 28px; font-size: 15px;">
        <strong style="color: #68192E;">Mario Noviello</strong><br>
        Noviello Advocacia<br>
        <a href="https://www.noviello.adv.br"
           style="color: #68192E;">www.noviello.adv.br</a>
      </p>
    </div>
  </body>
</html>"""
    return assunto, texto, html


def enviar_email_parabens(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_name: str,
    destinatario: str,
    nome: str,
) -> bool:
    """Envia o email via SMTP (STARTTLS). True só se aceito pelo servidor.

    Se as artes da marca existirem em ``assets/``, monta
    multipart/related com o cartão inline (clara=mulheres,
    escura=homens via escolher_arte). Sem as artes → versão só-texto
    de antes (graceful).
    """
    arte_path = escolher_arte(nome)
    com_arte = arte_path.is_file()
    assunto, texto, html = montar_email_parabens(nome, com_arte=com_arte)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(texto, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))

    if com_arte:
        msg: MIMEMultipart = MIMEMultipart("related")
        msg.attach(alt)
        img = MIMEImage(arte_path.read_bytes())
        img.add_header("Content-ID", "<arte>")
        img.add_header(
            "Content-Disposition", "inline", filename=arte_path.name,
        )
        msg.attach(img)
    else:
        msg = alt

    msg["Subject"] = assunto
    msg["From"] = formataddr((from_name, smtp_user))
    msg["To"] = destinatario
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [destinatario], msg.as_string())
        logger.info(
            "email de parabéns enviado pra %s <%s> (arte=%s)",
            nome, destinatario, arte_path.name if com_arte else "sem",
        )
        return True
    except Exception as exc:
        logger.exception(
            "email de parabéns FALHOU pra %s <%s>: %s", nome, destinatario, exc,
        )
        return False


def montar_mensagem(aniversariantes: list[dict], hoje: datetime.date) -> str:
    """Mensagem WhatsApp-ready pro canal de alertas do Mario.

    Itens com ``email_enviado=True`` ganham a marca 📧 — Mario sabe
    quem já foi coberto pelo email automático e prioriza o WhatsApp
    pessoal pros demais.
    """
    cab = (
        f"🎂 *Aniversariantes de hoje* "
        f"({_DIAS[hoje.weekday()]}, {hoje.day:02d}/{_MESES[hoje.month - 1]})\n"
    )
    linhas = []
    for a in aniversariantes:
        digits = "".join(c for c in a["telefone"] if c.isdigit())
        contato = f"https://wa.me/{digits}" if digits else (
            a["email"] or "sem contato cadastrado"
        )
        marca = " 📧✅" if a.get("email_enviado") else ""
        linhas.append(f"• {a['nome']} — {contato}{marca}")

    rodape = ""
    if any(a.get("email_enviado") for a in aniversariantes):
        rodape = "\n📧✅ = já recebeu email de parabéns automático\n"
    sugestao = (
        "\nSugestão pra colar (toque no link do cliente):\n"
        "_Olá, [nome]! Aqui é o Mario, da Noviello Advocacia. Passando "
        "pra te desejar um feliz aniversário! 🎉 Que seja um ano de "
        "muitas conquistas. Um abraço!_"
    )
    return cab + "\n" + "\n".join(linhas) + "\n" + rodape + sugestao


def main() -> int:
    """Entry point do console script ``noviello-aniversarios``.

    Sem aniversariantes hoje → exit 0 silencioso (nenhum envio).
    """
    from noviello_funil.config import Settings
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("aniversarios: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("aniversarios: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    hoje = datetime.datetime.now(TZ_BR).date()
    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        aniversariantes = buscar_aniversariantes(client, hoje)
    finally:
        client.close()

    logger.info(
        "aniversarios: %d aniversariante(s) hoje", len(aniversariantes),
    )
    if not aniversariantes:
        return 0

    # Disparo de EMAIL de parabéns (se SMTP configurado). Idempotente
    # por (person_id, data) — re-rodar o job no mesmo dia não duplica.
    if settings.smtp_user and settings.smtp_password:
        from noviello_funil.db import connect, run_migrations

        conn = connect(settings.database_path)
        run_migrations(conn)
        try:
            hoje_str = hoje.isoformat()
            for a in aniversariantes:
                if not a["email"]:
                    continue
                ja = conn.execute(
                    "SELECT 1 FROM emails_aniversario "
                    "WHERE person_id = ? AND enviado_em = ?",
                    (a["person_id"], hoje_str),
                ).fetchone()
                if ja:
                    a["email_enviado"] = True
                    continue
                ok = enviar_email_parabens(
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    smtp_user=settings.smtp_user,
                    smtp_password=settings.smtp_password,
                    from_name=settings.smtp_from_name,
                    destinatario=a["email"],
                    nome=a["nome"],
                )
                if ok:
                    a["email_enviado"] = True
                    conn.execute(
                        "INSERT OR IGNORE INTO emails_aniversario "
                        "(person_id, enviado_em, email) VALUES (?, ?, ?)",
                        (a["person_id"], hoje_str, a["email"]),
                    )
                time.sleep(1.0)  # gentileza com o SMTP do Google
        finally:
            conn.close()
    else:
        logger.info("aniversarios: SMTP não configurado — sem email automático")

    texto = montar_mensagem(aniversariantes, hoje)
    logger.info("aniversarios:\n%s", texto)

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
