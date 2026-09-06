from __future__ import annotations

import logging

import pytest


def test_redactor_hides_registered_literals_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    from sync.reporting import SecretRedactor

    secrets = ("sessionid=cookie123", "Bearer auth123", "session-bytes", "ghp_token", "any-secret")
    redactor = SecretRedactor([*secrets, "", "session", "any-secret"])
    with caplog.at_level(logging.WARNING):
        logging.getLogger(__name__).warning(redactor.redact(" | ".join(secrets)))
    for secret in secrets:
        assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert redactor.redact("ordinary text") == "ordinary text"


@pytest.mark.parametrize("failure_kind", ["authentication", "throttle", "unknown"])
def test_summary_uses_aggregate_counts_and_never_formats_failure(failure_kind: str) -> None:
    from sync.instagram.errors import LoginError, ThrottleError
    from sync.reporting import SyncReport, render_job_summary

    class UnprintableError(Exception):
        def __str__(self) -> str:
            raise AssertionError("exception bodies must not be formatted")

        def __repr__(self) -> str:
            raise AssertionError("exceptions must not be repr'd")

    errors = {
        "authentication": LoginError("sessionid=cookie123 Bearer auth123"),
        "throttle": ThrottleError("ghp_token session-bytes any-secret"),
        "unknown": UnprintableError("private_owner SECRET1"),
    }
    report = SyncReport(2, 3, 4, 5, 6, 7, ("PUBLICFAIL",), True, 1234)
    summary = render_job_summary(report, errors[failure_kind])
    for secret in (
        "cookie123",
        "auth123",
        "ghp_token",
        "session-bytes",
        "any-secret",
        "private_owner",
        "SECRET1",
    ):
        assert secret not in summary
    assert "PUBLICFAIL" in summary
    for number in (2, 3, 4, 5, 6, 7, 1234):
        assert str(number) in summary
    if failure_kind == "authentication":
        assert "bootstrap" in summary.lower()


def test_summary_supports_failure_before_report_exists() -> None:
    from sync.reporting import render_job_summary

    summary = render_job_summary(None, RuntimeError("private token"))
    assert "private token" not in summary
    assert "fail" in summary.lower()


@pytest.mark.parametrize(
    ("snapshot_bytes", "warns"),
    [(849_999_999, False), (850_000_000, True), (899_999_999, True), (900_000_000, False)],
)
def test_summary_warns_at_snapshot_budget_boundary(snapshot_bytes: int, warns: bool) -> None:
    from sync.reporting import SyncReport, render_job_summary

    summary = render_job_summary(SyncReport(snapshot_bytes=snapshot_bytes))
    assert ("warning" in summary.lower()) is warns
    if warns:
        assert "size" in summary.lower()
        assert "budget" in summary.lower()
