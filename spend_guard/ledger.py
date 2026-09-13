"""The append-only ledger.

Every decision the guard makes is derived by replaying this file. Nothing is
ever rewritten in place, so "why was this call allowed" always has an answer,
and two processes appending to the same file produce an interleaved but still
replayable history.

Sequence numbers are what make that last claim true, and they are allocated
from the file rather than from memory. An in-memory counter is stale the moment
another handle appends, and two lines carrying the same sequence number make
the whole ledger unreadable — a far worse failure than the bounded overshoot
concurrency is otherwise allowed to produce. Allocation and append happen under
an advisory lock on the file, so a genuine race between two processes still
yields distinct numbers.

The file format is NDJSON: one self-describing JSON object per line, no header,
no trailer. A crash mid-append can only damage the last line, and that single
failure mode is detected by :meth:`FileLedger.verify` and fixed by
:meth:`FileLedger.repair` rather than silently skipped.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from .clock import ensure_utc
from .errors import LedgerCorruptError
from .money import Money

try:  # pragma: no cover - trivial import shim
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore[assignment]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls


__all__ = [
    "EntryType",
    "Entry",
    "Ledger",
    "MemoryLedger",
    "FileLedger",
    "LedgerReport",
]

try:  # pragma: no cover - one of the two branches is dead on any given platform
    import fcntl  # type: ignore[import-not-found]

    _msvcrt = None
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    try:
        import msvcrt as _msvcrt  # type: ignore[import-not-found]
    except ImportError:
        _msvcrt = None  # type: ignore[assignment]


@contextmanager
def _exclusive_lock(handle):
    """Hold an advisory exclusive lock on ``handle`` for the duration.

    This exists so that allocating a sequence number and writing the line that
    uses it are one step as far as other processes are concerned. Locking is
    advisory and best effort: on a filesystem that does not support it (some
    network mounts) the lock call fails and the append proceeds unlocked, which
    is no worse than not having tried. Correctness for the ordinary
    one-writer-at-a-time case does not depend on it.
    """
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        elif _msvcrt is not None:
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
            locked = True
    except OSError:  # pragma: no cover - platform/filesystem dependent
        locked = False
    try:
        yield
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif _msvcrt is not None:
                    handle.seek(0)
                    _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:  # pragma: no cover - platform/filesystem dependent
                pass


class EntryType(str, Enum):
    RESERVE = "reserve"
    COMMIT = "commit"
    RELEASE = "release"
    EXPIRE = "expire"
    HALT = "halt"
    RESUME = "resume"
    NOTE = "note"


@dataclass(frozen=True)
class Entry:
    """One immutable fact about the budget."""

    seq: int
    ts: datetime
    type: EntryType
    reservation_id: str = ""
    period_key: str = ""
    category: str = ""
    work_key: str = ""
    units: Decimal = Decimal(0)
    amount: Money = field(default_factory=Money.zero)
    estimated: bool = False
    reason: str = ""
    lease_until: Optional[datetime] = None
    meta: Dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts))
        if not isinstance(self.type, EntryType):
            object.__setattr__(self, "type", EntryType(str(self.type)))
        if self.lease_until is not None:
            object.__setattr__(self, "lease_until", ensure_utc(self.lease_until))
        if not isinstance(self.units, Decimal):
            object.__setattr__(self, "units", Decimal(str(self.units)))
        if not isinstance(self.amount, Money):
            object.__setattr__(self, "amount", Money.parse(self.amount))

    def to_json(self) -> str:
        payload: Dict[str, object] = {
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "type": self.type.value,
        }
        if self.reservation_id:
            payload["reservation_id"] = self.reservation_id
        if self.period_key:
            payload["period_key"] = self.period_key
        if self.category:
            payload["category"] = self.category
        if self.work_key:
            payload["work_key"] = self.work_key
        if self.units != Decimal(0):
            payload["units"] = str(self.units)
        if self.amount.micros:
            payload["amount_micros"] = self.amount.micros
        if self.estimated:
            payload["estimated"] = True
        if self.reason:
            payload["reason"] = self.reason
        if self.lease_until is not None:
            payload["lease_until"] = self.lease_until.isoformat()
        if self.meta:
            payload["meta"] = self.meta
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str, *, line_number: Optional[int] = None) -> "Entry":
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise LedgerCorruptError(
                f"line {line_number}: not valid JSON ({exc})", line_number
            ) from None
        if not isinstance(payload, dict):
            raise LedgerCorruptError(
                f"line {line_number}: expected a JSON object", line_number
            )
        try:
            entry_type = EntryType(payload["type"])
            seq = int(payload["seq"])
            ts = datetime.fromisoformat(payload["ts"])
        except (KeyError, ValueError) as exc:
            raise LedgerCorruptError(
                f"line {line_number}: malformed entry ({exc})", line_number
            ) from None
        lease_raw = payload.get("lease_until")
        return cls(
            seq=seq,
            ts=ts,
            type=entry_type,
            reservation_id=payload.get("reservation_id", ""),
            period_key=payload.get("period_key", ""),
            category=payload.get("category", ""),
            work_key=payload.get("work_key", ""),
            units=Decimal(str(payload.get("units", "0"))),
            amount=Money.from_micros(int(payload.get("amount_micros", 0))),
            estimated=bool(payload.get("estimated", False)),
            reason=payload.get("reason", ""),
            lease_until=datetime.fromisoformat(lease_raw) if lease_raw else None,
            meta=payload.get("meta", {}) or {},
        )


@runtime_checkable
class Ledger(Protocol):
    """Append and read. Deliberately the whole interface."""

    def append(self, entry: Entry) -> Entry:  # pragma: no cover - protocol
        ...

    def __iter__(self) -> Iterator[Entry]:  # pragma: no cover - protocol
        ...

    def next_seq(self) -> int:  # pragma: no cover - protocol
        ...


class MemoryLedger:
    """An in-process ledger. Used by tests and by dry-run simulation."""

    def __init__(self, entries: Iterable[Entry] = ()) -> None:
        self._entries: List[Entry] = list(entries)

    def append(self, entry: Entry) -> Entry:
        if entry.seq <= 0:
            entry = replace(entry, seq=self.next_seq())
        self._entries.append(entry)
        return entry

    def __iter__(self) -> Iterator[Entry]:
        return iter(tuple(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def next_seq(self) -> int:
        return self._entries[-1].seq + 1 if self._entries else 1

    def snapshot(self) -> List[Entry]:
        return list(self._entries)

    def __repr__(self) -> str:
        return f"MemoryLedger({len(self._entries)} entries)"


@dataclass(frozen=True)
class LedgerReport:
    """The outcome of checking a ledger file."""

    path: Optional[Path]
    entries: int
    corrupt_lines: tuple = ()
    partial_tail: bool = False
    out_of_order: tuple = ()
    repaired: bool = False
    """True only when :meth:`FileLedger.repair` actually rewrote the file."""

    @property
    def ok(self) -> bool:
        return not self.corrupt_lines and not self.partial_tail and not self.out_of_order

    def describe(self) -> str:
        if self.ok:
            return f"{self.entries} entries, no problems found"
        parts = [f"{self.entries} readable entries"]
        if self.partial_tail:
            parts.append("last line is truncated (interrupted append)")
        if self.corrupt_lines:
            parts.append(
                "unreadable lines: " + ", ".join(str(n) for n in self.corrupt_lines)
            )
        if self.out_of_order:
            parts.append(
                "out-of-order sequence at lines: "
                + ", ".join(str(n) for n in self.out_of_order)
            )
        return "; ".join(parts)


class FileLedger:
    """An NDJSON file on disk.

    ``fsync`` defaults to true. A budget ledger whose last few entries live only
    in the page cache will, after a hard reset, authorise spend that already
    happened. The write cost is paid once per charged call, which is negligible
    next to the call being charged for.
    """

    def __init__(
        self,
        path,
        *,
        fsync: bool = True,
        tolerate_corrupt: bool = False,
        create: bool = True,
    ) -> None:
        self.path = Path(path)
        self.fsync = bool(fsync)
        self.tolerate_corrupt = bool(tolerate_corrupt)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- reading -----------------------------------------------------------

    def _iter_lines(self) -> Iterator[tuple]:
        if not self.path.exists():
            return
        # newline="" keeps the file byte-identical across platforms; the writer
        # always emits "\n" so a file written on Windows replays on Linux.
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            for number, raw in enumerate(handle, start=1):
                yield number, raw

    def _parse(self, lines: Iterable[tuple]) -> Iterator[Entry]:
        """Turn ``(line_number, raw)`` pairs into entries, refusing damage."""
        last_seq = 0
        for number, raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            if not raw.endswith("\n"):
                # A line without its terminator can only be the last one: an
                # append that did not finish. Treat it as damage, not data.
                if self.tolerate_corrupt:
                    continue
                raise LedgerCorruptError(
                    f"line {number}: the final line has no newline terminator, "
                    "which means an append was interrupted; run `spend-guard "
                    "repair --apply` to drop it",
                    number,
                )
            try:
                entry = Entry.from_json(stripped, line_number=number)
            except LedgerCorruptError:
                if self.tolerate_corrupt:
                    continue
                raise
            if entry.seq <= last_seq:
                if self.tolerate_corrupt:
                    continue
                raise LedgerCorruptError(
                    f"line {number}: sequence {entry.seq} does not advance past "
                    f"{last_seq}; the ledger has been reordered or edited",
                    number,
                )
            last_seq = entry.seq
            yield entry

    def __iter__(self) -> Iterator[Entry]:
        return self._parse(self._iter_lines())

    def verify(self) -> LedgerReport:
        """Read the whole file and report damage without raising."""
        corrupt: List[int] = []
        out_of_order: List[int] = []
        partial = False
        count = 0
        last_seq = 0
        for number, raw in self._iter_lines():
            stripped = raw.strip()
            if not stripped:
                if not raw.endswith("\n"):
                    partial = True
                continue
            if not raw.endswith("\n"):
                partial = True
                continue
            try:
                entry = Entry.from_json(stripped, line_number=number)
            except LedgerCorruptError:
                corrupt.append(number)
                continue
            if entry.seq <= last_seq:
                out_of_order.append(number)
                continue
            last_seq = entry.seq
            count += 1
        return LedgerReport(
            path=self.path,
            entries=count,
            corrupt_lines=tuple(corrupt),
            partial_tail=partial,
            out_of_order=tuple(out_of_order),
        )

    def repair(self, *, apply: bool = False) -> LedgerReport:
        """Make a damaged file readable again without discarding any spend.

        Two kinds of damage are repairable, and neither repair drops a recorded
        fact:

        * a **truncated final line** — an append that did not finish. It never
          became an entry, so dropping it loses nothing.
        * **sequence numbers that do not advance** — the signature of two
          writers that each numbered a line from a stale count. The entries
          themselves are intact and their order in the file is the order they
          were appended, so renumbering them ``1..N`` in place restores a
          replayable history. This is the non-lossy alternative to reading the
          file with ``tolerate_corrupt``, which would skip the duplicate line
          and quietly forget the money it recorded.

        Lines that are not readable JSON at all are never touched: a repair
        that guessed at them would be inventing history. Only writes when
        ``apply`` is true.
        """
        report = self.verify()
        if not apply or not self.path.exists():
            return report
        if report.corrupt_lines:
            # Renumbering would have to drop the unreadable lines to produce a
            # consistent file. Refuse; an operator must look at them.
            return report
        if not report.partial_tail and not report.out_of_order:
            return report
        kept: List[Entry] = []
        for number, raw in self._iter_lines():
            stripped = raw.strip()
            if not stripped or not raw.endswith("\n"):
                continue
            kept.append(Entry.from_json(stripped, line_number=number))
        renumbered = [
            replace(entry, seq=index) for index, entry in enumerate(kept, start=1)
        ]
        temporary = self.path.with_name(self.path.name + ".repair")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            for item in renumbered:
                handle.write(item.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(self.path))
        return replace(self.verify(), repaired=True)

    # -- writing -----------------------------------------------------------

    def next_seq(self) -> int:
        """The next free sequence number, read from the file every time.

        Never cached. A cached counter is stale the instant another handle or
        another process appends, and reusing a number writes a line that makes
        the whole ledger unreadable on the next replay.
        """
        highest = 0
        for entry in self:
            highest = max(highest, entry.seq)
        return highest + 1

    @contextmanager
    def _open_for_append(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" so the same handle can read the existing lines and append: the
        # sequence number and the line that carries it come from one lock.
        with self.path.open("a+", encoding="utf-8", newline="") as handle:
            yield handle

    def _next_seq_from(self, handle) -> int:
        handle.seek(0)
        highest = 0
        for entry in self._parse(enumerate(handle, start=1)):
            highest = max(highest, entry.seq)
        return highest + 1

    def append(self, entry: Entry) -> Entry:
        with self._open_for_append() as handle:
            with _exclusive_lock(handle):
                if entry.seq <= 0:
                    entry = replace(entry, seq=self._next_seq_from(handle))
                handle.seek(0, os.SEEK_END)
                handle.write(entry.to_json() + "\n")
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
        return entry

    def __repr__(self) -> str:
        return f"FileLedger({str(self.path)!r})"
