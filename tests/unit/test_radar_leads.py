"""Unit — radar de leads (30/ago, caso Paulo): análise de mensagens."""

import datetime

from noviello_funil.radar_leads import _fmt_espera, analisar_mensagens


def _msg(direction, *, at="2026-08-26T12:00:00Z", tipo="text", mid="m1"):
    return {"direction": direction, "messageAt": at, "type": tipo, "id": mid}


def test_sem_mensagens():
    info = analisar_mensagens([])
    assert info["espera_desde"] is None
    assert info["n_sem_resposta"] == 0
    assert info["doc_desde"] is None


def test_lead_respondido_nao_espera():
    msgs = [
        _msg("INBOUND", at="2026-08-26T12:00:00Z"),
        _msg("OUTBOUND", at="2026-08-26T12:01:00Z"),
    ]
    info = analisar_mensagens(msgs)
    assert info["espera_desde"] is None
    assert info["n_sem_resposta"] == 0


def test_espera_desde_a_primeira_sem_resposta():
    msgs = [
        _msg("OUTBOUND", at="2026-08-26T10:00:00Z"),
        _msg("INBOUND", at="2026-08-26T11:00:00Z", mid="a"),
        _msg("INBOUND", at="2026-08-26T11:30:00Z", mid="b"),
    ]
    info = analisar_mensagens(msgs)
    assert info["n_sem_resposta"] == 2
    assert info["espera_desde"] == datetime.datetime(
        2026, 8, 26, 11, 0, tzinfo=datetime.UTC,
    )


def test_documento_sem_resposta_detectado():
    # Caso Paulo: texto + PDF depois da última resposta nossa.
    msgs = [
        _msg("OUTBOUND", at="2026-08-19T10:00:00Z"),
        _msg("INBOUND", at="2026-08-19T11:00:00Z", mid="txt"),
        _msg("INBOUND", at="2026-08-19T11:05:00Z", tipo="document", mid="doc1"),
    ]
    info = analisar_mensagens(msgs)
    assert info["doc_id"] == "doc1"
    assert info["doc_tipo"] == "document"
    assert info["doc_desde"] == datetime.datetime(
        2026, 8, 19, 11, 5, tzinfo=datetime.UTC,
    )


def test_documento_ja_respondido_nao_conta():
    msgs = [
        _msg("INBOUND", at="2026-08-26T09:00:00Z", tipo="image", mid="doc"),
        _msg("OUTBOUND", at="2026-08-26T09:30:00Z"),
    ]
    info = analisar_mensagens(msgs)
    assert info["doc_desde"] is None
    assert info["espera_desde"] is None


def test_audio_e_texto_nao_sao_documento():
    msgs = [
        _msg("INBOUND", at="2026-08-26T09:00:00Z", tipo="audio", mid="a"),
        _msg("INBOUND", at="2026-08-26T09:01:00Z", tipo="text", mid="b"),
    ]
    info = analisar_mensagens(msgs)
    assert info["doc_desde"] is None
    assert info["n_sem_resposta"] == 2  # mas está esperando resposta


def test_fmt_espera():
    td = datetime.timedelta
    assert _fmt_espera(td(minutes=45)) == "45min"
    assert _fmt_espera(td(hours=2, minutes=13)) == "2h13"
    assert _fmt_espera(td(hours=5)) == "5h"
    assert _fmt_espera(td(days=11, hours=3)) == "11d 3h"
    assert _fmt_espera(td(days=2)) == "2d"
