"""Local-only interactive creation of an encoded Instagram session."""

from __future__ import annotations

import base64
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from instaloader import Instaloader
from instaloader import exceptions as instaloader_errors

from .instagram.client import _suppress_context_logging
from .instagram.errors import LoginError, translate_instaloader_error


class _BootstrapLoader(Protocol):
    context: object
    login: Callable[[str, str], None]
    two_factor_login: Callable[[str], None]

    def interactive_login(self, username: str) -> None: ...

    def test_login(self) -> str | None: ...

    def save_session_to_file(self, filename: str) -> None: ...


class _BootstrapLoaderFactory(Protocol):
    def __call__(self, *, max_connection_attempts: int) -> _BootstrapLoader: ...


def bootstrap_session(username: str, loader_factory: object = Instaloader) -> str:
    """Authenticate interactively and return the serialized session as base64 text."""
    factory = cast(_BootstrapLoaderFactory, loader_factory)
    loader = factory(max_connection_attempts=1)
    # Context errors include upstream bodies/URLs and are replayed by close().
    # Interactive prompts use getpass/input directly and remain available.
    _suppress_context_logging(loader.context)
    loader.login = _sanitize_bad_credentials(loader.login)
    loader.two_factor_login = _sanitize_bad_credentials(loader.two_factor_login)
    try:
        loader.interactive_login(username)
        authenticated_username = loader.test_login()
    except (
        instaloader_errors.InstaloaderException,
        instaloader_errors.AbortDownloadException,
    ) as error:
        translated = translate_instaloader_error(error)
        if translated is None:
            raise
        raise translated from None
    if authenticated_username != username:
        raise LoginError("authenticated identity does not match requested username")

    descriptor, temporary_name = tempfile.mkstemp(prefix="instagram-session-")
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        loader.save_session_to_file(str(temporary_path))
        return base64.b64encode(temporary_path.read_bytes()).decode("ascii")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _sanitize_bad_credentials[**P](operation: Callable[P, None]) -> Callable[P, None]:
    # Instaloader's interactive retry loops print this exception directly to stderr.
    def call(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            operation(*args, **kwargs)
        except instaloader_errors.BadCredentialsException:
            raise instaloader_errors.BadCredentialsException(
                "Instagram credentials were rejected. Try again."
            ) from None

    return call
