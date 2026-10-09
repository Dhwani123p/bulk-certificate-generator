"""Certificate rendering: the single predefined template, drawn with ReportLab.

``render_certificate`` is a pure function of its input (no DB access) so it is
easy to test and easy to swap for another implementation (e.g. HTML -> PDF).
"""

import os
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)

NAVY = colors.HexColor("#1f2a44")
GOLD = colors.HexColor("#b8902f")
GREY = colors.HexColor("#555555")


@dataclass(frozen=True)
class CertificateData:
    certificate_id: str
    recipient_name: str
    title: str
    course_name: str
    issuer_name: str
    issue_date: date


def _fit_font_size(text: str, font: str, max_size: float, min_size: float, max_width: float) -> float:
    """Largest font size (<= max_size) at which ``text`` fits in ``max_width``."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _draw_centered(c: canvas.Canvas, text: str, y: float, font: str, size: float, color) -> None:
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(PAGE_WIDTH / 2, y, text)


def render_certificate_bytes(data: CertificateData) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    c.setTitle(f"{data.title} - {data.recipient_name}")
    c.setAuthor(data.issuer_name)

    # Borders
    c.setStrokeColor(NAVY)
    c.setLineWidth(6)
    c.rect(20, 20, PAGE_WIDTH - 40, PAGE_HEIGHT - 40)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.5)
    c.rect(34, 34, PAGE_WIDTH - 68, PAGE_HEIGHT - 68)

    usable_width = PAGE_WIDTH - 160

    title_size = _fit_font_size(data.title.upper(), "Helvetica-Bold", 34, 18, usable_width)
    _draw_centered(c, data.title.upper(), PAGE_HEIGHT - 130, "Helvetica-Bold", title_size, NAVY)

    _draw_centered(c, "This is to certify that", PAGE_HEIGHT - 190, "Helvetica", 16, GREY)

    # Recipient name: shrink long names so they never overflow the page.
    name_size = _fit_font_size(data.recipient_name, "Times-BoldItalic", 44, 18, usable_width)
    _draw_centered(c, data.recipient_name, PAGE_HEIGHT - 260, "Times-BoldItalic", name_size, NAVY)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(PAGE_WIDTH / 2 - 220, PAGE_HEIGHT - 275, PAGE_WIDTH / 2 + 220, PAGE_HEIGHT - 275)

    _draw_centered(c, "has successfully completed", PAGE_HEIGHT - 315, "Helvetica", 16, GREY)

    course_size = _fit_font_size(data.course_name, "Helvetica-Bold", 24, 12, usable_width)
    _draw_centered(c, data.course_name, PAGE_HEIGHT - 355, "Helvetica-Bold", course_size, NAVY)

    # Footer: date (left) and issuer signature line (right).
    footer_y = 120
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.8)
    for x_center, label, value in (
        (PAGE_WIDTH * 0.28, "Date", data.issue_date.strftime("%d %B %Y")),
        (PAGE_WIDTH * 0.72, "Issued by", data.issuer_name),
    ):
        c.line(x_center - 110, footer_y, x_center + 110, footer_y)
        value_size = _fit_font_size(value, "Helvetica", 14, 8, 220)
        c.setFont("Helvetica", value_size)
        c.setFillColor(NAVY)
        c.drawCentredString(x_center, footer_y + 8, value)
        c.setFont("Helvetica", 10)
        c.setFillColor(GREY)
        c.drawCentredString(x_center, footer_y - 16, label)

    c.setFont("Helvetica", 8)
    c.setFillColor(GREY)
    c.drawCentredString(PAGE_WIDTH / 2, 48, f"Certificate ID: {data.certificate_id}")

    c.showPage()
    c.save()
    return buffer.getvalue()


def render_certificate(data: CertificateData, output_path: Path) -> Path:
    """Render a certificate PDF to ``output_path``.

    The file is written to a temporary name and atomically renamed, so a crash
    mid-write never leaves a half-written PDF that could be served to a client.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = render_certificate_bytes(data)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_bytes(pdf)
    os.replace(tmp_path, output_path)
    return output_path
