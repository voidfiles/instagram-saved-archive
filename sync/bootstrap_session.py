"""Local-only interactive creation of an encoded Instagram session."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import Protocol, cast

from instaloader import Instaloader
from instaloader import exceptions as instaloader_errors

from .instagram.errors import LoginError, translate_instaloader_error


class _BootstrapLoader(Protocol):
    def interactive_login(self, username: str) -> None: ...

    def test_login(self) -> str | None: ...

    def save_session_to_file(self, filename: str) -> None: ...


def bootstrap_session(username: str, loader_factory: object = Instaloader) -> str:
    """Authenticate interactively and return the serialized session as base64 text."""
    factory = cast("type[_BootstrapLoader]", loader_factory)
    loader = factory()
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
