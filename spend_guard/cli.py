"""Command line interface.

Exit codes are part of the contract, because the most useful thing a shell can
do with this tool is stop:

* ``0`` - the operation succeeded, or the simulated spend is allowed
* ``1`` - the command failed (bad declaration, missing file, bad arguments)
* ``2`` - the spend was refused, or ``project`` forecasts the period ending
  over its ceiling

So ``spend-guard simulate video.render --units 600 || exit 1`` is a working
budget gate in a build script, and ``spend-guard project`` is the same gate for
a trajectory rather than a single call: both say "stop queueing work" with the
same code.

Anything that writes to the ledger is opt-in. ``sweep`` and ``repair`` report
by default and need ``--apply``; ``resume`` needs ``--yes``. A command that
writes also requires a real ledger file: without one the write would go to an
in-memory ledger and vanish when the process exits, which for ``halt`` would
mean reporting that spending had stopped when nothing had.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Sequence

from . import __version__
from .budget import UNCATEGORISED
from .clock import FixedClock, SystemClock, ensure_utc
from .config import (
    ENV_BUDGET,
    ENV_LEDGER,
    ENV_PRICES,
    build_guard,
    parse_price_override,
)
from .errors import SpendGuardError
from .guard import SpendGuard, SpendRequest
from .ledger import FileLedger
from .money import Money
from .projection import project
from .simulate import simulate

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DENIED = 2


# --------------------------------------------------------------------------
# Argument plumbing
# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--budget",
        metavar="PATH",
        help=f"budget declaration file (default: ${ENV_BUDGET})",
    )
    parser.add_argument(
        "--prices",
        metavar="PATH",
        help=f"price table file (default: ${ENV_PRICES})",
    )
    parser.add_argument(
        "--ledger",
        metavar="PATH",
        help=f"ledger file (default: ${ENV_LEDGER}; in memory if unset)",
    )
    parser.add_argument(
        "--price",
        dest="price_overrides",
        metavar="KEY=AMOUNT",
        action="append",
        default=[],
        help="override one price; repeatable",
    )
    parser.add_argument(
        "--now",
        metavar="ISO8601",
        help="evaluate as if it were this instant (UTC), for reproducible output",
    )
    parser.add_argument(
        "--tolerate-corrupt",
        action="store_true",
        help="skip unreadable ledger lines instead of refusing to run",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def _build(args: argparse.Namespace) -> SpendGuard:
    import os

    budget = args.budget or os.environ.get(ENV_BUDGET)
    if not budget:
        raise SpendGuardError(
            f"no budget declaration: pass --budget PATH or set ${ENV_BUDGET}"
        )
    prices = args.prices or os.environ.get(ENV_PRICES)
    ledger = args.ledger or os.environ.get(ENV_LEDGER)
    overrides = dict(
        parse_price_override(item) for item in getattr(args, "price_overrides", [])
    )
    clock = SystemClock()
    if getattr(args, "now", None):
        clock = FixedClock(_parse_instant(args.now))
    return build_guard(
        budget,
        prices,
        ledger,
        overrides=overrides,
        clock=clock,
        tolerate_corrupt=bool(getattr(args, "tolerate_corrupt", False)),
    )


def _writable(guard: SpendGuard, action: str) -> SpendGuard:
    """Refuse to "write" to a ledger that disappears when the process exits.

    Without a ledger file every write command is a no-op that reports success.
    For ``halt`` that is the worst possible outcome: the operator is told every
    spend is now refused while the runaway loop keeps spending.
    """
    if not isinstance(guard.ledger, FileLedger):
        raise SpendGuardError(
            f"{action} writes to the ledger, but no ledger file is configured, "
            "so the write would be discarded when this command exits. Pass "
            f"--ledger PATH or set ${ENV_LEDGER}."
        )
    return guard


def _parse_instant(text: str) -> datetime:
    try:
        return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        raise SpendGuardError(
            f"--now {text!r} is not an ISO-8601 instant, e.g. 2026-03-14T12:00:00Z"
        ) from None


def _emit(payload, args: argparse.Namespace, lines: Sequence[str]) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)


def _bar(fraction: Decimal, width: int = 24) -> str:
    filled = int(max(Decimal(0), min(Decimal(1), fraction)) * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    guard = _build(args)
    snap = guard.snapshot()
    lines = [
        f"period    {snap.window}",
        f"ceiling   {snap.ceiling.format()} {snap.currency}"
        + (
            f"  (base {snap.base_ceiling.format()} carry {snap.carry_in.format()})"
            if not snap.carry_in.is_zero
            else ""
        ),
        f"committed {snap.committed.format()}",
        f"reserved  {snap.reserved.format()}  ({snap.open_reservations} open)",
        f"available {snap.available.format()}",
        f"usage     {_bar(snap.utilisation)} {snap.utilisation:.1%}",
    ]
    if snap.halted:
        lines.append(
            "HALTED    " + (snap.halt_reason or "no reason recorded")
        )
    elif snap.exhausted:
        lines.append("EXHAUSTED the ceiling is spent; further spend is refused")
    elif snap.warn_tripped:
        lines.append(
            f"WARN      past the soft threshold of {snap.warn_threshold:.0%}"
        )
    if snap.categories:
        lines.append("")
        lines.append(
            f"{'category':<18}{'ceiling':>12}{'committed':>12}"
            f"{'reserved':>12}{'available':>12}"
        )
        for name, entry in sorted(snap.categories.items()):
            label = name or "(uncategorised)"
            lines.append(
                f"{label:<18}{entry.ceiling.format():>12}"
                f"{entry.committed.format():>12}{entry.reserved.format():>12}"
                f"{entry.available.format():>12}"
            )
        # A category can be the thing that is about to refuse work while the
        # total still looks healthy, and it may carry its own threshold.
        for name, entry in sorted(snap.categories.items()):
            if not name or not entry.warn_tripped:
                continue
            if guard.budget.category(name) is None:
                continue
            lines.append(
                f"WARN      category {name!r} is past its soft threshold of "
                f"{entry.warn_threshold:.0%} ({entry.available.format()} left)"
            )
    _emit(snap.to_mapping(), args, lines)
    return EXIT_OK


def cmd_project(args: argparse.Namespace) -> int:
    guard = _build(args)
    snap = guard.snapshot()
    forecast = project(snap, include_reserved=args.include_reserved)
    lines = [
        f"period      {forecast.window}",
        f"elapsed     {forecast.elapsed_fraction:.1%}",
        f"committed   {forecast.committed.format()} of {forecast.ceiling.format()}",
    ]
    if forecast.burn_per_day is None:
        lines.append(
            "projection  unavailable: no spend recorded yet in this period, so "
            "there is no rate to extend"
        )
    else:
        lines.append(f"burn/day    {forecast.burn_per_day.format()}")
        lines.append(f"projected   {forecast.projected_total.format()} by period end")
        if forecast.projected_overage.is_positive:
            lines.append(f"OVER BY     {forecast.projected_overage.format()}")
        if forecast.exhausts_before_period_end:
            lines.append(
                f"exhausted   {forecast.exhaustion_at.isoformat()} "
                "(before the period ends)"
            )
        lines.append(f"on track    {'yes' if forecast.on_track else 'NO'}")
    _emit(forecast.to_mapping(), args, lines)
    return EXIT_OK if forecast.on_track in (True, None) else EXIT_DENIED


def cmd_price(args: argparse.Namespace) -> int:
    guard = _build(args)
    quote = guard.quote(args.key, args.units)
    payload = {
        "key": quote.key,
        "units": str(quote.units),
        "unit": quote.unit,
        "rule": quote.rule_key,
        "known": quote.known,
        "estimated": quote.estimated,
        "amount": None if quote.amount is None else quote.amount.format(),
    }
    if not quote.known:
        _emit(
            payload,
            args,
            [
                f"{quote.key}: NO PRICE DECLARED",
                "an unpriced unit of work is refused, not charged as zero",
            ],
        )
        return EXIT_DENIED
    tag = " (estimated from fallback_price)" if quote.estimated else ""
    _emit(
        payload,
        args,
        [
            f"{quote.key} x {quote.units} {quote.unit} = "
            f"{quote.amount.format()}{tag}",
            f"matched rule: {quote.rule_key or '(fallback)'}",
        ],
    )
    return EXIT_OK


def cmd_simulate(args: argparse.Namespace) -> int:
    guard = _build(args)
    requests: List[SpendRequest] = []
    repeat = max(1, args.repeat)
    for _ in range(repeat):
        requests.append(
            SpendRequest(
                key=args.key,
                units=args.units,
                category=args.category,
                amount=None if args.amount is None else Money.parse(args.amount),
            )
        )
    result = simulate(guard, requests, stop_on_denial=args.stop_on_denial)
    first = result.first_denied
    lines = [
        f"{repeat} x {args.key} (units {args.units}"
        + (f", category {args.category}" if args.category else "")
        + ")",
        f"allowed  {result.allowed_count}/{len(result.steps)}",
        f"total    {result.total.format()} if the allowed calls ran",
    ]
    if first is not None:
        lines.append(f"DENIED   at step {first.index + 1}: {first.decision.message}")
    _emit(result.to_mapping(), args, lines)
    return EXIT_OK if result.all_allowed else EXIT_DENIED


def cmd_reserve(args: argparse.Namespace) -> int:
    guard = _writable(_build(args), "reserve")
    lease = timedelta(seconds=args.lease) if args.lease else None
    decision, reservation = guard.try_reserve(
        args.key,
        args.units,
        category=args.category,
        amount=args.amount,
        lease=lease,
        note=args.note,
    )
    if reservation is None:
        _emit(decision.to_mapping(), args, [f"DENIED {decision.message}"])
        return EXIT_DENIED
    payload = dict(decision.to_mapping())
    payload["reservation_id"] = reservation.id
    payload["lease_until"] = reservation.lease_until.isoformat()
    _emit(
        payload,
        args,
        [
            f"reserved {reservation.estimate.format()} for {reservation.work_key}",
            f"id       {reservation.id}",
            f"lease    until {reservation.lease_until.isoformat()}",
        ],
    )
    return EXIT_OK


def cmd_commit(args: argparse.Namespace) -> int:
    guard = _writable(_build(args), "commit")
    reservation = guard.commit(args.id, args.actual)
    payload = {
        "reservation_id": reservation.id,
        "status": reservation.status.value,
        "charged": reservation.charged.format(),
        "estimate": reservation.estimate.format(),
    }
    _emit(
        payload,
        args,
        [
            f"committed {reservation.charged.format()} "
            f"(estimate was {reservation.estimate.format()})"
        ],
    )
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    guard = _writable(_build(args), "release")
    reservation = guard.release(args.id, reason=args.reason)
    payload = {
        "reservation_id": reservation.id,
        "status": reservation.status.value,
        "released": reservation.estimate.format(),
    }
    _emit(payload, args, [f"released {reservation.estimate.format()}"])
    return EXIT_OK


def cmd_sweep(args: argparse.Namespace) -> int:
    guard = _build(args)
    if args.apply:
        _writable(guard, "sweep --apply")
    stale = guard.sweep(apply=args.apply)
    payload = {
        "applied": bool(args.apply),
        "count": len(stale),
        "reservations": [
            {
                "id": item.id,
                "work_key": item.work_key,
                "category": item.category,
                "estimate": item.estimate.format(),
                "lease_until": item.lease_until.isoformat(),
            }
            for item in stale
        ],
    }
    lines = [
        ("swept" if args.apply else "would sweep")
        + f" {len(stale)} expired reservation(s)"
    ]
    for item in stale:
        lines.append(
            f"  {item.id} {item.work_key} {item.estimate.format()} "
            f"(lease ended {item.lease_until.isoformat()})"
        )
    if stale and not args.apply:
        lines.append("re-run with --apply to record the expiry in the ledger")
    lines.append(
        "note: expired holds already stopped counting against the budget; "
        "sweeping only writes the fact down"
    )
    _emit(payload, args, lines)
    return EXIT_OK


def cmd_ledger(args: argparse.Namespace) -> int:
    guard = _build(args)
    entries = list(guard.ledger)
    if args.limit:
        entries = entries[-args.limit :]
    payload = {"entries": [json.loads(entry.to_json()) for entry in entries]}
    lines = [
        f"{entry.seq:>5} {entry.ts.isoformat()} {entry.type.value:<8}"
        f"{entry.amount.format():>12} {entry.work_key or entry.reason}"
        for entry in entries
    ]
    _emit(payload, args, lines or ["(empty ledger)"])
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    guard = _build(args)
    store = guard.ledger
    if not isinstance(store, FileLedger):
        _emit(
            {"ok": True, "entries": len(list(store)), "note": "in-memory ledger"},
            args,
            ["in-memory ledger: nothing on disk to verify"],
        )
        return EXIT_OK
    report = store.verify()
    payload = {
        "ok": report.ok,
        "entries": report.entries,
        "corrupt_lines": list(report.corrupt_lines),
        "partial_tail": report.partial_tail,
        "out_of_order": list(report.out_of_order),
    }
    _emit(payload, args, [report.describe()])
    return EXIT_OK if report.ok else EXIT_ERROR


def cmd_repair(args: argparse.Namespace) -> int:
    guard = _build(args)
    store = guard.ledger
    if not isinstance(store, FileLedger):
        raise SpendGuardError("there is no ledger file to repair")
    report = store.repair(apply=args.apply)
    payload = {
        "applied": bool(args.apply),
        "repaired": report.repaired,
        "ok": report.ok,
        "entries": report.entries,
        "partial_tail": report.partial_tail,
        "corrupt_lines": list(report.corrupt_lines),
        "out_of_order": list(report.out_of_order),
    }
    lines = [report.describe()]
    if not args.apply:
        if report.partial_tail or report.out_of_order:
            lines.append("re-run with --apply to rewrite the file")
    elif report.repaired:
        lines.append("repaired")
    elif report.ok:
        lines.append("nothing to repair")
    else:
        # Saying "repaired" here was the old behaviour and it was a lie: the
        # exit code and the word both told an operator the ledger was healthy
        # while every read of it still failed.
        lines.append(
            "NOT repaired: unreadable lines cannot be rewritten without "
            "inventing history; inspect lines "
            + ", ".join(str(n) for n in report.corrupt_lines)
        )
    _emit(payload, args, lines)
    return EXIT_OK if (report.ok or not args.apply) else EXIT_ERROR


def cmd_halt(args: argparse.Namespace) -> int:
    guard = _writable(_build(args), "halt")
    guard.halt(args.reason)
    _emit(
        {"halted": True, "reason": args.reason},
        args,
        ["halted: every spend is now refused until `spend-guard resume --yes`"],
    )
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    guard = _writable(_build(args), "resume")
    if not args.yes:
        raise SpendGuardError(
            "resuming lifts a deliberate stop on spending; pass --yes to confirm"
        )
    guard.resume(args.reason)
    _emit({"halted": False, "reason": args.reason}, args, ["resumed"])
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spend-guard",
        description=(
            "Inspect and enforce hard budget ceilings. Exit code 2 means the "
            "spend was refused."
        ),
    )
    parser.add_argument("--version", action="version", version=f"spend-guard {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", help="show the current period")
    _add_common(status)
    status.set_defaults(func=cmd_status)

    projection = subparsers.add_parser(
        "project",
        help=(
            "extrapolate spend to the end of the period (exit 2 when the "
            "trajectory lands over the ceiling)"
        ),
    )
    _add_common(projection)
    projection.add_argument(
        "--include-reserved",
        action="store_true",
        help="count in-flight reservations as spend (the pessimistic reading)",
    )
    projection.set_defaults(func=cmd_project)

    price = subparsers.add_parser("price", help="quote a unit of work")
    _add_common(price)
    price.add_argument("key")
    price.add_argument("--units", default="1")
    price.set_defaults(func=cmd_price)

    sim = subparsers.add_parser(
        "simulate", help="ask whether a proposed spend would be allowed"
    )
    _add_common(sim)
    sim.add_argument("key")
    sim.add_argument("--units", default="1")
    sim.add_argument("--category", default=UNCATEGORISED)
    sim.add_argument("--amount", help="bypass the price table with an explicit cost")
    sim.add_argument("--repeat", type=int, default=1, help="simulate N identical calls")
    sim.add_argument(
        "--stop-on-denial",
        action="store_true",
        help="stop at the first refusal instead of evaluating the rest",
    )
    sim.set_defaults(func=cmd_simulate)

    reserve = subparsers.add_parser("reserve", help="hold budget for a call")
    _add_common(reserve)
    reserve.add_argument("key")
    reserve.add_argument("--units", default="1")
    reserve.add_argument("--category", default=UNCATEGORISED)
    reserve.add_argument("--amount", help="explicit cost instead of the price table")
    reserve.add_argument("--lease", type=float, help="lease in seconds")
    reserve.add_argument("--note", default="")
    reserve.set_defaults(func=cmd_reserve)

    commit = subparsers.add_parser("commit", help="reconcile a reservation")
    _add_common(commit)
    commit.add_argument("id")
    commit.add_argument("--actual", help="the real cost; defaults to the estimate")
    commit.set_defaults(func=cmd_commit)

    release = subparsers.add_parser("release", help="give back an unused reservation")
    _add_common(release)
    release.add_argument("id")
    release.add_argument("--reason", default="")
    release.set_defaults(func=cmd_release)

    sweep = subparsers.add_parser(
        "sweep", help="report expired reservations (use --apply to record them)"
    )
    _add_common(sweep)
    sweep.add_argument(
        "--apply", action="store_true", help="write expiry entries to the ledger"
    )
    sweep.set_defaults(func=cmd_sweep)

    ledger = subparsers.add_parser("ledger", help="show ledger entries")
    _add_common(ledger)
    ledger.add_argument("--limit", type=int, default=20, help="show the last N entries")
    ledger.set_defaults(func=cmd_ledger)

    verify = subparsers.add_parser("verify", help="check the ledger file for damage")
    _add_common(verify)
    verify.set_defaults(func=cmd_verify)

    repair = subparsers.add_parser(
        "repair",
        help=(
            "make a damaged ledger readable again without discarding spend "
            "(use --apply to write)"
        ),
    )
    _add_common(repair)
    repair.add_argument("--apply", action="store_true", help="rewrite the file")
    repair.set_defaults(func=cmd_repair)

    halt = subparsers.add_parser("halt", help="refuse all spend until resumed")
    _add_common(halt)
    halt.add_argument("--reason", default="")
    halt.set_defaults(func=cmd_halt)

    resume = subparsers.add_parser("resume", help="lift a halt (requires --yes)")
    _add_common(resume)
    resume.add_argument("--reason", default="")
    resume.add_argument("--yes", action="store_true", help="confirm")
    resume.set_defaults(func=cmd_resume)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except SpendGuardError as exc:
        print(f"spend-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover - shell piping
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
