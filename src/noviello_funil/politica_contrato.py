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
        # Config é editada à mão no .env — normaliza os DOIS lados (chave e
        # valor). politica_do_tipo também normaliza (.strip().lower()) o
        # tipo_caso recebido em runtime: os dois lados do lookup precisam
        # concordar, senão "AEREO_CONSUMIDOR:automatico" vira uma chave que
        # nunca bate — falha SILENCIOSA (cai em HUMANO sem avisar por quê),
        # pior que uma falha ruidosa.
        tipo, valor = tipo.strip().lower(), valor.strip().lower()
        if not tipo or not valor:
            continue
        mapa[tipo] = AUTOMATICO if valor == AUTOMATICO else HUMANO
    return mapa


def politica_do_tipo(tipo_caso: str, politicas: dict[str, str]) -> str:
    """Política do tipo. Ausente → ``HUMANO``.

    Normaliza (.strip().lower()) o mesmo jeito que parse_politicas normaliza
    a chave — hoje tipo_caso vem de código nosso (sempre minúsculo), mas na
    Fase 2 vem de um LLM, onde a caixa deixa de estar sob nosso controle. Sem
    normalizar os dois lados, uma variação de caixa não bate no dict e cai em
    HUMANO sem avisar por quê — falha fechada, mas silenciosa.
    """
    return politicas.get((tipo_caso or "").strip().lower(), HUMANO)


def decidir_liberacao(
    *,
    tipo_caso: str,
    politicas: dict[str, str],
    valor_honorarios: float,
    teto_automatico: float,
    tem_contra_assinante: bool,
) -> tuple[bool, str]:
    """Libera a assinatura sozinho? Devolve ``(libera, motivo)``.

    Motivos possíveis: ``politica_automatica`` (libera), ``politica_humana``,
    ``sem_contra_assinante``, ``acima_do_teto``.

    Os freios DUROS do pipeline (conflito de interesse, escopo ausente, CPF
    inválido, sem canal de contato) já barraram antes — nada chega aqui sem
    ter passado por eles. Aqui só ficam os freios da LIBERAÇÃO.

    Ordem importa: ``sem_contra_assinante`` é checado antes do teto porque é
    o freio do fundamento ético, e é o que precisa aparecer no alerta.
    """
    if politica_do_tipo(tipo_caso, politicas) != AUTOMATICO:
        return False, "politica_humana"
    if not tem_contra_assinante:
        return False, "sem_contra_assinante"
    if teto_automatico > 0 and valor_honorarios > teto_automatico:
        return False, "acima_do_teto"
    return True, "politica_automatica"
