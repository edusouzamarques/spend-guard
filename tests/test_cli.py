"""The command line interface.

The CLI is how a build script, a cron job and a human at 3am all talk to the
same budget. Its exit codes are load-bearing: ``|| exit 1`` in a shell script
is a real budget gate, and it only works if refusal is distinguishable from
failure.
"""

from __future__ import annotations

import json

import pytest

from spend_guard.cli import EXIT_DENIED, EXIT_ERROR, EXIT_OK, main

BUDGET = {
    "ceiling": "100.00",
    "warn_threshold": "0.80",
    "categories": {"images": {"ceiling": "30.00"}},
}
PRICES = {
    "rules": {
        "image.render": {"unit": "image", "unit_price": "0.04"},
        "text.generate": {"unit": "token", "per": 1000000, "unit_price": "3.00"},
    }
}


@pytest.fixture
def files(tmp_path):
    (tmp_path / "budget.json").write_text(json.dumps(BUDGET), encoding="utf-8")
    (tmp_path / "prices.json").write_text(json.dumps(PRICES), encoding="utf-8")
    return tmp_path


@pytest.fixture
def base(files):
    return [
        "--budget",
        str(files / "budget.json"),
        "--prices",
        str(files / "prices.json"),
        "--ledger",
        str(files / "spend.ndjson"),
    ]


def run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_no_arguments_prints_help_and_succeeds(capsys):
    """A bare invocation should teach, not fail."""
    code, out, _ = run([], capsys)
    assert code == EXIT_OK
    assert "spend-guard" in out


