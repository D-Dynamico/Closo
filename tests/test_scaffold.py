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


def test_session_log_is_gitignored() -> None:
    """SESSION.md is a local working log (§15.1), never committed."""
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "SESSION.md" in ignore
