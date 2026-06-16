"""Tests for the deterministic slot-pick matcher (agendamento_match).

Reproduz o bug Camila (16/jun): ela respondeu "Ter (16/jun) às 14h" a uma
oferta de horários e o bot derrapou. O matcher tem que casar isso de forma
determinística, sem depender do Claude — e ser CONSERVADOR (ambíguo → None).
"""

import pytest

from noviello_funil.agendamento_match import casar_horario_escolhido

# Horários ofertados (espelha o print da conversa da Camila).
SLOTS = [
    {"iso": "2026-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
    {"iso": "2026-06-16T18:30:00-03:00", "label": "ter (16/jun) às 18h30"},
    {"iso": "2026-06-17T14:00:00-03:00", "label": "qua (17/jun) às 14h"},
    {"iso": "2026-06-18T14:00:00-03:00", "label": "qui (18/jun) às 14h"},
]


# --- o caso real (regressão do bug) ------------------------------------------

def test_camila_label_copiado_na_integra():
    # exatamente o que a Camila digitou
    assert casar_horario_escolhido("Ter (16/jun) às 14h", SLOTS) == \
        "2026-06-16T14:00:00-03:00"


def test_hora_mais_18h30():
    assert casar_horario_escolhido("Ter (16/jun) às 18h30", SLOTS) == \
        "2026-06-16T18:30:00-03:00"


# --- variações naturais (casam) ----------------------------------------------

def test_terca_as_14h():
    assert casar_horario_escolhido("pode ser terça às 14h", SLOTS) == \
        "2026-06-16T14:00:00-03:00"


def test_quarta_14h():
    assert casar_horario_escolhido("qua 14h tá bom", SLOTS) == \
        "2026-06-17T14:00:00-03:00"


def test_weekday_unico_o_de_quinta():
    assert casar_horario_escolhido("o de quinta", SLOTS) == \
        "2026-06-18T14:00:00-03:00"


def test_hora_unica_18h30_sem_dia():
    assert casar_horario_escolhido("18h30", SLOTS) == \
        "2026-06-16T18:30:00-03:00"


def test_ordinal_primeiro():
    assert casar_horario_escolhido("o primeiro", SLOTS) == \
        "2026-06-16T14:00:00-03:00"


def test_ordinal_ultimo():
    assert casar_horario_escolhido("prefiro o último", SLOTS) == \
        "2026-06-18T14:00:00-03:00"


# --- conservador: ambíguo / irrelevante → None (defere ao Claude) ------------

def test_ambiguo_so_14h_tres_slots():
    # 3 slots às 14h, sem dia → ambíguo, NÃO casa (Claude pergunta o dia)
    assert casar_horario_escolhido("14h", SLOTS) is None


def test_mensagem_irrelevante():
    assert casar_horario_escolhido("quanto custa?", SLOTS) is None


def test_dia_sem_hora_defere():
    # "dia 17" sem hora — conservador, defere ao Claude
    assert casar_horario_escolhido("dia 17", SLOTS) is None


def test_sem_slots_ou_vazio():
    assert casar_horario_escolhido("ter 14h", []) is None
    assert casar_horario_escolhido("", SLOTS) is None
    assert casar_horario_escolhido(None, SLOTS) is None


def test_slot_com_iso_invalido_nao_quebra():
    ruins = [{"iso": "lixo", "label": "x"}, *SLOTS]
    assert casar_horario_escolhido("o de quinta", ruins) == \
        "2026-06-18T14:00:00-03:00"


# --- CRÍTICO 1: negação → None (lead REJEITA, bot não pode confirmar) ---------

@pytest.mark.parametrize("msg", [
    "o segundo não",
    "não quero o de terça",
    "quarta não pode",
    "o primeiro não serve",
    "nenhum desses",
    "a terça não",
    "terça não dá",
    "nem terça nem quarta",
])
def test_negacao_devolve_none(msg):
    assert casar_horario_escolhido(msg, SLOTS) is None


def test_negacao_com_slot_unico_casando_weekday():
    # Slot único de quinta + "não quero quinta" → None (não pode casar
    # por acidente de não-ambiguidade).
    um_slot = [{"iso": "2026-06-18T14:00:00-03:00", "label": "qui (18/jun) às 14h"}]
    assert casar_horario_escolhido("não quero quinta", um_slot) is None


def test_negacao_com_slot_unico_casando_label():
    um_slot = [{"iso": "2026-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"}]
    assert casar_horario_escolhido("ter (16/jun) às 14h, esse não", um_slot) is None


def test_negacao_com_ordinal_unico():
    assert casar_horario_escolhido("o primeiro não", SLOTS) is None


# --- CRÍTICO 2: colisão weekday × ordinal → None ------------------------------

def test_quarta_ausente_nao_vira_ordinal():
    # slots sem quarta-feira; "pode ser quarta?" não pode cair no ordinal=3.
    sem_quarta = [
        {"iso": "2026-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
        {"iso": "2026-06-18T14:00:00-03:00", "label": "qui (18/jun) às 14h"},
        {"iso": "2026-06-19T14:00:00-03:00", "label": "sex (19/jun) às 14h"},
        {"iso": "2026-06-22T14:00:00-03:00", "label": "seg (22/jun) às 14h"},
    ]
    assert casar_horario_escolhido("pode ser quarta?", sem_quarta) is None


def test_segunda_ausente_nao_vira_ordinal():
    # slots [ter, qui, sex] sem segunda-feira; "queria segunda" → None.
    sem_segunda = [
        {"iso": "2026-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
        {"iso": "2026-06-18T14:00:00-03:00", "label": "qui (18/jun) às 14h"},
        {"iso": "2026-06-19T14:00:00-03:00", "label": "sex (19/jun) às 14h"},
    ]
    assert casar_horario_escolhido("queria segunda", sem_segunda) is None


def test_quarta_presente_ainda_casa_via_weekday():
    # quarta-feira ESTÁ nos slots → casa por weekday (não regrediu).
    assert casar_horario_escolhido("o de quarta", SLOTS) == \
        "2026-06-17T14:00:00-03:00"


# --- CRÍTICO 3: hora com fronteira de dígito ('14h' ≠ '14h30') ----------------

def test_14h30_nao_casa_slot_de_14h():
    # contra-oferta de horário não ofertado: lead pede 14h30, slots têm 14h.
    so_14h = [
        {"iso": "2026-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
        {"iso": "2026-06-17T18:00:00-03:00", "label": "qua (17/jun) às 18h"},
    ]
    assert casar_horario_escolhido("consigo só 14h30 na verdade", so_14h) is None


def test_14h45_nao_casa_14h_nem_18h():
    dois = [
        {"iso": "2026-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
        {"iso": "2026-06-17T18:00:00-03:00", "label": "qua (17/jun) às 18h"},
    ]
    assert casar_horario_escolhido("14h45", dois) is None


@pytest.mark.parametrize("msg", ["14h", "14:00", "às 14 horas"])
def test_14h_cheia_ainda_casa(msg):
    # slot único às 14h continua casando nas variantes de hora cheia.
    so_14h = [
        {"iso": "2026-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
        {"iso": "2026-06-17T18:00:00-03:00", "label": "qua (17/jun) às 18h"},
    ]
    assert casar_horario_escolhido(msg, so_14h) == "2026-06-16T14:00:00-03:00"


def test_18h30_com_minuto_ainda_casa():
    assert casar_horario_escolhido("18h30", SLOTS) == \
        "2026-06-16T18:30:00-03:00"


# --- label-substring: ambíguo (2 labels) → None ------------------------------

def test_dois_labels_na_msg_eh_ambiguo():
    msg = "entre ter (16/jun) às 14h e qua (17/jun) às 14h, qual?"
    assert casar_horario_escolhido(msg, SLOTS) is None


# --- dia × hora no mesmo passo: '14:00' não vaza o dígito do dia --------------

def test_dia_x_hora_nao_vaza_digito():
    # [14/jun@14h, 18/jun@14h]: '14:00' é só hora; não pode casar o dia 14.
    slots = [
        {"iso": "2026-06-14T14:00:00-03:00", "label": "dom (14/jun) às 14h"},
        {"iso": "2026-06-18T14:00:00-03:00", "label": "qui (18/jun) às 14h"},
    ]
    assert casar_horario_escolhido("14:00", slots) is None


# --- "qualquer um" / "tanto faz" → primeiro slot ------------------------------

@pytest.mark.parametrize("msg", [
    "qualquer um",
    "tanto faz",
    "pode ser qualquer um",
    "o que for",
])
def test_qualquer_um_casa_primeiro_slot(msg):
    assert casar_horario_escolhido(msg, SLOTS) == "2026-06-16T14:00:00-03:00"


# --- robustez: item não-dict / None na lista não quebra ----------------------

def test_item_nao_dict_ou_none_nao_quebra():
    ruins = ["string solta", None, *SLOTS]
    assert casar_horario_escolhido("o de quinta", ruins) == \
        "2026-06-18T14:00:00-03:00"
    # sem casar nada também não estoura
    assert casar_horario_escolhido("xpto irrelevante", ["lixo", None]) is None


# --- hora sem separador ('1630' = 16h30, caso leopoldinojose 16/jun) ----------

_S_MIN = [
    {"iso": "2026-06-16T16:30:00-03:00", "label": "ter (16/jun) às 16h30"},
    {"iso": "2026-06-16T18:30:00-03:00", "label": "ter (16/jun) às 18h30"},
    {"iso": "2026-06-17T14:00:00-03:00", "label": "qua (17/jun) às 14h"},
]


def test_hhmm_cru_casa_slot_com_minuto():
    # o lead digita a hora sem 'h'/':' — formato real que falhou em produção
    assert casar_horario_escolhido("1630", _S_MIN) == "2026-06-16T16:30:00-03:00"
    assert casar_horario_escolhido("1830", _S_MIN) == "2026-06-16T18:30:00-03:00"


def test_hhmm_cru_nao_vale_pra_hora_cheia():
    # hora cheia ('1400') NÃO casa cru — número redondo, risco de valor solto
    so_cheia = [{"iso": "2026-06-17T14:00:00-03:00", "label": "qua (17/jun) às 14h"}]
    assert casar_horario_escolhido("1400", so_cheia) is None


def test_hhmm_cru_respeita_negacao_e_boundary():
    so16 = [{"iso": "2026-06-16T16:30:00-03:00", "label": "ter (16/jun) às 16h30"}]
    assert casar_horario_escolhido("1630 não quero", so16) is None   # negação
    assert casar_horario_escolhido("cpf 12345671630", so16) is None  # dígito antes
