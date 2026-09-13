"""The append-only ledger.

Every number the guard reports is derived from this file. So the file has to
survive the things that happen to files: a process killed mid-append, a
well-meaning edit, two writers at once.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spend_guard import Entry, EntryType, FileLedger, MemoryLedger, Money
from spend_guard.errors import LedgerCorruptError

T = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)


def entry(seq=0, **kwargs):
    fields = dict(seq=seq, ts=T, type=EntryType.NOTE, reason="x")
    fields.update(kwargs)
    return Entry(**fields)


def test_memory_ledger_assigns_sequence_numbers():
    """Replay depends on order; entries cannot rely on the caller to number them."""
    store = MemoryLedger()
    first = store.append(entry())
    second = store.append(entry())
    assert (first.seq, second.seq) == (1, 2)


def test_file_ledger_round_trips_an_entry(tmp_path):
    """The format has to survive a write and a read, which is the whole job."""
    store = FileLedger(tmp_path / "spend.ndjson")
    store.append(
        entry(
            type=EntryType.COMMIT,
            reservation_id="r1",
            period_key="2026-03",
            category="text",
            work_key="text.generate",
            amount=Money.parse("1.25"),
        )
    )
    (loaded,) = list(FileLedger(tmp_path / "spend.ndjson"))
    assert loaded.amount == Money.parse("1.25")
    assert loaded.work_key == "text.generate"
    assert loaded.period_key == "2026-03"


def test_amounts_are_stored_as_integers_not_decimal_text(tmp_path):
    """Micro-units on disk means no parser anywhere can reintroduce a float."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry(amount=Money.parse("0.10")))
    assert '"amount_micros":100000' in path.read_text(encoding="utf-8")


def test_a_second_ledger_instance_continues_the_sequence(tmp_path):
    """A restarted process must not restart numbering and collide with history."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    FileLedger(path).append(entry())
    assert [item.seq for item in FileLedger(path)] == [1, 2]


def test_appends_from_two_handles_interleave_and_still_replay(tmp_path):
    """Two workers charging the same budget is the normal case, not the exotic one.

    One append per handle is the case that always worked. The case that did
    not is a handle appending *again* after the other one has written: a
    cached counter hands out a number that is already on disk, and from that
    line onward every read of the ledger raises.
    """
    path = tmp_path / "spend.ndjson"
    first = FileLedger(path)
    second = FileLedger(path)
    first.append(entry())
    second.append(entry())
    first.append(entry())
    second.append(entry())
    first.append(entry())
    assert [item.seq for item in FileLedger(path)] == [1, 2, 3, 4, 5]


def test_a_handle_sees_another_handles_writes_when_numbering(tmp_path):
    """The sequence number is allocated from the file, never from memory."""
    path = tmp_path / "spend.ndjson"
    first = FileLedger(path)
    second = FileLedger(path)
    first.append(entry())
    assert first.next_seq() == 2
    second.append(entry())
    second.append(entry())
    assert first.next_seq() == 4


def test_repair_renumbers_duplicate_sequences_without_dropping_an_entry(tmp_path):
    """The non-lossy fix for the damage a stale counter used to write.

    ``tolerate_corrupt`` is the other option and it fails open: it skips the
    duplicate line, so a commit that really happened silently leaves the
    budget. Renumbering keeps every recorded fact, in the order it was
    appended, and makes the file readable again.
    """
    path = tmp_path / "spend.ndjson"
    ledger = FileLedger(path)
    ledger.append(entry(seq=1, reason="a"))
    ledger.append(entry(seq=2, reason="b"))
    ledger.append(entry(seq=2, reason="c"))  # the collision

    with pytest.raises(LedgerCorruptError):
        list(FileLedger(path))
    assert FileLedger(path).verify().out_of_order == (3,)

    report = FileLedger(path).repair(apply=True)
    assert report.repaired and report.ok
    recovered = list(FileLedger(path))
    assert [item.seq for item in recovered] == [1, 2, 3]
    assert [item.reason for item in recovered] == ["a", "b", "c"]


def test_repair_reports_that_it_did_nothing_to_a_healthy_ledger(tmp_path):
    """"repaired" has to mean the file changed, or the word is worthless."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    report = FileLedger(path).repair(apply=True)
    assert report.ok
    assert not report.repaired


def test_repair_refuses_to_touch_a_line_it_cannot_read(tmp_path):
    """Renumbering around unreadable JSON would mean deciding it never existed."""
    path = tmp_path / "spend.ndjson"
    ledger = FileLedger(path)
    ledger.append(entry(seq=1))
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("{not json at all}\n")
    ledger.append(entry(seq=1))
    report = FileLedger(path).repair(apply=True)
    assert not report.repaired
    assert report.corrupt_lines == (2,)


def test_a_truncated_final_line_is_detected_not_skipped(tmp_path):
    """A kill during append leaves half a line; that is damage, and damage is loud.

    Skipping it quietly would silently discard the most recent spend, which is
    precisely the entry that matters after a crash.
    """
    path = tmp_path / "spend.ndjson"
    store = FileLedger(path)
    store.append(entry())
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"seq":2,"ts":"2026-03-10T12:00')
    with pytest.raises(LedgerCorruptError):
        list(FileLedger(path))


