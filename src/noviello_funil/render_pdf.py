"""#39 (25/jun) — render do contrato (markdown da minuta → PDF timbrado).

reportlab puro-Python (sem dep de sistema no VPS). Converte o subconjunto de
markdown que o template usa (# título, ## cláusula, ---, **negrito**, *itálico*)
em flowables e desenha o **papel timbrado oficial** do escritório (extraído de
``Papel de Carta Padrão.docx``): logo no alto-esquerda, canto geométrico claret no
alto, faixa claret no rodapé com CNPJ/OAB + número de página.
"""

from __future__ import annotations

import html
import re
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

_CLARET = HexColor("#68192E")
_ASSETS = Path(__file__).parent / "assets"
_LOGO = str(_ASSETS / "timbre_logo.png")  # 608x164 RGBA
_HEADER = str(_ASSETS / "timbre_header.jpg")  # 2548x843 (canto geométrico)
_FOOTER = str(_ASSETS / "timbre_footer.jpg")  # 2548x545 (faixa claret)

_TITULO = ParagraphStyle(
    "titulo", fontName="Times-Bold", fontSize=13, leading=16, alignment=TA_CENTER,
    textColor=_CLARET, spaceAfter=14,
)
_CLAUSULA = ParagraphStyle(
    "clausula", fontName="Times-Bold", fontSize=11, leading=14, textColor=_CLARET,
    spaceBefore=10, spaceAfter=4,
)
_CORPO = ParagraphStyle(
    "corpo", fontName="Times-Roman", fontSize=10.5, leading=14, alignment=TA_JUSTIFY,
    spaceAfter=6,
)


def _inline(s: str) -> str:
    """Escapa &<> e converte **negrito**/*itálico* para o markup do reportlab."""
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    return s


def _flowables(minuta_md: str) -> list:
    """Quebra o markdown em blocos (parágrafos separados por linha em branco;
    headings e --- são blocos próprios) e mapeia pro estilo certo."""
    flow: list = []
    buffer: list[str] = []

    def flush():
        if buffer:
            flow.append(Paragraph(_inline(" ".join(buffer)), _CORPO))
            buffer.clear()

    for linha in minuta_md.splitlines():
        s = linha.strip()
        if not s:
            flush()
        elif s.startswith("# "):
            flush()
            flow.append(Paragraph(_inline(s[2:]), _TITULO))
        elif s.startswith("## "):
            flush()
            flow.append(Paragraph(_inline(s[3:]), _CLAUSULA))
        elif s == "---":
            flush()
            flow.append(Spacer(1, 4))
            flow.append(HRFlowable(width="100%", thickness=0.6, color=_CLARET))
            flow.append(Spacer(1, 4))
        else:
            buffer.append(s)
    flush()
    return flow


def _on_page(canvas, doc) -> None:
    """Desenha o papel timbrado oficial em cada página."""
    canvas.saveState()
    w, h = A4
    # canto geométrico claret no alto (full width, proporção mantida)
    hh = w * 843 / 2548
    canvas.drawImage(_HEADER, 0, h - hh, width=w, height=hh, preserveAspectRatio=False)
    # faixa claret no rodapé
    hf = w * 545 / 2548
    canvas.drawImage(_FOOTER, 0, 0, width=w, height=hf, preserveAspectRatio=False)
    # logo no alto-esquerda
    lw = 4.6 * cm
    lh = lw * 164 / 608
    canvas.drawImage(
        _LOGO, 2 * cm, h - 1.3 * cm - lh, width=lw, height=lh, mask="auto",
    )
    # texto institucional (branco) sobre a faixa claret + número da página
    canvas.setFillColor(white)
    canvas.setFont("Times-Roman", 7.5)
    canvas.drawCentredString(
        w / 2, 0.85 * cm,
        "Noviello Advocacia — CNPJ 27.340.554/0001-94 — OAB/SP 21.788 — "
        "Av. do Café, 238, Vila Guarani, São Paulo/SP",
    )
    canvas.drawRightString(w - 1.5 * cm, 0.85 * cm, f"Página {doc.page}")
    canvas.restoreState()


def render_contrato_pdf(minuta_md: str) -> bytes:
    """Renderiza o markdown da minuta em PDF timbrado e devolve os bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=3.8 * cm, bottomMargin=4.7 * cm,
        title="Contrato de Honorários — Noviello Advocacia",
    )
    doc.build(_flowables(minuta_md), onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()
