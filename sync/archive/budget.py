"""Publication size accounting for archive snapshots and site artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import MAX_GENERATED_FILE_BYTES

WARNING_BYTES = 850_000_000
REJECT_BYTES = 900_000_000
_LARGEST_COUNT = 10


@dataclass(frozen=True, slots=True)
class PathSize:
    """A snapshot-relative file path and its byte size."""

    path: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """The publication decision and largest contributing files."""

    level: Literal["ok", "warning", "reject"]
    total_bytes: int
    largest: tuple[PathSize, ...]


SizeWalker = Callable[[Path], Iterable[PathSize]]


def check_budget(root: Path, *, size_walker: SizeWalker | None = None) -> BudgetStatus:
    """Apply aggregate and per-file publication limits to a filesystem tree."""
    sizes = tuple((size_walker or _walk_sizes)(root))
    _validate_sizes(sizes)
    total_bytes = sum(item.size_bytes for item in sizes)
    largest = tuple(
        sorted(sizes, key=lambda item: (-item.size_bytes, item.path.as_posix()))[:_LARGEST_COUNT]
    )
    if (
        any(item.size_bytes > MAX_GENERATED_FILE_BYTES for item in sizes)
        or total_bytes >= REJECT_BYTES
    ):
        level: Literal["ok", "warning", "reject"] = "reject"
    elif total_bytes >= WARNING_BYTES:
        level = "warning"
    else:
        level = "ok"
    return BudgetStatus(level=level, total_bytes=total_bytes, largest=largest)


def _walk_sizes(root: Path) -> Iterator[PathSize]:
    if root.is_symlink():
        raise ValueError("budget root must not be a symlink")
    if not root.exists():
        return
    resolved_root = root.resolve(strict=True)
    for path in sorted(resolved_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(
                f"symlink is not allowed: {path.relative_to(resolved_root).as_posix()}"
            )
        if path.is_file():
            yield PathSize(path.relative_to(resolved_root), path.stat().st_size)


def _validate_sizes(sizes: tuple[PathSize, ...]) -> None:
    for item in sizes:
        if not isinstance(item, PathSize):
            raise ValueError("size walker must yield PathSize records")
        if not isinstance(item.path, Path):
            raise ValueError("size record path must be a Path")
        if (
            not isinstance(item.size_bytes, int)
            or isinstance(item.size_bytes, bool)
            or item.size_bytes < 0
        ):
            raise ValueError("size record byte size must be a non-negative integer")
