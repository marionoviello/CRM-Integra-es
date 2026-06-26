"""#39 (25/jun) — motor do caminho B: caso atípico → PDF do contrato.

Costura os 4 componentes numa chamada: redação IA das partes variáveis → montagem
da minuta (fixas intactas) → lint OAB (bloqueia crítico / sinaliza resto) → render
do PDF timbrado. Honorários NUNCA da IA — vêm do valor digitado pelo Mario.

Bloqueio crítico (ex.: promessa de resultado) → re-redige até ``max_redacao``; se
persistir, devolve ``ok=False`` (o caller faz handoff "redige manual", nunca envia
contrato com viola OAB). Alertas (duvidosos) vão no resultado pro review do Mario.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .minuta import DadosMinuta, montar_minuta
from .redacao import Achado, lint_contrato
from .redacao_ia import redigir_partes_variaveis
from .render_pdf import render_contrato_pdf


@dataclass(frozen=True)
class ResultadoMinuta:
    ok: bool
    pdf: bytes | None
    texto: str | None
    alertas: list[Achado]
    bloqueios: list[Achado]


async def gerar_minuta_atipica(
    *,
    client: Any,
    model: str,
    qualificacao: dict[str, str],
    descricao_caso: str,
    honorarios_fixo: str,
    honorarios_exito: str,
    data: str,
    multa_liminar_pct: str = "30% (trinta por cento)",
    max_redacao: int = 2,
) -> ResultadoMinuta:
    """Gera o PDF da minuta atípica. ``qualificacao`` traz os 8 campos ``cliente_*``.
    ``honorarios_*`` são os valores DIGITADOS PELO MARIO (a IA não precifica)."""
    bloqueios_final: list[Achado] = []
    for _tentativa in range(max_redacao):
        partes = await redigir_partes_variaveis(
            client=client, model=model, descricao_caso=descricao_caso,
        )
        objeto = partes.objeto
        if partes.clausula_atipica:
            objeto = f"{objeto}\n\n{partes.clausula_atipica}"
        dados = DadosMinuta(
            **qualificacao,
            objeto=objeto,
            honorarios_fixo=honorarios_fixo,
            honorarios_exito=honorarios_exito,
            multa_liminar_pct=multa_liminar_pct,
            data=data,
        )
        texto = montar_minuta(dados)
        achados = lint_contrato(texto, valor_honorarios=honorarios_fixo)
        bloqueios = [a for a in achados if a.severidade == "bloqueia"]
        alertas = [a for a in achados if a.severidade == "alerta"]
        if not bloqueios:
            return ResultadoMinuta(
                ok=True, pdf=render_contrato_pdf(texto), texto=texto,
                alertas=alertas, bloqueios=[],
            )
        bloqueios_final = bloqueios  # re-redige na próxima volta
    # Lint crítico persistiu → não gera PDF; o caller faz handoff pro Mario.
    return ResultadoMinuta(
        ok=False, pdf=None, texto=None, alertas=[], bloqueios=bloqueios_final,
    )
