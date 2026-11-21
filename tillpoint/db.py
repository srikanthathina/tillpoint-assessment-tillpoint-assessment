"""SQLite storage. All money is integer cents; all timestamps are UTC ISO-8601 strings."""
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    taxable INTEGER NOT NULL DEFAULT 1 CHECK (taxable IN (0, 1))
);
CREATE TABLE IF NOT EXISTS coupons (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('percent', 'fixed')),
    value INTEGER NOT NULL,
    min_subtotal_cents INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER,
    uses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer TEXT NOT NULL,
    region TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    coupon TEXT,
    subtotal_cents INTEGER NOT NULL,
    discount_cents INTEGER NOT NULL DEFAULT 0,
    tax_cents INTEGER NOT NULL DEFAULT 0,
    total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    stock_reserved INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    sku TEXT NOT NULL REFERENCES products(sku),
    qty INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    taxable INTEGER NOT NULL DEFAULT 1 CHECK (taxable IN (0, 1))
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    event_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'payment.succeeded',
    payment_ref TEXT,
    movement_cents INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer, created_at);
"""


def connect(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        added_movement = _ensure_column(
            conn, "payments", "movement_cents", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "payments", "event_type",
                       "TEXT NOT NULL DEFAULT 'payment.succeeded'")
        _ensure_column(conn, "payments", "payment_ref", "TEXT")
        _ensure_column(conn, "products", "taxable",
                       "INTEGER NOT NULL DEFAULT 1 CHECK (taxable IN (0, 1))")
        _ensure_column(conn, "order_items", "taxable",
                       "INTEGER NOT NULL DEFAULT 1 CHECK (taxable IN (0, 1))")
        added_expiry = _ensure_column(conn, "orders", "expires_at", "TEXT")
        _ensure_column(conn, "orders", "stock_reserved",
                       "INTEGER NOT NULL DEFAULT 1")

        if added_movement:
            conn.execute("UPDATE payments SET movement_cents = amount_cents")
        if added_expiry:
            rows = conn.execute(
                "SELECT id, created_at FROM orders "
                "WHERE status IN ('pending', 'partially_paid') AND expires_at IS NULL"
            ).fetchall()
            for row in rows:
                expires = from_iso(row["created_at"]) + timedelta(minutes=30)
                conn.execute("UPDATE orders SET expires_at = ? WHERE id = ?",
                             (to_iso(expires), row["id"]))
        conn.execute(
            "UPDATE orders SET stock_reserved = 0 WHERE status IN ('cancelled', 'expired', 'refunded', 'shipped')"
        )

        duplicate_ids = conn.execute(
            "SELECT event_id FROM payments GROUP BY event_id HAVING COUNT(*) > 1"
        ).fetchall()
        for duplicate in duplicate_ids:
            rows = conn.execute(
                "SELECT id, order_id, amount_cents, event_type, payment_ref, movement_cents "
                "FROM payments WHERE event_id = ? ORDER BY id",
                (duplicate["event_id"],),
            ).fetchall()
            signatures = {
                (row["order_id"], row["amount_cents"], row["event_type"],
                 row["payment_ref"], row["movement_cents"])
                for row in rows
            }
            if len(signatures) != 1:
                raise sqlite3.IntegrityError(
                    f"conflicting payment rows for event {duplicate['event_id']!r}"
                )
            conn.execute("DELETE FROM payments WHERE event_id = ? AND id != ?",
                         (duplicate["event_id"], rows[0]["id"]))

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_event_id ON payments(event_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_ref_capture "
            "ON payments(payment_ref) WHERE payment_ref IS NOT NULL AND movement_cents > 0"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _ensure_column(conn, table, column, definition):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
