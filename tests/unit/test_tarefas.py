"""Tests for the daily task agenda / SLA job (tarefas, roadmap 1.8 + 1.9)."""

import datetime

from noviello_funil.tarefas import classificar_tarefas, montar_mensagem

HOJE = datetime.date(2026, 6, 15)


def _task(titulo, final, *, col="Pendente", resp="Mario Noviello",
          arq=False, proc=""):
    return {
        "title": titulo,
        "finalDate": final,
        "column": col,
        "isArchived": arq,
        "responsibles": [{"name": resp}] if resp else [],
        "processNumber": proc,
    }


# --- classificar_tarefas -----------------------------------------------------

def test_vencida_entra_em_vencidas():
    diag = classificar_tarefas([_task("Prazo X", "2026-06-10")], HOJE)
    assert len(diag["vencidas"]) == 1
    assert diag["vencendo"] == []


def test_vencendo_em_3_dias():
    diag = classificar_tarefas([_task("Prazo Y", "2026-06-17")], HOJE)
    assert diag["vencendo"] and not diag["vencidas"]


def test_hoje_conta_como_vencendo():
    diag = classificar_tarefas([_task("Hoje", "2026-06-15")], HOJE)
    assert diag["vencendo"]


def test_concluida_e_arquivada_sao_ignoradas():
    diag = classificar_tarefas(
        [
            _task("Feita", "2026-06-01", col="Concluída"),
            _task("Arquivada", "2026-06-01", arq=True),
        ],
        HOJE,
    )
    assert diag["vencidas"] == [] and diag["vencendo"] == []


def test_futuro_distante_nao_entra():
    diag = classificar_tarefas([_task("Mês que vem", "2026-07-30")], HOJE)
    assert diag["vencidas"] == [] and diag["vencendo"] == []


def test_sem_data_nao_quebra():
    diag = classificar_tarefas(
        [_task("Sem prazo", None), _task("Lixo", "data-ruim")], HOJE,
    )
    assert diag["vencidas"] == [] and diag["vencendo"] == []


def test_vencidas_ordenadas_mais_antiga_primeiro():
    diag = classificar_tarefas(
        [_task("A", "2026-06-12"), _task("B", "2026-06-05")], HOJE,
    )
    assert diag["vencidas"][0]["title"] == "B"  # mais atrasada primeiro


# --- montar_mensagem ---------------------------------------------------------

def test_mensagem_vazia_quando_tudo_em_dia():
    assert montar_mensagem({"vencidas": [], "vencendo": []}, HOJE) is None


def test_mensagem_agrupa_por_responsavel_e_marca_vencidas():
    diag = classificar_tarefas(
        [
            _task("Contestação", "2026-06-10", resp="Mario Noviello"),
            _task("Recurso", "2026-06-08", resp="Hilde"),
            _task("Audiência prep", "2026-06-17", resp="Mario Noviello"),
        ],
        HOJE,
    )
    msg = montar_mensagem(diag, HOJE)
    assert "🔴" in msg and "🟡" in msg
    assert "Mario Noviello" in msg and "Hilde" in msg
    assert "Contestação" in msg and "Recurso" in msg
    assert "<" not in msg  # WhatsApp-safe


def test_mensagem_so_vencendo_sem_vencidas():
    diag = classificar_tarefas([_task("Prazo Z", "2026-06-16")], HOJE)
    msg = montar_mensagem(diag, HOJE)
    assert "🟡" in msg
    assert "🔴" not in msg
