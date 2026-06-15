"""Tests for the monthly client briefing (boletim_mensal, roadmap 3.1).

Classificação é WHITELIST (revisão adversarial 15/jun): auto só pra atos
procedimentais neutros; qualquer movimento sensível/desconhecido → rascunho
(fail-safe — um auto tom-surdo iria pré-preenchido ao cliente).
"""

import calendar
import datetime

from noviello_funil.boletim_mensal import (
    classificar_boletim,
    competencia,
    eh_comunicavel_auto,
    melhor_movel,
    montar_lote,
    montar_mensagem_cliente,
    motivo_sensivel,
    movimentos_do_mes,
    na_janela_de_envio,
    ultimo_dia_util_do_mes,
    wa_me_link,
)


def _m(nome):
    return [{"nome": nome, "data": "2026-06-10"}]


# --- calendário --------------------------------------------------------------

def test_ultimo_dia_util_e_dia_de_semana():
    for mes in range(1, 13):
        u = ultimo_dia_util_do_mes(datetime.date(2026, mes, 15))
        assert u.month == mes
        assert u.weekday() < 5
        last = calendar.monthrange(2026, mes)[1]
        assert (datetime.date(2026, mes, last) - u).days <= 2


def test_na_janela_de_envio():
    u = ultimo_dia_util_do_mes(datetime.date(2026, 6, 15))
    assert na_janela_de_envio(u) is True
    antes = u - datetime.timedelta(days=1)
    if antes.month == u.month:
        assert na_janela_de_envio(antes) is False
    fim = datetime.date(2026, 6, calendar.monthrange(2026, 6)[1])
    assert na_janela_de_envio(fim) is True


def test_competencia():
    assert competencia(datetime.date(2026, 6, 30)) == "2026-06"


# --- movimentos do mês + fuso ------------------------------------------------

def test_movimentos_do_mes_filtra():
    movs = [
        {"nome": "a", "data": "2026-06-10T12:00:00Z"},
        {"nome": "b", "data": "2026-05-30"},
        {"nome": "c", "data": None},
    ]
    assert [m["nome"] for m in movimentos_do_mes(movs, 2026, 6)] == ["a"]


def test_movimentos_do_mes_converte_fuso_brt():
    # 2026-07-01 01:00 UTC = 2026-06-30 22:00 BRT → conta em JUNHO.
    movs = [{"nome": "x", "data": "2026-07-01T01:00:00Z"}]
    assert len(movimentos_do_mes(movs, 2026, 6)) == 1
    assert len(movimentos_do_mes(movs, 2026, 7)) == 0


# --- classificação: sensíveis NUNCA viram auto (o coração da revisão) --------

def test_termos_sensiveis_viram_rascunho():
    sensiveis = [
        "Sequestro", "Busca e Apreensão", "Despejo", "Reintegração de Posse",
        "Imissão na Posse", "Prisão Civil", "Confisco", "Remoção de Bens",
        "Avaliação de Bens", "Recuperação Judicial", "Falência",
        "Designação de Praça", "Hasta Pública", "Penhora", "Arresto",
        "Indisponibilidade de Ativos", "Adjudicação", "Arrematação",
        "Expedição de RPV", "RPV", "Pagamento de RPV", "Alvará",
        "Levantamento de Valores", "Sentença", "Acórdão", "Improcedência",
        "Procedência", "Procedência em Parte", "Trânsito em Julgado",
        "Extinção", "Arquivamento dos Autos", "Baixa", "Homologação de Acordo",
        "Intimação para Pagamento",
    ]
    for nome in sensiveis:
        assert motivo_sensivel(_m(nome)) is not None, f"{nome}: motivo None"
        assert classificar_boletim(_m(nome))["modo"] == "rascunho", f"{nome}: virou auto!"


def test_familia_indeferimento_negacao_vira_rascunho():
    # Atos adversos que carregam um substantivo da whitelist (petição/despacho/
    # certidão) — escapavam pro auto antes da revisão 15/jun.
    adversos = [
        "Indeferimento da Petição Inicial",
        "Despacho - Negado seguimento ao recurso",
        "Manifestação - Recurso Inadmitido",
        "Petição de Renúncia ao mandato",
        "Despacho - Indefiro a Justiça Gratuita",
        "Suspensão do Processo (art. 921)",
        "Negado provimento ao recurso",
        "Recurso desprovido",
        "Não conhecido o recurso",
        "Certidão de não interposição de recurso",
    ]
    for nome in adversos:
        assert classificar_boletim(_m(nome))["modo"] == "rascunho", f"{nome}: virou auto!"


def test_procedimentais_viram_auto():
    seguros = [
        "Juntada de Petição", "Conclusão", "Despacho", "Publicação", "Vista",
        "Remessa", "Decurso de Prazo", "Ato Ordinatório", "Certidão",
        "Manifestação", "Designada Audiência",
    ]
    for nome in seguros:
        assert eh_comunicavel_auto(_m(nome)) is True, nome
        assert classificar_boletim(_m(nome))["modo"] == "auto", nome


def test_movimento_desconhecido_vira_rascunho():
    # Fail-safe: o que não está na whitelist nem é sensível → rascunho.
    r = classificar_boletim(_m("Movimento Exótico ZZZ"))
    assert r["modo"] == "rascunho"
    assert r["motivo"] == "movimentação não rotineira"


def test_misto_com_um_sensivel_vira_rascunho():
    movs = [{"nome": "Juntada", "data": "2026-06-01"},
            {"nome": "Penhora", "data": "2026-06-05"}]
    assert classificar_boletim(movs)["modo"] == "rascunho"


def test_classificar_skip_sem_movimento():
    assert classificar_boletim([])["modo"] == "skip"


# --- guardas de destinatário -------------------------------------------------

def test_co_autores_viram_rascunho():
    assert classificar_boletim(_m("Juntada"), multi_cliente=True)["modo"] == "rascunho"


def test_telefone_ambiguo_vira_rascunho():
    assert classificar_boletim(_m("Juntada"), telefone_ambiguo=True)["modo"] == "rascunho"


def test_telefone_fixo_vira_rascunho():
    r = classificar_boletim(_m("Juntada"), telefone_movel=False)
    assert r["modo"] == "rascunho"
    assert "celular" in r["motivo"]


def test_melhor_movel():
    assert melhor_movel({"1133334444", "11933334444"}) == "11933334444"
    assert melhor_movel({"1133334444"}) is None       # só fixo


# --- mensagem + wa.me + lote -------------------------------------------------

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


def test_montar_lote_agrupa_e_avisa_sem_telefone():
    itens = [
        {"nome": "Ana", "processo": "1", "telefone": "11999", "modo": "auto",
         "motivo": "", "data": "2026-06-10", "link": "L1"},
        {"nome": "Bia", "processo": "2", "telefone": "11988", "modo": "rascunho",
         "motivo": "constrição/patrimônio", "data": "2026-06-05", "link": "L2"},
    ]
    txt = montar_lote(itens, "2026-06", sem_telefone=3)
    assert "Prontos pra enviar" in txt and "Revisar antes" in txt
    assert "Ana" in txt and "Bia" in txt
    assert "ficaram de fora" in txt and "3" in txt
    assert montar_lote([], "2026-06") is None
