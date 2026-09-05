"""Detector de urgência jurídica na mensagem do lead (roadmap 1.12).

Um lead com prazo fatal ("fui citado, a audiência é amanhã", "penhoraram
minha conta", "meu imóvel vai a leilão") não pode esperar o funil
qualificar com calma — esses são os casos de maior valor e maior risco.
Este detector roda sobre a mensagem do lead e, quando encontra um sinal
de urgência aguda, o scheduler dispara um alerta 🚨 imediato ao Mario
SEM interromper o atendimento normal do bot.

Determinístico (léxico), não-LLM: roda em toda mensagem, custo zero.
Filosofia (roadmap): escalar a mais é seguro — melhor um alerta extra
que perder um caso com prazo fatal. Por isso o léxico é sensível.
"""

import unicodedata

# Termos que, sozinhos, indicam ato judicial de reação imediata.
# Chave já normalizada (lowercase, sem acento); valor = motivo legível.
_FORTES = {
    "citac": "citação recebida",
    "citad": "citação recebida",
    "intimac": "intimação recebida",
    "intimad": "intimação recebida",
    "penhora": "penhora/bloqueio de bens",
    "penhorad": "penhora/bloqueio de bens",
    "bloquea": "bloqueio de valores",
    "bloqueio": "bloqueio de valores",
    "sisbajud": "bloqueio de valores (Sisbajud)",
    "arresto": "arresto/sequestro de bens",
    "sequestro": "arresto/sequestro de bens",
    "leila": "leilão/hasta pública",
    "hasta publica": "leilão/hasta pública",
    "despejo": "despejo",
    "despejad": "despejo",
    "reintegracao de posse": "reintegração de posse",
    "mandado": "mandado judicial",
    "oficial de justica": "oficial de justiça",
    "me processaram": "ação judicial contra o lead",
    "processaram": "ação judicial contra o lead",
    "fui processad": "ação judicial contra o lead",
    "sendo processad": "ação judicial contra o lead",
    "acao contra mim": "ação judicial contra o lead",
    "processo contra mim": "ação judicial contra o lead",
}

# Pedido explícito de socorro.
_SOCORRO = {
    "urgente": "pedido explícito de urgência",
    "urgencia": "pedido explícito de urgência",
    "emergencia": "emergência",
    "socorro": "pedido de socorro",
}

# Termos que só são urgentes COM qualificador temporal.
_PRAZO = {
    "prazo": "prazo iminente",
    "audiencia": "audiência iminente",
}
_TEMPORAIS = (
    "amanha", "hoje", "agora", "vence", "venceu", "acaba",
    "esta semana", "essa semana", "ultimo dia", "ultimas horas",
)


# Sinais de PROPOSTA em jogo (pedido Mario 05/set, caso Kayan): lead
# pedindo/aguardando proposta, orçamento ou minuta é dinheiro na mesa.
# Quem ENVIA proposta é humano — sem alerta, a promessa do bot ("a equipe
# envia por email") morre invisível e o lead esfria.
_PROPOSTA = {
    "proposta": "lead falou de proposta",
    "orcamento": "lead falou de orçamento",
    "minuta": "lead falou de minuta",
    "documento formal": "lead aguarda documento formal",
    "doc formal": "lead aguarda documento formal",
}


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def detectar_urgencia(texto: object) -> str | None:
    """Retorna o motivo da urgência (string curta) ou None.

    None = nenhum sinal de urgência aguda na mensagem.
    """
    if not texto or not isinstance(texto, str):
        return None
    t = _norm(texto)

    for termo, motivo in _FORTES.items():
        if termo in t:
            return motivo
    for termo, motivo in _SOCORRO.items():
        if termo in t:
            return motivo
    if any(temp in t for temp in _TEMPORAIS):
        for termo, motivo in _PRAZO.items():
            if termo in t:
                return motivo
    return None


def detectar_proposta(texto: object) -> str | None:
    """Retorna o motivo (string curta) ou None — mesmo espírito do
    detector de urgência: léxico sensível, escalar a mais é seguro."""
    if not texto or not isinstance(texto, str):
        return None
    t = _norm(texto)
    for termo, motivo in _PROPOSTA.items():
        if termo in t:
            return motivo
    return None
