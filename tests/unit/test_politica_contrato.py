"""Testes do registry de política de liberação por tipo de caso."""

from noviello_funil.politica_contrato import (
    AUTOMATICO,
    HUMANO,
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
