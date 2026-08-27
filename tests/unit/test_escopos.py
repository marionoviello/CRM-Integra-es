"""Tests for the scope library (escopos, pipeline caminho A)."""

from noviello_funil.escopos import (
    ESCOPOS,
    HONORARIOS_PADRAO,
    TIPOS_CASO,
    resolver_escopo,
    tipos_disponiveis,
)


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


def test_aereo_esta_no_catalogo():
    assert "aereo_consumidor" in ESCOPOS
    assert "aereo_consumidor" in tipos_disponiveis()


def test_aereo_tem_os_quatro_textos_preenchidos():
    e = ESCOPOS["aereo_consumidor"]
    for chave in (
        "area_atuacao", "objeto_contrato",
        "contexto_normativo", "descricao_honorarios",
    ):
        assert e.get(chave, "").strip(), f"{chave} vazio"


def test_aereo_resolve_sem_placeholder_pendente():
    """Pegadinha da ZapSign: ela NÃO substitui placeholder dentro do valor de
    outro placeholder. Depois de resolver_escopo, nenhum {{...}} pode sobrar."""
    escopo = resolver_escopo(
        "aereo_consumidor",
        substituicoes={
            "{{VALOR_HONORARIOS}}": "500,00",
            "{{VALOR_HONORARIOS_EXTENSO}}": "quinhentos reais",
        },
    )
    assert escopo is not None
    for chave, texto in escopo.items():
        assert "{{" not in texto, f"placeholder pendente em {chave}: {texto}"


def test_tipos_disponiveis_e_subconjunto_de_tipos_caso():
    """TIPOS_CASO é a lista canônica (tudo que o escritório atende).
    tipos_disponiveis() é o subconjunto com texto curado escrito."""
    assert set(tipos_disponiveis()) <= set(TIPOS_CASO)


def test_tipos_caso_nao_tem_duplicata():
    assert len(TIPOS_CASO) == len(set(TIPOS_CASO))


def test_honorarios_padrao_do_aereo():
    """O valor vem de TABELA CURADA pelo advogado, nunca do modelo
    (invariante I7). R$ 500,00 é decisão do Mario de 27/ago/2026."""
    assert HONORARIOS_PADRAO["aereo_consumidor"] == (500.0, "quinhentos reais")


def test_honorarios_padrao_so_para_tipo_conhecido():
    assert set(HONORARIOS_PADRAO) <= set(TIPOS_CASO)


def test_todo_tipo_com_honorario_padrao_tem_escopo_escrito():
    """Ter preço de tabela sem texto curado geraria contrato sem cláusula.
    Se um dia alguém acrescentar um preço, o escopo tem que vir junto."""
    assert set(HONORARIOS_PADRAO) <= set(tipos_disponiveis())
