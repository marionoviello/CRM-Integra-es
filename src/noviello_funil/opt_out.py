"""Opt-out / lista de supressão (LGPD, roadmap 1.10).

Respeitar quem pede pra parar de receber é exigência da LGPD e de boa
conduta — e o pior risco reputacional é insistir com quem já disse não.
Este módulo detecta a intenção de descadastro na mensagem do lead e
mantém uma lista de supressão que TODOS os senders de relacionamento
(follow-up, reativação, aniversário, futuros broadcasts) consultam
antes de enviar.

Distinção importante: opt-out vale pra comunicação de RELACIONAMENTO,
não pra transacional do serviço já contratado (ex.: lembrete de uma
reunião que o próprio lead agendou continua — é serviço, não marketing).

Telefone é normalizado pela mesma chave do person_index (tolera 55 e o
9º dígito), pra um "pare" de um número casar com os envios pra ele.
"""

import re

from noviello_funil.person_index import chaves_telefone

# Frases que indicam claramente "pare de me mandar". Já normalizadas
# (lowercase, sem acento via _norm). Exige verbo de PARAR + objeto de
# comunicação, pra não confundir com "me manda o contrato" (pedido).
_PADROES = (
    # "parar/pare/para de mandar" — NÃO casa a preposição "para" sozinha
    # (senão "manda PARA meu email" virava opt-out — bug revisão 15/jun).
    r"\b(par(ar|e)|para de|pra de)\b.{0,20}(mandar|enviar|mensage|manda|receber|encher)",
    r"nao quero (mais )?(receber|mensage|nada)",
    r"descadastr",
    r"sair da lista",
    r"me tira(r)? da lista",
    r"remov[ae].{0,15}(numero|contato|email|e-mail|cadastro)",
    r"cancelar?.{0,15}(recebimento|inscri|cadastro)",
    r"nao me (mande|envie|manda)",
    r"sem (mais )?mensage",
    r"\bunsubscribe\b",
    r"^\s*stop\s*$",
    r"^\s*sair\s*$",
)


def _norm(texto: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", texto)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def detectar_opt_out(texto: object) -> bool:
    """True se a mensagem pede claramente pra parar de receber."""
    if not texto or not isinstance(texto, str):
        return False
    t = _norm(texto)
    return any(re.search(p, t) for p in _PADROES)


def _chave_telefone(telefone: str) -> str | None:
    chaves = chaves_telefone(telefone)
    # usa a forma canônica mais longa (com 9º dígito) como chave estável
    return max(chaves, key=len) if chaves else None


def registrar_opt_out(
    conn, *, telefone: str = "", email: str = "", motivo: str = "",
) -> None:
    """Adiciona telefone e/ou email à lista de supressão. Idempotente."""
    if telefone:
        ch = _chave_telefone(telefone)
        if ch:
            conn.execute(
                "INSERT OR IGNORE INTO opt_out (chave, tipo, motivo) "
                "VALUES (?, 'telefone', ?)",
                (ch, motivo),
            )
    if email:
        conn.execute(
            "INSERT OR IGNORE INTO opt_out (chave, tipo, motivo) "
            "VALUES (?, 'email', ?)",
            (email.strip().lower(), motivo),
        )


def esta_suprimido(conn, *, telefone: str = "", email: str = "") -> bool:
    """True se o telefone OU o email está na lista de supressão."""
    chaves = []
    if telefone:
        chaves.extend(chaves_telefone(telefone))
    if email:
        chaves.append(email.strip().lower())
    chaves = [c for c in chaves if c]
    if not chaves:
        return False
    placeholders = ",".join("?" * len(chaves))
    row = conn.execute(
        f"SELECT 1 FROM opt_out WHERE chave IN ({placeholders}) LIMIT 1",
        tuple(chaves),
    ).fetchone()
    return row is not None
