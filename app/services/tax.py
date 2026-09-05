"""Indian GST computation.

Prices in this catalogue are GST-inclusive (standard Indian retail practice),
so tax is extracted from the line total rather than added on top:

    taxable = gross * 100 / (100 + rate)
    tax     = gross - taxable

Place of supply decides the split: within the seller's state the tax is halved
into CGST + SGST; across states it is a single IGST line. Both halves are
derived from the same total so rounding can never make them disagree.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.config import settings

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def is_intra_state(buyer_state_code: str | None) -> bool:
    if not buyer_state_code or not settings.seller_state_code:
        return True
    return str(buyer_state_code).strip() == str(settings.seller_state_code).strip()


def split_inclusive(gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """Return `(taxable_value, tax_amount)` for a GST-inclusive gross amount."""
    if rate <= 0:
        return q(gross), Decimal("0.00")
    taxable = gross * HUNDRED / (HUNDRED + rate)
    taxable = q(taxable)
    return taxable, q(gross - taxable)


def compute_line_tax(
    unit_price: Decimal, quantity: int, rate: Decimal, discount: Decimal = Decimal("0.00")
) -> dict[str, Decimal]:
    """Tax for one order line, after its share of any order-level discount."""
    gross = q(unit_price * quantity - discount)
    if gross < 0:
        gross = Decimal("0.00")
    taxable, tax = split_inclusive(gross, rate)
    return {
        "gross": gross,
        "taxable_value": taxable,
        "tax_amount": tax,
        "tax_rate": rate,
    }


def build_tax_breakup(
    lines: list[dict[str, Any]], buyer_state_code: str | None
) -> dict[str, Any]:
    """Aggregate per-line tax into a GST-rate-wise breakup for the invoice.

    `lines` entries need `tax_rate`, `taxable_value` and `tax_amount`.
    """
    intra = is_intra_state(buyer_state_code)
    by_rate: dict[str, dict[str, Decimal]] = {}

    for line in lines:
        rate = Decimal(str(line["tax_rate"]))
        key = f"{rate.normalize():f}"
        bucket = by_rate.setdefault(
            key, {"taxable_value": Decimal("0.00"), "tax_amount": Decimal("0.00")}
        )
        bucket["taxable_value"] += Decimal(str(line["taxable_value"]))
        bucket["tax_amount"] += Decimal(str(line["tax_amount"]))

    rates = []
    total_tax = Decimal("0.00")
    total_taxable = Decimal("0.00")

    for key, bucket in sorted(by_rate.items(), key=lambda kv: Decimal(kv[0])):
        taxable = q(bucket["taxable_value"])
        tax = q(bucket["tax_amount"])
        total_taxable += taxable
        total_tax += tax

        entry: dict[str, Any] = {
            "rate": key,
            "taxable_value": str(taxable),
            "total_tax": str(tax),
        }
        if intra:
            half = q(tax / 2)
            entry["cgst"] = str(half)
            # The second half absorbs the rounding remainder so the parts sum exactly.
            entry["sgst"] = str(q(tax - half))
            entry["cgst_rate"] = str(Decimal(key) / 2)
            entry["sgst_rate"] = str(Decimal(key) / 2)
        else:
            entry["igst"] = str(tax)
            entry["igst_rate"] = key
        rates.append(entry)

    return {
        "type": "intra_state" if intra else "inter_state",
        "place_of_supply": buyer_state_code,
        "rates": rates,
        "total_taxable_value": str(q(total_taxable)),
        "total_tax": str(q(total_tax)),
    }


# Minimal GST state-code map for the states we ship to.
STATE_CODES: dict[str, str] = {
    "andhra pradesh": "37",
    "assam": "18",
    "bihar": "10",
    "chhattisgarh": "22",
    "delhi": "07",
    "goa": "30",
    "gujarat": "24",
    "haryana": "06",
    "himachal pradesh": "02",
    "jharkhand": "20",
    "karnataka": "29",
    "kerala": "32",
    "madhya pradesh": "23",
    "maharashtra": "27",
    "odisha": "21",
    "punjab": "03",
    "rajasthan": "08",
    "tamil nadu": "33",
    "telangana": "36",
    "uttar pradesh": "09",
    "uttarakhand": "05",
    "west bengal": "19",
}


def state_code_for(state_name: str | None) -> str | None:
    if not state_name:
        return None
    return STATE_CODES.get(state_name.strip().lower())
