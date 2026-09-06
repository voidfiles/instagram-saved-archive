"""Offline tests for Chrome-backed Instagram session bootstrapping."""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sync.bootstrap_session import bootstrap_session_from_chrome
from sync.instagram.errors import LoginError


class FakeBootstrapLoader:
    def __init__(self, login_name: str | None = "archive_owner") -> None:
        self.login_name = login_name
        self.loaded_path: Path | None = None
        self.context = SimpleNamespace(error_log=[])

    def load_session_from_file(self, username: str, filename: str) -> None:
        assert username == "archive_owner"
        self.loaded_path = Path(filename)

    def test_login(self) -> str | None:
        return self.login_name


def test_bootstrap_uses_instaloaders_chrome_cli_and_returns_its_session() -> None:
    """Break caught: bootstrap bypasses the working Instaloader Chrome-import command."""
    loader = FakeBootstrapLoader()
    commands: list[list[str]] = []
    modes: list[int] = []

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        session_path = Path(command[-1])
        modes.append(stat.S_IMODE(session_path.stat().st_mode))
        session_path.write_bytes(b"serialized\x00session")
        return subprocess.CompletedProcess(command, 0)

    encoded = bootstrap_session_from_chrome(
        "archive_owner", loader_factory=lambda **_options: loader, command_runner=run
    )

    assert commands == [
        [
            "uv",
            "run",
            "instaloader",
            "--load-cookies",
            "Chrome",
            "--sessionfile",
            str(loader.loaded_path),
        ]
    ]
    assert modes == [0o600]
    assert base64.b64decode(encoded, validate=True) == b"serialized\x00session"
    assert loader.loaded_path is not None
    assert not loader.loaded_path.exists()


def test_bootstrap_rejects_a_failed_instaloader_import_without_loading_a_session() -> None:
    """Break caught: a failed Chrome import emits or uploads an invalid session."""
    loader = FakeBootstrapLoader()

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1)

    with pytest.raises(LoginError, match="Could not import Instagram cookies from Chrome"):
        bootstrap_session_from_chrome(
            "archive_owner", loader_factory=lambda **_options: loader, command_runner=run
        )

    assert loader.loaded_path is None


def test_bootstrap_rejects_an_identity_mismatch_and_deletes_the_session() -> None:
    """Break caught: Chrome imports a session for an account other than the requested account."""
    loader = FakeBootstrapLoader(login_name="different_owner")

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        Path(command[-1]).write_bytes(b"serialized-session")
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(LoginError, match="authenticated identity does not match"):
        bootstrap_session_from_chrome(
            "archive_owner", loader_factory=lambda **_options: loader, command_runner=run
        )

    assert loader.loaded_path is not None
    assert not loader.loaded_path.exists()


def test_bootstrap_does_not_change_the_process_umask() -> None:
    """Break caught: temporary-session hardening changes the caller's file creation policy."""
    loader = FakeBootstrapLoader()
    previous = os.umask(0)
    os.umask(previous)

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        Path(command[-1]).write_bytes(b"serialized-session")
        return subprocess.CompletedProcess(command, 0)

    bootstrap_session_from_chrome(
        "archive_owner", loader_factory=lambda **_options: loader, command_runner=run
    )

    observed = os.umask(0)
    os.umask(observed)
    assert observed == previous
