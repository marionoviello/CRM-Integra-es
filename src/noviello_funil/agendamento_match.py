"""Casamento DETERMINÍSTICO da escolha de horário do lead (roadmap 1.x bugfix).

Causa-raiz do bug Camila (16/jun): o caminho "lead escolhe horário → confirma"
dependia 100% do Claude retornar ``confirmar_horario``. Quando o caso tinha
outra linha aberta (intake de documentos), o Claude derrapava pro ``responder`` e
DROPAVA a confirmação em silêncio — a conversa rodava até bater o teto de turnos.

Este módulo é a REDE: dado o texto do lead e os horários que o bot ACABOU de
oferecer (persistidos no lead), casa a escolha de forma determinística e devolve
o ISO do slot — sem depender do Claude. É CONSERVADOR de propósito: só casa
quando a escolha é inequívoca (um único slot). Em ambiguidade ('14h' com 3 slots
às 14h) devolve None e deixa o Claude pedir o dia.

LIMITAÇÕES CONHECIDAS (deixadas de propósito pro Claude — regra 6 da skill):
  - Horas por extenso ("duas da tarde", "meio-dia", "às duas") NÃO são
    reconhecidas — só formas numéricas ('14h', '14:00', '14 horas').
  - Negação ("o segundo não") é tratada como ambiguidade máxima → None,
    deferindo ao Claude (preferimos um falso negativo a confirmar um
    horário que o lead RECUSOU).
"""

import datetime
import re
import unicodedata

# weekday() → tokens aceitos (normalizados, sem acento). format_human usa
# "seg/ter/qua/qui/sex/sáb/dom"; o lead pode escrever por extenso.
_WEEKDAY_TOKENS: dict[int, list[str]] = {
    0: ["seg", "segunda"],
    1: ["ter", "terca"],
    2: ["qua", "quarta"],
    3: ["qui", "quinta"],
    4: ["sex", "sexta"],
    5: ["sab", "sabado"],
    6: ["dom", "domingo"],
}

# Seleção por ordinal ("o primeiro", "o segundo", "o último").
# IMPORTANTE: NÃO inclui formas que colidem com dia da semana
# ("segunda","quarta") nem femininas ambíguas ("primeira","terceira") —
# senão "pode ser quarta?" (quarta-feira ausente) cairia no ordinal=3 e
# confirmaria o slot ERRADO. Só masculinos inequívocos + numéricos.
_ORDINAIS: dict[str, int] = {
    "primeiro": 0, "1o": 0, "1a": 0,
    "segundo": 1, "2o": 1, "2a": 1,
    "terceiro": 2, "3o": 2, "3a": 2,
    "quarto": 3, "4o": 3, "4a": 3,
}
_ULTIMO = ("ultimo", "ultima", "por ultimo")

# Negação: lead REJEITA um horário. Texto já normalizado (sem acento).
_NEGACAO_RE = re.compile(r"\b(nao|nem|nenhum|nenhuma)\b")

# "Aceita qualquer slot" → confirma o primeiro (mais cedo).
_QUALQUER_RE = re.compile(
    r"\b(qualquer|tanto\s+faz|o\s+que\s+for|pode\s+ser\s+qualquer)\b"
)


def _norm(s: object) -> str:
    """lower + sem acento (compara texto do lead com label do slot)."""
    txt = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in txt if not unicodedata.combining(c))


def _hora_regexes(dt: datetime.datetime) -> list[re.Pattern[str]]:
    """Regex (com fronteira de dígito) das formas que o lead escreve a hora.

    Fronteira de dígito: '14h' NÃO casa '14h30' (contra-oferta de horário
    não ofertado) e '8h' NÃO casa dentro de '18h'. Zero-padding consistente.
    """
    h, m = dt.hour, dt.minute
    if m:
        # Minuto explícito ('14h30', '14:30', e o cru '1430' que o lead digita
        # sem separador — caso real leopoldinojose 16/jun). O HHMM cru só pra
        # slots COM minuto: '1630'/'1830' raramente são valor solto, enquanto
        # hora cheia ('1400'/'1800') é número redondo comum (evita falso-positivo).
        return [
            re.compile(rf"(?<!\d){h}\s*h\s*{m:02d}(?!\d)"),
            re.compile(rf"(?<!\d){h}\s*:\s*{m:02d}(?!\d)"),
            re.compile(rf"(?<!\d){h:02d}{m:02d}(?!\d)"),
        ]
    # Hora cheia (minuto 0): '14h', '14:00', '14 horas'.
    return [
        re.compile(rf"(?<!\d){h}\s*h(?!\s*\d)"),
        re.compile(rf"(?<!\d){h}\s*:\s*00(?!\d)"),
        re.compile(rf"(?<!\d){h}\s+horas?(?!\d)"),
    ]


def _tem_hora(msg: str, dt: datetime.datetime) -> bool:
    return any(rx.search(msg) for rx in _hora_regexes(dt))


def _mask_horas(msg: str, dts: list[datetime.datetime]) -> str:
    """Mascara as ocorrências de hora na msg (pra tem_dia não vazar o dígito).

    Ex.: '14:00' não pode contar como dia 14 — remove o token de hora antes
    de procurar o número do dia.
    """
    masked = msg
    for dt in dts:
        for rx in _hora_regexes(dt):
            masked = rx.sub(" ", masked)
    return masked


