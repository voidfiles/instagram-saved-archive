"""Stable, sanitized failures emitted by the Instagram boundary."""

from __future__ import annotations

from typing import ClassVar

from instaloader import exceptions as instaloader_errors


class ArchiveError(Exception):
    """Base class for a terminal archive-process failure."""

    exit_code: ClassVar[int]


class AuthenticationError(ArchiveError):
    exit_code = 20


class LoginError(AuthenticationError):
    pass


class CheckpointError(AuthenticationError):
    pass


class ChallengeError(AuthenticationError):
    pass


class ThrottleError(ArchiveError):
    exit_code = 21


class TransientExhaustionError(ArchiveError):
    exit_code = 22


class ValidationError(ArchiveError):
    exit_code = 30


class SizeError(ArchiveError):
    exit_code = 31


class PublicationError(ArchiveError):
    exit_code = 40


class TransientTransportError(Exception):
    """A transport operation that may safely be attempted again."""


class UnavailablePostError(Exception):
    """A single discovery candidate that is definitively unavailable."""


def translate_instaloader_error(error: Exception) -> Exception | None:
    """Return a sanitized boundary failure for a known Instaloader exception."""
    if isinstance(
        error,
        (
            ArchiveError,
            TransientTransportError,
            UnavailablePostError,
        ),
    ):
        return error
    chain = _exception_chain(error)
    if any(isinstance(item, instaloader_errors.TooManyRequestsException) for item in chain):
        return ThrottleError("Instagram throttled")
    if any(
        isinstance(
            item,
            (
                instaloader_errors.QueryReturnedNotFoundException,
                instaloader_errors.ProfileNotExistsException,
            ),
        )
        for item in chain
    ):
        return UnavailablePostError("Instagram post is unavailable")
    for item in chain:
        if isinstance(item, instaloader_errors.AbortDownloadException):
            description = str(item).casefold()
            if "checkpoint" in description:
                return CheckpointError("Instagram checkpoint required")
            if "challenge" in description or "feedback_required" in description:
                return ChallengeError("Instagram challenge required")
            return AuthenticationError("Instagram authentication was interrupted")
    if any(
        isinstance(
            item,
            (
                instaloader_errors.LoginException,
                instaloader_errors.LoginRequiredException,
                instaloader_errors.PrivateProfileNotFollowedException,
            ),
        )
        for item in chain
    ):
        return LoginError("Instagram login failed")
    if isinstance(error, instaloader_errors.InvalidArgumentException):
        return ValidationError("Instagram operation is invalid")
    if isinstance(error, instaloader_errors.InstaloaderException):
        return TransientTransportError("Instagram transport failed")
    return None


def _exception_chain(error: Exception) -> tuple[Exception, ...]:
    chain: list[Exception] = []
    seen: set[int] = set()
    current: Exception | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, Exception) else None
    return tuple(chain)
