"""Fase 2 do aéreo — coleta determinística e gate de prescrição.

Módulo PURO: não fala com Jurichat, ZapSign, Asaas nem banco. Recebe o que o
lead já respondeu e devolve o que ainda falta, se o CPF confere e se o prazo
está de pé.

Duas decisões de projeto que valem a leitura:

1. A lista de campos do cliente é DERIVADA de ``contrato._VARS_CLIENTE``, não
   repetida aqui. Se o template ganhar um slot novo, a coleta passa a pedir
   sozinha — divergência entre o que se pergunta e o que o contrato exige vira
   erro de teste, não contrato com ``{{...}}`` cru.

2. Prescrição é calculada AQUI, em código, nunca pelo modelo. Errar prazo não é
   bug de software: é perda do direito do cliente.

A ordem devolvida por ``campos_faltantes`` é a ordem de perguntar: primeiro o
caso, depois a identidade. Se o caso for inviável, não se gastou a paciência do
lead pedindo CEP e órgão emissor para no fim recusar.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from .contrato import _VARS_CLIENTE

# --- campos ---------------------------------------------------------------

# Os 16 slots do template, na ordem em que aparecem nele.
CAMPOS_CLIENTE: Final[tuple[str, ...]] = tuple(_VARS_CLIENTE.values())

# ``complemento`` é o único que pode faltar: nem todo endereço tem.
CAMPOS_CLIENTE_OPCIONAIS: Final[frozenset[str]] = frozenset({"complemento"})

# O que a triagem precisa saber ANTES de pedir dado pessoal.
CAMPOS_CASO: Final[tuple[str, ...]] = (
    "tipo_ocorrencia",
    "trecho",
    "data_voo",
    "companhia",
)

TIPOS_OCORRENCIA: Final[tuple[str, ...]] = (
    "atraso",
    "cancelamento",
    "preterição de embarque",
    "extravio de bagagem",
    "avaria de bagagem",
    "perda de conexão",
)

TRECHOS: Final[tuple[str, ...]] = ("doméstico", "internacional")

# STJ, REsp 2.232.322/MT (4ª Turma, rel. Min. Isabel Gallotti, jan/2026): nessas
# hipóteses caiu o dano moral in re ipsa — o passageiro precisa demonstrar lesão
# que ultrapasse o mero aborrecimento. Overbooking e bagagem seguem presumidos.
# Se o entendimento virar repetitivo ou o STF julgar o Tema 1.417, revisar.
EXIGE_PROVA_DE_TRANSTORNO: Final[frozenset[str]] = frozenset(
    {"atraso", "cancelamento", "perda de conexão"}
)

# --- prescrição -----------------------------------------------------------

# Doméstico: CDC, art. 27 (5 anos). Internacional: Convenção de Montreal,
# art. 35, recepcionada pelo Decreto 5.910/2006, com prevalência sobre o CDC
# quanto ao dano material (STF, Tema 210).
PRAZO_ANOS: Final[dict[str, int]] = {"doméstico": 5, "internacional": 2}

FUNDAMENTO_PRAZO: Final[dict[str, str]] = {
    "doméstico": "CDC, art. 27",
    "internacional": "Convenção de Montreal, art. 35",
}

# Janela em que o caso já entra como urgente. No internacional ela é larga de
# propósito: há julgados tratando a prescrição como una (2 anos também para o
# dano moral), então caso internacional não deve passar de 18 meses sem
# notificação. Sobre 2 anos, isso são os 6 meses finais — 183 dias, e não 180,
# porque seis meses de calendário variam de 181 a 184 dias e uma janela curta
# demais deixa passar exatamente a fronteira que ela existe para proteger.
JANELA_URGENCIA_DIAS: Final[dict[str, int]] = {"doméstico": 60, "internacional": 183}


@dataclass(frozen=True)
class Prescricao:
    """Resultado do gate. ``status`` é ``no_prazo``, ``urgente`` ou ``prescrito``."""

    status: str
    limite: date
    dias_restantes: int
    fundamento: str


def calcular_prescricao(data_voo: date, trecho: str, *, hoje: date | None = None) -> Prescricao:
    """Devolve o status do prazo para ``data_voo`` no ``trecho`` informado.

    ``hoje`` existe para o teste fixar a data; em produção fica ``None``.
    Levanta ``ValueError`` em trecho desconhecido — melhor estourar do que
    escolher um prazo por omissão.
    """
    if trecho not in PRAZO_ANOS:
        raise ValueError(f"trecho desconhecido: {trecho!r} (use um de {TRECHOS})")

    hoje = hoje or date.today()
    try:
        limite = data_voo.replace(year=data_voo.year + PRAZO_ANOS[trecho])
    except ValueError:
        # 29/02 em ano não bissexto: cai para 28/02, que é o critério conservador.
        limite = data_voo.replace(month=2, day=28, year=data_voo.year + PRAZO_ANOS[trecho])

    dias = (limite - hoje).days
    if dias < 0:
        status = "prescrito"
    elif dias <= JANELA_URGENCIA_DIAS[trecho]:
        status = "urgente"
    else:
        status = "no_prazo"

    return Prescricao(
        status=status,
        limite=limite,
        dias_restantes=dias,
        fundamento=FUNDAMENTO_PRAZO[trecho],
    )


# --- CPF ------------------------------------------------------------------


def so_digitos(valor: str | None) -> str:
    """Só os dígitos de ``valor``. ``None`` vira string vazia."""
    return re.sub(r"\D", "", valor or "")


def validar_cpf(cpf: str | None) -> bool:
    """Valida CPF pelos dois dígitos verificadores.

    Aceita com ou sem máscara. Rejeita as sequências repetidas (111.111.111-11
    e afins), que passam no cálculo mas não são CPF válido.
    """
    n = so_digitos(cpf)
    if len(n) != 11 or n == n[0] * 11:
        return False

    for tamanho in (9, 10):
        soma = sum(int(n[i]) * (tamanho + 1 - i) for i in range(tamanho))
        digito = (soma * 10) % 11 % 10
        if digito != int(n[tamanho]):
            return False
    return True


# --- coleta ---------------------------------------------------------------


def _vazio(valor: Any) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())


def campos_faltantes(ficha: Mapping[str, Any]) -> list[str]:
    """O que ainda falta perguntar, na ordem de perguntar.

    Caso primeiro, identidade depois. ``complemento`` nunca aparece: é opcional.
    CPF presente mas inválido conta como faltante — pedir de novo é melhor que
    emitir procuração com CPF errado.
    """
    faltam = [c for c in CAMPOS_CASO if _vazio(ficha.get(c))]
    faltam += [
        c for c in CAMPOS_CLIENTE if c not in CAMPOS_CLIENTE_OPCIONAIS and _vazio(ficha.get(c))
    ]
    if "cpf" not in faltam and not validar_cpf(ficha.get("cpf")):
        faltam.append("cpf")
    return faltam


def exige_prova_de_transtorno(tipo_ocorrencia: str) -> bool:
    """Se o subtipo perdeu o dano moral presumido (REsp 2.232.322/MT)."""
    return tipo_ocorrencia in EXIGE_PROVA_DE_TRANSTORNO


def pronta_para_contrato(ficha: Mapping[str, Any]) -> bool:
    """Se a ficha tem tudo que o contrato e a procuração exigem.

    NÃO opina sobre prescrição: caso prescrito pode seguir por decisão do
    advogado, e essa escolha não é deste módulo.
    """
    return not campos_faltantes(ficha)
