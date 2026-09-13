"""The documentation is part of the package, and it is checkable.

A README that describes a different package than the one shipped is not a
cosmetic problem: the install line, the exit-code table and the worked examples
are the interface most readers use first, and every one of them was wrong in a
way a test can pin. These tests read the real files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from spend_guard import Money
from spend_guard.config import build_guard, load_budget

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def fenced_blocks(language: str):
    return re.findall(rf"```{language}\n(.*?)```", README, re.DOTALL)


def command_lines():
    """Every line a reader could copy out of a shell block and run."""
    for block in fenced_blocks("bash"):
        for line in block.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                yield stripped


# -- claims about where the package comes from ------------------------------


def test_the_install_instructions_do_not_point_at_an_unpublished_name():
    """`pip install spend-guard` is the first command anybody runs.

    The name is not registered, so the instruction failed for every reader on
    day one — the worst possible first impression for a package submitted as
    evidence of shipped work.
    """
    for line in command_lines():
        assert not re.match(r"^pip install ['\"]?spend-guard", line), (
            f"README tells the reader to run {line!r}, but the name is not on PyPI"
        )
    assert "Not on PyPI yet" in README


def test_the_ci_badge_points_at_a_workflow_file_that_exists():
    """A badge is only evidence if it reports on a workflow that really runs.

    The badge URL and the workflow path are written in two different files, so
    renaming the workflow is exactly the kind of change that leaves a green
    badge reporting on nothing. Derive the path from the badge and look for it.
    """
    badge = re.search(
        r"actions/workflows/([\w.-]+)/badge\.svg",
        README,
    )
    assert badge is not None, "README has no CI badge"
    assert (ROOT / ".github" / "workflows" / badge.group(1)).is_file()


def test_every_github_url_points_at_one_repository_named_after_the_distribution():
    """Badge, metadata and package name are three places one slug is written.

    A typo in any of them 404s, and the mismatch that matters - a badge
    reporting on a different repository than the one the metadata links to - is
    invisible on the page because a broken badge and a missing run look alike.
    """
    distribution = re.search(r'^name = "([^"]+)"', PYPROJECT, re.M).group(1)
    owner_repo = {
        match.group(1)
        for text in (README, PYPROJECT)
        for match in re.finditer(r"github\.com/([\w.-]+/[\w.-]+)", text)
    }

    assert owner_repo == {f"edusouzaxGV/{distribution}"}, (
        f"expected every github.com URL to name edusouzaxGV/{distribution}, "
        f"found {sorted(owner_repo)}"
    )


def test_the_keywords_do_not_claim_a_scope_the_readme_disclaims():
    """PyPI keywords said "rate-limiting" and "quota"; the README denies both."""
    keywords = re.search(r"keywords = \[(.*?)\]", PYPROJECT, re.DOTALL)
    assert keywords is not None
    listed = set(re.findall(r'"([^"]+)"', keywords.group(1)))
    assert "rate-limiting" not in listed
    assert "quota" not in listed
    assert "not a rate limiter" in README


# -- claims about how the package behaves -----------------------------------


def test_the_exit_code_table_covers_the_only_other_source_of_code_2():
    """`project` exits 2 on an off-track forecast without refusing anything.

    A build script written against a table that said exit 2 means "the spend
    was refused" would mis-read it.
    """
    row = next(line for line in README.splitlines() if line.startswith("| `2`"))
    assert "project" in row
    from spend_guard import cli

    assert "project" in cli.__doc__


def test_the_readme_budget_is_the_shipped_example_budget():
    """A reader copies one and runs the other; they must not drift apart."""
    declared = json.loads(fenced_blocks("json")[0])
    shipped = json.loads((ROOT / "examples" / "budget.json").read_text(encoding="utf-8"))
    assert declared == shipped


def test_the_crash_case_example_runs_against_the_shipped_declarations():
    """The demonstration of the second headline property has to work as written.

    It omitted the category while the budget it is shown with refuses
    uncategorised spend, so copying both out of the README raised DeniedError
    instead of holding 40.00.
    """
    snippet = 'guard.reserve("image.render", 1000, category="images")'
    assert snippet in README
    guard = build_guard(
        ROOT / "examples" / "budget.json",
        ROOT / "examples" / "prices.json",
    )
    held = guard.reserve("image.render", 1000, category="images")
    assert held.estimate == Money.parse("40.00")


def test_the_readme_budget_declares_a_category_for_every_example_price():
    """Both files are loaded together by the first command a reader tries."""
    budget = load_budget(ROOT / "examples" / "budget.json")
    assert not budget.allow_uncategorised
    assert budget.knows_category("storage")


@pytest.mark.parametrize("path", ["LICENSE", "PROVENANCE.md"])
def test_the_files_the_badges_and_text_link_to_exist(path):
    """Relative links are only better than absolute ones if the target is there."""
    assert (ROOT / path).exists()


def test_the_two_documents_agree_on_the_size_of_the_suite():
    """Both files quote a test count; the pair drifting is how one goes stale."""
    provenance = (ROOT / "PROVENANCE.md").read_text(encoding="utf-8")
    in_readme = re.search(r"suite is (\d+) tests", README)
    in_provenance = re.search(r"test suite \((\d+) tests\)", provenance)
    assert in_readme and in_provenance
    assert in_readme.group(1) == in_provenance.group(1)
