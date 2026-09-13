# spend-guard

[![CI](https://github.com/edusouzaxGV/spend-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/edusouzaxGV/spend-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](./pyproject.toml)

**Hard budget ceilings for AI and cloud spend that fail closed.**
Zero runtime dependencies. Pure, testable decision logic with an injected clock.

---

## The failure this prevents

A pipeline logs the cost of every call it makes. The dashboard is beautiful. At
the end of the month the bill is four times the budget, and the post-mortem
finds two things:

1. **A new model was added and nobody added its price.** The cost estimator
   returned `0.0` for it — not an error, just a missing dictionary key — and
   `total += 0.0` ran forty thousand times. The dashboard showed a healthy
   budget the whole way, because the calls that were not priced were exactly
   the ones that were new, unfamiliar and expensive.

2. **Nothing ever refused a call.** Cost was recorded *after* each call
   returned. By the time the running total crossed the ceiling, the money was
   already gone; the only thing the ceiling could do was describe what had
   happened.

`spend-guard` fixes both. A unit of work with no declared price is **refused**,
never charged as zero. And budget is **held before the call runs**, so the call
that would break the ceiling is the one that does not happen.

```python
from spend_guard import Money, SpendGuard, build_guard

guard = build_guard("budget.json", "prices.json", "var/spend.ndjson")

with guard.charge("text.generate", units=8_000, category="text") as ticket:
    response = call_the_api()          # only runs if the budget had room
    ticket.actual(Money.parse("0.031"))  # reconcile with the real cost
```

If the budget is spent, `charge` raises `BudgetExceededError` and the body never
runs. If `"text.generate"` has no price, it raises `UnknownPriceError` — with
plenty of headroom, because not knowing the cost is a separate problem from not
having the money.

---

## Install

Not on PyPI yet — `pip install spend-guard` does not resolve, and the badge
row above deliberately carries no build-status claim until CI has actually run
somewhere public. Install from a checkout:

```bash
git clone <this repository> spend-guard
cd spend-guard
pip install -e .                  # core, no dependencies
pip install -e ".[toml,yaml]"     # optional config formats
pip install -e ".[test]" && python -m pytest -q
```

The core needs nothing but the standard library. TOML on Python 3.11+ uses
`tomllib`; on older versions and for YAML the package degrades with an error
that names the extra to install, rather than an `ImportError` from a loader.
The extras keep the names they will have when the package is published, so the
install line changes to `pip install "spend-guard[toml,yaml]"` and nothing
else does.

---

## How it works

### 1. Declare the budget as data

```json
{
  "currency": "USD",
  "period": { "kind": "monthly", "anchor_day": 1 },
  "ceiling": "1200.00",
  "warn_threshold": "0.80",
  "allow_uncategorised": false,
  "rollover": { "policy": "carry_unused", "cap": "200.00" },
  "categories": {
    "text":    { "ceiling": "250.00" },
    "speech":  { "ceiling": "150.00" },
    "images":  { "ceiling": "100.00" },
    "video":   { "ceiling": "300.00" },
    "compute": { "ceiling": "250.00", "warn_threshold": "0.90" },
    "storage": { "ceiling": "100.00" }
  }
}
```

* **Period**: `daily`, `weekly`, `monthly`, `quarterly`, `yearly`, or
  `fixed_days`. `anchor_day` follows a provider's billing cycle instead of the
  calendar, and clamps correctly — an anchor of 31 lands on the 28th in
  February without leaving a gap between windows.
* **Ceiling**: hard. **`warn_threshold`**: soft, the only signal between "fine"
  and "refused". A category may declare its own, and `status` reports a
  category past *its* threshold even while the total still looks healthy.
* **Categories**: sub-ceilings that bind *before* the total. Their sum may not
  exceed the total; a sub-budget that can never be spent is a declaration bug
  and is rejected at load.
* **Rollover**: `none` (default), `carry_unused`, `carry_deficit`,
  `carry_both`, with an optional `cap`. A period with **no ledger activity at
  all carries no unused budget** and breaks the chain in that direction, so a
  system switched off for six months does not wake up with six months of
  headroom. A **deficit survives an idle period**: silence is not repayment,
  and an overspend big enough to zero the next ceiling makes that period
  silent by construction — every call in it is refused, and a refusal writes
  nothing. The forgiving direction breaks on silence; the strict one does not.

### 2. Declare what work costs

```json
{
  "on_missing": "block",
  "rules": {
    "text.generate":     { "unit": "token",     "per": 1000000, "unit_price": "3.00" },
    "speech.synthesise": { "unit": "character", "per": 1000, "unit_price": "0.30", "minimum": "0.01" },
    "image.render":      { "unit": "image",     "unit_price": "0.04" },
    "compute.gpu.*":     { "unit": "hour",      "unit_price": "0.90" }
  }
}
```

* `per` keeps a rate like "$3 per million tokens" exact instead of pre-dividing
  it into a rounded per-token price.
* `compute.gpu.*` is a wildcard: a newly released name in a known family stays
  priced instead of blocking the pipeline at 2am. The longest matching prefix
  wins, and an exact rule beats any wildcard.
* `on_missing` is `block` or `estimate`. **There is no "treat as zero" mode.**
  An operator who wants unpriced work to pass must name a `fallback_price`, and
  every ledger entry produced that way is flagged `estimated` so an audit can
  separate measured spend from guessed spend.

Nothing in the package knows any vendor's name. Keys are whatever you call your
own units of work, and prices come from a file, `--price key=amount`, or
`PriceTable.with_overrides(...)`.

### 3. Reserve, then commit

```
reserve ──► (call runs) ──► commit(actual)     money is charged, ledger is reconciled
        └─► (call fails) ─► release            budget returns immediately
        └─► (process dies) ─► lease expires    budget returns on its own
```

`reserve` holds the estimate *before* the call. `commit` replaces it with the
real cost. `release` gives it back. Every transition is one line appended to an
NDJSON ledger, and all state is derived by replaying that file — so "why was
this call allowed" always has an answer.

Committing **more** than was reserved is not an error. The money already left
the account; the ledger's job is to say so. The overrun shows up as smaller (or
negative) headroom, and the *next* reserve is what refuses.

Committing the same reservation twice with the same amount charges once, so an
at-least-once caller can retry safely. Twice with *different* amounts raises
rather than guessing which one is true.

### 4. The crash case

A process that dies between `reserve` and `commit` leaves a hold nobody will
close. A guard that leaks a little on every crash eventually refuses
everything, which looks exactly like a working budget.

So every hold is **leased**. The arithmetic consults the clock: an expired hold
stops counting the instant its lease lapses — with no cleanup job, no
supervisor, and no need for the original process to ever run again.

```python
# 40.00 held, then the process is killed:
guard.reserve("image.render", 1000, category="images")
# ... 16 minutes later, from a completely different process:
guard.snapshot().available            # the 40.00 is back
```

(The budget above sets `"allow_uncategorised": false`, so every call has to
name a category. That is the stricter setting and the one worth showing.)

`sweep()` exists, but it only writes the expiry into the ledger so an operator
can see which call vanished. It is bookkeeping, not recovery. Several tests
never sweep at all, on purpose.

A late `commit` after the lease expired is still charged — the budget was
freed, but the API call may well have run and billed.

---

## Command line

```bash
export SPEND_GUARD_BUDGET=budget.json
export SPEND_GUARD_PRICES=prices.json
export SPEND_GUARD_LEDGER=var/spend.ndjson

spend-guard status                    # where the period stands, by category
spend-guard project                   # burn rate extended to period end
spend-guard simulate video.render --units 600 --category video --repeat 3
spend-guard price text.generate --units 1000000
spend-guard ledger --limit 20         # recent history
spend-guard verify                    # check the ledger file for damage
spend-guard halt --reason "runaway loop"
```

Exit codes are part of the contract:

| code | meaning |
|------|---------|
| `0`  | succeeded, or the simulated spend is allowed |
| `1`  | the command failed (bad declaration, missing file, unrepairable ledger) |
| `2`  | **the spend was refused** — and from `project`, the forecast lands over the ceiling |

`project` refuses nothing, but it reports an off-track trajectory with the same
code, because a scheduler reacts to both the same way: stop queueing work.

So this is a working budget gate in a build script:

```bash
spend-guard simulate video.render --units 600 --category video || exit 1
```

Every write is opt-in. `sweep` and `repair` report by default and need
`--apply`; `resume` needs `--yes`. `--now 2026-03-14T12:00:00Z` makes any
command's output reproducible.

A command that writes (`reserve`, `commit`, `release`, `sweep --apply`, `halt`,
`resume`) also requires a real ledger file and fails without one. An in-memory
ledger would accept the write, report success and then discard it when the
process exits — which for `halt` means telling an operator that spending has
stopped while it has not.

---

## Design

Decision logic is separated from I/O, so what matters is testable with no
network, no real clock, no browser and no filesystem:

| module | responsibility |
|---|---|
| `money` | exact integer micro-unit arithmetic; floats rejected |
| `periods` | period rules and the windows that tile time |
| `budget` | declarations, validation, rollover arithmetic |
| `pricing` | work → cost, and the explicit *unknown* outcome |
| `ledger` | append-only NDJSON, verification and repair |
| `state` | pure replay of entries into reservations and totals |
| `guard` | the reserve/commit/release enforcement point |
| `projection`, `simulate` | read-only analysis |
| `cli` | argparse front end |

Money is held as signed integer **micro-units** (six decimal places). Floats
are rejected at every entry point: `0.1 + 0.2` is not `0.3`, and a ledger that
inherits that error is not a ledger. Rounding is never "nearest" — costs round
**up** and ceilings round **down**, so both directions protect the budget.

Time is injected. Nothing calls `datetime.now()` except `SystemClock`, which
means lease expiry, period boundaries and burn projection are all pinned by
tests that never sleep.

The suite is 365 tests, each pinning one behaviour with a docstring saying why
it matters. The two headline properties have files of their own:
`tests/test_unknown_price_blocks.py` and `tests/test_reservation_leak.py`.

---

## Honest limitations

* **Concurrency is bounded, not eliminated.** The read-then-append in `reserve`
  is not a transaction. Two processes reserving at the same instant can jointly
  overshoot by up to one call's estimate each. Reserving before the call keeps
  the overshoot bounded by the number of racing workers times one in-flight
  call, instead of unbounded — but if you need a strict global cap across many
  machines, put the ledger behind something with real transactions.
  What is *not* a risk is corrupting the file: a sequence number is allocated
  from the file, never from memory, and allocation plus append happen under an
  advisory lock, so interleaved writers produce a history that still replays.
  (On a filesystem that refuses the lock — some network mounts — the lock is
  skipped and a true simultaneous write could duplicate a number. `verify`
  reports it and `repair --apply` renumbers, without discarding any entry.)
* **Replay is O(entries).** State is recomputed from the whole ledger on every
  decision, deliberately, so a second process's writes are visible immediately.
  That is fine for thousands of entries per period and wrong for a metering
  system handling millions of calls per second. Rotate the ledger per period if
  it grows.
* **Prices are what you declare.** The package never contacts a vendor and has
  no built-in price data, which is why the reconciliation step exists. If you
  never pass a real cost to `commit`, your ledger is a record of your estimates.
* **One currency per budget.** The `currency` field is a label. There is no
  conversion and no multi-currency arithmetic.
* **UTC only.** Period boundaries are computed in UTC. There is no local
  timezone or DST handling.
* **`fsync` gives durability, not atomicity.** A hard power loss can still tear
  the final line. That failure mode is detected by `verify` and fixed by
  `repair --apply`, which drops the torn tail and keeps everything before it.
  `repair` never invents history: a line that is not readable JSON is left
  alone and reported, and the command says so instead of claiming success.
* **Rollover looks back a bounded number of periods** (`max_lookback`, default
  3). Unused budget stops at any period with no recorded activity; a deficit
  carries through it.
* **This caps money, not concurrency.** It is not a rate limiter and not a
  quota manager for non-monetary units like requests per second.

---

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Eduardo de Souza Marques.
See [PROVENANCE.md](PROVENANCE.md) for authorship and origin.
