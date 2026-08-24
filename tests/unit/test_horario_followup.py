"""Unit — janela de horário dos follow-ups (regra Mario 24/ago).

Sem follow-up proativo: antes das 8h e depois das 20h (seg-sex); sábado só
9h-12h; domingo e feriado nunca. Fora da janela o lead FICA NA FILA — sai no
próximo horário permitido. Só afeta o run_followup_cycle (FU1/FU2); resposta
reativa, lembrete de reunião e aniversários seguem como estão.
"""

import datetime
import zoneinfo

from noviello_funil.scheduler import _fora_do_horario_followup

TZ = zoneinfo.ZoneInfo("America/Sao_Paulo")


def _dt(y, m, d, h, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=TZ)


# 2026-08-26 = quarta-feira comum
def test_dia_util_dentro_da_janela():
    assert not _fora_do_horario_followup(_dt(2026, 8, 26, 8, 0))
    assert not _fora_do_horario_followup(_dt(2026, 8, 26, 14, 30))
    assert not _fora_do_horario_followup(_dt(2026, 8, 26, 19, 59))


def test_dia_util_fora_da_janela():
    assert _fora_do_horario_followup(_dt(2026, 8, 26, 7, 59))
    assert _fora_do_horario_followup(_dt(2026, 8, 26, 20, 0))
    assert _fora_do_horario_followup(_dt(2026, 8, 26, 1, 40))  # caso Renato


# 2026-08-29 = sábado
def test_sabado_so_9_as_12():
    assert _fora_do_horario_followup(_dt(2026, 8, 29, 8, 59))
    assert not _fora_do_horario_followup(_dt(2026, 8, 29, 9, 0))
    assert not _fora_do_horario_followup(_dt(2026, 8, 29, 11, 59))
    assert _fora_do_horario_followup(_dt(2026, 8, 29, 12, 0))
    assert _fora_do_horario_followup(_dt(2026, 8, 29, 15, 0))


# 2026-08-30 = domingo
def test_domingo_nunca():
    assert _fora_do_horario_followup(_dt(2026, 8, 30, 10, 0))


def test_feriado_fixo_nunca():
    # 7 de setembro de 2026 cai numa segunda — 10h seria janela normal.
    assert _fora_do_horario_followup(_dt(2026, 9, 7, 10, 0))
    # Natal (sexta em 2026).
    assert _fora_do_horario_followup(_dt(2026, 12, 25, 10, 0))
    # Feriados de SP: aniversário da cidade e Revolução de 32.
    assert _fora_do_horario_followup(_dt(2027, 1, 25, 10, 0))
    assert _fora_do_horario_followup(_dt(2027, 7, 9, 10, 0))


def test_feriado_movel_nunca():
    # Páscoa 2026 = 05/abr → Carnaval 16-17/fev, Sexta Santa 03/abr,
    # Corpus Christi 04/jun. Todos em dia útil, 10h.
    assert _fora_do_horario_followup(_dt(2026, 2, 16, 10, 0))
    assert _fora_do_horario_followup(_dt(2026, 2, 17, 10, 0))
    assert _fora_do_horario_followup(_dt(2026, 4, 3, 10, 0))
    assert _fora_do_horario_followup(_dt(2026, 6, 4, 10, 0))


def test_vespera_de_feriado_normal():
    # 06/set/2026 é domingo, então usa 04/set (sexta) — dia comum, 10h ok.
    assert not _fora_do_horario_followup(_dt(2026, 9, 4, 10, 0))
