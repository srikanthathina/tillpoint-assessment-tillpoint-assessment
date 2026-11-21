# NOTES

## 1. Report

> Finance says that on some days the revenue in the daily report is higher than what the payment provider paid out, and a few orders show more money received than their total.

**Status:** fixed

**What I saw:** A repeated successful webhook with the same provider event ID was inserted as another payment row. Since the payment and daily sales totals sum payment rows, retry deliveries could inflate both totals.

**Root cause:** `handle_event` inserted every successful delivery without deduplicating by event ID, and the payments table did not enforce unique event IDs.

**Fix and why:** Added a unique index on `payments.event_id` and made duplicate inserts a no-op. A repeated webhook returns successfully without counting its amount again. Distinct event IDs remain separate payments; their overpayment handling is described in Report 6.

**How I verified it:** Added a retry test that sends the same event twice and checks both responses are HTTP 200, only one payment row exists, and the order's paid amount remains 1,200 cents. The full test suite passed.

**Commits:** `7a43d8f`

## 2. Report

> Products show as sold out, or nearly, while the shelf is full. Staff say it gets worse on days when many customers abandon checkout.

**Status:** not fixed

**What I saw:** Order creation reserves stock immediately. Cancelling an unpaid order does not release its reservation, and there is no expiration or abandoned-checkout cleanup.

**Root cause:** There is no lifecycle policy or mechanism to release stock held by pending orders that customers never pay for.

**Fix and why:** No fix was made. Choosing and implementing an order-expiration policy is still needed; without one, abandoned pending orders can continue to consume stock.

**How I verified it:** Inspected order creation and cancellation behavior. I did not add an expiration test because no expiration behavior was implemented.

**Commits:** none

## 3. Report

> Customers in taxed regions who used a coupon say the tax on their receipt is too high. Receipts without a coupon are right.

**Status:** fixed

**What I saw:** `price_order` calculated tax from the full subtotal, then subtracted the coupon discount only when calculating the total.

**Root cause:** The coupon discount was not applied to the taxable amount.

**Fix and why:** Tax is now calculated from `max(subtotal - discount, 0)`. This taxes the discounted price and ensures the taxable base cannot be negative.

**How I verified it:** A CA order for a 1,200-cent item with a 10% coupon failed before the fix (87 cents tax instead of 78). It passes afterward with a 1,158-cent total. The full test suite passed.

**Commits:** `a02d5e1`

## 4. Report

> Our shop in India (UTC+5:30) says sales taken in the morning do not appear in that day's report and turn up under the day before.

**Status:** fixed

**What I saw:** `day_bounds_utc` added the shop offset to local midnight. For a positive offset, this moved the UTC window later instead of earlier.

**Root cause:** UTC time for local midnight is local midnight minus the UTC offset; the sign was reversed.

**Fix and why:** The report now subtracts `utc_offset_minutes` when calculating the UTC start of the local day. The end remains one day after that start.

**How I verified it:** Added a test for a 330-minute offset that distinguishes a local-morning payment from a payment just after the following local midnight. It failed before the fix and passed afterward. The full test suite passed.

**Commits:** `6e55167`

## 5. Report

> Ever since we started running two server workers, we have sold more units of an item than we had, and its stock went negative.

**Status:** fixed

**What I saw:** `Orders.create` already takes a SQLite `BEGIN IMMEDIATE` lock, but each worker's catalog cache could retain a stale stock count. `Catalog.reserve` checked that cached count, then decremented the database stock unconditionally.

**Root cause:** The stock sufficiency check and decrement were not one conditional database operation, and worker-local cached stock could be stale after another worker committed.

**Fix and why:** Removed the per-process catalog cache and made reservation a conditional SQL update (`stock >= qty`). An insufficient-stock result uses the current database value and raises `OutOfStock`; the surrounding order transaction rolls back. The existing write lock remains in place.

**How I verified it:** A file-backed SQLite test starts two workers with one unit in stock. Before the fix both orders succeeded; afterward exactly one succeeds, the other gets `OutOfStock`, stock remains zero, and only one order exists. All tests passed, and the concurrency test passed five repeated runs.

**Commits:** `8d61cdf`

## 6. Open question

> Open question from support, no single right answer: sometimes a customer really does pay twice for one order (two separate payments, not a retry of the same one). Today the system records both and says nothing. Decide what should happen, build the smallest version of your decision, and explain what it costs in `NOTES.md`.

**My decision:** Accept and record each real successful payment. If the total exceeds the order total, report the excess as `overpaid_cents` in the successful webhook response.

**Why:** Rejecting a webhook cannot reverse a charge the provider already accepted. It could also cause retries and leave the payment record incomplete. Keeping the full payment trail and explicitly flagging the excess is safer.

**What it costs:** `overpaid_cents` is derived from recorded payments and the order total; it is not a separate credit ledger. Refunds or applying the credit to a future order are not automated, so staff must reconcile it. Daily sales still counts the full cash received; accounting must distinguish the overpayment from earned sales.

**What I built:** Added `Payments.overpaid_cents` and included that amount in successful payment webhook responses. The order remains paid and the extra payment remains recorded; a retry with the same event ID is still idempotent.

**How I verified it:** Added a test that submits two successful payments with different event IDs for a 1,200-cent order. Both return HTTP 200, the second reports 1,200 cents overpaid, and the recorded paid amount is 2,400 cents. The full suite passed.

**Commits:** `bf6dddd`

## Other things I noticed

No unrelated issues investigated. Report 2 remains unfixed as described above.

## Session logs

Used Copilot SDK in VS Code. `session_logs/copilot_sdk_context_2026-11-21.json` contains the available session-context transcript and tool-call records; the personal tracking URL and private session identifiers are redacted. The context tool marked its transcript as truncated and the current turn as in progress; no native raw-export tool was available.
