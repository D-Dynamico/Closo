"""Scaffold and documentation-integrity checks.

These prove nothing about reconciliation. What they protect is the
project's memory: that the session notes are committed rather than
ignored, and that the section numbers docstrings across ``closo/`` cite
still resolve to a real heading somewhere in ``docs/``.

That second one earns its place. The spec is split across three files,
and a rename or a renumber would turn dozens of docstring references
into dangling pointers without breaking a single line of code - the
comments would simply, silently, stop meaning anything.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_package_imports() -> None:
    """The closo package is importable."""
    import closo  # noqa: F401


def test_frozen_demo_dir_exists() -> None:
    """CLAUDE.md §11.10 requires the frozen demo set to ship in the repo."""
    assert (REPO_ROOT / "data" / "generated" / "demo").is_dir()


def test_session_notes_exist_and_are_committed() -> None:
    """The session protocol in CLAUDE.md requires a new session to open by
    reading the newest note in docs/sessions/. That only works if the notes
    are in the repo, so this guards against them being ignored again."""
    notes = sorted((REPO_ROOT / "docs" / "sessions").glob("*.md"))
    assert notes, "no session notes — the project has lost its memory"

    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    active = [
        line.strip()
        for line in ignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "docs/sessions/" not in active


SPEC_DOCS = ("ARCHITECTURE.md", "WORKFLOWS.md", "TEST_PLAN.md")


def _spec_text() -> str:
    return "\n".join(
        (REPO_ROOT / "docs" / name).read_text(encoding="utf-8") for name in SPEC_DOCS
    )


def test_spec_documents_are_present() -> None:
    for name in (*SPEC_DOCS, "SYSTEM_DESIGN.md"):
        assert (REPO_ROOT / "docs" / name).is_file(), f"docs/{name} is missing"


def _resolvable_sections() -> set[str]:
    """Every section number a docstring may legitimately cite.

    Two shapes count. Some subsections are real headings (``### 7.4``);
    others are numbered list items inside a section - §11's invariants and
    §8's verifier checks are written that way, so ``11.3`` means the third
    item under §11. Both are resolvable; anything else is not.
    """
    spec = _spec_text()
    valid = set(re.findall(r"(?m)^#{2,3} (\d+(?:\.\d+)?)", spec))

    # Numbered list items, attributed to whichever "## N." they sit under.
    current: str | None = None
    for line in spec.splitlines():
        heading = re.match(r"^## (\d+)\.", line)
        if heading:
            current = heading.group(1)
            continue
        item = re.match(r"^(\d+)\. ", line)
        if item and current:
            valid.add(f"{current}.{item.group(1)}")
    return valid


def test_every_section_cited_from_code_still_resolves() -> None:
    """The spec is split across three files and docstrings cite section
    numbers. A rename or a careless renumber turns every one of those into a
    dangling pointer *silently* - the code still runs and the comments just
    stop meaning anything. This is what makes the numbers safe to rely on.

    Deliberately exact: citing 7.4 must find 7.4, not merely a section 7.
    An earlier version of this test fell back to matching the parent, which
    let a renumbered subsection pass and made the whole check toothless.
    """
    valid = _resolvable_sections()

    cited: set[str] = set()
    for module in (REPO_ROOT / "closo").glob("*.py"):
        text = module.read_text(encoding="utf-8")
        cited |= set(re.findall(r"\((\d+\.\d+)\)", text))
        cited |= set(re.findall(r"section (\d+\.\d+)", text))

    assert cited, "no section references found - did the citation style change?"
    dangling = sorted(ref for ref in cited if ref not in valid)
    assert not dangling, f"cited from closo/ but absent from docs: {dangling}"


def test_section_index_covers_every_section() -> None:
    """SYSTEM_DESIGN.md is the map from section number to file. A section
    present in the spec but missing from the index cannot be found by
    someone following a docstring reference."""
    index = (REPO_ROOT / "docs" / "SYSTEM_DESIGN.md").read_text(encoding="utf-8")
    top_level = {
        n.split(".")[0]
        for n in re.findall(r"(?m)^## (\d+)\.", _spec_text())
    }
    for number in sorted(top_level, key=int):
        assert re.search(rf"\|\s*{number}\s*\|", index), (
            f"section {number} is not listed in the SYSTEM_DESIGN index"
        )


# --------------------------------------------------------------------------
# The section guard's blind spot
# --------------------------------------------------------------------------
#
# `_resolvable_sections` accepts a number that resolves *either* as a
# subsection heading or as a numbered list item. Two numbers currently
# resolve as both, and in both cases the code means the heading:
#
#   8.1  -> "### 8.1 The fee-schedule anomaly"   vs  §8's check 1, Existence
#   10.1 -> "### 10.1 Demo mode"                 vs  §10's screen 1, Ingest
#
# So renaming either heading leaves every citation still "resolving" - to
# something else entirely, silently, which is the exact failure the guard
# exists to prevent. Renumbering the docs to remove the overlap is not
# free: `8.2` and `8.3` are cited *as* list items, so §8's checks have to
# stay a numbered list. Pinning is the cheap half of the fix.

#: Cited from `closo/` and resolving to a real subsection heading today.
#: Pinned so a renamed heading fails here even when a same-numbered list
#: item would otherwise cover for it. Adding a legitimate new citation to a
#: heading means adding it to this set - that is the intended cost.
HEADING_CITATIONS = frozenset({
    "5.2", "7.2", "7.3", "7.4", "8.1", "9.1", "9.2", "10.1",
    "12.1", "12.2", "12.5",
})

#: Numbers that are a heading *and* a list item. Pinned so a third one
#: cannot appear unnoticed; both of these are read as the heading.
KNOWN_AMBIGUOUS = frozenset({"8.1", "10.1"})


def _heading_sections() -> set[str]:
    return set(re.findall(r"(?m)^#{2,3} (\d+(?:\.\d+)?)", _spec_text()))


def _list_item_sections() -> set[str]:
    """Numbered list items, attributed to the ``## N.`` they sit under."""
    current: str | None = None
    items: set[str] = set()
    for line in _spec_text().splitlines():
        heading = re.match(r"^## (\d+)\.", line)
        if heading:
            current = heading.group(1)
            continue
        item = re.match(r"^(\d+)\. ", line)
        if item and current:
            items.add(f"{current}.{item.group(1)}")
    return items


def _cited_from_code() -> set[str]:
    cited: set[str] = set()
    for module in (REPO_ROOT / "closo").glob("*.py"):
        text = module.read_text(encoding="utf-8")
        cited |= set(re.findall(r"\((\d+\.\d+)\)", text))
        cited |= set(re.findall(r"section (\d+\.\d+)", text))
    return cited


def test_a_cited_subsection_heading_cannot_be_renamed_away() -> None:
    """Renaming `### 8.1` passes the resolution test above, because §8's
    first check is also numbered 1. Every docstring citing 8.1 would then
    point at "Existence" instead of the fee-schedule anomaly - resolving
    perfectly and meaning something else. This is what catches that."""
    still_headings = _cited_from_code() & _heading_sections()
    assert still_headings == HEADING_CITATIONS, (
        "the set of cited subsection headings changed. If a heading was "
        "renumbered, put it back; if a new citation was added, add it here: "
        f"{sorted(still_headings ^ HEADING_CITATIONS)}"
    )


def test_no_third_section_number_means_two_things() -> None:
    """A number that is both a heading and a list item is a number a reader
    cannot follow. Two exist and are documented above; a third should be a
    deliberate decision rather than a surprise."""
    ambiguous = _heading_sections() & _list_item_sections()
    assert ambiguous == KNOWN_AMBIGUOUS, (
        f"section number(s) resolving two ways: {sorted(ambiguous)}"
    )
