# Provenance

## Authorship

**Eduardo de Souza Marques** — author and maintainer.
GitHub: [@edusouzamarques](https://github.com/edusouzamarques)

## Origin

`spend-guard` is a general-purpose extraction from a private production system
the author designed and operates: an automated media pipeline that spends money
on external APIs — text generation, speech synthesis, image and video
rendering, rented GPU time — without a human approving each call.

The original enforcement was a pre-execution hook plus a declared budget file.
The hook sat in front of every expensive operation and refused it outright when
the budget was exhausted, rather than recording the cost afterwards. The budget
file declared a monthly total, a split into categories, per-unit caps, a daily
circuit breaker, soft and hard alert thresholds, and a global kill switch that
any watchdog could trip and only a human could clear.

Two production failures shaped what that system became, and both are the reason
this package exists in the shape it does:

1. **An unpriced unit of work was treated as free.** A new provider was wired
   in without a price entry. The cost accounting returned zero for it — no
   error, just a missing key — so the spend was invisible until the invoice
   arrived. The fix was to make "no declared price" a distinct, blocking
   outcome that can never be summed into a total. In this package that is
   `Quote.amount is None`, `OnMissing.BLOCK` as the default, and the deliberate
   absence of any setting that would make unknown work free.

2. **Budget held before a call leaked when the process died.** Checking the
   ceiling after the fact cannot refuse anything, so the check had to move in
   front of the call — which meant holding budget across an operation that
   might never return. Crashed runs left holds nobody would ever close, and the
   budget drifted down until the pipeline refused work it could afford. The fix
   was to lease every hold and make the *arithmetic* consult the clock, so an
   abandoned reservation expires on its own without depending on any cleanup
   process surviving. In this package that is `Reservation.holds_at(now)`, and
   the reason `sweep()` is explicitly bookkeeping rather than recovery.

The smaller decisions carry the same kind of history: integer micro-units
because per-token rates live in the sixth decimal place and floats drift;
rounding costs up and ceilings down because both directions protect the budget;
an idle period contributing no rollover because an outage should not fund a
spending spree; committing more than reserved being recorded rather than
rejected because money that already left the account does not come back when
the ledger objects.

## What was deliberately left behind

Everything specific to the private system was excluded, not renamed:

* every vendor, provider, product and service name, and all API credentials,
  endpoints and account identifiers;
* the private pipeline's stage machine, its database schema, its agent roster
  and its operational playbooks;
* real budget figures, real per-unit prices and real spend history — the
  amounts in `examples/` are invented round numbers chosen to be readable;
* all personal and machine-specific paths.

What remains is the model: budgets as declared data with no privileged vendor,
prices as a loadable and overridable table with an explicit unknown outcome, and
a reserve/commit/release protocol behind a single call that any caller can wrap.

## AI assistance

This package was developed with substantial AI assistance. The architecture and
the operational decisions are the author's: which behaviours are load-bearing,
what must fail closed and in what order, how the private system's failures
generalise, and what belongs in a public release. AI assistance was used to
implement that design, to expand the test suite, and to draft documentation.
All code and prose were reviewed by the author before release.

## Verification

The behaviours this package claims are pinned by its test suite (365 tests),
which runs with no network, no real clock and no external services. The two
headline properties have dedicated files:

* `tests/test_unknown_price_blocks.py` — an unknown price blocks and is never
  treated as free;
* `tests/test_reservation_leak.py` — a crash between reserve and commit does
  not permanently leak the reservation.

```bash
python -m pytest -q
```
