# NOTES

## 1. Report

> Finance says that on some days the revenue in the daily report is higher than what the payment provider paid out, and a few orders show more money received than their total.

**Status:** fixed for captured/refunded cash movements; provider fees and payout-batch reconciliation remain unavailable without provider settlement data.

**What I saw:** The old handler inserted every successful delivery, and the report summed all payment rows. A repeated event ID could therefore inflate both an order's paid amount and daily revenue. The old model also had no representation for refunds or non-cash payment states.

**Root cause:** There was no unique event-ID constraint or cash-movement classification; the report treated every row as a positive receipt.

**Fix and why:** Added transactional, event-ID-idempotent payment processing and a signed movement amount. Captures are positive, refunds are negative on the refund event date, and pending/failed/voided/authorized events have zero movement. A stable `payment_ref` prevents a separate settled notification for an already captured payment from being counted twice. Legacy identical duplicate event rows are collapsed during migration; conflicting rows fail migration rather than being silently altered. The report now shows captured, refunded, and net movement totals. Provider fees are excluded because no payout-batch or fee fields exist in this integration.

**How I verified it:** Tests cover duplicate webhook IDs, capture followed by settlement with the same `payment_ref`, pending/failed/voided events, and a partial refund plus its retry. The full suite passes.

**Commits:** `7a43d8f`, `efd7a10`

## 2. Report

> Products show as sold out, or nearly, while the shelf is full. Staff say it gets worse on days when many customers abandon checkout.

**Status:** fixed

**What I saw:** Stock is reserved when an order is created. Previously, pending orders had no expiration and cancelling an unpaid order did not release its reservation.

**Root cause:** There was no stale-checkout lifecycle, and cancellation only restored stock for paid orders.

**Fix and why:** Pending and partially paid checkouts expire after 30 minutes. Creating another order or reading a product/order expires stale checkouts and restores their stock and coupon use. Cancelling an unshipped order also releases its reservation, tracked with `stock_reserved` so repeated cancellation cannot release stock twice. Partial payments on expired/cancelled orders remain recorded as refund due.

**How I verified it:** Tests cover exact timeout boundaries, expiry through product reads, pending cancellation, repeated cancellation, coupon-use restoration, and a late payment after expiry. The concurrency test also confirms only one order can reserve a single remaining unit.

**Commits:** `efd7a10`

## 3. Report

> Customers in taxed regions who used a coupon say the tax on their receipt is too high. Receipts without a coupon are right.

**Status:** fixed

**What I saw:** Tax used the full subtotal before the coupon was subtracted.

**Root cause:** Coupon reductions were not applied to the taxable base.

**Fix and why:** Tax now applies after the coupon. Product taxability is explicit, defaulting to taxable; discounts are allocated proportionally across taxable and exempt line values. The taxable base is floored at zero, and rounding remains half up.

**How I verified it:** Tests cover the original CA coupon case, mixed taxable/exempt products, a 100% coupon, a fixed coupon greater than the subtotal, and an exact half-cent tax result. The full suite passes.

**Commits:** `a02d5e1`, `efd7a10`

## 4. Report

> Our shop in India (UTC+5:30) says sales taken in the morning do not appear in that day's report and turn up under the day before.

**Status:** fixed

**What I saw:** The UTC day boundary added the shop offset, shifting the requested local day later.

**Root cause:** UTC local-midnight conversion needs to subtract a positive offset.

**Fix and why:** `day_bounds_utc` subtracts `utc_offset_minutes` and uses a half-open interval `[start, end)`.

**How I verified it:** Tests check IST local midnight, 05:29, and the following local midnight boundary. The full suite passes.

**Commits:** `6e55167`, `efd7a10`

## 5. Report

> Ever since we started running two server workers, we have sold more units of an item than we had, and its stock went negative.

**Status:** fixed

**What I saw:** Each worker's catalog cache could hold stale stock, and the old reserve path decremented unconditionally after checking that cached value.

**Root cause:** The sufficiency check and decrement were separate operations using worker-local stock.

**Fix and why:** Removed the process-local cache and made reservation a conditional SQL update (`stock >= qty`). Order creation uses `BEGIN IMMEDIATE`, and failed reservations roll back the entire order.

**How I verified it:** A file-backed two-worker test starts both workers with one unit in stock. Exactly one order succeeds, the other gets `OutOfStock`, stock remains zero, and only one order is created. The concurrency test also passed five repeated runs.

**Commits:** `8d61cdf`, `efd7a10`

## 6. Open question

> Open question from support, no single right answer: sometimes a customer really does pay twice for one order (two separate payments, not a retry of the same one). Today the system records both and says nothing. Decide what should happen, build the smallest version of your decision, and explain what it costs in `NOTES.md`.

**My decision:** Record real separate captures, apply partial payments to the balance, and flag amounts above the order total as overpayment credit. Do not reject a webhook for money the provider has already captured.

**Why:** Rejecting an already captured payment could cause retries and hide cash that actually moved. Keeping it in the ledger preserves the audit trail.

**What it costs:** Refunds are recorded and reported but are not initiated automatically. Overpayment and refund-due amounts are derived from the movement ledger rather than a separate credit/refund workflow. Staff must reconcile those balances. Daily sales reports cash movements, not revenue net of provider fees.

**What I built:** Webhooks are idempotent by event ID. Partial payments set `partially_paid`; captures and refunds update the net paid balance, order status, balance due, overpayment credit, and refund due. `payment_ref` correlates capture and settlement notifications. Full refunds release unshipped stock; a later capture does not reopen a refunded, cancelled, or expired order.

**How I verified it:** Tests cover partial payments, distinct payments that overpay, same-ID retries, capture/settlement deduplication, partial and full refunds, excessive refunds, and late payments after expiry. The full suite passes.

**Commits:** `bf6dddd`, `efd7a10`

## Other things I noticed

No unrelated issues investigated. Net settlement after provider fees cannot be calculated until the provider integration supplies payout-batch and fee data.

## Session logs

Used Copilot SDK in VS Code. `session_logs/copilot_sdk_context_2026-11-21.json` is a redacted bounded context export, not a complete raw session export. The context tool marked it truncated, and it predates the final audit changes. A full export of the complete conversation is still needed before submission.
