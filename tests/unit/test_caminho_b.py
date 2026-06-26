"""#39: gerar_minuta_atipica — motor caso→PDF com lint-gate (bloqueia/retry)."""

from unittest.mock import AsyncMock

import pytest

import noviello_funil.caminho_b as cb
from noviello_funil.caminho_b import gerar_minuta_atipica
from noviello_funil.redacao import Achado
from noviello_funil.redacao_ia import PartesRedigidas

_QUALIF = dict(
    cliente_nome="Fulano Teste", cliente_nacionalidade="brasileiro",
    cliente_estado_civil="solteiro", cliente_profissao="comerciante",
    cliente_rg="12.345.678-9 SSP/SP", cliente_cpf="123.456.789-00",
    cliente_endereco="Rua X, 1, São Paulo/SP", cliente_email="f@x.com",
)


def _args():
    return dict(
        client=object(), model="m", qualificacao=_QUALIF,
        descricao_caso="caso atípico de servidão", honorarios_fixo="R$ 5.000,00",
        honorarios_exito="10%", data="25/06/2026",
    )


def _mock_redige(monkeypatch, objeto="Ação atípica de servidão de passagem.", atipica=None):
    monkeypatch.setattr(
        cb, "redigir_partes_variaveis",
        AsyncMock(return_value=PartesRedigidas(objeto=objeto, clausula_atipica=atipica)),
    )


@pytest.mark.asyncio
async def test_lint_limpo_gera_pdf(monkeypatch):
    _mock_redige(monkeypatch)
    monkeypatch.setattr(cb, "lint_contrato", lambda *a, **k: [])
    r = await gerar_minuta_atipica(**_args())
    assert r.ok and r.pdf[:5] == b"%PDF-" and not r.bloqueios
    assert cb.redigir_partes_variaveis.await_count == 1


@pytest.mark.asyncio
async def test_bloqueio_persistente_nao_gera_e_reredige(monkeypatch):
    _mock_redige(monkeypatch)
    monkeypatch.setattr(
        cb, "lint_contrato",
        lambda *a, **k: [Achado("B1", "bloqueia", "promessa de resultado", "...")],
    )
    r = await gerar_minuta_atipica(**_args(), max_redacao=2)
    assert not r.ok and r.pdf is None
    assert r.bloqueios and r.bloqueios[0].regra == "B1"
    assert cb.redigir_partes_variaveis.await_count == 2  # re-redigiu até o teto


@pytest.mark.asyncio
async def test_bloqueio_depois_limpo_gera_no_retry(monkeypatch):
    _mock_redige(monkeypatch)
    bloq = [Achado("B1", "bloqueia", "promessa", "...")]
    monkeypatch.setattr(cb, "lint_contrato", _seq([bloq, []]))
    r = await gerar_minuta_atipica(**_args(), max_redacao=3)
    assert r.ok and r.pdf[:5] == b"%PDF-"
    assert cb.redigir_partes_variaveis.await_count == 2  # 1 bloqueada + 1 limpa


@pytest.mark.asyncio
async def test_alerta_passa_marcado(monkeypatch):
    _mock_redige(monkeypatch)
    monkeypatch.setattr(
        cb, "lint_contrato",
        lambda *a, **k: [Achado("A1", "alerta", "valor fora da faixa", "...")],
    )
    r = await gerar_minuta_atipica(**_args())
    assert r.ok and r.pdf[:5] == b"%PDF-"
    assert r.alertas and r.alertas[0].severidade == "alerta" and not r.bloqueios


def _seq(retornos):
    """lint_contrato síncrono com retornos em sequência (1 por tentativa)."""
    it = iter(retornos)
    return lambda *a, **k: next(it)
