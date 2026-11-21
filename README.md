# Tillpoint

A small order desk for a shop: a product catalog, orders with coupons and sales tax, payment webhooks and sales reports. Python 3.10+, standard library only, SQLite for storage.

## Run

```bash
python3 server.py tillpoint.db 8000
```

| Method | Path | Body |
|---|---|---|
| POST | `/orders` | `{"customer", "items": [{"sku", "qty"}], "region", "coupon"}` |
| GET | `/orders/<id>` | |
| POST | `/orders/<id>/cancel` | |
| GET / PUT | `/products/<sku>` | PUT: `{"price_cents"}` |
| POST | `/webhooks/payments` | `{"id", "type", "order_id", "amount_cents", "payment_ref"}` |
| GET | `/reports/daily/<YYYY-MM-DD>` | `{"utc_offset_minutes"}` |

Money is integer cents. Timestamps are stored in UTC. The payment provider retries a webhook until it receives a 2xx response.
Products are taxable by default; `Catalog.add_product(..., taxable=False)` marks exempt items.
Unpaid and partially paid checkouts expire after 30 minutes. Reading a product, reading an order, or creating another order runs stale-checkout cleanup and releases reserved stock.

Successful/captured/settled payment events are cash inflows. When a stable `payment_ref` is supplied, captured and settled notifications for the same provider payment are counted once. Refund events are cash outflows dated on the refund event; pending, failed, voided and authorized events do not change cash totals. Webhook responses include the net amount paid, balance due, overpayment credit and any refund due. The daily report returns captured, refunded and net cash-movement totals; provider fees are excluded because this API has no payout-batch or fee data.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## Layout

```
tillpoint/db.py        schema and time helpers
tillpoint/catalog.py   products, taxability, atomic stock operations
tillpoint/pricing.py   subtotal, coupons, tax
tillpoint/orders.py    create, cancel, ship, list
tillpoint/payments.py  idempotent payment and refund webhooks
tillpoint/reports.py   daily sales, top products
tillpoint/api.py       JSON API
server.py              HTTP server
```