def _sinais(
    msg: str, msg_sem_horas: str, dt: datetime.datetime,
) -> tuple[bool, bool, bool]:
    """(tem_hora, tem_dia_numero, tem_weekday) do slot na mensagem do lead.

    ``msg_sem_horas`` é a mensagem com os tokens de hora REMOVIDOS — usada
    pra tem_dia, pra '14:00'/'14h' não vazar o dígito do dia.
    """
    tem_hora = _tem_hora(msg, dt)
    tem_dia = re.search(rf"\b{dt.day}\b", msg_sem_horas) is not None
    tem_wd = any(
        re.search(rf"\b{tok}\b", msg) for tok in _WEEKDAY_TOKENS[dt.weekday()]
    )
    return tem_hora, tem_dia, tem_wd


def casar_horario_escolhido(
    mensagem: object, horarios_oferecidos: list[dict],
) -> str | None:
    """Casa a escolha do lead com um slot oferecido. Retorna o ISO ou None.

    ``horarios_oferecidos``: ``[{"iso": "...", "label": "ter (16/jun) às 14h"}]``
    (persistido quando o bot ofereceu). Conservador: só casa o INEQUÍVOCO.

    Filtro de horário no passado NÃO é feito aqui (módulo puro, sem relógio):
    fica a cargo do scheduler (Signal 1.8) antes de chamar este matcher.
    """
    msg = _norm(mensagem)
    slots: list[dict] = horarios_oferecidos or []
    if not msg.strip() or not slots:
        return None

    # GUARDA DE NEGAÇÃO (crítico): lead REJEITA um horário ("o segundo não",
    # "quarta não pode", "nenhum desses"). Defere ao Claude — vale ANTES de
    # todos os passos (inclusive o label-substring).
    if _NEGACAO_RE.search(msg):
        return None

    # "Aceita qualquer um" / "tanto faz" → confirma o PRIMEIRO slot (mais
    # cedo na lista). Intenção inequívoca; não pode cair no limbo do Claude.
    if _QUALQUER_RE.search(msg):
        for s in slots:
            if isinstance(s, dict) and isinstance(s.get("iso"), str) and s["iso"]:
                return s["iso"]
        return None

    # 0. Label copiado na íntegra ("Ter (16/jun) às 14h") — sinal mais forte.
    # Coleta TODOS os labels que são substring; só casa se houver EXATAMENTE 1
    # (≥2 → ambíguo, defere ao Claude).
    label_hits: list[str] = []
    for s in slots:
        if not isinstance(s, dict):
            continue
        lbl = _norm(s.get("label"))
        if lbl and lbl in msg and isinstance(s.get("iso"), str) and s["iso"]:
            label_hits.append(s["iso"])
    if len(label_hits) == 1:
        return label_hits[0]
    if len(label_hits) >= 2:
        return None

    # Parse dos datetimes dos slots (uma vez). Pula itens malformados.
    parsed: list[tuple[dict, datetime.datetime]] = []
    for s in slots:
        if not isinstance(s, dict) or not isinstance(s.get("iso"), str):
            continue
        try:
            parsed.append((s, datetime.datetime.fromisoformat(s["iso"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not parsed:
        return None

    # Mensagem sem os tokens de hora (pra tem_dia não vazar o dígito).
    msg_sem_horas = _mask_horas(msg, [dt for _, dt in parsed])

    # 1. Candidatos FORTES: hora + (dia OU weekday). Único → casa.
    fortes = []
    for s, dt in parsed:
        tem_hora, tem_dia, tem_wd = _sinais(msg, msg_sem_horas, dt)
        if tem_hora and (tem_dia or tem_wd):
            fortes.append((s["iso"], 1 + tem_dia + tem_wd))
    if len(fortes) == 1:
        return fortes[0][0]
    if len(fortes) > 1:
        fortes.sort(key=lambda c: -c[1])
        if fortes[0][1] > fortes[1][1]:   # um claramente mais específico
            return fortes[0][0]
        return None                        # empate → ambíguo, Claude pede o dia

    # 2. Só weekday (sem hora), mas que identifica UM slot ('o de quinta').
    por_wd = [
        s["iso"] for s, dt in parsed
        if any(re.search(rf"\b{t}\b", msg) for t in _WEEKDAY_TOKENS[dt.weekday()])
    ]
    if len(por_wd) == 1:
        return por_wd[0]

    # 3. Só hora (sem dia), mas que identifica UM slot ('18h30' único).
    por_hora = [s["iso"] for s, dt in parsed if _tem_hora(msg, dt)]
    if len(por_hora) == 1:
        return por_hora[0]

    # 4. Ordinal ("o primeiro", "o último"). Sem colisão com weekday — ver
    # _ORDINAIS acima.
    if any(u in msg for u in _ULTIMO):
        return parsed[-1][0]["iso"]
    for palavra, idx in _ORDINAIS.items():
        if re.search(rf"\b{re.escape(palavra)}\b", msg) and idx < len(parsed):
            return parsed[idx][0]["iso"]

    return None
