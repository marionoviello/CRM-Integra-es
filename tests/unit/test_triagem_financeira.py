"""Tests for the financial triage job (triagem_financeira, roadmap 2.10).

Varre movimentações do DataJud atrás de dinheiro a levantar (RPV,
precatório, alvará) e de constrição (penhora, bloqueio, leilão), alertando
só o canal interno e só uma vez por evento.
"""

import datetime

from noviello_funil.db import connect, run_migrations
from noviello_funil.triagem_financeira import (
    classificar_movimento,
    diff_novos,
    eventos_financeiros,
    evento_hash,
    montar_mensagem,
)

HOJE = datetime.date(2026, 6, 15)
JANELA = 120


# --- classificar_movimento (regra pura) --------------------------------------

def test_constricao_penhora_bloqueio_leilao():
    assert classificar_movimento("Penhora de bens") == "constricao"
    assert classificar_movimento("Bloqueio SISBAJUD de valores") == "constricao"
    assert classificar_movimento("Designada Hasta Pública") == "constricao"
    assert classificar_movimento("Arrematação do imóvel") == "constricao"


def test_levantar_rpv_precatorio_alvara():
    assert classificar_movimento("Expedição de RPV") == "levantar"
    assert classificar_movimento("Expedido Precatório") == "levantar"
    assert classificar_movimento("Expedição de Alvará") == "levantar"
    assert classificar_movimento("Levantamento de depósito judicial") == "levantar"


def test_ruido_nao_classifica():
    assert classificar_movimento("Juntada de petição") is None
    assert classificar_movimento("Conclusos para despacho") is None
    assert classificar_movimento("") is None
    assert classificar_movimento(None) is None


def test_desbloqueio_nao_casa_constricao():
    # "desbloqueio" não tem boundary antes de 'bloqueio' → não vira constrição.
    assert classificar_movimento("Desbloqueio de valores") is None


def test_praca_endereco_nao_vira_constricao():
    # "praça" solto (endereço) não deve disparar — só leilão/hasta/arremata.
    assert classificar_movimento("Intimação na Praça da Sé, 100") is None


def test_constricao_tem_prioridade_sobre_levantar():
    # Texto raro casando os dois sentidos → risco vence.
    assert classificar_movimento("Levantamento de penhora") == "constricao"


# --- eventos_financeiros (janela + montagem) ---------------------------------

def _proc(num, movimentos, *, resp="Mario Noviello"):
    return {
        "processNumber": num,
        "responsibles": [{"name": resp}] if resp else [],
        "movimentos": movimentos,
    }


def test_filtra_so_financeiros_na_janela():
    proc = _proc("1-1", [
        {"nome": "Penhora online", "data": "2026-06-01T10:00:00.000Z"},   # ok
        {"nome": "Juntada de petição", "data": "2026-06-02"},             # ruído
        {"nome": "Expedição de RPV", "data": "2020-01-01"},               # fora janela
    ])
    evs = eventos_financeiros(proc, HOJE, JANELA)
    assert len(evs) == 1
    assert evs[0]["tipo"] == "constricao"
    assert evs[0]["processo"] == "1-1"
    assert evs[0]["responsavel"] == "Mario Noviello"
    assert evs[0]["data"] == "2026-06-01"


def test_movimento_sem_data_ignorado():
    proc = _proc("1-1", [{"nome": "Penhora", "data": None}])
    assert eventos_financeiros(proc, HOJE, JANELA) == []


def test_processo_sem_movimentos_nao_quebra():
    assert eventos_financeiros(_proc("1-1", []), HOJE, JANELA) == []
    assert eventos_financeiros({"processNumber": "x"}, HOJE, JANELA) == []


# --- evento_hash -------------------------------------------------------------

def test_hash_estavel_e_distingue():
    h1 = evento_hash("1-1", "2026-06-01", "Penhora")
    h2 = evento_hash("1-1", "2026-06-01", "Penhora")
    h3 = evento_hash("1-1", "2026-06-02", "Penhora")
    assert h1 == h2
    assert h1 != h3


# --- diff_novos (idempotência, só insere) ------------------------------------

def test_diff_novos_alerta_uma_vez_por_evento():
    conn = connect(":memory:")
    run_migrations(conn)
    ev = {"hash": "abc", "processo": "1-1", "tipo": "constricao"}
    assert diff_novos(conn, [ev]) == {"abc"}
    assert diff_novos(conn, [ev]) == set()   # já alertado, não repete
    conn.close()


def test_diff_novos_dedup_dentro_da_mesma_rodada():
    conn = connect(":memory:")
    run_migrations(conn)
    ev = {"hash": "abc", "processo": "1-1", "tipo": "levantar"}
    assert diff_novos(conn, [ev, ev]) == {"abc"}  # mesmo hash 2x → 1
    conn.close()


# --- montar_mensagem ---------------------------------------------------------

def test_mensagem_none_quando_nada_novo():
    evs = [{"processo": "1", "responsavel": "M", "tipo": "constricao",
            "nome": "Penhora", "data": "2026-06-01", "hash": "h1"}]
    assert montar_mensagem(evs, novos=set()) is None
    assert montar_mensagem([], set()) is None


def test_mensagem_agrupa_constricao_antes_de_levantar():
    evs = [
        {"processo": "1-1", "responsavel": "Mario", "tipo": "levantar",
         "nome": "Expedição de RPV", "data": "2026-06-10", "hash": "h1"},
        {"processo": "2-2", "responsavel": "Hilde", "tipo": "constricao",
         "nome": "Penhora online", "data": "2026-06-12", "hash": "h2"},
    ]
    msg = montar_mensagem(evs, novos={"h1", "h2"})
    assert msg is not None
    assert msg.index("Constrição") < msg.index("Levantar")  # risco primeiro
    assert "1-1" in msg and "2-2" in msg
    assert "<" not in msg  # WhatsApp-safe


def test_mensagem_so_inclui_novos():
    evs = [
        {"processo": "1-1", "responsavel": "Mario", "tipo": "constricao",
         "nome": "Penhora", "data": "2026-06-12", "hash": "novo"},
        {"processo": "9-9", "responsavel": "Mario", "tipo": "constricao",
         "nome": "Penhora antiga", "data": "2026-06-01", "hash": "velho"},
    ]
    msg = montar_mensagem(evs, novos={"novo"})
    assert "1-1" in msg
    assert "9-9" not in msg
