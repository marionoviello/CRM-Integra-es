"""Entrega ≠ aceite (caso Vizca, 20/jul).

``send-message`` responder 200 NÃO garante que o WhatsApp entregou: a perna
Jurichat→WhatsApp falha depois, e a mensagem fica com ``externalStatus=FAILED``.
O bot não via nada (log limpo) e o lead ficava sem resposta em silêncio.
"""

from noviello_funil.outbound import mensagens_nao_entregues
from noviello_funil.state import (
    falha_ja_vista,
    marcar_falha_reenviada,
    marcar_falha_vista,
)


def _msg(**kw):
    base = {
        "id": "m1", "direction": "OUTBOUND", "content": "oi",
        "externalStatus": "SENT",
    }
    base.update(kw)
    return base


# --- detecção -------------------------------------------------------------

def test_outbound_failed_e_detectada():
    msgs = [_msg(id="m1"), _msg(id="m2", externalStatus="FAILED")]
    assert [m["id"] for m in mensagens_nao_entregues(msgs)] == ["m2"]


def test_status_de_sucesso_nao_entra():
    for status in ("SENT", "DELIVERED", "READ", "PENDING", None, ""):
        assert mensagens_nao_entregues([_msg(externalStatus=status)]) == []


def test_variantes_de_falha_da_api():
    for status in ("failed", "ERROR", "UNDELIVERED", "rejected"):
        assert len(mensagens_nao_entregues([_msg(externalStatus=status)])) == 1


def test_mensagem_do_lead_nunca_entra():
    # INBOUND com status estranho é problema do outro lado — não reenviamos.
    assert mensagens_nao_entregues(
        [_msg(direction="INBOUND", externalStatus="FAILED")]
    ) == []


def test_mensagem_sem_id_e_ignorada():
    # Sem id não há como deduplicar → alertaria a cada tick, pra sempre.
    assert mensagens_nao_entregues([_msg(id="", externalStatus="FAILED")]) == []


# --- idempotência ---------------------------------------------------------

def test_falha_vista_uma_vez_nao_volta(db_conn):
    assert falha_ja_vista(db_conn, "m2") is False
    marcar_falha_vista(db_conn, "m2", lead_id=1)
    assert falha_ja_vista(db_conn, "m2") is True


def test_marcar_vista_e_idempotente(db_conn):
    marcar_falha_vista(db_conn, "m2", lead_id=1)
    marcar_falha_vista(db_conn, "m2", lead_id=1)
    assert falha_ja_vista(db_conn, "m2") is True


def test_reenvio_e_carimbado(db_conn):
    marcar_falha_vista(db_conn, "m2", lead_id=1)
    marcar_falha_reenviada(db_conn, "m2")
    row = db_conn.execute(
        "SELECT reenviada_em FROM mensagem_falha_vista WHERE message_id = 'm2'"
    ).fetchone()
    assert row["reenviada_em"] is not None
