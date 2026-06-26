"""#39: redacao_ia — Claude redige só as partes variáveis, nunca honorários."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.redacao_ia import (
    PARTES_SCHEMA,
    PartesRedigidas,
    parse_partes,
    redigir_partes_variaveis,
)


def _text_block(s: str):
    return SimpleNamespace(type="text", text=s)


def test_schema_nao_tem_campo_de_honorario():
    # Garantia ESTRUTURAL: a IA não pode emitir valor de honorário (não há campo).
    props = PARTES_SCHEMA["properties"]
    assert set(props) == {"objeto", "clausula_atipica"}
    assert not any("honorar" in k.lower() for k in props)


def test_parse_partes_com_e_sem_atipica():
    p = parse_partes('{"objeto":"Ação de X","clausula_atipica":"Cláusula Y"}')
    assert p == PartesRedigidas(objeto="Ação de X", clausula_atipica="Cláusula Y")
    p2 = parse_partes('{"objeto":"Ação de X","clausula_atipica":null}')
    assert p2.clausula_atipica is None


@pytest.mark.asyncio
async def test_redigir_usa_schema_e_proibe_honorario():
    fake = MagicMock()
    resp = MagicMock()
    resp.content = [_text_block('{"objeto":"Ação atípica de Z","clausula_atipica":null}')]
    fake.messages.create = AsyncMock(return_value=resp)

    out = await redigir_partes_variaveis(
        client=fake, model="m", descricao_caso="caso atípico de servidão de passagem",
    )
    assert out == PartesRedigidas(objeto="Ação atípica de Z", clausula_atipica=None)

    kwargs = fake.messages.create.call_args.kwargs
    assert kwargs["output_config"]["format"]["schema"] is PARTES_SCHEMA
    # o system prompt proíbe honorário e promessa de resultado
    assert "honorário" in kwargs["system"] and "resultado" in kwargs["system"]


@pytest.mark.asyncio
async def test_redigir_sem_texto_levanta():
    fake = MagicMock()
    resp = MagicMock()
    resp.content = []  # refusal/truncamento → sem bloco de texto
    fake.messages.create = AsyncMock(return_value=resp)
    with pytest.raises(ValueError, match="sem bloco de texto"):
        await redigir_partes_variaveis(client=fake, model="m", descricao_caso="x")
