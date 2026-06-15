"""Tests for the financial triage job (triagem_financeira, roadmap 2.10).

Varre movimentações do DataJud atrás de dinheiro a levantar (RPV,
precatório, alvará, liberação de depósito) e de constrição (penhora,
bloqueio, leilão), alertando só o canal interno e só uma vez por evento —
e só DEPOIS de o envio dar certo (não enterra alerta financeiro).
"""

import datetime

from noviello_funil.db import connect, run_migrations
from noviello_funil.triagem_financeira import (
    calcular_novos,
    classificar_movimento,
    eventos_financeiros,
    evento_hash,
    fatiar_mensagem,
    marcar_vistos,
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


def test_levantar_liberacao_e_conversao_deposito():
    # Dinheiro ENTRANDO sem a palavra "levantamento" (achado #3 da revisão).
    assert classificar_movimento("Liberação de Depósito") == "levantar"
    assert classificar_movimento("Liberação de valores") == "levantar"
    assert classificar_movimento("Conversão de Depósito em Renda") == "levantar"


def test_ruido_nao_classifica():
    assert classificar_movimento("Juntada de petição") is None
    assert classificar_movimento("Conclusos para despacho") is None
    assert classificar_movimento("Pagamento de custas") is None  # "pagamento" cru fora
    assert classificar_movimento("") is None
    assert classificar_movimento(None) is None


def test_desbloqueio_nao_casa_constricao():
    # "desbloqueio" não tem boundary antes de 'bloqueio' → não vira constrição;
    # e não reivindicamos 'levantar' (precisão) — fica neutro.
    assert classificar_movimento("Desbloqueio de valores") is None
    assert classificar_movimento("Desbloqueio") is None


def test_praca_endereco_nao_vira_constricao():
    assert classificar_movimento("Intimação na Praça da Sé, 100") is None


def test_constricao_tem_prioridade_sobre_levantar():
    assert classificar_movimento("Levantamento de penhora") == "constricao"


# --- eventos_financeiros (janela + identidade) -------------------------------

def _proc(movimentos, *, num="1-1", resp="Mario Noviello"):
    return {
        "processNumber": num,
        "responsibles": [{"name": resp}] if resp else [],
        "movimentos": movimentos,
    }


def test_filtra_so_financeiros_na_janela():
    proc = _proc([
        {"nome": "Penhora online", "data": "2026-06-01T10:00:00Z", "codigo": 246},
        {"nome": "Juntada de petição", "data": "2026-06-02", "codigo": 50},
        {"nome": "Expedição de RPV", "data": "2020-01-01", "codigo": 60},  # fora janela
    ])
    evs = eventos_financeiros(proc, HOJE, JANELA)
    assert len(evs) == 1
    assert evs[0]["tipo"] == "constricao"
    assert evs[0]["processo"] == "1-1"
    assert evs[0]["responsavel"] == "Mario Noviello"
    assert evs[0]["data"] == "2026-06-01"


def test_movimento_sem_data_ignorado():
    proc = _proc([{"nome": "Penhora", "data": None, "codigo": 246}])
    assert eventos_financeiros(proc, HOJE, JANELA) == []


def test_processo_sem_movimentos_nao_quebra():
    assert eventos_financeiros(_proc([]), HOJE, JANELA) == []
    assert eventos_financeiros({"processNumber": "x"}, HOJE, JANELA) == []


def test_identidade_por_codigo_estavel_apesar_do_nome():
    # Mesmo evento (código+timestamp), texto reemitido diferente → 1 hash (#4).
    a = eventos_financeiros(
        _proc([{"nome": "Penhora", "data": "2026-06-01T10:00:00Z", "codigo": 246}]),
        HOJE, JANELA,
    )[0]
    b = eventos_financeiros(
        _proc([{"nome": "PENHORA de R$ 1.000,00", "data": "2026-06-01T10:00:00Z",
                "codigo": 246}]),
        HOJE, JANELA,
    )[0]
    assert a["hash"] == b["hash"]


def test_dois_eventos_mesmo_dia_nao_colidem():
    # Dois eventos distintos no mesmo dia (códigos/horas diferentes) → 2 hashes (#5).
    evs = eventos_financeiros(_proc([
        {"nome": "Penhora", "data": "2026-06-01T10:00:00Z", "codigo": 246},
        {"nome": "Expedição de Alvará", "data": "2026-06-01T15:00:00Z", "codigo": 60},
    ]), HOJE, JANELA)
    assert len(evs) == 2
    assert evs[0]["hash"] != evs[1]["hash"]


def test_fallback_nome_quando_sem_codigo():
    a = eventos_financeiros(
        _proc([{"nome": "Penhora", "data": "2026-06-01T10:00:00Z", "codigo": None}]),
        HOJE, JANELA,
    )[0]
    b = eventos_financeiros(
        _proc([{"nome": "penhora", "data": "2026-06-01T10:00:00Z"}]),  # sem codigo
        HOJE, JANELA,
    )[0]
    assert a["hash"] == b["hash"]


def test_evento_hash_estavel_e_distingue():
    assert evento_hash("1-1", "2026-06-01T10:00:00Z", "246") == \
        evento_hash("1-1", "2026-06-01T10:00:00Z", "246")
    assert evento_hash("1-1", "2026-06-01T10:00:00Z", "246") != \
        evento_hash("1-1", "2026-06-01T11:00:00Z", "246")


# --- calcular_novos / marcar_vistos (idempotência pós-envio) -----------------

def _ev(h, tipo="constricao", proc="1-1"):
    return {"hash": h, "processo": proc, "tipo": tipo}


def test_calcular_novos_nao_grava():
    # Só LER não persiste — chamar de novo ainda traz o mesmo (achado #1).
    conn = connect(":memory:")
    run_migrations(conn)
    assert calcular_novos(conn, [_ev("abc")]) == {"abc"}
    assert calcular_novos(conn, [_ev("abc")]) == {"abc"}  # NÃO foi gravado
    conn.close()


def test_marcar_vistos_depois_calcular_pula():
    conn = connect(":memory:")
    run_migrations(conn)
    evs = [_ev("abc")]
    assert calcular_novos(conn, evs) == {"abc"}
    marcar_vistos(conn, evs, {"abc"})
    assert calcular_novos(conn, evs) == set()  # agora sim, visto
    conn.close()


def test_calcular_novos_dedup_na_rodada():
    conn = connect(":memory:")
    run_migrations(conn)
    assert calcular_novos(conn, [_ev("abc"), _ev("abc")]) == {"abc"}
    conn.close()


def test_marcar_vistos_so_marca_o_enviado_resto_reaparece():
    # Truncados não são marcados → reaparecem (sem perda silenciosa, #2).
    conn = connect(":memory:")
    run_migrations(conn)
    evs = [_ev("a"), _ev("b")]
    assert calcular_novos(conn, evs) == {"a", "b"}
    marcar_vistos(conn, evs, {"a"})            # só "a" foi enviado
    assert calcular_novos(conn, evs) == {"b"}  # "b" ainda novo
    conn.close()


def test_marcar_vistos_vazio_nao_quebra():
    conn = connect(":memory:")
    run_migrations(conn)
    marcar_vistos(conn, [], set())
    conn.close()


# --- montar_mensagem (retorna texto + incluídos) -----------------------------

def test_mensagem_none_quando_nada_novo():
    evs = [_ev("h1")]
    assert montar_mensagem(evs, novos=set()) == (None, set())
    assert montar_mensagem([], set()) == (None, set())


def test_mensagem_agrupa_constricao_antes_de_levantar():
    evs = [
        {"processo": "1-1", "responsavel": "Mario", "tipo": "levantar",
         "nome": "Expedição de RPV", "data": "2026-06-10", "hash": "h1"},
        {"processo": "2-2", "responsavel": "Hilde", "tipo": "constricao",
         "nome": "Penhora online", "data": "2026-06-12", "hash": "h2"},
    ]
    texto, incluidos = montar_mensagem(evs, novos={"h1", "h2"})
    assert texto is not None
    assert texto.index("Constrição") < texto.index("Levantar")
    assert "1-1" in texto and "2-2" in texto
    assert "<" not in texto
    assert incluidos == {"h1", "h2"}


def test_mensagem_so_inclui_novos():
    evs = [
        {"processo": "1-1", "responsavel": "Mario", "tipo": "constricao",
         "nome": "Penhora", "data": "2026-06-12", "hash": "novo"},
        {"processo": "9-9", "responsavel": "Mario", "tipo": "constricao",
         "nome": "Penhora antiga", "data": "2026-06-01", "hash": "velho"},
    ]
    texto, incluidos = montar_mensagem(evs, novos={"novo"})
    assert "1-1" in texto
    assert "9-9" not in texto
    assert incluidos == {"novo"}


def test_mensagem_truncamento_e_lossless():
    # Mais que MAX_LISTA numa categoria → exibe o cap, incluídos = só o exibido.
    from noviello_funil.triagem_financeira import MAX_LISTA
    evs = [
        {"processo": f"p{i}", "responsavel": "Mario", "tipo": "constricao",
         "nome": "Penhora", "data": f"2026-06-{(i % 28) + 1:02d}", "hash": f"h{i}"}
        for i in range(MAX_LISTA + 5)
    ]
    novos = {e["hash"] for e in evs}
    texto, incluidos = montar_mensagem(evs, novos)
    assert "e mais 5" in texto
    assert len(incluidos) == MAX_LISTA          # só o exibido é marcável
    assert len(novos - incluidos) == 5          # os 5 truncados reaparecem


# --- fatiar_mensagem (chunking sob o teto do WhatsApp) -----------------------

def test_fatiar_curto_um_bloco():
    assert fatiar_mensagem("linha única", limite=100) == ["linha única"]


def test_fatiar_quebra_por_tamanho_preservando_linhas():
    linhas = [f"linha {i} " + "x" * 40 for i in range(20)]
    texto = "\n".join(linhas)
    blocos = fatiar_mensagem(texto, limite=200)
    assert len(blocos) > 1
    assert all(len(b) <= 200 for b in blocos)
    # Reconstrói exatamente as mesmas linhas (nenhuma cortada no meio).
    assert "\n".join(blocos).split("\n") == linhas


def test_fatiar_linha_gigante_vai_sozinha():
    gigante = "y" * 500
    blocos = fatiar_mensagem(f"curta\n{gigante}\noutra", limite=100)
    assert gigante in blocos  # vai como seu próprio bloco, não trava
