"""Retry behavior and stable Instagram failure-category tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

import pytest

from sync.instagram.errors import (
    AuthenticationError,
    ChallengeError,
    CheckpointError,
    LoginError,
    PublicationError,
    SizeError,
    ThrottleError,
    TransientExhaustionError,
    TransientTransportError,
    UnavailablePostError,
    ValidationError,
)
from sync.instagram.retry import RetryPolicy, retry_transport


def test_retry_uses_exponential_delays_capped_by_the_policy() -> None:
    """Break caught: transport retries ignore exponential growth or exceed the delay cap."""
    delays: list[float] = []

    def operation() -> NoReturn:
        raise TransientTransportError("temporary failure")

    policy = RetryPolicy(
        max_attempts=5,
        base_delay_seconds=2.0,
        max_delay_seconds=5.0,
        jitter_seconds=0.0,
    )
    with pytest.raises(TransientExhaustionError):
        retry_transport(operation, policy, sleep=delays.append, random_value=lambda: 0.0)

    assert delays == [2.0, 4.0, 5.0, 5.0]


def test_retry_jitter_never_pushes_a_delay_past_the_cap() -> None:
    """Break caught: positive jitter can make a retry sleep longer than the configured cap."""
    delays: list[float] = []

    def operation() -> NoReturn:
        raise TransientTransportError("temporary failure")

    policy = RetryPolicy(
        max_attempts=3,
        base_delay_seconds=4.0,
        max_delay_seconds=5.0,
        jitter_seconds=3.0,
    )
    with pytest.raises(TransientExhaustionError):
        retry_transport(operation, policy, sleep=delays.append, random_value=lambda: 1.0)

    assert delays == [5.0, 5.0]


def test_retry_returns_after_two_transient_failures() -> None:
    """Break caught: a later successful transport result is discarded or retried again."""
    attempts = 0
    delays: list[float] = []

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientTransportError("temporary failure")
        return "complete"

    result = retry_transport(
        operation,
        RetryPolicy(jitter_seconds=0.0),
        sleep=delays.append,
        random_value=lambda: 0.0,
    )

    assert result == "complete"
    assert attempts == 3
    assert delays == [1.0, 2.0]


def test_retry_exhaustion_is_sanitized_and_has_no_sensitive_cause() -> None:
    """Break caught: exhaustion leaks the upstream response through text or exception chaining."""

    def operation() -> NoReturn:
        raise TransientTransportError("cookie=secret-session")

    with pytest.raises(TransientExhaustionError) as captured:
        retry_transport(
            operation,
            RetryPolicy(max_attempts=1),
            sleep=lambda _delay: None,
            random_value=lambda: 0.0,
        )

    assert str(captured.value) == "Instagram transport retries exhausted"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: LoginError("login required"), id="login"),
        pytest.param(lambda: CheckpointError("session refresh required"), id="checkpoint"),
        pytest.param(lambda: ChallengeError("challenge required"), id="challenge"),
        pytest.param(lambda: ThrottleError("throttled"), id="throttle"),
        pytest.param(lambda: UnavailablePostError("unavailable"), id="unavailable-post"),
    ],
)
def test_terminal_and_candidate_failures_are_never_retried(
    error_factory: Callable[[], Exception],
) -> None:
    """Break caught: a non-transport failure causes another operation attempt or sleep."""
    attempts = 0
    delays: list[float] = []

    def operation() -> NoReturn:
        nonlocal attempts
        attempts += 1
        raise error_factory()

    with pytest.raises(type(error_factory())):
        retry_transport(
            operation,
            RetryPolicy(),
            sleep=delays.append,
            random_value=lambda: 0.0,
        )

    assert attempts == 1
    assert delays == []


def test_archive_errors_expose_stable_exit_codes() -> None:
    """Break caught: CLI-visible failure categories drift from their specified process codes."""
    assert AuthenticationError.exit_code == 20
    assert LoginError.exit_code == 20
    assert CheckpointError.exit_code == 20
    assert ChallengeError.exit_code == 20
    assert ThrottleError.exit_code == 21
    assert TransientExhaustionError.exit_code == 22
    assert ValidationError.exit_code == 30
    assert SizeError.exit_code == 31
    assert PublicationError.exit_code == 40
