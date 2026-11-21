import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from tillpoint import db, reports
from tillpoint.api import App
from tillpoint.catalog import OutOfStock


class OrderDeskTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.app = App(self.conn)
        self.app.catalog.add_product("MUG", "Mug", 1200, stock=10)
        self.app.catalog.add_product("TEE", "T-shirt", 2500, stock=3)
        self.conn.execute("INSERT INTO coupons (code, kind, value) VALUES ('TEN', 'percent', 10)")

    def test_order_totals_with_tax(self):
        order = self.app.orders.create("ana", [("MUG", 2), ("TEE", 1)], region="CA")
        self.assertEqual(order["subtotal_cents"], 4900)
        self.assertEqual(order["tax_cents"], 355)
        self.assertEqual(order["total_cents"], 5255)
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 8)

    def test_coupon_takes_ten_percent(self):
        order = self.app.orders.create("ana", [("MUG", 1)], coupon_code="TEN")
        self.assertEqual(order["discount_cents"], 120)
        self.assertEqual(order["total_cents"], 1080)

    def test_coupon_reduces_taxable_amount(self):
        order = self.app.orders.create("ana", [("MUG", 1)], region="CA", coupon_code="TEN")
        self.assertEqual(order["discount_cents"], 120)
        self.assertEqual(order["tax_cents"], 78)
        self.assertEqual(order["total_cents"], 1158)

    def test_out_of_stock(self):
        with self.assertRaises(OutOfStock):
            self.app.orders.create("ana", [("TEE", 4)])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_concurrent_orders_do_not_oversell_stale_stock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "orders.sqlite")
            seed_conn = db.connect(path)
            App(seed_conn).catalog.add_product("LOW", "Low stock", 500, stock=1)
            seed_conn.close()

            barrier = threading.Barrier(2)
            outcomes = []
            observed_stock = []
            errors = []

            def create_order(customer):
                conn = None
                app = None
                try:
                    conn = db.connect(path)
                    app = App(conn)
                    app.catalog.get_product("LOW")
                    barrier.wait(timeout=5)
                    app.orders.create(customer, [("LOW", 1)])
                    outcomes.append("created")
                except OutOfStock:
                    outcomes.append("out_of_stock")
                except Exception as error:
                    errors.append(error)
                finally:
                    if app is not None:
                        try:
                            observed_stock.append(app.catalog.get_product("LOW").stock)
                        except Exception as error:
                            errors.append(error)
                    if conn is not None:
                        conn.close()

            threads = [
                threading.Thread(target=create_order, args=("ana",)),
                threading.Thread(target=create_order, args=("ben",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertCountEqual(outcomes, ["created", "out_of_stock"])
            self.assertEqual(observed_stock, [0, 0])

            check_conn = db.connect(path)
            self.assertEqual(check_conn.execute(
                "SELECT stock FROM products WHERE sku = 'LOW'").fetchone()["stock"], 0)
            self.assertEqual(check_conn.execute("SELECT COUNT(*) AS count FROM orders").fetchone()["count"], 1)
            check_conn.close()

    def test_payment_marks_order_paid(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        status, payload = self.app.handle("POST", "/webhooks/payments", {
            "id": "evt_1", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200})
        self.assertEqual((status, payload["status"]), (200, "paid"))

    def test_retried_payment_webhook_is_idempotent(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        event = {
            "id": "evt_retry", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200,
        }

        first_status, first_payload = self.app.handle("POST", "/webhooks/payments", event)
        retry_status, retry_payload = self.app.handle("POST", "/webhooks/payments", event)

        self.assertEqual((first_status, first_payload["status"]), (200, "paid"))
        self.assertEqual((retry_status, retry_payload["status"]), (200, "paid"))
        self.assertEqual(self.app.payments.paid_cents(order["id"]), 1200)
        payment_count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM payments WHERE event_id = ?", (event["id"],)
        ).fetchone()["count"]
        self.assertEqual(payment_count, 1)

    def test_cancel_paid_order_restocks(self):
        order = self.app.orders.create("ana", [("TEE", 2)])
        self.app.payments.handle_event(
            {"id": "evt_2", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 5000})
        self.app.orders.cancel(order["id"])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_daily_sales(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        noon = datetime(2025, 3, 10, 12, 0, tzinfo=timezone.utc)
        self.app.payments.handle_event(
            {"id": "evt_3", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200}, now=noon)
        self.assertEqual(reports.daily_sales(self.conn, "2025-03-10"),
                         {"day": "2025-03-10", "revenue_cents": 1200, "orders": 1})

    def test_daily_sales_uses_positive_utc_offset(self):
        morning_order = self.app.orders.create("ana", [("MUG", 1)])
        next_day_order = self.app.orders.create("ana", [("TEE", 1)])
        self.app.payments.handle_event(
            {"id": "evt_utc_morning", "type": "payment.succeeded",
             "order_id": morning_order["id"], "amount_cents": 1200},
            now=datetime(2025, 3, 9, 19, 0, tzinfo=timezone.utc))
        self.app.payments.handle_event(
            {"id": "evt_utc_evening", "type": "payment.succeeded",
             "order_id": next_day_order["id"], "amount_cents": 2500},
            now=datetime(2025, 3, 10, 19, 0, tzinfo=timezone.utc))

        self.assertEqual(
            reports.daily_sales(self.conn, "2025-03-10", utc_offset_minutes=330),
            {"day": "2025-03-10", "revenue_cents": 1200, "orders": 1})

    def test_price_change(self):
        self.app.handle("PUT", "/products/MUG", {"price_cents": 1500})
        self.assertEqual(self.app.catalog.get_product("MUG").price_cents, 1500)


if __name__ == "__main__":
    unittest.main()
