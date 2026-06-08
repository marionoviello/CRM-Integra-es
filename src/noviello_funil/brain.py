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
    "oferecer_horarios", "confirmar_horario",
})


@dataclass
class Decisao:
    acao: Literal[
        "responder", "propor", "handoff",
        "oferecer_horarios", "confirmar_horario",
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


class DecisaoInvalida(Exception):
    """Claude returned malformed JSON or unknown acao after all retries."""


def load_skill(name: str) -> str:
    """Read a skill .md file from src/noviello_funil/skills/."""
    path = SKILLS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


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
        mensagem=mensagem,
        resumo_caso=data.get("resumo_caso"),
        motivo_handoff=data.get("motivo_handoff"),
        horario_escolhido_iso=data.get("horario_escolhido_iso"),
        lead_email=data.get("lead_email"),
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


async def triagem(
    *,
    client: Any,
    model: str,
    skill_content: str,
    conversation_transcript: str,
) -> Decisao:
    """Send the conversation to Claude and parse a Decisao.

    Retries once on invalid JSON with a stricter instruction.
    """
    user_text = (
        "Abaixo está a transcrição completa da conversa atual com o lead. "
        "Decida a próxima ação seguindo as regras da skill e responda APENAS "
        "com o objeto JSON especificado, sem texto fora dele.\n\n"
        "=== TRANSCRIÇÃO ===\n"
        f"{conversation_transcript}"
    )

    first = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=_build_system(skill_content),
        messages=[{"role": "user", "content": user_text}],
    )
    raw = first.content[0].text

    try:
        return parse_decisao(raw)
    except DecisaoInvalida:
        pass

    retry_text = (
        "Sua resposta anterior não foi JSON válido. Responda AGORA apenas com "
        "o objeto JSON especificado, sem texto antes ou depois, sem markdown."
    )
    second = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=_build_system(skill_content),
        messages=[
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": raw},
            {"role": "user", "content": retry_text},
        ],
    )
    second_raw = second.content[0].text
    try:
        return parse_decisao(second_raw)
    except DecisaoInvalida as exc:
        # Enrich the error with both raw responses so prod debugging
        # doesn't require log archaeology.
        raise DecisaoInvalida(
            f"after retry: {exc}; first_raw={raw!r}; second_raw={second_raw!r}"
        ) from exc


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
    return resp.content[0].text.strip()
