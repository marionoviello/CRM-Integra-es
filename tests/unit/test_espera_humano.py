"""Unit — espera-humano (Signal 1.45): o bot cala enquanto um humano conduz.

Detecção por prefixo "Noviello Advocacia:" (o painel Fixo põe em TODA msg
digitada por humano; o bot nunca usa — validado com o Mario 2026-07-01), mais
a regra de janela: o bot só responde se a última msg do lead veio >= espera
DEPOIS da última msg do humano.
"""

from noviello_funil.scheduler import (
    _bot_deve_esperar_humano,
    _msg_eh_de_humano,
    _parse_message_at,
)

UMA_HORA = 3600


# ---- _msg_eh_de_humano ------------------------------------------------------

def test_prefixo_humano_com_html() -> None:
    assert _msg_eh_de_humano("<b>Noviello Advocacia</b>:<br /><br />Vamos elaborar")


def test_prefixo_humano_texto_puro() -> None:
    assert _msg_eh_de_humano("Noviello Advocacia: bom dia")


def test_prefixo_humano_case_insensitive() -> None:
    assert _msg_eh_de_humano("NOVIELLO ADVOCACIA: teste")


def test_bot_sem_prefixo() -> None:
    assert not _msg_eh_de_humano("Perfeito, Alison! Recebi o endereço.")


def test_bot_menciona_marca_no_meio_nao_conta() -> None:
    # saudação do bot cita "Noviello Advocacia" mas NÃO como prefixo → é bot.
    assert not _msg_eh_de_humano("Olá! Aqui é a Julia, da Noviello Advocacia.")


def test_conteudo_vazio() -> None:
    assert not _msg_eh_de_humano("")


# ---- _parse_message_at ------------------------------------------------------

def test_parse_iso_com_z() -> None:
    dt = _parse_message_at("2026-07-01T19:45:00.051Z")
    assert dt is not None
    assert (dt.year, dt.hour, dt.minute) == (2026, 19, 45)


def test_parse_invalido() -> None:
    assert _parse_message_at("") is None
    assert _parse_message_at(None) is None
    assert _parse_message_at("nao-e-data") is None


def test_parse_naive_vira_aware_utc() -> None:
    # sem 'Z'/offset → assume UTC (aware), pra não quebrar a subtração.
    dt = _parse_message_at("2026-07-01T19:45:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_mistura_naive_e_aware_nao_crasha() -> None:
    # humano naive, lead aware → coerção evita TypeError; retorna bool.
    msgs = [
        _humano("2026-07-01T19:45:00"),          # naive
        _lead("2026-07-01T19:50:00.000Z"),       # aware
    ]
    assert _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA) is True


# ---- _bot_deve_esperar_humano ----------------------------------------------

def _humano(ts: str, texto: str = "Noviello Advocacia: oi") -> dict:
    return {"direction": "OUTBOUND", "content": texto, "messageAt": ts}


def _bot(ts: str, texto: str = "Perfeito! Recebi.") -> dict:
    return {"direction": "OUTBOUND", "content": texto, "messageAt": ts}


def _lead(ts: str, texto: str = "ok") -> dict:
    return {"direction": "INBOUND", "content": texto, "messageAt": ts}


def test_sem_humano_nao_espera() -> None:
    msgs = [_bot("2026-07-01T19:00:00Z"), _lead("2026-07-01T19:05:00Z")]
    assert not _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA)


def test_lead_respondeu_dentro_da_janela_espera() -> None:
    # humano 19:45, lead 20:04 (19 min depois) → espera (caso da Alison).
    msgs = [_humano("2026-07-01T19:45:00Z"), _lead("2026-07-01T20:04:00Z")]
    assert _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA)


def test_lead_respondeu_fora_da_janela_nao_espera() -> None:
    # humano 19:45, lead 21:00 (75 min depois) → responde.
    msgs = [_humano("2026-07-01T19:45:00Z"), _lead("2026-07-01T21:00:00Z")]
    assert not _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA)


def test_humano_falou_por_ultimo_espera() -> None:
    # lead 19:40, humano respondeu 19:45 → humano conduz → espera.
    msgs = [_lead("2026-07-01T19:40:00Z"), _humano("2026-07-01T19:45:00Z")]
    assert _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA)


def test_humano_sem_lead_algum_espera() -> None:
    msgs = [_humano("2026-07-01T19:45:00Z")]
    assert _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA)


def test_usa_ultima_msg_de_cada_lado() -> None:
    # 2 msgs do humano (última 19:45), bot no meio (não conta), lead 19:50
    # (5 min após a última do humano) → espera.
    msgs = [
        _humano("2026-07-01T19:44:00Z"),
        _humano("2026-07-01T19:45:00Z"),
        _bot("2026-07-01T19:46:00Z"),
        _lead("2026-07-01T19:50:00Z"),
    ]
    assert _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA)


def test_desligado_com_espera_zero() -> None:
    msgs = [_humano("2026-07-01T19:45:00Z"), _lead("2026-07-01T19:50:00Z")]
    assert not _bot_deve_esperar_humano(msgs, espera_segundos=0)


def test_borda_exatamente_na_janela_responde() -> None:
    # lead exatamente 1h depois → NÃO espera (>= espera já pode responder).
    msgs = [_humano("2026-07-01T19:00:00Z"), _lead("2026-07-01T20:00:00Z")]
    assert not _bot_deve_esperar_humano(msgs, espera_segundos=UMA_HORA)
