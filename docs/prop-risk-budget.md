# P0.6: admission control and unresolved risk model

P0.6 deliberately blocks **all new One-Step entries, DCA and replacements**.
Positive headroom is necessary but is not sufficient to authorize an order.
This is the explicit conservative fallback requested for an unproven risk model;
it is not a trading-ready implementation of a positive authorization policy.

## Static model decision

`NostalgiaForInfinityX8_Prop.py` declares `stoploss = -0.99`,
`use_custom_stoploss = False`, and `stoploss_on_exchange = False`. Its custom
exits and position adjustments do not establish a guaranteed maximum future
loss including gaps, slippage, fees and execution delays. P0.5's software
liquidation also depends on evidence, ownership and exchange availability.
Neither margin, leverage nor 100% of notional is adopted as a risk bound.

The gate computes P0.1's operating floor as the maximum daily/overall floor,
then headroom as `max(0, equity - floor)`. Contractual and operating thresholds
remain unchanged. Floating P&L is already in equity; it is not subtracted again.
Future loss and fee bounds remain explicitly `null` (unknown), never zero.
There is no configurable percentage or override that permits entries.

## Integration and accounting

`journal_create_order` invokes the gate for the enabled prop profile, after
P0.2/P0.4/P0.5 interlocks and fresh P0.3 reconciliation, before prepare/dispatch.
It covers initial entries and the same execution path used by DCA/replacement.
Quantities and all strategy parameters are preserved exactly. A replacement's
old order may already have been cancelled by existing logic; P0.6 refuses its
new entry. This intentionally reduces new exposure, and can prevent recovery
behaviour the strategy would otherwise attempt.

The gate validates profile/journal identity, heartbeat, baseline, wake-up/fault
locks, finite equity and reconciliation freshness (15 seconds). Config binding
is not remote UID attestation. P0.5 performs direct identity checks independently;
this patch does not treat its heartbeat as a new positive authorization token.

Inventory records actual positions and only the unfilled portion of pending
entry orders. An observed pending order replaces its journal reservation in the
inventory, rather than adding both. Terminal fills belong to positions. Missing
order evidence stays an unknown reservation. Requested new exposure is preserved
in the associated intent. Inventory quantities across different assets are not
summed as if they were monetary risk. Incomplete accounting never grants entry.

## Persistence and concurrency

Additive SQLite table `risk_reservations` uses the existing P0.3 lifetime process
lock, RLock and FULL commits. No new lock, exchange call or supervisor dependency
is added. Existing unresolved entry intents are backfilled as `held` with unknown
loss. Low-level journal prepare atomically inserts intent and held reservation.
It remains a persistence primitive, not a standalone risk authorization API.

Admission refusal atomically records an intent in `risk_denied` and a `denied`
reservation containing the decision. The requested quantity is unchanged. These
intents are settled because they were never submitted, and cannot transition
through dispatch's prepared-to-sending check. They consume no exposure budget.
No positive monetary reservation is fabricated while the model is unavailable.

Held reservations survive timeout, crash and restart. Only successful full
exchange/ORM reconciliation marks settled reservations `reconciled`: exposure
has moved into actual positions, or cancellation proved no remaining order.
Incomplete reconciliation retains them. P0.5 may leave conservative held rows
after emergency execution; this does not block reductions or imply permission
to restart. Its existing manual reconciliation requirement still applies.

Reduce-only orders bypass P0.6, including zero headroom and missing budget state.
Their existing recovery/ownership protections still apply. Emergency submissions
use P0.5's separate reduction-only authority and are unchanged. Journal ownership
still requires one shared directory on one host; it does not coordinate separate
hosts or unrelated volumes. No new lock can delay emergency handoff, although
normal existing SQLite writes and OS ownership still have their original limits.

## Proof and remaining decisions

Offline tests cover positive/small/zero headroom, overall floor dominance,
unchanged concurrent entry/DCA quantities, denied dispatch, pending orders,
partial fills, cancellation/replacement, unknown reservations, migration,
transaction rollback, timeout/crash/restart, owner exclusion, identity/baseline/
fault/stale data, actual core integration and reduction bypass. P0.1-P0.5 suites
are also rerun. No live exchange, ORM integration, backtest or paper run is claimed.
Tests with positive headroom intentionally still refuse orders: there is no
certified monetary requirement against which to prove sufficient trade budget.

A subsequent reviewed decision must define defensible aggregate loss and cost
bounds, execution/slippage assumptions, treatment of legacy exposure, and a fresh
remote identity/equity/position authorization contract. Positive reservation
amounts and admission checks must be implemented together with that model.
Do not merely remove the refusal or wire a user-supplied risk percentage.

Denied intents accumulate in the existing journal; retention/archival is not
implemented here. Storage failure fails closed. Rolling back P0.6 code removes
its admission protection, even though the additive table is data-compatible:
rollback must not be used as authorization to resume trading.

The budget arithmetic contains no One-Step percentages. Future Two-Step reuse
requires its own validated profile and model; no Two-Step branch was modified.
This patch neither implements P0.7 nor establishes challenge readiness.
