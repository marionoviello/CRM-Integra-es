"""Unit — norma culta na escrita da Julia (pedido Mario 29/ago).

Duas camadas: a instrução na skill (norma culta, sem travessão) e esta
rede de segurança determinística — o modelo adora travessão e a instrução
sozinha vaza. `remover_travessoes` converte travessão de meio de frase em
vírgula; roda em TODO texto gerado que vai ao lead (parse_decisao e
gerar_followup_msg). Bullets de início de linha ficam como estão.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.brain import gerar_followup_msg, parse_decisao, remover_travessoes


def test_travessao_no_meio_vira_virgula():
    # Caso real Kayan 28/ago: "já reservo um horário — qual seu melhor email"
    entrada = "Se quiser, já reservo um horário — qual seu melhor email?"
    assert remover_travessoes(entrada) == (
        "Se quiser, já reservo um horário, qual seu melhor email?"
    )


def test_meia_risca_tambem_e_normalizada():
    entrada = "a cobertura foi negada – isso é comum"
    assert remover_travessoes(entrada) == "a cobertura foi negada, isso é comum"


def test_apos_pontuacao_nao_duplica_virgula():
    entrada = "Perfeito! — vamos marcar"
    assert remover_travessoes(entrada) == "Perfeito! vamos marcar"


def test_travessao_solto_no_fim_da_linha_some():
    assert remover_travessoes("vamos analisar —") == "vamos analisar"
    assert remover_travessoes("vamos analisar —\nAté já!") == "vamos analisar\nAté já!"


def test_travessao_de_inicio_de_linha_fica():
    entrada = "Documentos:\n— RG e CPF\n— Certidão de óbito"
    assert remover_travessoes(entrada) == entrada


def test_texto_limpo_passa_intacto():
    entrada = "Perfeito, anotado! Nossa equipe vai analisar a documentação."
    assert remover_travessoes(entrada) == entrada


def test_multiplos_travessoes_na_mesma_mensagem():
    entrada = "o formal de partilha — de 2023 — já ajuda muito"
    assert remover_travessoes(entrada) == "o formal de partilha, de 2023, já ajuda muito"


def test_parse_decisao_normaliza_mensagem():
    raw = '{"acao": "responder", "mensagem": "Perfeito — vamos marcar"}'
    assert parse_decisao(raw).mensagem == "Perfeito, vamos marcar"


@pytest.mark.asyncio
async def test_gerar_followup_msg_normaliza():
    block = MagicMock()
    block.type = "text"
    block.text = "Oi Maria — conseguiu ver a matrícula?"
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.content = [block]
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    texto = await gerar_followup_msg(
        client=fake_client,
        model="claude-sonnet-4-5",
        skill_content="SKILL",
        conversation_transcript="Lead: oi (há 2 dias)",
    )
    assert texto == "Oi Maria, conseguiu ver a matrícula?"
