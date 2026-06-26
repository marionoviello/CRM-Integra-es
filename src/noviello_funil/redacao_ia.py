"""#39 (25/jun) — redação por IA das partes VARIÁVEIS do contrato atípico (caminho B).

A IA redige SÓ o objeto (corpo da Cláusula 1ª) e, se o caso exigir, UMA cláusula
atípica. NUNCA menciona valor de honorários — a Cláusula 5ª é preenchida com o valor
DIGITADO PELO MARIO; o schema nem tem campo de honorário (garantia estrutural). As 14
cláusulas fixas nunca passam por aqui. Structured output garante o formato; o lint OAB
(``redacao.lint_contrato``) roda depois sobre o que foi redigido.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Schema do structured output. SEM campo de honorário → a IA não pode precificar.
PARTES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objeto", "clausula_atipica"],
    "properties": {
        "objeto": {"type": "string"},
        "clausula_atipica": {"type": ["string", "null"]},
    },
}

_SYSTEM = (
    "Você é assistente de redação jurídica do escritório Noviello Advocacia. Sua "
    "ÚNICA tarefa é redigir, em português formal, o CORPO da Cláusula 1ª (o OBJETO "
    "do contrato de honorários) e, SE o caso exigir, UMA cláusula atípica adicional. "
    "Regras invioláveis:\n"
    "- NUNCA mencione valor, percentual ou forma de pagamento de honorários (é fixo, "
    "definido fora por um humano).\n"
    "- NUNCA prometa, garanta ou sugira resultado/êxito — obrigação de MEIO, vedação "
    "da OAB. Descreva o que se PRETENDE, não o que se vai conseguir.\n"
    "- NÃO inclua o parágrafo de '1ª instância' nem qualquer cláusula fixa (já "
    "existem no contrato).\n"
    "- Linguagem objetiva, formal, sem floreios. Sem cláusula atípica necessária → "
    "clausula_atipica = null."
)


@dataclass(frozen=True)
class PartesRedigidas:
    objeto: str
    clausula_atipica: str | None


def _texto_da_resposta(resp: Any) -> str | None:
    """Texto do 1º bloco 'text' da resposta (None se refusal/truncamento)."""
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


def parse_partes(texto: str) -> PartesRedigidas:
    d = json.loads(texto)
    return PartesRedigidas(objeto=d["objeto"], clausula_atipica=d.get("clausula_atipica"))


async def redigir_partes_variaveis(
    *, client: Any, model: str, descricao_caso: str,
) -> PartesRedigidas:
    """Claude redige o objeto (+ eventual cláusula atípica) do caso. Structured
    output garante {objeto, clausula_atipica}. Levanta se vier sem texto."""
    user_text = (
        "Redija o objeto da Cláusula 1ª para o caso atípico abaixo. Se o caso exigir "
        "uma cláusula adicional não coberta pelo modelo padrão, redija-a em "
        "clausula_atipica; senão, null.\n\n"
        f"=== CASO ===\n{descricao_caso}"
    )
    resp = await client.messages.create(
        model=model,
        max_tokens=1500,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
        output_config={"format": {"type": "json_schema", "schema": PARTES_SCHEMA}},
    )
    texto = _texto_da_resposta(resp)
    if texto is None:
        raise ValueError("redacao_ia: resposta sem bloco de texto")
    return parse_partes(texto)
