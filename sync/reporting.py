"""Aggregate-only reporting and literal secret redaction."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sync.instagram.errors import (
    AuthenticationError,
    PublicationError,
    SizeError,
    ThrottleError,
    TransientExhaustionError,
    ValidationError,
)


@dataclass(frozen=True, slots=True)
class SyncReport:
    new_count: int = 0
    known_count: int = 0
    private_skip_count: int = 0
    unavailable_skip_count: int = 0
    explicit_removal_count: int = 0
    automatic_removal_count: int = 0
    media_failure_shortcodes: tuple[str, ...] = ()
    backfill_complete: bool = False
    snapshot_bytes: int = 0


class SecretRedactor:
    """Replace registered nonempty literals, matching overlapping secrets longest first."""

    def __init__(self, values: Iterable[str]) -> None:
        literals = sorted(
            {value for value in values if value}, key=lambda value: (-len(value), value)
        )
        self._pattern = (
            re.compile("|".join(re.escape(value) for value in literals)) if literals else None
        )

    def redact(self, text: str) -> str:
        return self._pattern.sub("[REDACTED]", text) if self._pattern is not None else text


def render_job_summary(report: SyncReport | None, failure: BaseException | None = None) -> str:
    """Render private job diagnostics without reading exception messages or private post data."""
    lines = ["Instagram saved archive", ""]
    if report is not None:
        lines.extend(
            (
                f"New posts: {report.new_count}",
                f"Known posts: {report.known_count}",
                f"Private posts skipped: {report.private_skip_count}",
                f"Unavailable posts skipped: {report.unavailable_skip_count}",
                f"Explicit removals: {report.explicit_removal_count}",
                f"Automatic removals: {report.automatic_removal_count}",
                f"Backfill complete: {'yes' if report.backfill_complete else 'no'}",
                f"Snapshot bytes: {report.snapshot_bytes}",
            )
        )
        if report.media_failure_shortcodes:
            safe_codes = [
                code
                for code in report.media_failure_shortcodes
                if re.fullmatch(r"[A-Za-z0-9_-]+", code)
            ]
            lines.append("Public posts with media failures: " + ", ".join(safe_codes))
    if failure is not None:
        lines.extend(("", _failure_message(failure)))
    return "\n".join(lines) + "\n"


def _failure_message(failure: BaseException) -> str:
    if isinstance(failure, AuthenticationError):
        return "Authentication failed. Rerun the local session bootstrap and replace INSTAGRAM_SESSION_B64."
    if isinstance(failure, ThrottleError):
        return "Instagram throttled this run. Wait before rerunning."
    if isinstance(failure, TransientExhaustionError):
        return "Instagram transport retries were exhausted. Rerun later."
    if isinstance(failure, SizeError):
        return (
            "Publication failed because the snapshot or a generated file exceeds its size budget."
        )
    if isinstance(failure, ValidationError):
        return "Archive validation failed. The snapshot was not published."
    if isinstance(failure, PublicationError):
        return "Publication failed. Rerun after checking the publication configuration."
    return "Synchronization failed. The run did not complete."
