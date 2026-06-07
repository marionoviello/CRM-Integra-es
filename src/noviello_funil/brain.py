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

VALID_ACOES = frozenset({"responder", "propor", "handoff"})


@dataclass
class Decisao:
    acao: Literal["responder", "propor", "handoff"]
    mensagem: str
    resumo_caso: str | None = None
    motivo_handoff: str | None = None


class DecisaoInvalida(Exception):
    """Claude returned malformed JSON or unknown acao after all retries."""


def load_skill(name: str) -> str:
    """Read a skill .md file from src/noviello_funil/skills/."""
    path = SKILLS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def parse_decisao(raw: str) -> Decisao:
    """Parse Claude's text response into a Decisao.

    Strips markdown code fences if Claude added them despite instructions.
    Raises DecisaoInvalida on any parse problem.
    """
    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers if present
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DecisaoInvalida(f"not valid json: {exc}") from exc

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
    )


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
    return parse_decisao(second.content[0].text)


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
