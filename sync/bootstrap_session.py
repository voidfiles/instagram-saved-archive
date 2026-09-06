"""Local-only creation of an encoded Instagram session through Instaloader's Chrome CLI flow."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol, TextIO, cast

from instaloader import Instaloader
from instaloader import exceptions as instaloader_errors

from .instagram.client import _suppress_context_logging
from .instagram.errors import LoginError, translate_instaloader_error


class _BootstrapLoader(Protocol):
    context: object

    def load_session_from_file(self, username: str, filename: str) -> None: ...

    def test_login(self) -> str | None: ...


class _BootstrapLoaderFactory(Protocol):
    def __call__(self, *, max_connection_attempts: int) -> _BootstrapLoader: ...


class _CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        check: bool,
        stdout: TextIO,
        stderr: TextIO,
    ) -> subprocess.CompletedProcess[bytes]: ...


def _run_command(
    command: list[str], *, check: bool, stdout: TextIO, stderr: TextIO
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, check=check, stdout=stdout, stderr=stderr)


def bootstrap_session_from_chrome(
    username: str,
    loader_factory: object = Instaloader,
    command_runner: _CommandRunner = _run_command,
) -> str:
    """Import Chrome cookies via Instaloader CLI and return the encoded generated session."""
    descriptor, temporary_name = tempfile.mkstemp(prefix="instagram-session-")
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        result = command_runner(
            [
                "uv",
                "run",
                "instaloader",
                "--load-cookies",
                "Chrome",
                "--sessionfile",
                str(temporary_path),
            ],
            check=False,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        if result.returncode != 0:
            raise LoginError("Could not import Instagram cookies from Chrome")

        factory = cast(_BootstrapLoaderFactory, loader_factory)
        loader = factory(max_connection_attempts=1)
        _suppress_context_logging(loader.context)
        try:
            loader.load_session_from_file(username, str(temporary_path))
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
        return base64.b64encode(temporary_path.read_bytes()).decode("ascii")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
