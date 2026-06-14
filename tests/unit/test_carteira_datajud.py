"""Tests for the DataJud cross-check job (carteira_datajud).

Cobre o furo do saude_carteira: processos que o Juridiq mostra como OK
(monitoringStatus != ERRO, lastMovementDate recente) mas que o tribunal
moveu e o Juridiq NÃO capturou — só o cruzamento com o DataJud pega.
"""

from noviello_funil.carteira_datajud import (
    _alias_datajud,
    classificar,
    diff_novos,
    eh_silenciosa,
    montar_mensagem,
)
from noviello_funil.db import connect, run_migrations

LIMIAR = 30


def _proc(num, ms, jq_mov, dj_mov, *, resp="Mario Noviello"):
    return {
        "processNumber": num,
        "monitoringStatus": ms,
        "lastMovementDate": jq_mov,
        "responsibles": [{"name": resp}] if resp else [],
        "dj_date": dj_mov,
    }


# --- eh_silenciosa (regra pura) ----------------------------------------------

def test_cadastrado_tribunal_a_frente_eh_silenciosa():
    # Juridiq parado em 2020, tribunal moveu em 2026 → falha silenciosa.
    assert eh_silenciosa("2020-12-01", "2026-03-16", "CADASTRADO", LIMIAR) is True


def test_erro_nao_eh_silenciosa_ja_vai_no_saude_carteira():
    # ERRO já é alertado pelo saude_carteira — não duplica aqui.
    assert eh_silenciosa("2024-06-07", "2026-03-16", "ERRO", LIMIAR) is False


def test_em_dia_dentro_do_limiar_nao_alerta():
    assert eh_silenciosa("2026-05-01", "2026-05-20", "CADASTRADO", LIMIAR) is False


def test_atraso_no_limite_exato_nao_alerta():
    # exatamente 30 dias não passa do limiar (> estrito).
    assert eh_silenciosa("2026-01-01", "2026-01-31", "CADASTRADO", LIMIAR) is False


def test_sem_dj_nao_alerta():
    # Sem dado no DataJud não há com o que comparar.
    assert eh_silenciosa("2026-01-01", None, "CADASTRADO", LIMIAR) is False


def test_jq_ausente_com_dj_alerta():
    # Juridiq nunca capturou nada, mas o tribunal tem movimento.
    assert eh_silenciosa(None, "2026-03-16", "CADASTRADO", LIMIAR) is True


def test_status_none_tambem_alerta():
    assert eh_silenciosa("2020-01-01", "2026-01-01", None, LIMIAR) is True


# --- classificar -------------------------------------------------------------

def test_classificar_filtra_e_ordena_por_movimento_recente():
    procs = [
        _proc("antigo", "CADASTRADO", "2020-01-01", "2024-06-19"),
        _proc("recente", "CADASTRADO", "2025-10-14", "2026-05-04"),
        _proc("erro", "ERRO", "2024-06-07", "2026-03-16"),       # fora
        _proc("emdia", "CADASTRADO", "2026-05-01", "2026-05-10"),  # fora
    ]
    flags = classificar(procs, LIMIAR)
    # só os dois CADASTRADO atrasados, mais recente no tribunal primeiro.
    assert [f["processo"] for f in flags] == ["recente", "antigo"]
    assert flags[0]["trib_mov"] == "2026-05-04"
    assert flags[0]["responsavel"] == "Mario Noviello"


def test_classificar_calcula_atraso_em_dias():
    flags = classificar([_proc("p", "CADASTRADO", "2026-01-01", "2026-03-02")], LIMIAR)
    assert flags[0]["atraso_dias"] == 60


def test_classificar_jq_ausente_marca_sem_data():
    flags = classificar([_proc("p", "CADASTRADO", None, "2026-03-02")], LIMIAR)
    assert flags[0]["atraso_dias"] == "sem data no Juridiq"
    assert flags[0]["jq_mov"] == ""


# --- diff_novos (idempotência) -----------------------------------------------

def test_diff_novos_marca_novos_e_persiste():
    conn = connect(":memory:")
    run_migrations(conn)
    assert diff_novos(conn, ["P1", "P2"]) == {"P1", "P2"}
    assert diff_novos(conn, ["P2", "P3"]) == {"P3"}      # P2 já visto
    assert diff_novos(conn, ["P1", "P2", "P3"]) == {"P1"}  # P1 saiu e voltou
    conn.close()


# --- montar_mensagem ---------------------------------------------------------

def test_mensagem_vazia_quando_nada_a_reportar():
    assert montar_mensagem([], set()) is None


def test_mensagem_lista_marca_novos_e_e_whatsapp_safe():
    falhas = [
        {"processo": "1-1", "responsavel": "Mario", "jq_mov": "2020-12-01",
         "trib_mov": "2026-03-16", "atraso_dias": 1931, "status": "CADASTRADO"},
        {"processo": "2-2", "responsavel": "Hilde", "jq_mov": "",
         "trib_mov": "2026-02-13", "atraso_dias": "sem data no Juridiq",
         "status": "CADASTRADO"},
    ]
    msg = montar_mensagem(falhas, novos={"2-2"})
    assert "1-1" in msg and "2-2" in msg
    assert "🆕" in msg          # 2-2 é novo
    assert "DataJud" in msg
    assert "<" not in msg        # WhatsApp-safe


# --- _alias_datajud (mapa de tribunais) --------------------------------------

def test_alias_resolve_tjsp_e_trf3():
    assert _alias_datajud("5023249-42.2023.4.03.6183") == "trf3"
    assert _alias_datajud("1059803-85.2023.8.26.0002") == "tjsp"


def test_alias_tribunal_nao_mapeado():
    assert _alias_datajud("0000000-00.2020.9.99.0000") is None
