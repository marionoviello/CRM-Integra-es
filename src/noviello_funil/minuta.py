"""#39 (25/jun) — montagem da minuta do caminho B.

Carrega o template aprovado (``assets/contrato_honorarios_modelo.md``) e preenche
os slots variáveis com os dados do caso. As 14 cláusulas FIXAS passam intactas — a
IA do caminho B redige só ``objeto`` (e eventual cláusula atípica), nunca toca aqui
no esqueleto vetado. O ``honorarios_*`` é o VALOR DIGITADO PELO MARIO (a IA nunca
precifica). Falha alto se sobrar slot (não envia contrato com ``{{...}}`` cru).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

_TEMPLATE = Path(__file__).parent / "assets" / "contrato_honorarios_modelo.md"


@dataclass(frozen=True)
class DadosMinuta:
    """Os slots variáveis do contrato. Nome do campo == nome do slot em UPPER."""

    cliente_nome: str
    cliente_nacionalidade: str
    cliente_estado_civil: str
    cliente_profissao: str
    cliente_rg: str
    cliente_cpf: str
    cliente_endereco: str
    cliente_email: str
    objeto: str
    honorarios_fixo: str
    honorarios_exito: str
    multa_liminar_pct: str
    data: str


def _carregar_template() -> str:
    """Lê o template e tira o comentário de metadados (aviso v1 + doc dos slots) —
    esse bloco não é parte do contrato."""
    texto = _TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"^<!--.*?-->\s*", "", texto, count=1, flags=re.DOTALL)


def montar_minuta(dados: DadosMinuta) -> str:
    """Preenche o template com ``dados`` e devolve o texto do contrato. Levanta
    ``ValueError`` se algum slot ficar sem preencher (template/código divergiram)."""
    texto = _carregar_template()
    for campo, valor in asdict(dados).items():
        texto = texto.replace("{{" + campo.upper() + "}}", valor)
    restantes = re.findall(r"\{\{[A-Z_]+\}\}", texto)
    if restantes:
        raise ValueError(
            f"slots não preenchidos na minuta: {sorted(set(restantes))}"
        )
    return texto
