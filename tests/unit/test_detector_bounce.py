"""Tests for the bounce detector (detector_bounce).

Fecha o loop de entrega de email: o SMTP aceitar (📧✅) não garante
entrega. Este job lê devoluções (bounces) da caixa e avisa o Mario
quando um email registrado como enviado na verdade voltou.
"""

from noviello_funil.db import connect, run_migrations
from noviello_funil.detector_bounce import (
    cruzar_com_enviados,
    eh_email_de_bounce,
    extrair_destinatario_falho,
)

# --- eh_email_de_bounce ------------------------------------------------------

def test_reconhece_bounce_do_google():
    assert eh_email_de_bounce(
        "mailer-daemon@googlemail.com",
        "Delivery Status Notification (Failure)",
    )


def test_reconhece_bounce_do_bol_portugues():
    assert eh_email_de_bounce(
        "MAILER-DAEMON@mx3.bol.com.br",
        "Problema ao entregar o e-mail - retorno ao remetente / Undelivered Mail Returned to Sender",
    )


def test_reconhece_postmaster():
    assert eh_email_de_bounce("postmaster@outlook.com", "Undeliverable: ...")


def test_email_normal_nao_e_bounce():
    assert not eh_email_de_bounce("cliente@x.com", "Re: nossa reunião")
    assert not eh_email_de_bounce("mario@noviello.adv.br", "Feliz aniversário!")


# --- extrair_destinatario_falho ----------------------------------------------

def test_extrai_final_recipient_dsn():
    corpo = (
        "Content-Type: message/delivery-status\n\n"
        "Final-Recipient: rfc822; sansystemseguros@bol.com.br\n"
        "Action: failed\nStatus: 5.1.1\n"
    )
    assert extrair_destinatario_falho(corpo) == "sansystemseguros@bol.com.br"


def test_extrai_do_texto_google_portugues():
    corpo = (
        "Endereço não encontrado\n"
        "Sua mensagem não foi entregue a celina.campos@ig.com.br porque o "
        "endereço não foi encontrado.\n"
    )
    assert extrair_destinatario_falho(corpo) == "celina.campos@ig.com.br"


def test_extrai_do_texto_ingles():
    corpo = "Your message couldn't be delivered to joao@empresa.com.br because..."
    assert extrair_destinatario_falho(corpo) == "joao@empresa.com.br"


def test_extrai_bounce_postfix_bol_real():
    # formato real do mx3.bol.com.br (Nayara, 14/jun) — sem Final-Recipient
    corpo = (
        "I'm sorry to have to inform you that your message could not\n"
        "be delivered to one or more recipients.\n\n"
        "<sansystemseguros@bol.com.br>: host mfbol.mail.sys.intranet[10.241.8.24] said:\n"
        "    550 RCPT TO:<sansystemseguros@bol.com.br> User unknown (in reply to RCPT TO\n"
        "    command)\n"
    )
    assert extrair_destinatario_falho(corpo) == "sansystemseguros@bol.com.br"


def test_final_recipient_tem_prioridade_sobre_outros_emails():
    corpo = (
        "De: mario@noviello.adv.br\n"
        "Final-Recipient: rfc822; alvo@dominio.com\n"
        "Reportei o erro para suporte@google.com\n"
    )
    assert extrair_destinatario_falho(corpo) == "alvo@dominio.com"


def test_sem_email_retorna_none():
    assert extrair_destinatario_falho("mensagem sem endereço nenhum") is None
    assert extrair_destinatario_falho("") is None


# --- cruzar_com_enviados -----------------------------------------------------

def test_cruza_bounce_com_aniversario_enviado():
    conn = connect(":memory:")
    run_migrations(conn)
    # sistema registrou 2 envios de aniversário
    conn.execute(
        "INSERT INTO emails_aniversario (person_id, enviado_em, email) VALUES (?,?,?)",
        ("P-NAY", "2026-06-14", "sansystemseguros@bol.com.br"),
    )
    conn.execute(
        "INSERT INTO emails_aniversario (person_id, enviado_em, email) VALUES (?,?,?)",
        ("P-OSW", "2026-06-13", "dinogentille@uol.com.br"),
    )
    # bounces detectados na caixa
    casados = cruzar_com_enviados(
        conn, ["sansystemseguros@bol.com.br", "estranho@x.com"],
    )
    # só o que bate com um envio nosso conta
    assert len(casados) == 1
    assert casados[0]["email"] == "sansystemseguros@bol.com.br"
    assert casados[0]["person_id"] == "P-NAY"
    # e o email vira "morto" pra não tentar de novo
    morto = conn.execute(
        "SELECT 1 FROM emails_mortos WHERE email = ?",
        ("sansystemseguros@bol.com.br",),
    ).fetchone()
    assert morto is not None
    conn.close()


def test_cruzar_idempotente_nao_duplica_morto():
    conn = connect(":memory:")
    run_migrations(conn)
    conn.execute(
        "INSERT INTO emails_aniversario (person_id, enviado_em, email) VALUES (?,?,?)",
        ("P1", "2026-06-14", "morto@x.com"),
    )
    cruzar_com_enviados(conn, ["morto@x.com"])
    cruzar_com_enviados(conn, ["morto@x.com"])  # 2ª vez não quebra
    n = conn.execute("SELECT COUNT(*) FROM emails_mortos").fetchone()[0]
    assert n == 1
    conn.close()
