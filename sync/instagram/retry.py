"""Serialized retry policy for transient Instagram transport failures."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from .errors import TransientExhaustionError, TransientTransportError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("retry policy requires at least one attempt")
        if self.base_delay_seconds < 0:
            raise ValueError("base retry delay cannot be negative")
        if self.max_delay_seconds < 0:
            raise ValueError("maximum retry delay cannot be negative")
        if self.jitter_seconds < 0:
            raise ValueError("retry jitter cannot be negative")


def retry_transport[ResultT](
    operation: Callable[[], ResultT],
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
) -> ResultT:
    """Run ``operation``, retrying only sanitized transient transport failures."""
    for attempt in range(policy.max_attempts):
        try:
            return operation()
        except TransientTransportError:
            if attempt + 1 == policy.max_attempts:
                raise TransientExhaustionError("Instagram transport retries exhausted") from None
            exponential_delay = policy.base_delay_seconds * (2**attempt)
            delay = min(
                policy.max_delay_seconds,
                exponential_delay + policy.jitter_seconds * random_value(),
            )
            sleep(delay)
    raise AssertionError("retry loop did not return or raise")
