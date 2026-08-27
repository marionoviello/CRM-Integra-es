"""Política de LIBERAÇÃO do contrato, por tipo de caso.

Até 26/ago/2026 a liberação era global e sempre humana: o doc nascia em
silêncio (``send_automatic_email=False``) e só ia ao cliente quando o Mario
aprovava o PDF real. Este módulo torna isso decidível POR TIPO DE CASO, para
que um produto padronizado (aéreo do consumidor) rode sem intervenção.

DEFAULT É ``HUMANO``. Um tipo só libera sozinho se estiver EXPLÍCITO na
config — entrada ausente, vazia ou malformada cai no gate humano. É o que
faz esta mudança ser zero-regressão por construção.

Módulo PURO de propósito: sem banco, sem rede, sem settings. Recebe tudo por
parâmetro e devolve decisão. Assim a regra que libera contrato ao cliente é
testável sem subir nada.
"""

from typing import Final

HUMANO: Final = "humano"
AUTOMATICO: Final = "automatico"


def parse_politicas(raw: str) -> dict[str, str]:
    """``"aereo_consumidor:automatico,usucapiao:humano"`` → dict.

    Par sem ``:``, com tipo vazio ou com valor vazio é DESCARTADO (não vira
    entrada nenhuma). Valor que não seja exatamente ``automatico`` vira
    ``HUMANO`` — na dúvida, gate humano.
    """
    mapa: dict[str, str] = {}
    for par in (raw or "").split(","):
        if ":" not in par:
            continue
        tipo, _, valor = par.partition(":")
        tipo, valor = tipo.strip(), valor.strip().lower()
        if not tipo or not valor:
            continue
        mapa[tipo] = AUTOMATICO if valor == AUTOMATICO else HUMANO
    return mapa


def politica_do_tipo(tipo_caso: str, politicas: dict[str, str]) -> str:
    """Política do tipo. Ausente → ``HUMANO``."""
    return politicas.get((tipo_caso or "").strip(), HUMANO)
