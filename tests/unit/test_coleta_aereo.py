"""Fase 2 do aéreo: coleta determinística e gate de prescrição."""

from __future__ import annotations

from datetime import date

import pytest

from noviello_funil.coleta_aereo import (
    CAMPOS_CASO,
    CAMPOS_CLIENTE,
    EXIGE_PROVA_DE_TRANSTORNO,
    TIPOS_OCORRENCIA,
    calcular_prescricao,
    campos_faltantes,
    exige_prova_de_transtorno,
    pronta_para_contrato,
    validar_cpf,
)
from noviello_funil.contrato import _VARS_CLIENTE

HOJE = date(2026, 8, 31)

# CPFs fictícios, válidos no dígito verificador (regra do CLAUDE.md: dado real
# de cliente não entra em teste).
CPF_OK = "529.982.247-25"
CPF_OK_2 = "11144477735"


def _ficha_completa(**troca):
    ficha = {
        "tipo_ocorrencia": "atraso",
        "trecho": "doméstico",
        "data_voo": date(2026, 6, 1),
        "companhia": "LATAM",
        "nome_completo": "Fulano Teste",
        "nacionalidade": "Brasileiro(a)",
        "estado_civil": "Solteiro(a)",
        "profissao": "Analista",
        "rg": "00.000.000-0",
        "orgao_emissor": "SSP/SP",
        "cpf": CPF_OK,
        "logradouro": "Rua Teste",
        "numero": "100",
        "complemento": "",
        "bairro": "Centro",
        "cidade": "São Paulo",
        "uf": "SP",
        "cep": "01000-000",
        "celular": "5500000000001",
        "email": "fulano@example.com",
    }
    ficha.update(troca)
    return ficha


# --- campos ---------------------------------------------------------------


def test_campos_cliente_sao_os_do_template_do_contrato():
    """A coleta não pode divergir do que o contrato exige: se o template ganhar
    slot novo, a lista tem que acompanhar sozinha."""
    assert tuple(_VARS_CLIENTE.values()) == CAMPOS_CLIENTE
    assert len(CAMPOS_CLIENTE) == 16


def test_ordem_pergunta_caso_antes_de_identidade():
    faltam = campos_faltantes({})
    assert faltam[: len(CAMPOS_CASO)] == list(CAMPOS_CASO)
    assert "nome_completo" in faltam
    assert faltam.index("tipo_ocorrencia") < faltam.index("nome_completo")


def test_complemento_nao_e_obrigatorio():
    assert "complemento" not in campos_faltantes(_ficha_completa(complemento=""))


def test_ficha_completa_nao_falta_nada():
    assert campos_faltantes(_ficha_completa()) == []
    assert pronta_para_contrato(_ficha_completa()) is True


def test_campo_so_com_espaco_conta_como_vazio():
    assert "cidade" in campos_faltantes(_ficha_completa(cidade="   "))


def test_cpf_invalido_volta_a_ser_perguntado():
    faltam = campos_faltantes(_ficha_completa(cpf="111.111.111-11"))
    assert faltam == ["cpf"]
    assert pronta_para_contrato(_ficha_completa(cpf="111.111.111-11")) is False


def test_cpf_ausente_aparece_uma_vez_so():
    assert campos_faltantes(_ficha_completa(cpf="")).count("cpf") == 1


# --- CPF ------------------------------------------------------------------


@pytest.mark.parametrize("valor", [CPF_OK, CPF_OK_2, "529 982 247 25"])
def test_cpf_valido(valor):
    assert validar_cpf(valor) is True


@pytest.mark.parametrize(
    "valor",
    [
        None,
        "",
        "529.982.247-26",  # dígito trocado
        "5299822472",  # 10 dígitos
        "529982247255",  # 12 dígitos
        "111.111.111-11",  # repetido
        "000.000.000-00",
        "abc",
    ],
)
def test_cpf_invalido(valor):
    assert validar_cpf(valor) is False


# --- prescrição -----------------------------------------------------------


def test_domestico_cinco_anos_no_prazo():
    p = calcular_prescricao(date(2026, 6, 1), "doméstico", hoje=HOJE)
    assert p.status == "no_prazo"
    assert p.limite == date(2031, 6, 1)
    assert p.fundamento == "CDC, art. 27"


def test_internacional_dois_anos_no_prazo():
    p = calcular_prescricao(date(2026, 6, 1), "internacional", hoje=HOJE)
    assert p.status == "no_prazo"
    assert p.limite == date(2028, 6, 1)
    assert p.fundamento == "Convenção de Montreal, art. 35"


def test_domestico_prescrito_no_dia_seguinte_ao_limite():
    p = calcular_prescricao(date(2021, 8, 30), "doméstico", hoje=HOJE)
    assert p.status == "prescrito"
    assert p.dias_restantes < 0


def test_ultimo_dia_ainda_nao_prescreveu():
    """Fronteira: no próprio dia do limite ainda dá para agir."""
    p = calcular_prescricao(date(2021, 8, 31), "doméstico", hoje=HOJE)
    assert p.status != "prescrito"
    assert p.dias_restantes == 0


def test_domestico_urgente_dentro_de_60_dias():
    p = calcular_prescricao(date(2021, 10, 1), "doméstico", hoje=HOJE)
    assert p.status == "urgente"
    assert 0 <= p.dias_restantes <= 60


def test_internacional_urgente_a_partir_de_18_meses():
    """Janela larga de propósito: há julgados tratando a prescrição como una."""
    dezoito_meses = date(2025, 3, 1)  # ~18 meses antes de HOJE
    p = calcular_prescricao(dezoito_meses, "internacional", hoje=HOJE)
    assert p.status == "urgente"


def test_internacional_prescreve_antes_do_domestico():
    voo = date(2023, 1, 10)
    assert calcular_prescricao(voo, "internacional", hoje=HOJE).status == "prescrito"
    assert calcular_prescricao(voo, "doméstico", hoje=HOJE).status == "no_prazo"


def test_voo_em_29_de_fevereiro_nao_estoura():
    p = calcular_prescricao(date(2024, 2, 29), "internacional", hoje=HOJE)
    assert p.limite == date(2026, 2, 28)
    assert p.status == "prescrito"


def test_trecho_desconhecido_falha_alto():
    with pytest.raises(ValueError, match="trecho desconhecido"):
        calcular_prescricao(date(2026, 1, 1), "suborbital", hoje=HOJE)


# --- prova de transtorno --------------------------------------------------


@pytest.mark.parametrize("tipo", ["atraso", "cancelamento", "perda de conexão"])
def test_perderam_o_dano_moral_presumido(tipo):
    assert exige_prova_de_transtorno(tipo) is True


@pytest.mark.parametrize(
    "tipo", ["preterição de embarque", "extravio de bagagem", "avaria de bagagem"]
)
def test_seguem_com_dano_moral_presumido(tipo):
    assert exige_prova_de_transtorno(tipo) is False


def test_todo_tipo_que_exige_prova_e_um_tipo_conhecido():
    assert set(TIPOS_OCORRENCIA) >= EXIGE_PROVA_DE_TRANSTORNO
