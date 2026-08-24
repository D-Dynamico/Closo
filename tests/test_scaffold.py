"""Stage 0 scaffold checks.

Deliberately thin — these exist so `pytest` is green and wired from the
first commit, not to prove anything about reconciliation.
"""

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


def test_system_design_is_present() -> None:
    """Docstrings across closo/ cite its section numbers, so a missing or
    renumbered spec quietly turns every one of those references into a
    dangling pointer."""
    design = (REPO_ROOT / "docs" / "SYSTEM_DESIGN.md").read_text(encoding="utf-8")
    for section in ("## 6.", "## 7.", "## 8.", "## 11.", "## 12."):
        assert section in design, f"{section} missing from SYSTEM_DESIGN.md"
