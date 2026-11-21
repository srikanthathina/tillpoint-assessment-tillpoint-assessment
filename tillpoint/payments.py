"""Payment provider webhooks. The provider retries a delivery until it gets a 2xx, so events can repeat."""
from . import db


class PaymentError(Exception):
    pass


class Payments:
    def __init__(self, conn):
        self.conn = conn

    def handle_event(self, event, now=None):
        """event: dict(id, type, order_id, amount_cents). Returns the order's status after the event."""
        if event.get("type") != "payment.succeeded":
            return None
        existing_status = self._event_status(event["id"])
        if existing_status is not None:
            return existing_status
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (event["order_id"],)).fetchone()
        if order is None:
            raise PaymentError(f"payment for unknown order {event['order_id']}")
        if order["status"] == "cancelled":
            raise PaymentError(f"order {order['id']} is cancelled")
        cursor = self.conn.execute(
            "INSERT INTO payments (order_id, event_id, amount_cents, received_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(event_id) DO NOTHING",
            (order["id"], event["id"], event["amount_cents"], db.to_iso(now or db.utcnow())),
        )
        if cursor.rowcount == 0:
            existing_status = self._event_status(event["id"])
            if existing_status is None:
                raise PaymentError(f"payment event {event['id']} was not recorded")
            return existing_status
        if order["status"] == "pending" and self.paid_cents(order["id"]) >= order["total_cents"]:
            self.conn.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order["id"],))
            return "paid"
        return order["status"]

    def _event_status(self, event_id):
        row = self.conn.execute(
            "SELECT o.status FROM payments p JOIN orders o ON o.id = p.order_id WHERE p.event_id = ?",
            (event_id,),
        ).fetchone()
        return None if row is None else row["status"]

    def paid_cents(self, order_id):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS paid FROM payments WHERE order_id = ?", (order_id,)
        ).fetchone()
        return row["paid"]

    def overpaid_cents(self, order_id):
        order = self.conn.execute(
            "SELECT total_cents FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise PaymentError(f"payment for unknown order {order_id}")
        return max(self.paid_cents(order_id) - order["total_cents"], 0)
