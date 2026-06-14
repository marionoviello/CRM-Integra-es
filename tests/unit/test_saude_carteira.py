"""Tests for the weekly portfolio-health job (saude_carteira).

Cobre 1.2 (monitoramento falhando) + 1.3 (processos parados) do roadmap.
"""

import datetime

from noviello_funil.db import connect, run_migrations
from noviello_funil.saude_carteira import (
    classificar_carteira,
    diff_novos_erros,
    montar_mensagem,
)

HOJE = datetime.date(2026, 6, 14)


def _proc(num, ms, ult_mov, *, secret=False, resp="Mario Noviello"):
    return {
        "processNumber": num,
        "monitoringStatus": ms,
        "lastMovementDate": ult_mov,
        "isSecret": secret,
        "status": "active",
        "responsibles": [{"name": resp}] if resp else [],
    }


# --- classificar_carteira ----------------------------------------------------

def test_erro_entra_na_lista_de_monitoramento():
    diag = classificar_carteira(
        [_proc("1-1", "ERRO", "2026-05-29T03:00:00Z")], HOJE,
    )
    assert len(diag["erro"]) == 1
    assert diag["erro"][0]["processo"] == "1-1"
    assert diag["erro"][0]["responsavel"] == "Mario Noviello"


def test_cadastrado_recente_nao_alerta():
    diag = classificar_carteira(
        [_proc("2-2", "CADASTRADO", "2026-06-10T03:00:00Z")], HOJE,
    )
    assert diag["erro"] == []
    assert diag["parados"] == []


def test_parado_so_conta_monitoramento_ok_e_nao_segredo():
    procs = [
        _proc("ok-parado", "CADASTRADO", "2024-01-01T03:00:00Z"),   # >1 ano → parado
        _proc("erro-velho", "ERRO", "2023-01-01T03:00:00Z"),        # ERRO não entra em parados
        _proc("segredo", "CADASTRADO", "2023-01-01", secret=True),  # segredo fora
    ]
    diag = classificar_carteira(procs, HOJE)
    nums_parados = {p["processo"] for p in diag["parados"]}
    assert nums_parados == {"ok-parado"}
    # erro-velho aparece só em erro
    assert {p["processo"] for p in diag["erro"]} == {"erro-velho"}


def test_parados_ordenados_mais_antigo_primeiro():
    procs = [
        _proc("a", "CADASTRADO", "2025-01-01T03:00:00Z"),
        _proc("b", "CADASTRADO", "2023-01-01T03:00:00Z"),
    ]
    diag = classificar_carteira(procs, HOJE)
    assert [p["processo"] for p in diag["parados"]] == ["b", "a"]
    assert diag["parados"][0]["dias"] > diag["parados"][1]["dias"]


def test_data_invalida_nao_quebra():
    diag = classificar_carteira(
        [_proc("x", "CADASTRADO", None), _proc("y", "CADASTRADO", "lixo")],
        HOJE,
    )
    assert diag["parados"] == []


# --- diff_novos_erros (idempotência / só destaca o que mudou) -----------------

def test_diff_novos_erros_marca_novos_e_persiste():
    conn = connect(":memory:")
    run_migrations(conn)
    # 1ª execução: tudo novo
    novos = diff_novos_erros(conn, ["P1", "P2"])
    assert novos == {"P1", "P2"}
    # 2ª execução: P2 continua, P3 é novo, P1 sumiu (resolvido)
    novos2 = diff_novos_erros(conn, ["P2", "P3"])
    assert novos2 == {"P3"}
    # P1 saiu da tabela (se voltar a dar erro, conta como novo de novo)
    novos3 = diff_novos_erros(conn, ["P1", "P2", "P3"])
    assert novos3 == {"P1"}
    conn.close()


# --- montar_mensagem ---------------------------------------------------------

def test_mensagem_vazia_quando_tudo_saudavel():
    msg = montar_mensagem({"erro": [], "parados": []}, set())
    assert msg is None


def test_mensagem_tem_erro_parados_e_marca_novos():
    diag = {
        "erro": [
            {"processo": "1-1", "responsavel": "Mario", "ultima_mov": "2026-05-29"},
            {"processo": "2-2", "responsavel": "Hilde", "ultima_mov": "2026-04-01"},
        ],
        "parados": [
            {"processo": "9-9", "responsavel": "Mario", "dias": 800},
        ],
    }
    msg = montar_mensagem(diag, novos_erros={"2-2"})
    assert "monitoramento" in msg.lower()
    assert "1-1" in msg and "2-2" in msg and "9-9" in msg
    assert "🆕" in msg            # 2-2 é novo
    assert "800" in msg           # dias parado
    assert "<" not in msg         # WhatsApp-safe


def test_mensagem_so_erro_sem_parados():
    diag = {"erro": [{"processo": "1-1", "responsavel": "M", "ultima_mov": "2026-05-29"}],
            "parados": []}
    msg = montar_mensagem(diag, set())
    assert "1-1" in msg
    assert "parado" not in msg.lower()  # seção de parados omitida