def test_verify_reports_a_truncated_tail_without_raising(tmp_path):
    """An operator needs to be able to look before deciding what to do."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("{partial")
    report = FileLedger(path).verify()
    assert report.partial_tail
    assert not report.ok
    assert report.entries == 1


def test_repair_only_writes_when_asked(tmp_path):
    """Repair rewrites the ledger, so it is opt-in, never a side effect of looking."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("{partial")
    before = path.read_text(encoding="utf-8")
    FileLedger(path).repair(apply=False)
    assert path.read_text(encoding="utf-8") == before
    FileLedger(path).repair(apply=True)
    assert path.read_text(encoding="utf-8") != before
    assert FileLedger(path).verify().ok


def test_repair_keeps_every_complete_entry(tmp_path):
    """Dropping the torn tail must not drop the history in front of it."""
    path = tmp_path / "spend.ndjson"
    store = FileLedger(path)
    store.append(entry(amount=Money.parse("1")))
    store.append(entry(amount=Money.parse("2")))
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"seq":3')
    FileLedger(path).repair(apply=True)
    assert [item.amount.format() for item in FileLedger(path)] == ["1.00", "2.00"]


def test_invalid_json_raises_with_the_line_number(tmp_path):
    """"Something is wrong with your ledger" is not actionable; a line number is."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("not json at all\n")
    with pytest.raises(LedgerCorruptError) as excinfo:
        list(FileLedger(path))
    assert excinfo.value.line_number == 2


def test_tolerate_corrupt_skips_damage_when_the_operator_opts_in(tmp_path):
    """Sometimes reading most of a damaged ledger beats reading none of it."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("not json at all\n")
    assert len(list(FileLedger(path, tolerate_corrupt=True))) == 1


def test_out_of_order_sequence_numbers_are_rejected(tmp_path):
    """A hand-edited or reordered ledger must not be replayed as if it were true."""
    path = tmp_path / "spend.ndjson"
    store = FileLedger(path)
    store.append(entry(seq=5))
    store.append(entry(seq=2))
    with pytest.raises(LedgerCorruptError) as excinfo:
        list(FileLedger(path))
    assert "reordered" in str(excinfo.value)


def test_blank_lines_are_ignored(tmp_path):
    """Editors add trailing newlines; that is not corruption."""
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("\n")
    assert len(list(FileLedger(path))) == 1


def test_a_missing_file_reads_as_an_empty_ledger(tmp_path):
    """First run must work without a setup step."""
    assert list(FileLedger(tmp_path / "nope.ndjson")) == []
    assert FileLedger(tmp_path / "nope.ndjson").next_seq() == 1


def test_entries_are_written_with_unix_newlines_on_every_platform(tmp_path):
    """A ledger written on Windows has to replay on Linux, byte for byte.

    Default text mode would translate "\\n" to "\\r\\n" on Windows, and the
    torn-tail detection reads the terminator directly.
    """
    path = tmp_path / "spend.ndjson"
    FileLedger(path).append(entry())
    assert b"\r\n" not in path.read_bytes()
    assert path.read_bytes().endswith(b"\n")


def test_entry_json_omits_empty_fields(tmp_path):
    """The ledger is read by people; an entry should not be mostly empty strings."""
    text = entry().to_json()
    assert "category" not in text
    assert "amount_micros" not in text


def test_entry_rejects_an_unknown_type(tmp_path):
    """A type this version does not understand must not be replayed as a note."""
    with pytest.raises(LedgerCorruptError):
        Entry.from_json('{"seq":1,"ts":"2026-03-10T12:00:00+00:00","type":"teleport"}')


def test_entry_requires_a_timestamp():
    """Every fact in the ledger is dated; an undated one cannot be placed in a period."""
    with pytest.raises(LedgerCorruptError):
        Entry.from_json('{"seq":1,"type":"note"}')


def test_naive_timestamps_are_normalised_to_utc():
    """Mixed awareness would make ordering comparisons raise at replay time."""
    loaded = Entry.from_json('{"seq":1,"ts":"2026-03-10T12:00:00","type":"note"}')
    assert loaded.ts.tzinfo is not None


# -- two guards on one file ------------------------------------------------


def test_two_guards_sharing_a_ledger_file_keep_it_replayable(tmp_path):
    """The documented multi-process case, driven through the guard itself.

    ``reserve`` from one process, ``reserve`` from another, then ``commit``
    from the first: three appends, and the third used to reuse the second's
    sequence number. The commit line landed on disk *and* raised, and from
    then on every read by either process failed — a bricked budget, not the
    bounded overshoot the README calls the worst case.
    """
    from spend_guard import build_guard

    budget = {"ceiling": "100.00"}
    prices = {"rules": {"image.render": {"unit": "image", "unit_price": "0.04"}}}
    path = tmp_path / "shared.ndjson"
    first = build_guard(budget, prices, path)
    second = build_guard(budget, prices, path)

    held = first.reserve("image.render", 100)
    second.reserve("image.render", 100)
    first.commit(held.id, Money.parse("4.00"))

    assert [item.seq for item in FileLedger(path)] == [1, 2, 3]
    assert first.snapshot().committed == Money.parse("4.00")
    assert second.snapshot().committed == Money.parse("4.00")
    assert FileLedger(path).verify().ok