def test_version_is_reported():
    """Packaging smoke test: the entry point and the version must agree."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_status_shows_the_period_and_the_ceiling(base, capsys):
    """The first command anybody runs has to answer "where am I"."""
    code, out, _ = run(["status"] + base, capsys)
    assert code == EXIT_OK
    assert "ceiling   100.00 USD" in out
    assert "available 100.00" in out


def test_status_json_is_machine_readable(base, capsys):
    """Monitoring reads this; it must parse without scraping a table."""
    code, out, _ = run(["status", "--json"] + base, capsys)
    payload = json.loads(out)
    assert code == EXIT_OK
    assert payload["ceiling"] == "100.00"
    assert payload["categories"]["images"]["ceiling"] == "30.00"


def test_a_missing_budget_declaration_is_an_error_not_a_default(capsys):
    """Falling back to an unlimited budget would defeat the entire package."""
    code, _, err = run(["status"], capsys)
    assert code == EXIT_ERROR
    assert "budget" in err


def test_price_quotes_a_unit_of_work(base, capsys):
    """The quickest way to answer "what does this cost" before committing to it."""
    code, out, _ = run(["price", "text.generate", "--units", "1000000"] + base, capsys)
    assert code == EXIT_OK
    assert "3.00" in out


def test_price_of_an_unknown_key_exits_denied_not_zero(base, capsys):
    """Exit code 2 lets a script stop on an unpriced call instead of running it."""
    code, out, _ = run(["price", "video.render"] + base, capsys)
    assert code == EXIT_DENIED
    assert "NO PRICE DECLARED" in out


def test_simulate_allows_a_spend_that_fits(base, capsys):
    """Exit 0 is the signal a build script waits for."""
    code, out, _ = run(["simulate", "image.render", "--units", "10"] + base, capsys)
    assert code == EXIT_OK
    assert "allowed  1/1" in out


def test_simulate_denies_a_spend_that_does_not_fit(base, capsys):
    """The gate has to actually close, with a distinguishable exit code."""
    code, out, _ = run(["simulate", "image.render", "--units", "10000"] + base, capsys)
    assert code == EXIT_DENIED
    assert "DENIED" in out


def test_simulate_repeat_models_a_batch(base, capsys):
    """Planning is about the batch, not the call: three 40.00 calls do not fit."""
    code, out, _ = run(
        ["simulate", "image.render", "--units", "1000", "--repeat", "3"] + base, capsys
    )
    assert code == EXIT_DENIED
    assert "allowed  2/3" in out


def test_simulate_writes_nothing_to_the_ledger(base, files, capsys):
    """The dry-run guarantee has to hold through the CLI as well as the API."""
    run(["simulate", "image.render", "--units", "10"] + base, capsys)
    assert not (files / "spend.ndjson").exists()


def test_reserve_then_commit_moves_the_committed_total(base, capsys):
    """The manual path exists for shell pipelines that cannot hold a context manager."""
    code, out, _ = run(["reserve", "image.render", "--units", "10", "--json"] + base, capsys)
    assert code == EXIT_OK
    reservation_id = json.loads(out)["reservation_id"]

    code, out, _ = run(["commit", reservation_id, "--actual", "0.35"] + base, capsys)
    assert code == EXIT_OK

    code, out, _ = run(["status", "--json"] + base, capsys)
    assert json.loads(out)["committed"] == "0.35"


def test_release_returns_the_hold(base, capsys):
    """A shell script whose call failed needs a way to give the budget back."""
    _, out, _ = run(["reserve", "image.render", "--units", "10", "--json"] + base, capsys)
    reservation_id = json.loads(out)["reservation_id"]
    run(["release", reservation_id, "--reason", "call failed"] + base, capsys)
    _, out, _ = run(["status", "--json"] + base, capsys)
    assert json.loads(out)["available"] == "100.00"


def test_a_refused_reserve_exits_denied(base, capsys):
    """Same contract as simulate, on the path that would really spend."""
    code, _, _ = run(["reserve", "image.render", "--units", "10000"] + base, capsys)
    assert code == EXIT_DENIED


def test_sweep_reports_without_writing_by_default(base, files, capsys):
    """Looking at expired holds must be safe; writing is the opt-in."""
    run(
        ["reserve", "image.render", "--units", "10", "--lease", "1"] + base,
        capsys,
    )
    before = (files / "spend.ndjson").read_text(encoding="utf-8")
    code, out, _ = run(["sweep", "--now", "2099-01-01T00:00:00Z"] + base, capsys)
    assert code == EXIT_OK
    assert "would sweep 1" in out
    assert (files / "spend.ndjson").read_text(encoding="utf-8") == before


def test_sweep_apply_records_the_expiry(base, files, capsys):
    """With --apply the fact is written down, which is all sweeping ever does."""
    run(
        ["reserve", "image.render", "--units", "10", "--lease", "1"] + base,
        capsys,
    )
    code, out, _ = run(
        ["sweep", "--apply", "--now", "2099-01-01T00:00:00Z"] + base, capsys
    )
    assert code == EXIT_OK
    assert "swept 1" in out
    assert "expire" in (files / "spend.ndjson").read_text(encoding="utf-8")


def test_sweep_explains_that_the_budget_was_already_free(base, capsys):
    """The most misunderstood behaviour in the package deserves a line of output."""
    _, out, _ = run(["sweep"] + base, capsys)
    assert "already stopped counting" in out


def test_halt_blocks_a_subsequent_reserve(base, capsys):
    """The circuit breaker has to work from the command line, under pressure."""
    run(["halt", "--reason", "incident 12"] + base, capsys)
    code, _, _ = run(["reserve", "image.render", "--units", "1"] + base, capsys)
    assert code == EXIT_DENIED


def test_resume_requires_explicit_confirmation(base, capsys):
    """Lifting a deliberate stop is not something to do by autocomplete."""
    run(["halt"] + base, capsys)
    code, _, err = run(["resume"] + base, capsys)
    assert code == EXIT_ERROR
    assert "--yes" in err


def test_resume_with_yes_restores_service(base, capsys):
    """And with the confirmation it works, or the breaker could never be cleared."""
    run(["halt"] + base, capsys)
    assert run(["resume", "--yes"] + base, capsys)[0] == EXIT_OK
    assert run(["reserve", "image.render", "--units", "1"] + base, capsys)[0] == EXIT_OK


def test_status_shows_the_halt_reason(base, capsys):
    """Whoever finds the halted system did not necessarily set the halt."""
    run(["halt", "--reason", "runaway loop"] + base, capsys)
    _, out, _ = run(["status"] + base, capsys)
    assert "HALTED" in out and "runaway loop" in out


def test_status_warns_past_the_soft_threshold(base, capsys):
    """The soft warning is the only output between "fine" and "refused"."""
    _, out, _ = run(["reserve", "image.render", "--units", "2100", "--json"] + base, capsys)
    _, out, _ = run(["status"] + base, capsys)
    assert "WARN" in out


def test_status_warns_on_a_category_while_the_total_still_looks_healthy(base, capsys):
    """The category is what refuses the next call; the total says nothing yet.

    30.00 of a 100.00 budget is quiet, but it is 80% of the images sub-ceiling.
    The per-category threshold was parsed and then read by nobody, so this line
    did not exist at all.
    """
    run(["reserve", "image.render", "--units", "600"] + base + ["--category", "images"], capsys)
    _, out, _ = run(["status"] + base, capsys)
    assert "WARN      past the soft threshold" not in out  # the total is fine
    assert "WARN      category 'images'" in out


def test_project_reports_that_it_cannot_project_without_data(base, capsys):
    """Refusing to guess has to be visible, not an empty field."""
    code, out, _ = run(["project"] + base, capsys)
    assert code == EXIT_OK
    assert "unavailable" in out


def test_project_flags_an_overspending_trajectory(base, capsys):
    """The whole reason to project: find out before the ceiling is reached.

    80.00 committed on the second day of a 100.00 month projects far past the
    ceiling, and the non-zero exit is what lets a scheduler stop queueing work.
    """
    _, out, _ = run(
        [
            "reserve",
            "image.render",
            "--units",
            "2000",
            "--json",
            "--now",
            "2026-03-02T00:00:00Z",
        ]
        + base,
        capsys,
    )
    reservation_id = json.loads(out)["reservation_id"]
    run(["commit", reservation_id, "--now", "2026-03-02T00:10:00Z"] + base, capsys)
    code, out, _ = run(["project", "--now", "2026-03-03T00:00:00Z"] + base, capsys)
    assert code == EXIT_DENIED
    assert "OVER BY" in out


def test_ledger_shows_recent_entries(base, capsys):
    """An operator reconstructing an incident starts here."""
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    code, out, _ = run(["ledger"] + base, capsys)
    assert code == EXIT_OK
    assert "reserve" in out


def test_ledger_of_an_empty_file_says_so(base, capsys):
    """Blank output would look like a broken command."""
    _, out, _ = run(["ledger"] + base, capsys)
    assert "empty ledger" in out


def test_verify_passes_on_a_healthy_ledger(base, capsys):
    """A clean bill of health has to be reachable, or the check means nothing."""
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    code, out, _ = run(["verify"] + base, capsys)
    assert code == EXIT_OK
    assert "no problems" in out


def test_verify_fails_on_a_truncated_ledger(base, files, capsys):
    """The failure mode of an interrupted append, surfaced as a non-zero exit."""
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    with (files / "spend.ndjson").open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"seq":9,"ts"')
    code, out, _ = run(["verify"] + base, capsys)
    assert code == EXIT_ERROR
    assert "truncated" in out


def test_repair_needs_apply_to_change_the_file(base, files, capsys):
    """Rewriting the spend history is the most destructive thing here."""
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    with (files / "spend.ndjson").open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"seq":9,"ts"')
    before = (files / "spend.ndjson").read_text(encoding="utf-8")
    _, out, _ = run(["repair"] + base, capsys)
    assert "--apply" in out
    assert (files / "spend.ndjson").read_text(encoding="utf-8") == before
    run(["repair", "--apply"] + base, capsys)
    assert run(["verify"] + base, capsys)[0] == EXIT_OK


def test_a_price_override_unblocks_a_missing_key(base, capsys):
    """The documented emergency fix has to work from the command line."""
    code, out, _ = run(
        ["price", "video.render", "--units", "2", "--price", "video.render=0.50"] + base,
        capsys,
    )
    assert code == EXIT_OK
    assert "1.00" in out


def test_a_malformed_price_override_is_an_error(base, capsys):
    """Half-applying an override would price the wrong thing silently."""
    code, _, err = run(["price", "image.render", "--price", "nonsense"] + base, capsys)
    assert code == EXIT_ERROR
    assert "key=amount" in err


def test_a_malformed_now_is_an_error(base, capsys):
    """--now drives every period calculation; a bad value must not be ignored."""
    code, _, err = run(["status", "--now", "yesterday"] + base, capsys)
    assert code == EXIT_ERROR
    assert "ISO-8601" in err


def test_now_makes_output_reproducible(base, capsys):
    """Reporting across a boundary needs to name the period, not infer it."""
    _, out, _ = run(["status", "--json", "--now", "2026-07-04T12:00:00Z"] + base, capsys)
    assert json.loads(out)["period"] == "2026-07"


def test_committing_an_unknown_id_is_an_error_not_a_crash(base, capsys):
    """Operators mistype ids; the tool should say so rather than traceback."""
    code, _, err = run(["commit", "nope"] + base, capsys)
    assert code == EXIT_ERROR
    assert "nope" in err


def test_status_without_a_ledger_path_still_works(files, capsys):
    """Inspecting a declaration before wiring up storage is a normal first step."""
    code, out, _ = run(
        ["status", "--budget", str(files / "budget.json")], capsys
    )
    assert code == EXIT_OK
    assert "100.00" in out


# -- a write with nowhere to write -----------------------------------------


def test_halt_without_a_ledger_refuses_instead_of_claiming_success(files, capsys):
    """The kill switch must never report a stop that did not happen.

    With no ledger configured the halt went to an in-memory ledger that died
    with the process: the command printed "every spend is now refused" and
    exited 0 while the runaway loop kept spending, and the very next `status`
    showed no halt at all.
    """
    code, out, err = run(
        [
            "halt",
            "--reason",
            "runaway loop",
            "--budget",
            str(files / "budget.json"),
            "--prices",
            str(files / "prices.json"),
        ],
        capsys,
    )
    assert code == EXIT_ERROR
    assert "now refused" not in out
    assert "--ledger" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["reserve", "image.render", "--units", "1"],
        ["commit", "whatever"],
        ["release", "whatever"],
        ["sweep", "--apply"],
        ["resume", "--yes"],
    ],
)
def test_every_write_command_requires_a_real_ledger(argv, files, capsys):
    """Same trap on every command that appends; none of them may no-op quietly."""
    code, _, err = run(
        argv
        + [
            "--budget",
            str(files / "budget.json"),
            "--prices",
            str(files / "prices.json"),
        ],
        capsys,
    )
    assert code == EXIT_ERROR
    assert "ledger" in err


def test_sweep_without_apply_still_works_with_no_ledger(files, capsys):
    """Reporting is not writing, and the read-only path must stay usable."""
    code, _, _ = run(["sweep", "--budget", str(files / "budget.json")], capsys)
    assert code == EXIT_OK


# -- refusals that used to get through -------------------------------------


def test_a_negative_amount_is_refused_at_the_command_line(base, files, capsys):
    """`reserve --amount=-500` used to print a hold and *raise* the available budget."""
    code, out, _ = run(
        ["reserve", "hack", "--amount", "-500"] + base, capsys
    )
    assert code == EXIT_DENIED
    assert "negative" in out

    _, out, _ = run(["status", "--json"] + base, capsys)
    assert json.loads(out)["available"] == "100.00"


def test_a_bad_period_kind_is_an_error_message_not_a_traceback(tmp_path, capsys):
    """The bare-string form escaped as a ValueError and printed a stack trace."""
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps({"ceiling": "100.00", "period": "fortnightly"}), encoding="utf-8"
    )
    code, _, err = run(["status", "--budget", str(path)], capsys)
    assert code == EXIT_ERROR
    assert "fortnightly" in err and "monthly" in err


# -- repair tells the truth -------------------------------------------------


def test_repair_apply_does_not_claim_success_when_it_cannot_fix_the_file(
    base, files, capsys
):
    """"repaired" plus exit 0 on a file that still fails every read is a lie."""
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    with (files / "spend.ndjson").open("a", encoding="utf-8", newline="") as handle:
        handle.write("{not json at all}\n")
    code, out, _ = run(["repair", "--apply"] + base, capsys)
    assert "NOT repaired" in out
    assert code == EXIT_ERROR
    assert run(["verify"] + base, capsys)[0] == EXIT_ERROR


def test_repair_apply_on_a_healthy_ledger_says_there_was_nothing_to_do(base, capsys):
    """Running it defensively in a script must not imply damage was found."""
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    code, out, _ = run(["repair", "--apply"] + base, capsys)
    assert code == EXIT_OK
    assert "nothing to repair" in out


def test_repair_apply_recovers_a_ledger_with_a_duplicate_sequence(base, files, capsys):
    """The damage an older, cached-counter writer could leave behind.

    Renumbering keeps the entry, so the spend it recorded stays on the budget;
    `--tolerate-corrupt` would have skipped the line and forgotten the money.
    """
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    run(["reserve", "image.render", "--units", "10"] + base, capsys)
    path = files / "spend.ndjson"
    lines = path.read_text(encoding="utf-8").splitlines()
    # What a writer numbering from a stale count leaves behind.
    lines[1] = lines[1].replace('"seq":2', '"seq":1')
    # open(newline="") rather than write_text(newline=""): the keyword only
    # reached Path.write_text in 3.10, and this package supports 3.9. It is
    # needed either way - the default would translate these \n into \r\n on
    # Windows and the ledger would no longer be the byte-exact NDJSON the
    # reader parses.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")
    assert run(["status"] + base, capsys)[0] == EXIT_ERROR

    code, out, _ = run(["repair", "--apply"] + base, capsys)
    assert code == EXIT_OK
    assert "repaired" in out
    assert run(["status"] + base, capsys)[0] == EXIT_OK
    assert run(["verify"] + base, capsys)[0] == EXIT_OK


# -- the shipped examples ---------------------------------------------------


#: Every rule in examples/prices.json and the category in examples/budget.json
#: it is meant to be charged against. The example budget refuses uncategorised
#: spend, so a rule with no category here cannot be charged at all.
EXAMPLE_CHARGEABLE = {
    "text.generate": "text",
    "text.embed": "text",
    "speech.synthesise": "speech",
    "image.render": "images",
    "image.upscale": "images",
    "video.render": "video",
    "compute.gpu.*": "compute",
    "storage.egress": "storage",
}


def test_every_example_price_rule_can_actually_be_charged():
    """The two example files are run together first; no rule may be dead on arrival.

    `storage.egress` was priced with no matching category in a budget that
    refuses uncategorised spend, so the one command a reader is most likely to
    try with it exited 2 with "category 'storage' is not declared".
    """
    from pathlib import Path

    from spend_guard.config import load_budget, load_prices

    root = Path(__file__).resolve().parent.parent / "examples"
    budget = load_budget(root / "budget.json")
    prices = load_prices(root / "prices.json")
    assert set(prices.keys()) == set(EXAMPLE_CHARGEABLE)
    for key, category in EXAMPLE_CHARGEABLE.items():
        assert budget.knows_category(category), (
            f"{key} is priced but {category!r} is not a declared category"
        )


def test_the_shipped_examples_allow_the_egress_rule_end_to_end(capsys):
    """Driven the way a reader would drive it, through the command line."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "examples"
    code, out, _ = run(
        [
            "simulate",
            "storage.egress",
            "--units",
            "100",
            "--category",
            "storage",
            "--budget",
            str(root / "budget.json"),
            "--prices",
            str(root / "prices.json"),
        ],
        capsys,
    )
    assert code == EXIT_OK
    assert "allowed  1/1" in out
