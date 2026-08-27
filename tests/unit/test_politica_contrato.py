"""Testes do registry de política de liberação por tipo de caso."""

from noviello_funil.politica_contrato import (
    AUTOMATICO,
    HUMANO,
    decidir_liberacao,
    parse_politicas,
    politica_do_tipo,
)


def test_parse_politicas_vazio():
    assert parse_politicas("") == {}
    assert parse_politicas("   ") == {}


def test_parse_politicas_um_par():
    assert parse_politicas("aereo_consumidor:automatico") == {
        "aereo_consumidor": AUTOMATICO,
    }


def test_parse_politicas_varios_pares_e_espacos():
    raw = " aereo_consumidor : automatico , usucapiao:humano "
    assert parse_politicas(raw) == {
        "aereo_consumidor": AUTOMATICO,
        "usucapiao": HUMANO,
    }


def test_parse_politicas_ignora_par_malformado():
    """Entrada torta na config NÃO pode virar liberação automática acidental."""
    assert parse_politicas("aereo_consumidor,lixo:,:vazio") == {}


def test_parse_politicas_valor_desconhecido_vira_humano():
    """Qualquer coisa que não seja exatamente 'automatico' é gate humano."""
    assert parse_politicas("aereo_consumidor:auto") == {
        "aereo_consumidor": HUMANO,
    }


def test_politica_do_tipo_default_e_humano():
    """Tipo ausente do mapa NUNCA libera sozinho — é o que garante
    zero-regressão: nada migra sem entrada explícita."""
    assert politica_do_tipo("inventario", {}) == HUMANO
    assert politica_do_tipo("", {"aereo_consumidor": AUTOMATICO}) == HUMANO


def test_politica_do_tipo_encontrada():
    mapa = {"aereo_consumidor": AUTOMATICO}
    assert politica_do_tipo("aereo_consumidor", mapa) == AUTOMATICO


def test_parse_politicas_valor_e_case_insensitive():
    """A config é editada à mão no .env — "AUTOMATICO" tem que valer."""
    assert parse_politicas("aereo_consumidor:AUTOMATICO") == {
        "aereo_consumidor": AUTOMATICO,
    }


def test_parse_politicas_chave_e_case_insensitive():
    """Chave em caixa alta no .env não pode virar entrada morta."""
    assert parse_politicas("AEREO_CONSUMIDOR:automatico") == {
        "aereo_consumidor": AUTOMATICO,
    }


def _ctx(**over):
    base = dict(
        tipo_caso="aereo_consumidor",
        politicas={"aereo_consumidor": AUTOMATICO},
        valor_honorarios=500.0,
        teto_automatico=0.0,
        tem_contra_assinante=True,
    )
    base.update(over)
    return base


def test_libera_automatico_no_caminho_feliz():
    libera, motivo = decidir_liberacao(**_ctx())
    assert libera is True
    assert motivo == "politica_automatica"


def test_nao_libera_quando_politica_e_humana():
    libera, motivo = decidir_liberacao(**_ctx(tipo_caso="inventario"))
    assert libera is False
    assert motivo == "politica_humana"


def test_nao_libera_sem_contra_assinante():
    """FREIO DURO. O fundamento que sustenta a liberação automática (decisão
    Mario 26/ago/2026) é a contra-assinatura do escritório no order_group 2.
    Sem contra-assinante configurado, some o fundamento — não libera."""
    libera, motivo = decidir_liberacao(**_ctx(tem_contra_assinante=False))
    assert libera is False
    assert motivo == "sem_contra_assinante"


def test_nao_libera_acima_do_teto():
    libera, motivo = decidir_liberacao(
        **_ctx(valor_honorarios=5000.0, teto_automatico=600.0)
    )
    assert libera is False
    assert motivo == "acima_do_teto"


def test_teto_zero_desliga_a_checagem_de_teto():
    libera, _ = decidir_liberacao(
        **_ctx(valor_honorarios=99999.0, teto_automatico=0.0)
    )
    assert libera is True


def test_valor_exatamente_no_teto_libera():
    libera, _ = decidir_liberacao(
        **_ctx(valor_honorarios=600.0, teto_automatico=600.0)
    )
    assert libera is True


def test_ordem_dos_freios_contra_assinante_antes_do_teto():
    """Os dois freios ativos ao mesmo tempo: reporta o do fundamento ético,
    que é o que o Mario precisa ver primeiro no alerta."""
    libera, motivo = decidir_liberacao(
        **_ctx(tem_contra_assinante=False, valor_honorarios=9e9,
               teto_automatico=10.0)
    )
    assert libera is False
    assert motivo == "sem_contra_assinante"
