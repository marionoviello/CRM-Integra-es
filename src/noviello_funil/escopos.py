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

# Lista CANÔNICA dos tipos de caso que o escritório atende. É a fonte única
# de verdade — a Fase 2 (classificação pelo modelo) vai gerar o enum do
# schema a partir daqui, pra nunca haver tipo que o modelo emite e o catálogo
# desconhece. Ter entrada aqui NÃO significa ter escopo escrito: quem tem
# texto curado é ESCOPOS/tipos_disponiveis().
TIPOS_CASO: list[str] = [
    "aereo_consumidor",
    "urbanistico_iptu_regularizacao",
    "saude_suplementar",
    "sucessorio_inventario",
    "usucapiao",
    "imobiliario_compra_venda",
    "locacao_despejo",
    "condominial",
    "previdenciario_inss",
    "direito_senior",
]

# Honorários INICIAIS (pro labore) de tabela, por tipo de caso:
# ``tipo → (valor, valor_por_extenso)``. Curado pelo advogado, igual ao texto
# do escopo — é o que permite fechar sem humano no circuito SEM violar a
# invariante de que a IA nunca precifica. Tipo ausente daqui exige valor
# informado na chamada (é o caso de todos os que passam por reunião).
# O êxito (ad exitum) NÃO entra aqui: é percentual, vive no texto do escopo.
HONORARIOS_PADRAO: dict[str, tuple[float, str]] = {
    # Decisão Mario 27/ago/2026: R$ 500,00 + 35% de êxito. ATENÇÃO — o
    # contrato-modelo em `Ações 2026\_Geral\Contrato - Aereo.docx` ainda diz
    # R$ 1.000,00 no texto fixo da Cláusula 4ª §1; o valor vigente é ESTE.
    "aereo_consumidor": (500.0, "quinhentos reais"),
}

# Conteúdo curado pelo Mario. Adicionar um tipo = uma entrada aqui (texto vetado).
ESCOPOS: dict[str, dict[str, str]] = {
    "aereo_consumidor": {
        "area_atuacao": "Direito do Consumidor e Direito Aéreo",
        "objeto_contrato": (
            "O objeto do presente contrato é a prestação de serviços "
            "advocatícios, em âmbito judicial e/ou extrajudicial, para a "
            "defesa dos interesses do CONTRATANTE em decorrência de eventos "
            "relativos a falhas na execução do contrato de transporte aéreo, "
            "abrangendo, de forma exemplificativa e não exaustiva, as "
            "seguintes hipóteses de ilícito civil praticado pelas companhias "
            "aéreas ou demais fornecedores envolvidos na cadeia de consumo: "
            "atraso ou cancelamento de voo, total ou parcial; preterição de "
            "embarque (overbooking); extravio, perda, furto, avaria ou atraso "
            "na entrega de bagagem despachada; negativa de assistência "
            "material em solo, incluindo alimentação, comunicação e "
            "hospedagem, nos prazos e formas exigidos pela regulamentação "
            "setorial; perda de conexão que resulte na inviabilidade da "
            "viagem ou no atraso significativo na chegada ao destino final; "
            "alteração unilateral e indevida do contrato de transporte pelo "
            "fornecedor; falha no cumprimento de pacotes turísticos que "
            "envolvam o serviço aéreo; e qualquer outra ocorrência que viole "
            "os direitos do passageiro aéreo estabelecidos no arcabouço "
            "normativo brasileiro. A prestação engloba todas as fases do "
            "processo ou procedimento necessário à plena defesa dos direitos "
            "do CONTRATANTE, desde a análise preliminar da documentação e "
            "fatos, passando pela fase de tentativa de acordo amigável, "
            "instauração de reclamações perante órgãos reguladores ou "
            "plataformas de consumo, até o ajuizamento, acompanhamento e "
            "condução da ação judicial em todas as suas instâncias, inclusive "
            "por meio de recursos cabíveis, procedimentos de cumprimento de "
            "sentença e realização de atos de expropriação, se necessários."
        ),
        "contexto_normativo": (
            "A atividade de defesa dos direitos do passageiro aéreo se insere "
            "no sistema normativo brasileiro que compreende o Código de "
            "Defesa do Consumidor (Lei nº 8.078/90) como norma "
            "principiológica de proteção, a Resolução nº 400/2016 da Agência "
            "Nacional de Aviação Civil (ANAC) e suas alterações, que dispõe "
            "sobre as condições gerais de transporte aéreo e os deveres de "
            "assistência ao passageiro, e os Tratados e Convenções "
            "Internacionais, como a Convenção de Montreal, que podem ter "
            "aplicação preponderante, especialmente nos casos de transporte "
            "aéreo internacional, para a fixação de limites de indenização "
            "por danos materiais, conforme entendimento consolidado pelas "
            "Cortes Superiores brasileiras."
        ),
        "descricao_honorarios": (
            "Honorários iniciais (pro labore): quantia fixa, única e "
            "irrepetível de R$ {{VALOR_HONORARIOS}} "
            "({{VALOR_HONORARIOS_EXTENSO}}), devida pela aceitação do "
            "mandato, pela consultoria jurídica especializada inicial, pela "
            "análise dos documentos e fatos do caso e pela elaboração das "
            "peças inaugurais, paga no ato da assinatura do contrato. "
            "Honorários de êxito (ad exitum): 35% (trinta e cinco por cento) "
            "sobre o proveito econômico bruto total auferido pelo "
            "CONTRATANTE em decorrência da atuação do CONTRATADO, "
            "compreendida a integralidade dos valores recebidos a título de "
            "danos morais, danos materiais, reembolso de passagens ou "
            "serviços e valores oriundos de acordos judiciais ou "
            "extrajudiciais. Os honorários de sucumbência, quando houver "
            "condenação da parte adversa, pertencem integralmente ao "
            "CONTRATADO."
        ),
    },
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
