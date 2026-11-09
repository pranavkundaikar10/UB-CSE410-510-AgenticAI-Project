"""Safe-path helper that confines file access to fixtures/ and output/."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from triage.errors import PathViolation

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
FIXTURES_DIR: Path = REPO_ROOT / "fixtures"
RUNBOOKS_DIR: Path = REPO_ROOT / "runbooks"
OUTPUT_DIR: Path = REPO_ROOT / "output"
LOGS_DIR: Path = REPO_ROOT / "logs"
GOLD_DIR: Path = REPO_ROOT / "evaluation" / "gold"


def _is_within(path: Path, base: Path) -> bool:
    return path == base or path.is_relative_to(base)


def safe_join(base: Path, relative: str | Path) -> Path:
    """Join `relative` onto `base`, resolve it, and refuse anything that escapes `base`.

    Rejects empty paths, NUL bytes, absolute paths (POSIX, UNC or drive-letter),
    any `..` component (forward- or back-slash separated), and any result under GOLD_DIR.
    """
    raw = str(relative)
    if not raw or "\x00" in raw:
        raise PathViolation(f"invalid path: {raw!r}")

    win, posix = PureWindowsPath(raw), PurePosixPath(raw.replace("\\", "/"))
    if win.drive or win.root or win.is_absolute() or posix.is_absolute():
        raise PathViolation(f"absolute paths are not allowed: {raw!r}")
    if ".." in posix.parts:
        raise PathViolation(f"parent traversal is not allowed: {raw!r}")

    base_resolved = Path(base).resolve()
    candidate = (base_resolved / Path(*posix.parts)).resolve()
    if not _is_within(candidate, base_resolved):
        raise PathViolation(f"path escapes {base_resolved}: {raw!r}")
    if _is_within(candidate, GOLD_DIR.resolve()):
        raise PathViolation("access to evaluation/gold is forbidden")
    return candidate
