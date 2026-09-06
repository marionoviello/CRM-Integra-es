"""Modelo ZapSign escolhido POR TIPO DE CASO (aéreo, 06/set/2026).

O template_id deixa de ser obrigatório no JSON do ``gerar_contrato.py``: se
ausente, vem de ``ZAPSIGN_TEMPLATE_POR_TIPO`` ("tipo:token,tipo:token").
Tipo sem entrada → ``None`` (o chamador decide falhar alto). O token é
copiado como veio — UUID não sofre normalização de caixa.
"""

from noviello_funil.contrato import parse_templates_por_tipo, template_do_tipo

TOKEN = "2a4b99ec-48f3-4686-acbc-9d24b2a497f3"


def test_parse_vazio():
    assert parse_templates_por_tipo("") == {}
    assert parse_templates_por_tipo(None) == {}


def test_parse_um_par():
    assert parse_templates_por_tipo(f"aereo_consumidor:{TOKEN}") == {
        "aereo_consumidor": TOKEN,
    }


def test_parse_varios_pares_espacos_e_caixa_da_chave():
    raw = f" AEREO_CONSUMIDOR : {TOKEN} , saude_suplementar:abc "
    assert parse_templates_por_tipo(raw) == {
        "aereo_consumidor": TOKEN,
        "saude_suplementar": "abc",
    }


def test_parse_ignora_par_malformado():
    assert parse_templates_por_tipo(f"semdoispontos,:{TOKEN},aereo:") == {}


def test_template_do_tipo_normaliza_o_tipo():
    mapa = {"aereo_consumidor": TOKEN}
    assert template_do_tipo(" Aereo_Consumidor ", mapa) == TOKEN


def test_template_do_tipo_ausente_e_none():
    assert template_do_tipo("usucapiao", {"aereo_consumidor": TOKEN}) is None
    assert template_do_tipo("", {}) is None
