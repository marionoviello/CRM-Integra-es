"""Alerta de falha de API/billing na triagem.

Gap aberto desde jul/2026: o crédito da Anthropic zerou e TODA a triagem
morreu com BadRequestError — que cai no catch genérico
(``triagem_unexpected_error``) e NÃO avisa o Mario. Sintoma em campo: a IA
"ficou confusa/muda" em vários leads ao mesmo tempo, sem nenhum alerta.
"""

import pytest

from noviello_funil.scheduler import classificar_erro_api
from noviello_funil.state import deve_alertar_global


class _ErroAPI(Exception):
    """Espelha a superfície do ``anthropic.APIStatusError``: mensagem + status."""

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status_code = status


class APIConnectionError(Exception):
    """Mesmo NOME da exceção de transporte do SDK (a classificação é por nome)."""


# --- classificador --------------------------------------------------------

def test_credito_zerado_e_classificado_como_saldo():
    exc = _ErroAPI(
        "Your credit balance is too low to access the Anthropic API", 400,
    )
    assert classificar_erro_api(exc) == "saldo"


def test_credito_zerado_sem_status_ainda_e_saldo():
    # Nem toda camada preserva o status_code (wrapper/retry pode reembrulhar).
    assert classificar_erro_api(Exception("credit balance is too low")) == "saldo"


@pytest.mark.parametrize("status", [401, 403])
def test_erro_de_autenticacao_e_classificado_como_chave(status):
    assert classificar_erro_api(_ErroAPI("invalid x-api-key", status)) == "chave"


def test_rate_limit():
    assert classificar_erro_api(_ErroAPI("rate limited", 429)) == "rate_limit"


@pytest.mark.parametrize("status", [500, 529])
def test_erro_do_servidor_e_sobrecarga(status):
    assert classificar_erro_api(_ErroAPI("overloaded", status)) == "sobrecarga"


def test_falha_de_transporte_e_conexao():
    assert classificar_erro_api(APIConnectionError("connection error")) == "conexao"


def test_erro_comum_do_nosso_codigo_nao_e_erro_de_api():
    # Bug nosso (TypeError/KeyError) NÃO pode virar alerta de billing — senão o
    # alerta perde valor e o Mario aprende a ignorar.
    assert classificar_erro_api(TypeError("'NoneType' is not subscriptable")) is None
    assert classificar_erro_api(_ErroAPI("bad request: campo x", 400)) is None


# --- cooldown do alerta ---------------------------------------------------

def test_primeiro_alerta_passa_e_o_segundo_nao(db_conn):
    # Sem cooldown, 100 leads na fila = 100 alertas (a enxurrada de 40 alertas
    # F1 de 24/jul mostrou como isso queima o canal).
    assert deve_alertar_global(db_conn, "api:saldo", cooldown_min=60) is True
    assert deve_alertar_global(db_conn, "api:saldo", cooldown_min=60) is False


def test_cooldown_e_por_chave(db_conn):
    assert deve_alertar_global(db_conn, "api:saldo", cooldown_min=60) is True
    assert deve_alertar_global(db_conn, "api:chave", cooldown_min=60) is True


def test_alerta_volta_a_sair_depois_do_cooldown(db_conn):
    assert deve_alertar_global(db_conn, "api:saldo", cooldown_min=60) is True
    db_conn.execute(
        "UPDATE alertas_globais SET ultimo_em = datetime('now', '-61 minutes') "
        "WHERE chave = 'api:saldo'"
    )
    assert deve_alertar_global(db_conn, "api:saldo", cooldown_min=60) is True
