"""The evidence packet as one PDF, the way card issuers take it.

Format follows the payment providers' guidance: US Letter, portrait, 12-point text,
bold for emphasis instead of colour highlighting, grouped by evidence type, with a
one-line summary over each group. Issuers cap a response at 4.5 MB, and Mastercard
at 19 pages, so a packet that would exceed either is refused here rather than
rejected by the bank.
"""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from copilot.store import STORE_NAME

MAX_BYTES = 4_500_000  # all card networks
MAX_PAGES = 19  # Mastercard; Visa allows more, so this fits every network


class EvidenceTooLarge(ValueError):
    pass


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontName="Helvetica", fontSize=12, leading=16)
    return {
        "title": ParagraphStyle("title", parent=body, fontName="Helvetica-Bold", fontSize=17, leading=22),
        "h2": ParagraphStyle(
            "h2", parent=body, fontName="Helvetica-Bold", fontSize=13.5, leading=18, spaceBefore=12
        ),
        "body": body,
        "small": ParagraphStyle(
            "small", parent=body, fontSize=10.5, leading=14, textColor=colors.HexColor("#444444")
        ),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def _table(rows: list[list[str]], style: ParagraphStyle, widths: list[float]) -> Table:
    t = Table([[_p(str(c), style) for c in r] for r in rows], colWidths=widths, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def render(packet: dict[str, Any]) -> bytes:
    """The packet as a PDF. Raises EvidenceTooLarge past the card networks' limits."""
    s = _styles()
    d, o, c = packet["dispute"], packet["order"], packet["customer"]
    width = LETTER[0] - 2 * inch
    story: list[Any] = [
        _p(f"Chargeback response: dispute {d['id']}", s["title"]),
        _p(f"{STORE_NAME} · order {o['name']} · {d['reason_label']} · ${d['amount']}", s["small"]),
        Spacer(1, 10),
        _p("Summary", s["h2"]),
        _p(packet["rebuttal"] or packet["recommendation"]["why"], s["body"]),
        _p("Transaction", s["h2"]),
        _table(
            [
                ["Customer", f"{c['name']} ({c['email']})"],
                ["Order", f"{o['name']}, placed {o['placed']}"],
                ["Items", "; ".join(f"{i['quantity']} x {i['title']} (${i['price']})" for i in o["items"])],
                [
                    "Amount charged",
                    f"${o['total']}" + (f", card ending {o['card_last4']}" if o.get("card_last4") else ""),
                ],
                ["Statement descriptor", o.get("descriptor") or ""],
                ["Disputed", f"${d['amount']} on {d['opened']} ({d['reason_label']})"],
            ],
            s["body"],
            [1.9 * inch, width - 1.9 * inch],
        ),
        _p("Timeline", s["h2"]),
        _table(
            [[e["date"], e["event"]] for e in packet["timeline"]], s["body"], [1.4 * inch, width - 1.4 * inch]
        ),
    ]
    for ev in packet["evidence"].values():  # already in order of strength for the dispute's reason
        story += [_p(ev["title"], s["h2"]), _p(ev["summary"], s["body"]), Spacer(1, 4)]
        if ev.get("rows"):
            cols = len(ev["rows"][0])
            story.append(_table(ev["rows"], s["body"], [width / cols] * cols))
        for m in ev.get("messages", []):
            story += [Spacer(1, 4), _p(f"{m['date']} · {m['from']}", s["small"]), _p(m["text"], s["body"])]
    if packet.get("policy"):
        pol = packet["policy"]
        story += [
            _p(f"Policy shown to customers: {pol['title']}", s["h2"]),
            _p(pol["text"], s["body"]),
            _p(pol["url"], s["small"]),
        ]

    pages: list[int] = []
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER, leftMargin=inch, rightMargin=inch, topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        title=f"Chargeback response {d['id']}", author=STORE_NAME,
    )  # fmt: skip
    doc.build(
        story,
        onFirstPage=lambda cv, dc: pages.append(dc.page),
        onLaterPages=lambda cv, dc: pages.append(dc.page),
    )
    data = buf.getvalue()
    if len(pages) > MAX_PAGES:
        raise EvidenceTooLarge(
            f"The evidence runs to {len(pages)} pages; card networks accept at most {MAX_PAGES}."
        )
    if len(data) > MAX_BYTES:
        raise EvidenceTooLarge(
            f"The evidence is {len(data):,} bytes; card networks accept at most {MAX_BYTES:,}."
        )
    return data
