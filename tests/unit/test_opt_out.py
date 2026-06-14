"""Tests for the opt-out / suppression handler (opt_out, roadmap 1.10)."""

from noviello_funil.db import connect, run_migrations
from noviello_funil.opt_out import (
    detectar_opt_out,
    esta_suprimido,
    registrar_opt_out,
)

# --- detectar_opt_out --------------------------------------------------------

def test_frases_claras_de_descadastro_disparam():
    for t in (
        "pode parar de me mandar mensagem",
        "não quero mais receber nada de vocês",
        "me descadastra dessa lista",
        "para de me enviar isso",
        "quero sair da lista",
        "remove meu número por favor",
        "STOP",
        "não me mande mais mensagens",
    ):
        assert detectar_opt_out(t), t


def test_conversa_normal_nao_dispara():
    for t in (
        "oi, quero saber sobre inventário",
        "pode me mandar mais informações?",   # PEDE pra mandar — oposto
        "parabéns pelo escritório",
        "quero agendar uma consulta",
        "",
        None,
    ):
        assert not detectar_opt_out(t), t


def test_nao_confunde_pedido_de_envio_com_opt_out():
    # "me manda" é pedido, não opt-out
    assert not detectar_opt_out("me manda o contrato por favor")
    assert not detectar_opt_out("pode mandar os documentos")


# --- registrar / consultar ---------------------------------------------------

def test_registrar_e_consultar_por_telefone():
    conn = connect(":memory:")
    run_migrations(conn)
    assert not esta_suprimido(conn, telefone="5511999998888")
    registrar_opt_out(conn, telefone="5511999998888", motivo="pediu no WhatsApp")
    assert esta_suprimido(conn, telefone="5511999998888")
    # normaliza: com/sem 9º dígito casam
    assert esta_suprimido(conn, telefone="551199998888")
    conn.close()


def test_registrar_e_consultar_por_email():
    conn = connect(":memory:")
    run_migrations(conn)
    registrar_opt_out(conn, email="Cliente@X.com")
    assert esta_suprimido(conn, email="cliente@x.com")  # case-insensitive
    assert not esta_suprimido(conn, email="outro@x.com")
    conn.close()


def test_idempotente():
    conn = connect(":memory:")
    run_migrations(conn)
    registrar_opt_out(conn, telefone="5511999998888")
    registrar_opt_out(conn, telefone="5511999998888")  # 2ª vez não quebra
    n = conn.execute("SELECT COUNT(*) FROM opt_out").fetchone()[0]
    assert n >= 1
    conn.close()


def test_consulta_vazia_retorna_false():
    conn = connect(":memory:")
    run_migrations(conn)
    assert not esta_suprimido(conn, telefone="")
    assert not esta_suprimido(conn, email="")
    assert not esta_suprimido(conn)
    conn.close()
