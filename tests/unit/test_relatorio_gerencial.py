"""Tests for the weekly management report (relatorio_gerencial, roadmap 2.8/2.9)."""

import datetime

from noviello_funil.relatorio_gerencial import (
    montar_relatorio,
    parse_valor_brl,
    resumir_carteira,
)

HOJE = datetime.date(2026, 6, 15)


# --- parse_valor_brl ---------------------------------------------------------

def test_parse_valor_brl():
    assert parse_valor_brl("R$ 35.180,19") == 35180.19
    assert parse_valor_brl("R$\xa01.000,00") == 1000.0   # non-breaking space
    assert parse_valor_brl("1234,56") == 1234.56
    assert parse_valor_brl(None) == 0.0
    assert parse_valor_brl("") == 0.0
    assert parse_valor_brl("grátis") == 0.0


# --- resumir_carteira --------------------------------------------------------

def _proc(ms="CADASTRADO", *, secret=False, valor=""):
    return {"status": "active", "monitoringStatus": ms,
            "isSecret": secret, "valueOfCause": valor}


def test_resumir_carteira_conta_e_soma():
    procs = [
        _proc("CADASTRADO", valor="R$ 1.000,00"),
        _proc("ERRO", valor="R$ 2.000,50"),
        _proc("SEGREDO", secret=True),
        _proc("CADASTRADO"),
    ]
    r = resumir_carteira(procs)
    assert r["total"] == 4
    assert r["em_segredo"] == 1
    assert r["valor_total"] == 3000.50
    assert r["monitoramento"]["CADASTRADO"] == 2
    assert r["monitoramento"]["ERRO"] == 1


def test_resumir_carteira_vazia():
    r = resumir_carteira([])
    assert r["total"] == 0 and r["valor_total"] == 0.0


# --- montar_relatorio --------------------------------------------------------

def test_relatorio_traz_carteira_e_tarefas():
    carteira = resumir_carteira([
        _proc("CADASTRADO", valor="R$ 500.000,00"),
        _proc("ERRO"),
    ])
    tarefas_resumo = {
        "abertas": 5, "vencidas": 2,
        "por_responsavel": {"Mario Noviello": 4, "Hilde": 1},
    }
    msg = montar_relatorio(carteira, tarefas_resumo, HOJE)
    assert "2 processos" in msg
    assert "500.000" in msg or "500000" in msg or "R$" in msg
    assert "ERRO" in msg or "monitoramento" in msg.lower()
    assert "Mario Noviello" in msg and "Hilde" in msg
    assert "<" not in msg  # WhatsApp-safe


def test_relatorio_sempre_envia():
    # relatório gerencial é um pulso semanal — sempre vai (não silencia)
    msg = montar_relatorio(resumir_carteira([_proc()]), {"abertas": 0, "vencidas": 0, "por_responsavel": {}}, HOJE)
    assert msg is not None
    assert "1 processo" in msg
