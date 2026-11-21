"""Idempotent payment webhook processing and payment-balance reconciliation."""
from . import db


class PaymentError(Exception):
    pass


class Payments:
    CAPTURE_TYPES = {"payment.succeeded", "payment.captured", "payment.settled"}
    REFUND_TYPES = {"payment.refunded"}
    NO_MOVEMENT_TYPES = {
        "payment.pending", "payment.failed", "payment.voided", "payment.authorized",
    }

    def __init__(self, conn):
        self.conn = conn

    def handle_event(self, event, now=None):
        """Record a provider event once and return the order's reconciled payment summary."""
        event_type = event.get("type")
        if event_type not in self.CAPTURE_TYPES | self.REFUND_TYPES | self.NO_MOVEMENT_TYPES:
            return None

        amount = event.get("amount_cents", 0)
        if event_type in self.CAPTURE_TYPES | self.REFUND_TYPES:
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise ValueError("payment amount_cents must be a positive integer")
        elif isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("payment amount_cents must be a non-negative integer")

        payment_ref = event.get("payment_ref")
        if payment_ref is not None and (not isinstance(payment_ref, str) or not payment_ref):
            raise ValueError("payment_ref must be a non-empty string")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT order_id FROM payments WHERE event_id = ?", (event["id"],)
            ).fetchone()
            if existing is not None:
                result = self._summary(existing["order_id"])
                self.conn.execute("COMMIT")
                return result

            order = self.conn.execute(
                "SELECT id FROM orders WHERE id = ?", (event["order_id"],)
            ).fetchone()
            if order is None:
                raise PaymentError(f"payment for unknown order {event['order_id']}")

            movement = 0
            if event_type in self.CAPTURE_TYPES:
                prior_capture = None
                if payment_ref is not None:
                    prior_capture = self.conn.execute(
                        "SELECT order_id, amount_cents FROM payments "
                        "WHERE payment_ref = ? AND movement_cents > 0 LIMIT 1",
                        (payment_ref,),
                    ).fetchone()
                if prior_capture is not None:
                    if prior_capture["order_id"] != order["id"] or prior_capture["amount_cents"] != amount:
                        raise PaymentError(f"payment reference {payment_ref!r} conflicts with an earlier capture")
                else:
                    movement = amount
            elif event_type in self.REFUND_TYPES:
                movement = -amount

            cursor = self.conn.execute(
                "INSERT INTO payments "
                "(order_id, event_id, amount_cents, received_at, event_type, payment_ref, movement_cents) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
                (order["id"], event["id"], amount, db.to_iso(now or db.utcnow()),
                 event_type, payment_ref, movement),
            )
            if cursor.rowcount == 0:
                existing = self.conn.execute(
                    "SELECT order_id FROM payments WHERE event_id = ?", (event["id"],)
                ).fetchone()
                if existing is None:
                    raise PaymentError(f"payment event {event['id']} was not recorded")
                result = self._summary(existing["order_id"])
                self.conn.execute("COMMIT")
                return result

            if movement != 0:
                self._sync_order_status(order["id"])
            result = self._summary(order["id"])
            self.conn.execute("COMMIT")
            return result
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _sync_order_status(self, order_id):
        order = self.conn.execute(
            "SELECT status, total_cents, stock_reserved FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order["status"] in ("cancelled", "expired", "refunded", "shipped"):
            return

        paid = self.paid_cents(order_id)
        captured = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN movement_cents > 0 THEN movement_cents ELSE 0 END), 0) "
            "AS captured FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()["captured"]
        if paid <= 0:
            status = "refunded" if captured > 0 else "pending"
        elif paid < order["total_cents"]:
            status = "partially_paid"
        else:
            status = "paid"

        stock_reserved = order["stock_reserved"]
        if status == "refunded" and stock_reserved:
            items = self.conn.execute(
                "SELECT sku, qty FROM order_items WHERE order_id = ?", (order_id,)
            ).fetchall()
            for item in items:
                self.conn.execute(
                    "UPDATE products SET stock = stock + ? WHERE sku = ?",
                    (item["qty"], item["sku"]),
                )
            stock_reserved = 0
        self.conn.execute(
            "UPDATE orders SET status = ?, stock_reserved = ? WHERE id = ?",
            (status, stock_reserved, order_id),
        )

    def _summary(self, order_id):
        order = self.conn.execute(
            "SELECT status, total_cents FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise PaymentError(f"payment for unknown order {order_id}")
        paid = self.paid_cents(order_id)
        status = order["status"]
        return {
            "status": status,
            "paid_cents": paid,
            "balance_due_cents": (
                0 if status in ("cancelled", "expired", "refunded")
                else max(order["total_cents"] - max(paid, 0), 0)
            ),
            "overpaid_cents": max(paid - order["total_cents"], 0),
            "refund_due_cents": (
                max(paid, 0) if status in ("cancelled", "expired", "refunded") else 0
            ),
            "refund_excess_cents": max(-paid, 0),
        }

    def paid_cents(self, order_id):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(movement_cents), 0) AS paid FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return row["paid"]

    def overpaid_cents(self, order_id):
        order = self.conn.execute(
            "SELECT total_cents FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise PaymentError(f"payment for unknown order {order_id}")
        return max(self.paid_cents(order_id) - order["total_cents"], 0)
