"""Snapshot publication size-budget tests."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from sync.archive.budget import PathSize, check_budget


def _walker(*sizes: int) -> Iterable[PathSize]:
    return tuple(
        PathSize(Path(f"media/{index:02d}.webp"), size) for index, size in enumerate(sizes)
    )


def _files_with_total(total: int) -> tuple[int, ...]:
    """Keep aggregate threshold inputs below the separate per-file rejection limit."""
    max_file = 95 * 1024 * 1024
    full_files, remainder = divmod(total, max_file)
    return (max_file,) * full_files + ((remainder,) if remainder else ())


@pytest.mark.parametrize(
    ("total", "level"),
    [
        (849_999_999, "ok"),
        (850_000_000, "warning"),
        (899_999_999, "warning"),
        (900_000_000, "reject"),
    ],
)
def test_check_budget_applies_aggregate_boundaries(total: int, level: str, tmp_path: Path) -> None:
    """Break caught: a publication total uses the wrong inclusive warning or refusal boundary."""
    status = check_budget(tmp_path, size_walker=lambda _root: _walker(*_files_with_total(total)))

    assert status.level == level
    assert status.total_bytes == total


def test_check_budget_rejects_one_file_above_95_mib(tmp_path: Path) -> None:
    """Break caught: a single generated file can exceed the independent 95 MiB safety cap."""
    status = check_budget(tmp_path, size_walker=lambda _root: _walker(95 * 1024 * 1024 + 1))

    assert status.level == "reject"


def test_check_budget_walks_real_files_and_reports_largest_first(tmp_path: Path) -> None:
    """Break caught: filesystem totals omit files or private size reporting is not size ordered."""
    (tmp_path / "media").mkdir()
    (tmp_path / "manifest.json").write_bytes(b"123")
    (tmp_path / "media" / "small.webp").write_bytes(b"1")
    (tmp_path / "media" / "large.webp").write_bytes(b"12345")

    status = check_budget(tmp_path)

    assert status.total_bytes == 9
    assert status.largest[:2] == (
        PathSize(Path("media/large.webp"), 5),
        PathSize(Path("manifest.json"), 3),
    )


def test_check_budget_rejects_symlinked_files(tmp_path: Path) -> None:
    """Break caught: a symlink can evade the actual-size walk used before publication."""
    target = tmp_path / "real.bin"
    target.write_bytes(b"real")
    link = tmp_path / "linked.bin"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ValueError):
        check_budget(tmp_path)
