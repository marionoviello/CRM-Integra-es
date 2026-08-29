"""Brain module: Claude prompt assembly, invocation, and structured parsing.

Strategy:
- Static skill content goes in `system` with cache_control=ephemeral so
  Anthropic caches it (5-min TTL, dramatic latency/cost reduction on
  back-to-back turns of the same conversation).
- Conversation transcript pulled live from Jurichat is the dynamic part
  passed as a `user` message.
- Response is plain text expected to be JSON. We parse and validate.
  If invalid, retry once with a tightened instruction. Then give up.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SKILLS_DIR = Path(__file__).parent / "skills"

VALID_ACOES = frozenset({
    "responder", "propor", "handoff",
    "oferecer_horarios", "confirmar_horario", "remarcar_reuniao",
    "cancelar_reuniao", "encerrar_atendimento",
})

# JSON Schema do Decisao para STRUCTURED OUTPUTS (output_config.format).
# Garante, na API, que a resposta é JSON válido com `acao` no enum e
# `mensagem` presente — elimina a classe de falha de JSON malformado (que
# deixava o lead mudo) e o retry. Os campos opcionais entram como nullable
# (o modelo emite null quando não se aplicam). A regra de mensagem não-vazia
# fica no parse_decisao (structured outputs não suporta minLength).
DECISAO_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "acao", "mensagem", "resumo_caso", "motivo_handoff",
        "horario_escolhido_iso", "lead_email", "lead_recusou_videochamada",
        "pref_dias", "pref_periodo",
    ],
    "properties": {
        "acao": {
            "type": "string",
            "enum": [
                "responder", "propor", "handoff", "oferecer_horarios",
                "confirmar_horario", "remarcar_reuniao", "cancelar_reuniao",
                "encerrar_atendimento",
            ],
        },
        "mensagem": {"type": "string"},
        "resumo_caso": {"type": ["string", "null"]},
        "motivo_handoff": {"type": ["string", "null"]},
        "horario_escolhido_iso": {"type": ["string", "null"]},
        "lead_email": {"type": ["string", "null"]},
        # G1 (auditoria 24/jun, revisão adversarial): o modelo sinaliza quando o
        # lead RECUSOU videochamada (quer presencial / só por escrito / disse não
        # ao vídeo) — NÃO marcar por mera restrição de dia/horário. O scheduler
        # usa isso pra fazer handoff em vez de insistir no Meet. Regex não
        # distinguia "recusa" de "restrição de horário"; o modelo distingue.
        "lead_recusou_videochamada": {"type": "boolean"},
        # Caso José Lucas (03/ago): lead pede dia/período específico ("terça
        # ou quarta À TARDE") — o modelo sinaliza e o scheduler FILTRA a
        # agenda de verdade (antes o texto prometia a tarde e a lista vinha
        # no padrão, contradizendo a mensagem e irritando o lead).
        "pref_dias": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": ["seg", "ter", "qua", "qui", "sex"]},
        },
        "pref_periodo": {"type": ["string", "null"]},
    },
}


@dataclass
class Decisao:
    acao: Literal[
        "responder", "propor", "handoff",
        "oferecer_horarios", "confirmar_horario", "remarcar_reuniao",
        "cancelar_reuniao", "encerrar_atendimento",
    ]
    mensagem: str
    resumo_caso: str | None = None
    motivo_handoff: str | None = None
    # Presente apenas em ``acao = confirmar_horario`` — ISO 8601 com tz
    # offset (ex: ``2026-06-09T14:30:00-03:00``). Claude parseia da
    # transcrição (ele se lembra do que ofereceu + do que lead escolheu).
    horario_escolhido_iso: str | None = None
    # Email do lead, presente em ``confirmar_horario`` quando Claude
    # extraiu da transcrição. Vai como attendee no evento — Google manda
    # convite ICS + Meet link automático.
    lead_email: str | None = None
    # G1 (auditoria 24/jun): True quando o lead RECUSOU videochamada (quer
    # presencial / só por escrito / disse não ao vídeo). NÃO é restrição de
    # dia/horário. O scheduler usa no ramo ``propor`` pra fazer handoff em vez
    # de insistir no Meet.
    lead_recusou_videochamada: bool = False
    # Preferência de agenda do lead (caso José Lucas 03/ago). Presentes em
    # ``oferecer_horarios``/``remarcar_reuniao`` quando o lead pediu dia(s)
    # ("seg".."sex") e/ou período ("manha"/"tarde") — o handler filtra o
    # find_available_slots com isso, pra lista bater com a mensagem.
    pref_dias: list[str] | None = None
    pref_periodo: str | None = None


class DecisaoInvalida(Exception):
    """Claude returned malformed JSON or unknown acao after all retries."""


def load_skill(name: str) -> str:
    """Read a skill .md file from src/noviello_funil/skills/."""
    path = SKILLS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


# Travessão/meia-risca com espaços opcionais em volta (sem cruzar linha).
_RE_TRAVESSAO = re.compile(r"[ \t]*[—–][ \t]*")


def remover_travessoes(texto: str) -> str:
    """Rede de segurança de estilo (pedido Mario 29/ago): o modelo adora
    travessão no meio da frase e a instrução na skill sozinha vaza. Meio de
    frase vira vírgula (ou nada, se já há pontuação antes); travessão de
    início de linha (bullet) fica; travessão solto no fim da linha some.
    """
    def _sub(m: re.Match) -> str:
        antes = texto[: m.start()]
        if not antes.strip() or antes.rstrip(" \t").endswith("\n"):
            return m.group(0)
        depois = texto[m.end():]
        if not depois or depois.startswith("\n"):
            return ""
        if antes.rstrip()[-1] in ",;:.!?…(":
            return " "
        return ", "

    return _RE_TRAVESSAO.sub(_sub, texto)


def parse_decisao(raw: str) -> Decisao:
    """Parse Claude's text response into a Decisao.

    Robust to the most common Claude misbehaviors:
      1. Wrapping the whole response in a ```json``` / ``` fence.
      2. Adding prose before/after a fenced or bare JSON object.

    Strategy:
      a. Try strict parse on the trimmed text.
      b. If that fails, strip a leading/trailing fence and try again.
      c. If still failing, find the first ``{`` and last ``}`` and try
         that substring as JSON.

    Raises DecisaoInvalida on any unrecoverable problem.
    """
    text = raw.strip()

    data: Any = None
    last_err: Exception | None = None

    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            last_err = exc

    if data is None:
        raise DecisaoInvalida(f"not valid json: {last_err}") from last_err

    if not isinstance(data, dict):
        raise DecisaoInvalida(f"expected json object, got {type(data).__name__}")

    acao = data.get("acao")
    if acao not in VALID_ACOES:
        raise DecisaoInvalida(f"unknown acao: {acao!r}")

    mensagem = data.get("mensagem")
    if not isinstance(mensagem, str) or not mensagem.strip():
        raise DecisaoInvalida("mensagem must be non-empty string")

    return Decisao(
        acao=acao,
        mensagem=remover_travessoes(mensagem),
        resumo_caso=data.get("resumo_caso"),
        motivo_handoff=data.get("motivo_handoff"),
        horario_escolhido_iso=data.get("horario_escolhido_iso"),
        lead_email=data.get("lead_email"),
        lead_recusou_videochamada=bool(
            data.get("lead_recusou_videochamada", False)
        ),
        pref_dias=(
            [str(d) for d in data["pref_dias"]]
            if isinstance(data.get("pref_dias"), list) and data["pref_dias"]
            else None
        ),
        pref_periodo=(
            str(data["pref_periodo"]).strip().lower() or None
            if isinstance(data.get("pref_periodo"), str)
            else None
        ),
    )


def _json_candidates(text: str):
    """Yield successive candidate strings that might parse as JSON.

    Each candidate is tried in order; the first to parse wins.
    """
    # 1. The raw text as-is (the happy path: pure JSON, no prose)
    yield text

    # 2. Strip a wrapping ```json``` or ``` fence anywhere in the text
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        yield fenced.group(1).strip()

    # 3. Extract from the first ``{`` to the last ``}`` (handles JSON
    #    surrounded by prose without code fences). Greedy on purpose so
    #    nested braces are preserved.
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        yield text[first_brace : last_brace + 1]


