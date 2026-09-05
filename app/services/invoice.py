"""GST-compliant tax invoice rendering.

PDF construction is CPU-bound and synchronous, so it is isolated in
`render_invoice_pdf` and pushed onto a worker thread by `generate_invoice`.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.errors import NotFoundError
from app.models.order import Order, OrderItem
from app.models.user import User
from app.services.tax import is_intra_state
from logging_config import get_logger

log = get_logger(__name__)

TWO_PLACES = Decimal("0.01")

FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
RUPEE = "Rs."

# The built-in Type 1 fonts are limited to WinAnsi, which has no U+20B9, so the
# rupee sign would render as a blank box. Embed a Unicode TTF when the image
# ships one; otherwise degrade to the "Rs." spelling, which is equally valid on
# a GST invoice.
_FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/DejaVuSans.ttf", "/Library/Fonts/DejaVuSans-Bold.ttf"),
    (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ),
)


def _init_fonts() -> None:
    global FONT_REGULAR, FONT_BOLD, RUPEE

    for regular_path, bold_path in _FONT_CANDIDATES:
        if not (os.path.exists(regular_path) and os.path.exists(bold_path)):
            continue
        try:
            regular = TTFont("InvoiceSans", regular_path)
            bold = TTFont("InvoiceSans-Bold", bold_path)
            # Older Unicode fonts predate U+20B9 and would draw a .notdef box.
            if 0x20B9 not in regular.face.charToGlyph:
                continue
            pdfmetrics.registerFont(regular)
            pdfmetrics.registerFont(bold)
            pdfmetrics.registerFontFamily(
                "InvoiceSans", normal="InvoiceSans", bold="InvoiceSans-Bold"
            )
        except Exception:
            continue
        FONT_REGULAR = "InvoiceSans"
        FONT_BOLD = "InvoiceSans-Bold"
        RUPEE = "₹"
        return


_init_fonts()


# --------------------------------------------------------------------------
# GST state codes
# --------------------------------------------------------------------------
GST_STATE_CODES: dict[str, str] = {
    "jammu and kashmir": "01",
    "himachal pradesh": "02",
    "punjab": "03",
    "chandigarh": "04",
    "uttarakhand": "05",
    "uttaranchal": "05",
    "haryana": "06",
    "delhi": "07",
    "new delhi": "07",
    "nct of delhi": "07",
    "rajasthan": "08",
    "uttar pradesh": "09",
    "bihar": "10",
    "sikkim": "11",
    "arunachal pradesh": "12",
    "nagaland": "13",
    "manipur": "14",
    "mizoram": "15",
    "tripura": "16",
    "meghalaya": "17",
    "assam": "18",
    "west bengal": "19",
    "jharkhand": "20",
    "odisha": "21",
    "orissa": "21",
    "chhattisgarh": "22",
    "madhya pradesh": "23",
    "gujarat": "24",
    "daman and diu": "25",
    "dadra and nagar haveli and daman and diu": "26",
    "dadra and nagar haveli": "26",
    "maharashtra": "27",
    "karnataka": "29",
    "goa": "30",
    "lakshadweep": "31",
    "kerala": "32",
    "tamil nadu": "33",
    "puducherry": "34",
    "pondicherry": "34",
    "andaman and nicobar islands": "35",
    "telangana": "36",
    "andhra pradesh": "37",
    "ladakh": "38",
    "other territory": "97",
}

STATE_NAMES: dict[str, str] = {code: name.title() for name, code in GST_STATE_CODES.items()}


def _normalise_state(value: str) -> str:
    cleaned = re.sub(r"[^a-z ]+", " ", value.replace("&", " and ").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def resolve_state_code(address: dict[str, Any] | None) -> str | None:
    """GST state code for an address snapshot, from an explicit code or the name."""
    if not address:
        return None
    explicit = address.get("state_code") or address.get("gst_state_code")
    if explicit:
        return str(explicit).strip().zfill(2)
    name = address.get("state")
    if not name:
        return None
    return GST_STATE_CODES.get(_normalise_state(str(name)))


# --------------------------------------------------------------------------
# Money and words
# --------------------------------------------------------------------------
def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    elif value is None or value == "":
        amount = Decimal("0")
    else:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            amount = Decimal("0")
    return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _group_indian(digits: str) -> str:
    # Indian grouping: last three digits, then pairs (12,34,567.00).
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def format_money(value: Any) -> str:
    amount = to_decimal(value)
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    return f"{sign}{_group_indian(whole)}.{fraction}"


def _rupees(value: Any) -> str:
    return f"{RUPEE} {format_money(value)}"


def format_percent(value: Any) -> str:
    rate = to_decimal(value).normalize()
    text = f"{rate:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


_ONES = (
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _words_under_thousand(value: int) -> str:
    words: list[str] = []
    hundreds, rest = divmod(value, 100)
    if hundreds:
        words += [_ONES[hundreds], "Hundred"]
    if rest:
        if words:
            words.append("and")
        if rest < 20:
            words.append(_ONES[rest])
        else:
            tens, units = divmod(rest, 10)
            words.append(_TENS[tens])
            if units:
                words.append(_ONES[units])
    return " ".join(words)


def number_to_indian_words(value: int) -> str:
    if value < 0:
        return f"Minus {number_to_indian_words(-value)}"
    if value == 0:
        return "Zero"

    words: list[str] = []
    crore, value = divmod(value, 10_000_000)
    lakh, value = divmod(value, 100_000)
    thousand, remainder = divmod(value, 1_000)
    if crore:
        words += [number_to_indian_words(crore), "Crore"]
    if lakh:
        words += [_words_under_thousand(lakh), "Lakh"]
    if thousand:
        words += [_words_under_thousand(thousand), "Thousand"]
    if remainder:
        words.append(_words_under_thousand(remainder))
    return " ".join(words)


def amount_in_words(value: Any) -> str:
    amount = to_decimal(value)
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    words = f"Rupees {number_to_indian_words(rupees)}"
    if paise:
        words = f"{words} and {number_to_indian_words(paise)} Paise"
    return f"{words} Only"


# --------------------------------------------------------------------------
# Invoice data assembly
# --------------------------------------------------------------------------
def _split_intra_state(total_tax: Decimal) -> tuple[Decimal, Decimal]:
    # CGST and SGST are equal halves; the residual paisa is pushed onto SGST so
    # the two components always add back to the tax actually charged.
    cgst = (total_tax / 2).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return cgst, total_tax - cgst


def _breakup_from_stored(stored: dict[str, Any] | None) -> dict[str, Decimal] | None:
    """Read the rate-wise breakup frozen by the tax service, if there is one."""
    if not isinstance(stored, dict) or not stored:
        return None

    rates = stored.get("rates")
    if isinstance(rates, list) and rates:
        totals = {"cgst": Decimal("0.00"), "sgst": Decimal("0.00"), "igst": Decimal("0.00")}
        for entry in rates:
            if not isinstance(entry, dict):
                continue
            for component in totals:
                if entry.get(component) is not None:
                    totals[component] += to_decimal(entry[component])
        return totals

    keys = {str(k).lower(): v for k, v in stored.items()}
    if all(keys.get(component) is None for component in ("cgst", "sgst", "igst")):
        return None
    return {
        "cgst": to_decimal(keys.get("cgst")),
        "sgst": to_decimal(keys.get("sgst")),
        "igst": to_decimal(keys.get("igst")),
    }


def build_item_rows(items: list[OrderItem]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        taxable = to_decimal(item.line_subtotal) - to_decimal(item.line_discount)
        description = item.product_name
        if item.variant_name:
            description = f"{description} ({item.variant_name})"
        rows.append(
            {
                "sno": index,
                "description": description,
                "sku": item.sku,
                "hsn_code": item.hsn_code or "",
                "quantity": item.quantity,
                "unit_price": to_decimal(item.unit_price),
                "line_subtotal": to_decimal(item.line_subtotal),
                "line_discount": to_decimal(item.line_discount),
                "taxable_value": taxable,
                "tax_rate": to_decimal(item.tax_rate),
                "tax_amount": to_decimal(item.tax_amount),
                "line_total": to_decimal(item.line_total),
            }
        )
    return rows


def build_invoice_data(order: Order, user: User | None) -> dict[str, Any]:
    """Flatten an Order into the plain dict `render_invoice_pdf` consumes."""
    address = dict(order.address_snapshot or {})
    buyer_state_code = resolve_state_code(address)
    seller_state_code = str(settings.seller_state_code or "").strip().zfill(2)

    stored = order.tax_breakup if isinstance(order.tax_breakup, dict) else {}

    # The invoice must show the split that was actually charged, so the frozen
    # breakup wins over re-deriving it. Same state means intra-state (CGST +
    # SGST halves), a different state means a single IGST line.
    if stored.get("type") in {"intra_state", "inter_state"}:
        intra_state = stored["type"] == "intra_state"
    else:
        intra_state = is_intra_state(buyer_state_code)

    items = build_item_rows(list(order.items))
    tax_total = to_decimal(sum((row["tax_amount"] for row in items), Decimal("0")))

    breakup = _breakup_from_stored(stored)
    if breakup is None:
        if intra_state:
            cgst, sgst = _split_intra_state(tax_total)
            breakup = {"cgst": cgst, "sgst": sgst, "igst": Decimal("0.00")}
        else:
            breakup = {"cgst": Decimal("0.00"), "sgst": Decimal("0.00"), "igst": tax_total}

    subtotal = to_decimal(order.subtotal)
    discount = to_decimal(order.discount_amount)

    return {
        "order_number": order.order_number,
        "invoice_date": order.confirmed_at or order.created_at or datetime.now(timezone.utc),
        "currency": order.currency or settings.currency,
        "is_intra_state": intra_state,
        "place_of_supply": {
            "state": address.get("state") or STATE_NAMES.get(buyer_state_code or "", ""),
            "state_code": buyer_state_code or "",
        },
        "seller": {
            "legal_name": settings.seller_legal_name,
            "address": settings.seller_address,
            "gstin": settings.seller_gstin,
            "state_code": seller_state_code,
        },
        "buyer": {
            "name": address.get("recipient_name")
            or (user.display_name if user else None)
            or "Customer",
            "phone": address.get("recipient_phone") or (user.phone if user else None),
            "email": user.email if user else None,
            "gstin": user.gstin if user else None,
        },
        "shipping_address": address,
        "items": items,
        "totals": {
            "subtotal": subtotal,
            "discount": discount,
            "taxable_value": subtotal - discount,
            "cgst": breakup["cgst"],
            "sgst": breakup["sgst"],
            "igst": breakup["igst"],
            "tax_total": breakup["cgst"] + breakup["sgst"] + breakup["igst"],
            "shipping": to_decimal(order.shipping_cost),
            "grand_total": to_decimal(order.total),
        },
    }


def invoice_filename(order_number: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (order_number or "").strip()).strip("-")
    return f"invoice-{safe or 'order'}.pdf"


# --------------------------------------------------------------------------
# PDF rendering
# --------------------------------------------------------------------------
def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "invoice_base", fontName=FONT_REGULAR, fontSize=8.5, leading=11, spaceAfter=0
    )
    return {
        "title": ParagraphStyle(
            "invoice_title",
            parent=base,
            fontName=FONT_BOLD,
            fontSize=16,
            leading=20,
            alignment=1,
        ),
        "heading": ParagraphStyle(
            "invoice_heading", parent=base, fontName=FONT_BOLD, fontSize=9, leading=12
        ),
        "body": base,
        "small": ParagraphStyle("invoice_small", parent=base, fontSize=7.5, leading=10),
        "cell": ParagraphStyle("invoice_cell", parent=base, fontSize=7.5, leading=9.5),
        "cell_right": ParagraphStyle(
            "invoice_cell_right", parent=base, fontSize=7.5, leading=9.5, alignment=2
        ),
        "footer": ParagraphStyle(
            "invoice_footer", parent=base, fontSize=7.5, leading=10, alignment=1, textColor=colors.grey
        ),
    }


def _address_lines(address: dict[str, Any]) -> list[str]:
    city_bits = [address.get("city"), address.get("state"), address.get("pincode")]
    lines = [
        address.get("line1"),
        address.get("line2"),
        ", ".join(str(bit) for bit in city_bits if bit),
        address.get("country"),
    ]
    return [str(line) for line in lines if line]


def _party_block(data: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    seller = data["seller"]
    buyer = data["buyer"]
    place = data["place_of_supply"]

    seller_lines = [
        f"<b>{seller['legal_name']}</b>",
        seller["address"],
        f"GSTIN: {seller['gstin'] or 'Unregistered'}",
        f"State Code: {seller['state_code']}",
    ]
    buyer_lines = [f"<b>{buyer['name']}</b>", *_address_lines(data["shipping_address"])]
    if buyer.get("phone"):
        buyer_lines.append(f"Phone: {buyer['phone']}")
    if buyer.get("gstin"):
        buyer_lines.append(f"GSTIN: {buyer['gstin']}")
    if place.get("state"):
        buyer_lines.append(
            f"Place of Supply: {place['state']}"
            + (f" ({place['state_code']})" if place.get("state_code") else "")
        )

    rows = [
        [
            Paragraph("Sold By", styles["heading"]),
            Paragraph("Billed / Shipped To", styles["heading"]),
        ],
        [
            Paragraph("<br/>".join(seller_lines), styles["small"]),
            Paragraph("<br/>".join(buyer_lines), styles["small"]),
        ],
    ]
    table = Table(rows, colWidths=[93 * mm, 93 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#8a8a8a")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9c9c9")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _items_table(data: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    intra_state = bool(data["is_intra_state"])
    cell, cell_right = styles["cell"], styles["cell_right"]

    if intra_state:
        header_top = ["S.No", "Description", "HSN", "Qty", "Rate", "Taxable", "CGST", "", "SGST", "", "Amount"]
        header_sub = ["", "", "", "", "", "", "%", "Amt", "%", "Amt", ""]
        widths = [9, 40, 14, 9, 17, 20, 11, 16, 11, 16, 23]
        spans = [
            ("SPAN", (6, 0), (7, 0)),
            ("SPAN", (8, 0), (9, 0)),
            ("SPAN", (10, 0), (10, 1)),
        ]
    else:
        header_top = ["S.No", "Description", "HSN", "Qty", "Rate", "Taxable", "IGST", "", "Amount"]
        header_sub = ["", "", "", "", "", "", "%", "Amt", ""]
        widths = [10, 52, 16, 10, 20, 24, 13, 20, 21]
        spans = [("SPAN", (6, 0), (7, 0)), ("SPAN", (8, 0), (8, 1))]

    spans += [("SPAN", (col, 0), (col, 1)) for col in range(6)]

    rows: list[list[Any]] = [header_top, header_sub]
    for index, item in enumerate(data["items"], start=1):
        description = item["description"]
        if item.get("sku"):
            description = f"{description}<br/><font size=6.5 color='#777777'>SKU: {item['sku']}</font>"
        taxable = item.get("taxable_value")
        if taxable is None:
            taxable = to_decimal(item["line_subtotal"]) - to_decimal(item.get("line_discount"))
        common = [
            Paragraph(str(item.get("sno", index)), cell),
            Paragraph(description, cell),
            Paragraph(str(item.get("hsn_code") or ""), cell),
            Paragraph(str(item["quantity"]), cell_right),
            Paragraph(format_money(item["unit_price"]), cell_right),
            Paragraph(format_money(taxable), cell_right),
        ]
        if intra_state:
            half_rate = to_decimal(item["tax_rate"]) / 2
            half_cgst, half_sgst = _split_intra_state(to_decimal(item["tax_amount"]))
            tax_cells = [
                Paragraph(format_percent(half_rate), cell_right),
                Paragraph(format_money(half_cgst), cell_right),
                Paragraph(format_percent(half_rate), cell_right),
                Paragraph(format_money(half_sgst), cell_right),
            ]
        else:
            tax_cells = [
                Paragraph(format_percent(item["tax_rate"]), cell_right),
                Paragraph(format_money(item["tax_amount"]), cell_right),
            ]
        rows.append([*common, *tax_cells, Paragraph(format_money(item["line_total"]), cell_right)])

    table = Table(rows, colWidths=[w * mm for w in widths], repeatRows=2)
    table.setStyle(
        TableStyle(
            [
                *spans,
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#8a8a8a")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c9c9c9")),
                ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#eeeeee")),
                ("FONTNAME", (0, 0), (-1, 1), FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, 1), 7.5),
                ("ALIGN", (0, 0), (-1, 1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _totals_table(data: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    totals = data["totals"]
    lines: list[tuple[str, Any, bool]] = [
        ("Subtotal", totals["subtotal"], False),
        ("Discount", -to_decimal(totals["discount"]), False),
        ("Taxable Value", totals["taxable_value"], False),
    ]
    if data["is_intra_state"]:
        lines += [("CGST", totals["cgst"], False), ("SGST", totals["sgst"], False)]
    else:
        lines.append(("IGST", totals["igst"], False))
    lines += [
        ("Shipping", totals["shipping"], False),
        ("Grand Total", totals["grand_total"], True),
    ]

    rows = [
        [
            Paragraph(f"<b>{label}</b>" if strong else label, styles["cell_right"]),
            Paragraph(
                f"<b>{_rupees(value)}</b>" if strong else _rupees(value), styles["cell_right"]
            ),
        ]
        for label, value, strong in lines
    ]

    table = Table(rows, colWidths=[38 * mm, 38 * mm], hAlign="RIGHT")
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#8a8a8a")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c9c9c9")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eeeeee")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _intra_state_flag(order_data: dict[str, Any]) -> bool:
    flag = order_data.get("is_intra_state")
    if flag is not None:
        return bool(flag)
    seller = str(order_data.get("seller", {}).get("state_code") or "").strip()
    buyer = str(order_data.get("place_of_supply", {}).get("state_code") or "").strip()
    return bool(buyer) and buyer.zfill(2) == seller.zfill(2)


def render_invoice_pdf(order_data: dict[str, Any]) -> bytes:
    """Build the tax invoice PDF. Pure and synchronous — safe for a thread pool."""
    styles = _styles()
    buffer = io.BytesIO()
    order_data = {**order_data, "is_intra_state": _intra_state_flag(order_data)}

    order_number = str(order_data.get("order_number") or "")
    raw_date = order_data.get("invoice_date")
    invoice_date = raw_date if isinstance(raw_date, datetime) else None
    date_text = invoice_date.strftime("%d %b %Y") if invoice_date else str(raw_date or "")

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title=f"Tax Invoice {order_number}",
        author=order_data["seller"]["legal_name"],
        subject="Tax Invoice",
    )

    meta = Table(
        [
            [
                Paragraph(f"<b>Invoice No:</b> {order_number}", styles["small"]),
                Paragraph(f"<b>Invoice Date:</b> {date_text}", styles["small"]),
                Paragraph(
                    "<b>Supply Type:</b> "
                    + ("Intra-State" if order_data["is_intra_state"] else "Inter-State"),
                    styles["small"],
                ),
            ]
        ],
        colWidths=[62 * mm, 62 * mm, 62 * mm],
    )
    meta.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#8a8a8a")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9c9c9")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    story: list[Any] = [
        Paragraph("TAX INVOICE", styles["title"]),
        Spacer(1, 6),
        meta,
        Spacer(1, 6),
        _party_block(order_data, styles),
        Spacer(1, 8),
        _items_table(order_data, styles),
        Spacer(1, 8),
        _totals_table(order_data, styles),
        Spacer(1, 8),
        Paragraph(
            f"<b>Amount in Words:</b> {amount_in_words(order_data['totals']['grand_total'])}",
            styles["small"],
        ),
        Spacer(1, 4),
        Paragraph(
            f"All amounts are in {order_data.get('currency') or settings.currency}. "
            "Tax is charged as per the applicable GST rate frozen at the time of order.",
            styles["small"],
        ),
        Spacer(1, 14),
        Paragraph(
            "This is a computer-generated invoice and does not require a signature.",
            styles["footer"],
        ),
    ]

    doc.build(story)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def generate_invoice(db: AsyncSession, order_id: int) -> bytes:
    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found")

    user = (
        await db.execute(select(User).where(User.id == order.user_id))
    ).scalar_one_or_none()

    order_data = build_invoice_data(order, user)
    pdf = await asyncio.to_thread(render_invoice_pdf, order_data)

    log.info(
        "invoice_generated",
        order_id=order_id,
        order_number=order.order_number,
        intra_state=order_data["is_intra_state"],
        item_count=len(order_data["items"]),
        size_bytes=len(pdf),
    )
    return pdf
