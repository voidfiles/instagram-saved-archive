"""Archive validation boundary tests."""

import importlib
from datetime import UTC, datetime

import pytest


def test_parse_utc_accepts_zulu_and_normalizes_to_utc() -> None:
    """Break caught: UTC parser rejects valid Zulu timestamps or returns a non-UTC timezone."""
    validation = importlib.import_module("sync.archive.validation")

    parsed = validation.parse_utc("2025-12-31T23:59:58Z")

    assert parsed == datetime(2025, 12, 31, 23, 59, 58, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    ["2025-12-31T23:59:58", "2025-12-31T23:59:58+00:00", "2025-12-31T23:59:58+01:00", "not-a-date"],
)
def test_parse_utc_rejects_non_utc_or_malformed_timestamps(value: str) -> None:
    """Break caught: UTC parser accepts naive, offset, or malformed timestamps."""
    validation = importlib.import_module("sync.archive.validation")

    with pytest.raises(ValueError):
        validation.parse_utc(value)
