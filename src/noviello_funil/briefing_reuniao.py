"""Briefing pré-reunião pra equipe (roadmap 3.2).

No lembrete de 2h que já roda, manda pro canal INTERNO (Mario + Hilde) um
dossiê de quem está chegando: se é cliente da casa, lista os processos com a
última movimentação — pra equipe chegar à reunião sem garimpar três sistemas.

Read-only e interno. O contexto do processo vem instantâneo do
``cliente_processo`` (telefone↔processo autenticado por person_id, já no
banco) — sem DataJud, sem Claude. Diferente do boletim (cliente-facing), aqui
processos SIGILOSOS ENTRAM no briefing (a equipe é quem cuida deles) — só
ficam marcados 🔒 pra lembrar do cuidado.
"""

import re


def _fmt_data(iso: object) -> str:
    s = str(iso or "")[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def montar_briefing(
    nome: object,
    telefone: object,
    horario: str,
    meet_link: str,
    processos: list[dict],
) -> str:
    """Dossiê interno pra equipe. ``processos`` = saída de
    ``consultar_processos_do_telefone`` (pode ser [])."""
    nome_str = str(nome or "").strip() or "Lead"
    linhas = [
        "📋 *Briefing — reunião em ~2h*",
        f"👤 {nome_str} ({telefone})",
        f"🗓️ {horario}" + (f" · Meet: {meet_link}" if meet_link else ""),
    ]
    if processos:
        linhas.append(f"\n🗂️ *Cliente da casa* — {len(processos)} processo(s):")
        for p in processos[:8]:
            num = p.get("process_number") or "(sem número)"
            data = _fmt_data(p.get("last_movement_date"))
            sig = " 🔒 sigiloso" if p.get("is_secret") else ""
            linha = f"• {num}" + (f" — última mov {data}" if data else "") + sig
            linhas.append(linha)
        if len(processos) > 8:
            linhas.append(f"… e mais {len(processos) - 8}.")
    else:
        linhas.append("\n🆕 Não consta como cliente (lead novo ou número novo).")
    linhas.append("\nHistórico da conversa: no WhatsApp do lead.")
    return "\n".join(linhas)
