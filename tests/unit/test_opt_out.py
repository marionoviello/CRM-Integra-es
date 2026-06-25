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


def test_frases_pt_br_comuns_de_descadastro_disparam():
    """E2 (auditoria 24/jun): frases coloquiais que o regex perdia."""
    for t in (
        "me deixa em paz",
        "me deixe em paz por favor",
        "não envie mais mensagens",
        "não me envie mais nada",
        "não manda mais",
        "chega de mensagem",
        "chega de me mandar isso",
    ):
        assert detectar_opt_out(t), t


def test_e2_novas_frases_nao_geram_falso_positivo():
    """E2: as novas frases não podem disparar em pedidos legítimos."""
    for t in (
        "chega de novidade boa, adorei!",   # "chega de" sem objeto de comunicação
        "que paz esse atendimento",          # "paz" sem "me deixa em paz"
        "pode me enviar mais informações",   # PEDE mais — oposto de opt-out
        "manda mais detalhes por favor",
    ):
        assert not detectar_opt_out(t), t


def test_e2_nao_envie_mais_OBJETO_e_correcao_nao_opt_out():
    """E2 (revisão adversarial 24/jun, P0): 'não [verbo] mais OBJETO' é
    CORREÇÃO ou troca de canal de um lead ATIVO, NÃO descadastro — não pode
    virar supressão permanente. Só 'mais' sem objeto concreto (ou seguido de
    nada/mensagem/isso) é opt-out."""
    for t in (
        "não manda mais documento errado, manda o certo",
        "não envie mais aquele link quebrado, envie o novo",
        "não me envie mais cobrança, já paguei",
        "não manda mais email, me manda no whatsapp",
        "não manda mais boleto errado, manda o certo",
    ):
        assert not detectar_opt_out(t), t
    # objeto concreto SEM "mais" também é correção, não opt-out:
    assert not detectar_opt_out("não me envie o contrato ainda")
    assert not detectar_opt_out("não me manda o boleto, manda o pix")
    # ...mas o opt-out genuíno CONTINUA disparando — fim, fechamento, OU
    # objeto de marketing (propaganda/publicidade/spam):
    for t in (
        "não manda mais",
        "não envie mais mensagens",
        "não me envie mais nada",
        "não me envie mais nada por favor",
        "não me mande",
        "não me envie propaganda",
        "não me manda mais publicidade",
        "não me envie spam",
    ):
        assert detectar_opt_out(t), t


def test_nao_confunde_pedido_de_envio_com_opt_out():
    # "me manda" é pedido, não opt-out
    assert not detectar_opt_out("me manda o contrato por favor")
    assert not detectar_opt_out("pode mandar os documentos")
    # armadilha da preposição "para" (bug ALTA revisão 15/jun):
    # pedir pra enviar PARA um destino NÃO é descadastro
    assert not detectar_opt_out("manda para meu email a mensagem")
    assert not detectar_opt_out("pode mandar para mim os documentos")
    assert not detectar_opt_out("envia para o whatsapp da minha esposa")


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
