"""Tests for the monthly client briefing (boletim_mensal, roadmap 3.1).

Híbrido: seguros (procedimental) → '✅ pronto'; sensíveis/ambíguos →
'⚠️ revisar'. Sem movimentação no mês → não entra. Travas OAB reusam o 2.4.
"""

import calendar
import datetime

from noviello_funil.boletim_mensal import (
    classificar_boletim,
    competencia,
    eh_sensivel,
    montar_lote,
    montar_mensagem_cliente,
    movimentos_do_mes,
    na_janela_de_envio,
    ultimo_dia_util_do_mes,
    wa_me_link,
)

# --- calendário (último dia útil) --------------------------------------------

def test_ultimo_dia_util_e_dia_de_semana():
    for mes in range(1, 13):
        u = ultimo_dia_util_do_mes(datetime.date(2026, mes, 15))
        assert u.month == mes
        assert u.weekday() < 5                       # seg-sex
        last = calendar.monthrange(2026, mes)[1]
        # volta no máximo sáb+dom a partir do último dia do mês
        assert (datetime.date(2026, mes, last) - u).days <= 2


def test_na_janela_de_envio():
    u = ultimo_dia_util_do_mes(datetime.date(2026, 6, 15))
    assert na_janela_de_envio(u) is True
    antes = u - datetime.timedelta(days=1)
    if antes.month == u.month:
        assert na_janela_de_envio(antes) is False
    fim = datetime.date(2026, 6, calendar.monthrange(2026, 6)[1])
    assert na_janela_de_envio(fim) is True           # fim de semana ainda conta


def test_competencia():
    assert competencia(datetime.date(2026, 6, 30)) == "2026-06"
    assert competencia(datetime.date(2026, 12, 1)) == "2026-12"


# --- movimentos do mês -------------------------------------------------------

def test_movimentos_do_mes_filtra():
    movs = [
        {"nome": "a", "data": "2026-06-10T00:00:00Z"},
        {"nome": "b", "data": "2026-05-30"},
        {"nome": "c", "data": None},
    ]
    assert [m["nome"] for m in movimentos_do_mes(movs, 2026, 6)] == ["a"]


# --- sensibilidade -----------------------------------------------------------

def test_eh_sensivel():
    assert eh_sensivel([{"nome": "Penhora online", "data": "2026-06-01"}]) == "constrição"
    assert eh_sensivel([{"nome": "Expedição de RPV", "data": "2026-06-01"}]) == \
        "dinheiro a levantar"
    assert eh_sensivel([{"nome": "Sentença proferida", "data": "2026-06-01"}]) == \
        "desfecho/decisão"
    assert eh_sensivel([{"nome": "Conclusão", "data": "2026-06-01"}]) is None
    assert eh_sensivel([]) is None


# --- classificação -----------------------------------------------------------

def _m(nome):
    return [{"nome": nome, "data": "2026-06-10"}]


def test_classificar_skip_sem_movimento():
    assert classificar_boletim([], False)["modo"] == "skip"


def test_classificar_rascunho_se_ambiguo():
    assert classificar_boletim(_m("Conclusão"), True)["modo"] == "rascunho"


def test_classificar_rascunho_se_sensivel():
    r = classificar_boletim(_m("Penhora"), False)
    assert r["modo"] == "rascunho"
    assert r["motivo"] == "constrição"


def test_classificar_auto_procedimental():
    assert classificar_boletim(_m("Juntada de Petição"), False)["modo"] == "auto"


# --- mensagem ao cliente + wa.me ---------------------------------------------

def test_mensagem_cliente_sobria_e_marca():
    msg = montar_mensagem_cliente("1234567-00.2024.8.26.0100", "2026-06-12")
    assert "1234567-00.2024.8.26.0100" in msg
    assert "12/06/2026" in msg
    assert "nossa equipe" in msg.lower()
    assert "Dr. Mario" not in msg
    assert "SAIR" in msg


def test_wa_me_link():
    link = wa_me_link("11987654321", "oi mundo")
    assert link.startswith("https://wa.me/5511987654321?text=")
    assert "oi%20mundo" in link
    assert wa_me_link("11987654321") == "https://wa.me/5511987654321"


# --- lote ao Mario -----------------------------------------------------------

def test_montar_lote_agrupa_e_vazio():
    itens = [
        {"nome": "Ana", "processo": "1", "telefone": "11999", "modo": "auto",
         "motivo": "", "data": "2026-06-10", "link": "L1"},
        {"nome": "Bia", "processo": "2", "telefone": "11988", "modo": "rascunho",
         "motivo": "constrição", "data": "2026-06-05", "link": "L2"},
    ]
    txt = montar_lote(itens, "2026-06")
    assert "Prontos pra enviar" in txt and "Revisar antes" in txt
    assert "Ana" in txt and "Bia" in txt
    assert "constrição" in txt
    assert montar_lote([], "2026-06") is None