def _build_system(skill_content: str) -> list[dict[str, Any]]:
    """System prompt with prompt caching enabled on the static skill block."""
    return [
        {
            "type": "text",
            "text": skill_content,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _contexto_temporal() -> str:
    """Âncora de data/hora pro modelo gerar ISO de horário com a data certa
    (sem isto ele chuta ano/dia em remarcações e virada de ano)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    return (
        f"CONTEXTO TEMPORAL: agora é {agora.strftime('%Y-%m-%d %H:%M')} "
        "(America/Sao_Paulo). Use isto como referência pra qualquer data/horário."
    )


def _primeiro_texto(resp: Any) -> str | None:
    """Texto do 1º bloco de tipo 'text' da resposta, ou None.

    None quando não há bloco de texto — um refusal de segurança ou truncamento
    por max_tokens NÃO geram bloco de texto. O caller trata como falha
    explícita, em vez de estourar IndexError/AttributeError em silêncio."""
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


async def triagem(
    *,
    client: Any,
    model: str,
    skill_content: str,
    conversation_transcript: str,
) -> Decisao:
    """Send the conversation to Claude and parse a Decisao.

    Usa structured outputs (output_config.format): o Opus 4.8 garante JSON
    válido conforme DECISAO_SCHEMA, então não há mais retry nem a classe de
    falha de JSON malformado (que deixava o lead mudo). parse_decisao valida a
    2ª linha — mensagem não-vazia, que o schema não cobre.
    """
    user_text = (
        f"{_contexto_temporal()}\n\n"
        "Abaixo está a transcrição completa da conversa atual com o lead. "
        "Decida a próxima ação seguindo as regras da skill.\n\n"
        "=== TRANSCRIÇÃO ===\n"
        f"{conversation_transcript}"
    )

    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=_build_system(skill_content),
        messages=[{"role": "user", "content": user_text}],
        output_config={
            "format": {"type": "json_schema", "schema": DECISAO_SCHEMA},
        },
    )
    texto = _primeiro_texto(resp)
    if texto is None:
        stop = getattr(resp, "stop_reason", None)
        raise DecisaoInvalida(
            f"triagem: resposta sem bloco de texto (stop_reason={stop})"
        )
    return parse_decisao(texto)


async def gerar_followup_msg(
    *,
    client: Any,
    model: str,
    skill_content: str,
    conversation_transcript: str,
) -> str:
    """Generate a contextual follow-up message (1st follow-up only).

    Different from `triagem`: returns plain text, not JSON. Claude is
    instructed to write a single short message to send to the lead.
    """
    user_text = (
        "O lead abaixo não respondeu há ~48h. Escreva uma mensagem curta, "
        "natural e empática para retomar a conversa, fazendo referência ao "
        "tema concreto que conversamos. NÃO repita a última mensagem nossa. "
        "Responda APENAS com o texto da mensagem a enviar, sem aspas, sem "
        "preâmbulo.\n\n"
        "=== TRANSCRIÇÃO ===\n"
        f"{conversation_transcript}"
    )

    resp = await client.messages.create(
        model=model,
        max_tokens=512,
        system=_build_system(skill_content),
        messages=[{"role": "user", "content": user_text}],
    )
    texto = _primeiro_texto(resp)
    if texto is None:
        raise DecisaoInvalida("follow-up: resposta sem bloco de texto (refusal?)")
    return remover_travessoes(texto.strip())
