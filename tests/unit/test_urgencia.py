"""Tests for the legal-urgency detector (urgencia, roadmap 1.12)."""

from noviello_funil.urgencia import detectar_urgencia

# --- termos fortes: disparam sozinhos -----------------------------------------

def test_citacao_dispara():
    assert detectar_urgencia("Dr, fui citado num processo, e agora?")
    assert detectar_urgencia("recebi uma citação ontem")


def test_penhora_e_bloqueio_disparam():
    assert detectar_urgencia("penhoraram minha conta")
    assert detectar_urgencia("bloquearam meu dinheiro no banco")
    assert detectar_urgencia("caiu um sisbajud na minha conta")


def test_leilao_e_despejo_disparam():
    assert detectar_urgencia("meu imóvel vai a leilão")
    assert detectar_urgencia("recebi ordem de despejo")
    assert detectar_urgencia("vão fazer a reintegração de posse")


def test_mandado_e_oficial_disparam():
    assert detectar_urgencia("um oficial de justiça veio aqui em casa")
    assert detectar_urgencia("chegou um mandado de penhora")


def test_processado_dispara():
    assert detectar_urgencia("me processaram, preciso de ajuda")
    assert detectar_urgencia("entraram com uma ação contra mim")


# --- termos que precisam de qualificador temporal -----------------------------

def test_prazo_com_urgencia_temporal_dispara():
    assert detectar_urgencia("o prazo vence amanhã!")
    assert detectar_urgencia("tenho um prazo que acaba hoje")
    assert detectar_urgencia("o prazo está acabando")


def test_audiencia_iminente_dispara():
    assert detectar_urgencia("minha audiência é amanhã e não tenho advogado")
    assert detectar_urgencia("tenho audiência hoje à tarde")


def test_prazo_sem_urgencia_nao_dispara():
    # "prazo" sozinho, contexto tranquilo → não é urgência aguda
    assert not detectar_urgencia("qual o prazo normal de um inventário?")
    assert not detectar_urgencia("quanto tempo leva esse tipo de processo?")


# --- palavra de socorro explícita ---------------------------------------------

def test_urgente_explicito_dispara():
    assert detectar_urgencia("URGENTE, preciso falar com alguém agora")
    assert detectar_urgencia("é uma emergência")


# --- não-urgentes (evitar falso positivo) -------------------------------------

def test_conversa_comum_nao_dispara():
    assert detectar_urgencia("oi, gostaria de saber sobre inventário") is None
    assert detectar_urgencia("bom dia, vocês fazem usucapião?") is None
    assert detectar_urgencia("quero agendar uma consulta") is None
    assert detectar_urgencia("") is None
    assert detectar_urgencia(None) is None


# --- retorno é o motivo (string curta), não só bool ---------------------------

def test_retorna_motivo_legivel():
    motivo = detectar_urgencia("penhoraram minha conta")
    assert isinstance(motivo, str) and len(motivo) > 0
    assert "penhora" in motivo.lower() or "bloqueio" in motivo.lower()
