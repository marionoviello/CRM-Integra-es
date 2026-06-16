"""Tests for the scope library (escopos, pipeline caminho A)."""

from noviello_funil.escopos import resolver_escopo, tipos_disponiveis


def test_resolver_urbanistico_substitui_placeholders_internos():
    r = resolver_escopo("urbanistico_iptu_regularizacao", substituicoes={
        "{{VALOR_HONORARIOS}}": "3.500,00",
        "{{VALOR_HONORARIOS_EXTENSO}}": "três mil e quinhentos reais",
    })
    assert r is not None
    assert r["area_atuacao"] == "Direito Imobiliário e Urbanístico"
    assert "MANDADO DE SEGURANÇA" in r["objeto_contrato"]
    assert "17.202" in r["contexto_normativo"]
    # a pegadinha: o {{VALOR_HONORARIOS}} dentro do texto foi pré-resolvido
    assert "R$ 3.500,00 (três mil e quinhentos reais)" in r["descricao_honorarios"]
    assert "{{VALOR_HONORARIOS}}" not in r["descricao_honorarios"]
    assert "{{VALOR_HONORARIOS_EXTENSO}}" not in r["descricao_honorarios"]


def test_resolver_sem_substituicoes_mantem_texto():
    r = resolver_escopo("urbanistico_iptu_regularizacao")
    assert r is not None
    assert "{{VALOR_HONORARIOS}}" in r["descricao_honorarios"]   # sem subs, fica cru


def test_resolver_tipo_inexistente_retorna_none():
    assert resolver_escopo("nao_existe") is None


def test_tipos_disponiveis_inclui_urbanistico():
    assert "urbanistico_iptu_regularizacao" in tipos_disponiveis()
