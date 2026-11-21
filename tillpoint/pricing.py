"""Order maths: subtotal, coupon discount, tax, total. Integer cents throughout."""

# Sales tax in basis points (1/100 of a percent) by region.
TAX_BPS = {"NONE": 0, "CA": 725, "NY": 888, "TX": 625, "DE": 1900}


class CouponRejected(Exception):
    pass


def subtotal(lines):
    """lines: iterable of (unit_price_cents, qty[, taxable])."""
    return sum(price * qty for price, qty, _ in _normalize_lines(lines))


def _normalize_lines(lines):
    normalized = []
    for line in lines:
        parts = tuple(line)
        if len(parts) == 2:
            price, qty = parts
            taxable = True
        elif len(parts) == 3:
            price, qty, taxable = parts
        else:
            raise ValueError("each line must contain price, quantity, and optional taxable flag")
        normalized.append((price, qty, bool(taxable)))
    return normalized


def discount_for(coupon, subtotal_cents):
    """Discount in cents for a coupon row (or None) against a subtotal. Never more than the subtotal."""
    if coupon is None:
        return 0
    if coupon["value"] < 0:
        raise CouponRejected(f"{coupon['code']} has an invalid negative value")
    if subtotal_cents < coupon["min_subtotal_cents"]:
        raise CouponRejected(f"{coupon['code']} needs a subtotal of at least {coupon['min_subtotal_cents']}")
    if coupon["max_uses"] is not None and coupon["uses"] >= coupon["max_uses"]:
        raise CouponRejected(f"{coupon['code']} has been used up")
    if coupon["kind"] == "percent":
        amount = round_half_up(subtotal_cents * coupon["value"], 100)
    else:
        amount = coupon["value"]
    return min(amount, subtotal_cents)


def tax_for(taxable_cents, region):
    if region not in TAX_BPS:
        raise ValueError(f"unknown region {region!r}")
    return round_half_up(taxable_cents * TAX_BPS[region], 10_000)


def round_half_up(numerator, denominator):
    return (2 * numerator + denominator) // (2 * denominator)


def price_order(lines, region, coupon=None):
    """Return dict(subtotal_cents, discount_cents, tax_cents, total_cents)."""
    normalized = _normalize_lines(lines)
    sub = sum(price * qty for price, qty, _ in normalized)
    taxable_subtotal = sum(price * qty for price, qty, taxable in normalized if taxable)
    discount = discount_for(coupon, sub)
    taxable_discount = round_half_up(discount * taxable_subtotal, sub) if sub else 0
    taxable_after_discount = max(taxable_subtotal - taxable_discount, 0)
    tax = tax_for(taxable_after_discount, region)
    return {
        "subtotal_cents": sub,
        "discount_cents": discount,
        "taxable_subtotal_cents": taxable_after_discount,
        "tax_cents": tax,
        "total_cents": sub - discount + tax,
    }
