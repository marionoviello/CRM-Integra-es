"""#39 (25/jun) — render do contrato (markdown da minuta → PDF timbrado).

reportlab puro-Python (sem dep de sistema no VPS). Converte o subconjunto de
markdown que o template usa (# título, ## cláusula, ---, **negrito**, *itálico*)
em flowables e desenha o timbre da marca (faixa claret + cabeçalho OAB + rodapé
CNPJ + número de página). v1 = timbre de TEXTO; quando o Mario fornecer o PNG do
logo, basta desenhá-lo no ``_on_page`` (slot marcado).
"""

from __future__ import annotations

import html
import re
from io import BytesIO

from reportlab.lib.colors import HexColor, black
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

_CLARET = HexColor("#68192E")

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
    """Timbre: cabeçalho (marca + OAB) + faixa claret + rodapé (CNPJ + página)."""
    canvas.saveState()
    w, h = A4
    # [SLOT LOGO] quando houver PNG: canvas.drawImage(logo, 2*cm, h-2.0*cm, ...)
    canvas.setFillColor(_CLARET)
    canvas.setFont("Times-Bold", 15)
    canvas.drawString(2 * cm, h - 1.5 * cm, "NOVIELLO ADVOCACIA")
    canvas.setFillColor(black)
    canvas.setFont("Times-Roman", 8)
    canvas.drawString(
        2 * cm, h - 1.95 * cm, "Mario Luiz Noviello Junior — OAB/SP 370.796",
    )
    canvas.setStrokeColor(_CLARET)
    canvas.setLineWidth(1.2)
    canvas.line(2 * cm, h - 2.15 * cm, w - 2 * cm, h - 2.15 * cm)
    # rodapé
    canvas.setFont("Times-Roman", 7)
    canvas.setFillColor(black)
    canvas.drawCentredString(
        w / 2, 1.2 * cm,
        "Noviello Advocacia — CNPJ 27.340.554/0001-94 — "
        "Av. do Café, 238, Vila Guarani, São Paulo/SP",
    )
    canvas.drawRightString(w - 2 * cm, 1.2 * cm, f"Página {doc.page}")
    canvas.restoreState()


def render_contrato_pdf(minuta_md: str) -> bytes:
    """Renderiza o markdown da minuta em PDF timbrado e devolve os bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2.7 * cm, bottomMargin=1.8 * cm,
        title="Contrato de Honorários — Noviello Advocacia",
    )
    doc.build(_flowables(minuta_md), onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()
