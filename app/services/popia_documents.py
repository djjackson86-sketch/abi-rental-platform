"""Printable POPIA documents: published DB wording, and the existing short agreement."""
from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

AGREEMENT_PATH = Path(__file__).resolve().parents[2] / "docs" / "popia" / "CLIENT-DATA-NOTICE-JACKAPP-SANO.md"


def agreement_text():
    return AGREEMENT_PATH.read_text(encoding="utf-8")


def _pdf_link_text(match):
    label, target = match.group(1), match.group(2)
    plain_target = re.sub(r"^(mailto:|tel:)", "", target)
    return label if label == plain_target else f"{label} ({target})"


def _inline(text):
    # Escape XML first; only generate our own formatting. No user HTML or URI is executable.
    text = escape(text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _pdf_link_text, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def document_pdf(text, title, version="", published_at="", *, signing_page=False):
    """A4, wrapped paragraphs, repeated table headers, and numbered pages."""
    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=A4, rightMargin=48, leftMargin=48,
                                 topMargin=48, bottomMargin=48, title=title,
                                 author="Sano Trailers")
    styles = getSampleStyleSheet()
    body = ParagraphStyle("PopiaBody", parent=styles["BodyText"], fontName="Helvetica",
                          fontSize=10, leading=14, spaceAfter=7, splitLongWords=True)
    bullet_style = ParagraphStyle("PopiaBullet", parent=body, leftIndent=12, bulletIndent=0)
    heading = ParagraphStyle("PopiaHeading", parent=styles["Heading2"], fontName="Helvetica-Bold",
                             fontSize=12, leading=16, spaceBefore=12, spaceAfter=7,
                             textColor=colors.HexColor("#174d36"), keepWithNext=True)
    title_style = ParagraphStyle("PopiaTitle", parent=heading, fontSize=18, leading=23)
    cell_style = ParagraphStyle("PopiaCell", parent=body, fontSize=9, leading=12, spaceAfter=0)
    footer_style = ParagraphStyle("PopiaFooter", parent=body, fontSize=8, leading=10,
                                  textColor=colors.HexColor("#64748b"), alignment=TA_LEFT)
    story = []
    if version:
        meta = f"Published version: {version}"
        if published_at:
            meta += f" | Published: {published_at}"
        story.extend([Paragraph(_inline(meta), footer_style), Spacer(1, 12)])
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"[:\-\s]*", cell) for cell in cells):
                    rows.append(cells)
                i += 1
            if rows:
                count = max(len(row) for row in rows)
                if count > 8:
                    # Very wide user-authored tables are printed as wrapped rows instead.
                    for row in rows:
                        story.append(Paragraph(_inline(" | ".join(row)), body))
                    continue
                rows = [[Paragraph(_inline(cell), cell_style) for cell in row + [""] * (count - len(row))]
                        for row in rows]
                widths = ([76] + [(document.width - 76) / (count - 1)] * (count - 1)) if count > 1 else [document.width]
                table = Table(rows, colWidths=widths, repeatRows=1, splitInRow=1, hAlign="LEFT")
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf5ef")),
                    ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#cbd5e1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ]))
                story.extend([table, Spacer(1, 10)])
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            story.append(Paragraph(_inline(match.group(2)), title_style if len(match.group(1)) == 1 else heading))
            i += 1
            continue
        if re.fullmatch(r"([-*_])\1{2,}", line):
            i += 1
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", line)
        if bullet:
            story.append(Paragraph(_inline(bullet.group(1)), bullet_style, bulletText="•"))
            i += 1
            continue
        paragraph = [line]
        i += 1
        while i < len(lines) and lines[i].strip():
            next_line = lines[i].strip()
            if next_line.startswith("|") or re.match(r"^(#{1,6}|[-*+])\s+", next_line):
                break
            paragraph.append(next_line)
            i += 1
        story.append(Paragraph(_inline(" ".join(paragraph)), body))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(48, 27, "Sano Trailers | POPIA")
        canvas.drawRightString(A4[0] - 48, 27, f"Page {doc.page}")
        canvas.restoreState()

    if signing_page:
        from reportlab.platypus import PageBreak
        from app.services.popia_contract import ContractSigningPage
        story.extend([PageBreak(), ContractSigningPage(text)])
    document.build(story or [Paragraph("No document text available.", body)], onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
