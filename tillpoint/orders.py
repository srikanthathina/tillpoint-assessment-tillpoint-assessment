"""Creating, cancelling and listing orders."""
from datetime import timedelta

from . import db, pricing
from .catalog import Catalog


class OrderError(Exception):
    pass


class Orders:
    CHECKOUT_TTL = timedelta(minutes=30)

    def __init__(self, conn, catalog=None):
        self.conn = conn
        self.catalog = catalog or Catalog(conn)

    def create(self, customer, items, region="NONE", coupon_code=None, now=None):
        """items: list of (sku, qty). Reserves stock, redeems the coupon and stores the order, all or nothing."""
        if not items:
            raise OrderError("an order needs at least one item")
        if any(qty <= 0 for _, qty in items):
            raise OrderError("quantities must be positive")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            coupon = self._coupon(coupon_code)
            priced = []
            for sku, qty in items:
                product = self.catalog.get_product(sku)
                priced.append((sku, qty, product.price_cents, product.taxable))
            totals = pricing.price_order(
                [(price, qty, taxable) for _, qty, price, taxable in priced], region, coupon)
            for sku, qty, _, _ in priced:
                self.catalog.reserve(sku, qty)
            if coupon is not None:
                self.conn.execute("UPDATE coupons SET uses = uses + 1 WHERE code = ?", (coupon_code,))
            created_at = now or db.utcnow()
            cur = self.conn.execute(
                "INSERT INTO orders (customer, region, coupon, subtotal_cents, discount_cents, tax_cents,"
                " total_cents, created_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (customer, region, coupon_code, totals["subtotal_cents"], totals["discount_cents"],
                 totals["tax_cents"], totals["total_cents"], db.to_iso(created_at),
                 db.to_iso(created_at + self.CHECKOUT_TTL),
                 "paid" if totals["total_cents"] == 0 else "pending"),
            )
            order_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO order_items (order_id, sku, qty, unit_price_cents, taxable) VALUES (?, ?, ?, ?, ?)",
                [(order_id, sku, qty, price, int(taxable))
                 for sku, qty, price, taxable in priced],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(order_id)

    def expire_stale(self, now=None):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            expired = self._expire_stale_locked(now)
            self.conn.execute("COMMIT")
            return expired
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _expire_stale_locked(self, now=None):
        now_iso = db.to_iso(now or db.utcnow())
        rows = self.conn.execute(
            "SELECT id, coupon, stock_reserved FROM orders "
            "WHERE status IN ('pending', 'partially_paid') AND expires_at <= ?",
            (now_iso,),
        ).fetchall()
        expired = []
        for row in rows:
            cur = self.conn.execute(
                "UPDATE orders SET status = 'expired', stock_reserved = 0 "
                "WHERE id = ? AND status IN ('pending', 'partially_paid') AND expires_at <= ?",
                (row["id"], now_iso),
            )
            if cur.rowcount == 0:
                continue
            if row["stock_reserved"]:
                items = self.conn.execute(
                    "SELECT sku, qty FROM order_items WHERE order_id = ?", (row["id"],)
                ).fetchall()
                for item in items:
                    self.catalog.release(item["sku"], item["qty"])
            if row["coupon"]:
                self.conn.execute(
                    "UPDATE coupons SET uses = uses - 1 WHERE code = ? AND uses > 0",
                    (row["coupon"],),
                )
            expired.append(row["id"])
        return expired

    def cancel(self, order_id):
        """Cancel an order that has not shipped. Its stock and its coupon use go back."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise OrderError(f"no order {order_id}")
            if order["status"] == "shipped":
                raise OrderError("a shipped order cannot be cancelled")
            if order["status"] in ("cancelled", "expired", "refunded"):
                self.conn.execute("COMMIT")
                return self.get(order_id)
            if order["stock_reserved"]:
                items = self.conn.execute(
                    "SELECT sku, qty FROM order_items WHERE order_id = ?", (order_id,)
                ).fetchall()
                for item in items:
                    self.catalog.release(item["sku"], item["qty"])
            if order["coupon"]:
                self.conn.execute("UPDATE coupons SET uses = uses - 1 WHERE code = ? AND uses > 0",
                                  (order["coupon"],))
            self.conn.execute(
                "UPDATE orders SET status = 'cancelled', stock_reserved = 0 WHERE id = ?",
                (order_id,),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get(order_id)

    def ship(self, order_id):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            order = self.conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise OrderError(f"no order {order_id}")
            if order["status"] != "paid":
                raise OrderError("only a paid order can ship")
            self.conn.execute(
                "UPDATE orders SET status = 'shipped', stock_reserved = 0 WHERE id = ?",
                (order_id,),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get(order_id)

    def get(self, order_id):
        row = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise OrderError(f"no order {order_id}")
        order = dict(row)
        order["items"] = [dict(r) for r in self.conn.execute(
            "SELECT sku, qty, unit_price_cents, taxable FROM order_items WHERE order_id = ? ORDER BY rowid",
            (order_id,))]
        paid = self.conn.execute(
            "SELECT COALESCE(SUM(movement_cents), 0) AS paid FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()["paid"]
        order["paid_cents"] = paid
        order["balance_due_cents"] = (
            0 if order["status"] in ("cancelled", "expired", "refunded")
            else max(order["total_cents"] - max(paid, 0), 0)
        )
        order["overpaid_cents"] = max(paid - order["total_cents"], 0)
        order["refund_due_cents"] = (
            max(paid, 0) if order["status"] in ("cancelled", "expired") else 0
        )
        order["refund_excess_cents"] = max(-paid, 0)
        return order

    def list_for(self, customer, page=1, per_page=20):
        rows = self.conn.execute(
            "SELECT id FROM orders WHERE customer = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (customer, per_page, (page - 1) * per_page),
        ).fetchall()
        return [self.get(r["id"]) for r in rows]

    def _coupon(self, code):
        if code is None:
            return None
        row = self.conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
        if row is None:
            raise pricing.CouponRejected(f"unknown coupon {code}")
        return row
