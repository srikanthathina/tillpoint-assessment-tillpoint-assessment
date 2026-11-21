import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from tillpoint import db, reports
from tillpoint.api import App
from tillpoint.catalog import OutOfStock
from tillpoint.payments import PaymentError


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

    def test_coupon_is_allocated_proportionally_for_exempt_items(self):
        self.app.catalog.add_product("BOOK", "Book", 500, stock=1, taxable=False)
        order = self.app.orders.create(
            "ana", [("MUG", 1), ("BOOK", 1)], region="CA", coupon_code="TEN")

        self.assertEqual(order["discount_cents"], 170)
        self.assertEqual(order["tax_cents"], 78)
        self.assertEqual(order["total_cents"], 1608)

    def test_full_coupon_makes_order_tax_free_and_paid(self):
        self.conn.execute(
            "INSERT INTO coupons (code, kind, value) VALUES ('FREE', 'percent', 100)")
        order = self.app.orders.create("ana", [("MUG", 1)], region="CA", coupon_code="FREE")

        self.assertEqual(order["discount_cents"], 1200)
        self.assertEqual(order["tax_cents"], 0)
        self.assertEqual(order["total_cents"], 0)
        self.assertEqual(order["status"], "paid")

    def test_fixed_coupon_over_subtotal_is_capped(self):
        self.conn.execute(
            "INSERT INTO coupons (code, kind, value) VALUES ('TOO_MUCH', 'fixed', 5000)")
        order = self.app.orders.create(
            "ana", [("MUG", 1)], region="CA", coupon_code="TOO_MUCH")

        self.assertEqual(order["discount_cents"], 1200)
        self.assertEqual(order["tax_cents"], 0)
        self.assertEqual(order["total_cents"], 0)
        self.assertEqual(order["status"], "paid")

    def test_tax_rounds_exact_half_up(self):
        self.app.catalog.add_product("SMALL", "Small item", 200, stock=1)
        order = self.app.orders.create("ana", [("SMALL", 1)], region="CA")
        self.assertEqual(order["tax_cents"], 15)

    def test_out_of_stock(self):
        with self.assertRaises(OutOfStock):
            self.app.orders.create("ana", [("TEE", 4)])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_cancel_pending_checkout_releases_stock_once(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 9)

        self.app.orders.cancel(order["id"])
        self.app.orders.cancel(order["id"])

        self.assertEqual(self.app.catalog.get_product("MUG").stock, 10)
        self.assertEqual(self.app.orders.get(order["id"])["stock_reserved"], 0)

    def test_expired_checkout_releases_stock_and_coupon_use(self):
        self.conn.execute(
            "INSERT INTO coupons (code, kind, value, max_uses) VALUES ('LIMITED', 'percent', 10, 1)")
        created = datetime(2025, 3, 10, 12, 0, tzinfo=timezone.utc)
        order = self.app.orders.create(
            "ana", [("MUG", 1)], coupon_code="LIMITED", now=created)

        self.assertEqual(
            self.app.orders.expire_stale(now=created + timedelta(minutes=29, seconds=59)), [])
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 9)

        self.assertEqual(
            self.app.orders.expire_stale(now=created + timedelta(minutes=30)), [order["id"]])
        self.assertEqual(self.app.orders.get(order["id"])["status"], "expired")
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 10)
        self.assertEqual(
            self.conn.execute("SELECT uses FROM coupons WHERE code = 'LIMITED'").fetchone()["uses"], 0)

    def test_payment_after_expiry_is_recorded_as_refund_due(self):
        created = db.utcnow() - timedelta(minutes=31)
        order = self.app.orders.create("ana", [("MUG", 1)], now=created)
        self.app.orders.expire_stale()

        result = self.app.payments.handle_event({
            "id": "evt_late_payment", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200})

        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["refund_due_cents"], 1200)
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 10)

    def test_product_read_expires_abandoned_checkout(self):
        created = db.utcnow() - timedelta(minutes=31)
        order = self.app.orders.create("ana", [("MUG", 1)], now=created)
        status, product = self.app.handle("GET", "/products/MUG")

        self.assertEqual(status, 200)
        self.assertEqual(product["stock"], 10)
        self.assertEqual(self.app.orders.get(order["id"])["status"], "expired")

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

    def test_separate_payment_is_reported_as_overpayment_credit(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        first_status, first_payload = self.app.handle("POST", "/webhooks/payments", {
            "id": "evt_first_payment", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200})
        second_status, second_payload = self.app.handle("POST", "/webhooks/payments", {
            "id": "evt_second_payment", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200})

        self.assertEqual((first_status, first_payload["status"]), (200, "paid"))
        self.assertEqual((second_status, second_payload["status"]), (200, "paid"))
        self.assertEqual(second_payload.get("overpaid_cents"), 1200)
        self.assertEqual(self.app.payments.paid_cents(order["id"]), 2400)

    def test_partial_payments_update_balance_and_status(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        first = self.app.payments.handle_event({
            "id": "evt_part_1", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 500})
        second = self.app.payments.handle_event({
            "id": "evt_part_2", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 700})

        self.assertEqual((first["status"], first["balance_due_cents"]), ("partially_paid", 700))
        self.assertEqual((second["status"], second["balance_due_cents"]), ("paid", 0))
        self.assertEqual(second["paid_cents"], 1200)
        order_view = self.app.orders.get(order["id"])
        self.assertEqual(order_view["paid_cents"], 1200)
        self.assertEqual(order_view["balance_due_cents"], 0)

    def test_settlement_for_captured_payment_is_not_counted_twice(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.app.payments.handle_event({
            "id": "evt_capture", "type": "payment.succeeded",
            "order_id": order["id"], "payment_ref": "charge-1", "amount_cents": 1200},
            now=datetime(2025, 3, 10, 12, tzinfo=timezone.utc))
        self.app.payments.handle_event({
            "id": "evt_settled", "type": "payment.settled",
            "order_id": order["id"], "payment_ref": "charge-1", "amount_cents": 1200},
            now=datetime(2025, 3, 10, 13, tzinfo=timezone.utc))

        self.assertEqual(self.app.payments.paid_cents(order["id"]), 1200)
        self.assertEqual(
            reports.daily_sales(self.conn, "2025-03-10")["captured_cents"], 1200)

    def test_conflicting_payment_reference_is_rejected(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.app.payments.handle_event({
            "id": "evt_reference_1", "type": "payment.succeeded",
            "order_id": order["id"], "payment_ref": "charge-shared", "amount_cents": 1200})

        with self.assertRaises(PaymentError):
            self.app.payments.handle_event({
                "id": "evt_reference_2", "type": "payment.succeeded",
                "order_id": order["id"], "payment_ref": "charge-shared", "amount_cents": 600})
        self.assertEqual(self.app.payments.paid_cents(order["id"]), 1200)

    def test_pending_failed_and_voided_events_do_not_change_cash_totals(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        for event_id, event_type in [
            ("evt_pending", "payment.pending"),
            ("evt_failed", "payment.failed"),
            ("evt_void", "payment.voided"),
        ]:
            result = self.app.payments.handle_event({
                "id": event_id, "type": event_type,
                "order_id": order["id"], "amount_cents": 1200})
            self.assertEqual(result["paid_cents"], 0)
            self.assertEqual(result["status"], "pending")

        today = db.utcnow().date().isoformat()
        self.assertEqual(
            reports.daily_sales(self.conn, today),
            {"day": today, "captured_cents": 0, "refunded_cents": 0,
             "revenue_cents": 0, "orders": 0},
        )

    def test_refund_is_negative_cash_flow_on_refund_date_and_idempotent(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.app.payments.handle_event({
            "id": "evt_capture_refund", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200},
            now=datetime(2025, 3, 10, 12, tzinfo=timezone.utc))
        refund = {
            "id": "evt_refund", "type": "payment.refunded",
            "order_id": order["id"], "amount_cents": 300,
        }
        first = self.app.payments.handle_event(
            refund, now=datetime(2025, 3, 11, 12, tzinfo=timezone.utc))
        retry = self.app.payments.handle_event(
            refund, now=datetime(2025, 3, 11, 13, tzinfo=timezone.utc))

        self.assertEqual((first["status"], first["paid_cents"],
                          first["balance_due_cents"]), ("partially_paid", 900, 300))
        self.assertEqual(retry["paid_cents"], 900)
        self.assertEqual(reports.daily_sales(self.conn, "2025-03-10"),
                         {"day": "2025-03-10", "captured_cents": 1200,
                          "refunded_cents": 0, "revenue_cents": 1200, "orders": 1})
        self.assertEqual(reports.daily_sales(self.conn, "2025-03-11"),
                         {"day": "2025-03-11", "captured_cents": 0,
                          "refunded_cents": 300, "revenue_cents": -300, "orders": 1})

    def test_full_refund_releases_stock_and_marks_order_refunded(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.app.payments.handle_event({
            "id": "evt_full_capture", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200})

        result = self.app.payments.handle_event({
            "id": "evt_full_refund", "type": "payment.refunded",
            "order_id": order["id"], "amount_cents": 1200})

        self.assertEqual(result["status"], "refunded")
        self.assertEqual(result["paid_cents"], 0)
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 10)

    def test_refund_larger_than_captured_amount_is_flagged(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.app.payments.handle_event({
            "id": "evt_capture_short", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200})

        result = self.app.payments.handle_event({
            "id": "evt_refund_excess", "type": "payment.refunded",
            "order_id": order["id"], "amount_cents": 1500})

        self.assertEqual(result["status"], "refunded")
        self.assertEqual(result["paid_cents"], -300)
        self.assertEqual(result["refund_excess_cents"], 300)

    def test_refunded_order_does_not_reopen_on_late_capture(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        self.app.payments.handle_event({
            "id": "evt_original_capture", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200})
        self.app.payments.handle_event({
            "id": "evt_original_refund", "type": "payment.refunded",
            "order_id": order["id"], "amount_cents": 1200})

        result = self.app.payments.handle_event({
            "id": "evt_late_capture", "type": "payment.succeeded",
            "order_id": order["id"], "amount_cents": 1200})

        self.assertEqual(result["status"], "refunded")
        self.assertEqual(result["refund_due_cents"], 1200)
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 10)

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
                         {"day": "2025-03-10", "captured_cents": 1200, "refunded_cents": 0,
                          "revenue_cents": 1200, "orders": 1})

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
            {"day": "2025-03-10", "captured_cents": 1200, "refunded_cents": 0,
             "revenue_cents": 1200, "orders": 1})

    def test_ist_report_includes_0000_to_0529_and_excludes_0530(self):
        events = [
            ("evt_ist_midnight", datetime(2025, 3, 9, 18, 30, tzinfo=timezone.utc), 100),
            ("evt_ist_0529", datetime(2025, 3, 9, 23, 59, tzinfo=timezone.utc), 200),
            ("evt_ist_next_midnight", datetime(2025, 3, 10, 18, 30, tzinfo=timezone.utc), 300),
        ]
        for event_id, received_at, amount in events:
            order = self.app.orders.create("ana", [("MUG", 1)])
            self.app.payments.handle_event(
                {"id": event_id, "type": "payment.succeeded",
                 "order_id": order["id"], "amount_cents": amount},
                now=received_at)

        self.assertEqual(
            reports.daily_sales(self.conn, "2025-03-10", utc_offset_minutes=330),
            {"day": "2025-03-10", "captured_cents": 300, "refunded_cents": 0,
             "revenue_cents": 300, "orders": 2})

    def test_legacy_database_migration_deduplicates_identical_event_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "legacy.sqlite")
            legacy = sqlite3.connect(path)
            legacy.executescript("""
                CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT NOT NULL,
                    price_cents INTEGER NOT NULL, stock INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE coupons (code TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    value INTEGER NOT NULL, min_subtotal_cents INTEGER NOT NULL DEFAULT 0,
                    max_uses INTEGER, uses INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT NOT NULL,
                    region TEXT NOT NULL, status TEXT NOT NULL, coupon TEXT,
                    subtotal_cents INTEGER NOT NULL, discount_cents INTEGER NOT NULL,
                    tax_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE order_items (order_id INTEGER NOT NULL, sku TEXT NOT NULL,
                    qty INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL);
                CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
                    event_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, received_at TEXT NOT NULL);
                INSERT INTO products VALUES ('MUG', 'Mug', 1200, 1);
                INSERT INTO orders VALUES (1, 'ana', 'NONE', 'pending', NULL,
                    1200, 0, 0, 1200, '2025-03-10T12:00:00Z');
                INSERT INTO payments VALUES (1, 1, 'legacy_evt', 1200, '2025-03-10T12:01:00Z');
                INSERT INTO payments VALUES (2, 1, 'legacy_evt', 1200, '2025-03-10T12:02:00Z');
            """)
            legacy.close()

            migrated = db.connect(path)
            count = migrated.execute(
                "SELECT COUNT(*) AS count FROM payments WHERE event_id = 'legacy_evt'"
            ).fetchone()["count"]
            self.assertEqual(count, 1)
            self.assertEqual(App(migrated).payments.paid_cents(1), 1200)
            self.assertEqual(
                migrated.execute("SELECT SUM(movement_cents) AS paid FROM payments").fetchone()["paid"],
                1200)
            self.assertIsNotNone(
                migrated.execute("SELECT expires_at FROM orders WHERE id = 1").fetchone()["expires_at"])
            migrated.close()
    def test_price_change(self):
        self.app.handle("PUT", "/products/MUG", {"price_cents": 1500})
        self.assertEqual(self.app.catalog.get_product("MUG").price_cents, 1500)


if __name__ == "__main__":
    unittest.main()
