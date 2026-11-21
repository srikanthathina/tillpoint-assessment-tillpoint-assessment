"""Sales reports for the shop owner."""
from datetime import datetime, timedelta, timezone

from . import db


def day_bounds_utc(day, utc_offset_minutes=0):
    """The UTC instants at which a shop-local calendar day starts and ends. day is 'YYYY-MM-DD'."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start -= timedelta(minutes=utc_offset_minutes)
    return db.to_iso(start), db.to_iso(start + timedelta(days=1))


def daily_sales(conn, day, utc_offset_minutes=0):
    """Cash movements on a shop-local day: captures less refunds, excluding non-cash events."""
    start, end = day_bounds_utc(day, utc_offset_minutes)
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN movement_cents > 0 THEN movement_cents ELSE 0 END), 0) AS captured,"
        " COALESCE(SUM(CASE WHEN movement_cents < 0 THEN -movement_cents ELSE 0 END), 0) AS refunded,"
        " COALESCE(SUM(movement_cents), 0) AS revenue,"
        " COUNT(DISTINCT CASE WHEN movement_cents != 0 THEN order_id END) AS orders"
        " FROM payments WHERE received_at >= ? AND received_at < ?",
        (start, end),
    ).fetchone()
    return {
        "day": day,
        "captured_cents": row["captured"],
        "refunded_cents": row["refunded"],
        "revenue_cents": row["revenue"],
        "orders": row["orders"],
    }


def top_products(conn, limit=5):
    """Best sellers by units, counting orders that were paid or shipped."""
    rows = conn.execute(
        "SELECT i.sku, SUM(i.qty) AS units, SUM(i.qty * i.unit_price_cents) AS gross_cents"
        " FROM order_items i JOIN orders o ON o.id = i.order_id"
        " WHERE o.status IN ('paid', 'shipped') GROUP BY i.sku ORDER BY units DESC, i.sku LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
