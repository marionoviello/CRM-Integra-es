"""Biblioteca de ESCOPOS de contrato (pipeline de fechamento, caminho A).

Mapeia TIPO_CASO → texto CURADO pelo advogado (área de atuação, objeto, contexto
normativo, descrição de honorários) que alimenta as variáveis {{AREA_ATUACAO}},
{{OBJETO_CONTRATO}}, {{CONTEXTO_NORMATIVO}}, {{DESCRICAO_HONORARIOS}} do template
ZapSign. DETERMINÍSTICO e vetado: a IA NÃO escreve cláusula — só seleciona o
escopo pelo tipo de caso (muito mais seguro OAB que geração livre de texto).

Pegadinha tratada por ``resolver_escopo``: o texto do escopo pode conter
placeholders internos (ex.: a descrição de honorários cita {{VALOR_HONORARIOS}}).
A ZapSign NÃO substitui placeholder DENTRO de um valor, então pré-resolvemos
esses placeholders no texto do escopo aqui, antes de montar o ``data`` do doc.
"""

# Conteúdo curado pelo Mario. Adicionar um tipo = uma entrada aqui (texto vetado).
ESCOPOS: dict[str, dict[str, str]] = {
    "urbanistico_iptu_regularizacao": {
        "area_atuacao": "Direito Imobiliário e Urbanístico",
        "objeto_contrato": (
            "O presente contrato tem como objeto a prestação de serviços "
            "advocatícios pelo CONTRATADO em favor do CONTRATANTE, "
            "exclusivamente em primeira instância, para: (i) propositura de "
            "MANDADO DE SEGURANÇA contra ato do Secretário da SMUL, visando à "
            "análise de processo de regularização de área construída não "
            "averbada; (ii) propositura de AÇÃO DE REPETIÇÃO DE INDÉBITO contra "
            "a Prefeitura Municipal de São Paulo, para restituição de valores "
            "pagos a título de IPTU, em razão da anistia da Lei Municipal nº "
            "17.202/2019. Caso seja necessária atuação em instância(s) "
            "superior(es), será objeto de contratação apartada."
        ),
        "contexto_normativo": (
            "A atuação fundamenta-se na Lei Municipal nº 17.202, de 16 de "
            "outubro de 2019, que estabelece a regularização de edificações "
            "concluídas até 31 de julho de 2014, e na disciplina do mandado de "
            "segurança (Lei nº 12.016/2009)."
        ),
        "descricao_honorarios": (
            "Mandado de segurança: R$ {{VALOR_HONORARIOS}} "
            "({{VALOR_HONORARIOS_EXTENSO}}), em parcela única na assinatura. "
            "Ação de repetição de indébito: percentual sobre o valor economizado "
            "ao final do processo em primeira instância, em caso de êxito."
        ),
    },
    # PENDENTE (Mario fornece o texto):
    #   saude_suplementar  → texto pronto no contrato de planos (arquivo a7287f9a)
    #   aereo_consumidor   → texto pronto no contrato aéreo (arquivo f4e00927)
    #   sucessorio_inventario, previdenciario_inss, direito_senior,
    #   imobiliario_compra_venda, usucapiao, locacao_despejo, condominial → a preencher
}


def tipos_disponiveis() -> list[str]:
    """Tipos de caso com escopo já cadastrado."""
    return list(ESCOPOS.keys())


def resolver_escopo(
    tipo_caso: str, *, substituicoes: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Escopo do tipo de caso, com placeholders internos PRÉ-RESOLVIDOS.

    Retorna ``{area_atuacao, objeto_contrato, contexto_normativo,
    descricao_honorarios}`` com os placeholders de ``substituicoes`` (ex.:
    ``{"{{VALOR_HONORARIOS}}": "3.500,00"}``) já trocados no texto — porque a
    ZapSign não substitui placeholder dentro de valor. None se o tipo não está
    na biblioteca (o orquestrador trata: sem escopo → não gera, avisa o Mario).
    """
    escopo = ESCOPOS.get(tipo_caso)
    if not escopo:
        return None
    subs = substituicoes or {}
    out: dict[str, str] = {}
    for campo, texto in escopo.items():
        for de, para in subs.items():
            texto = texto.replace(de, para)
        out[campo] = texto
    return out
