"""Offline tests for local interactive Instagram session bootstrapping."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest
from instaloader.exceptions import BadCredentialsException

from sync.bootstrap_session import bootstrap_session
from sync.instagram.errors import LoginError


class FakeBootstrapLoader:
    def __init__(
        self,
        *,
        login_name: str | None = "archive_owner",
        session_bytes: bytes = b"serialized\x00session",
        login_error: Exception | None = None,
    ) -> None:
        self.login_name = login_name
        self.session_bytes = session_bytes
        self.login_error = login_error
        self.interactive_calls: list[str] = []
        self.two_factor_codes: list[str] = []
        self.saved_path: Path | None = None
        self.mode_during_save: int | None = None

    def interactive_login(self, username: str) -> None:
        self.interactive_calls.append(username)
        if self.login_error is not None:
            raise self.login_error
        self.two_factor_login("246810")

    def two_factor_login(self, two_factor_code: str) -> None:
        self.two_factor_codes.append(two_factor_code)

    def test_login(self) -> str | None:
        return self.login_name

    def save_session_to_file(self, filename: str) -> None:
        self.saved_path = Path(filename)
        self.mode_during_save = stat.S_IMODE(self.saved_path.stat().st_mode)
        self.saved_path.write_bytes(self.session_bytes)


def test_bootstrap_runs_interactive_and_two_factor_flow_then_returns_base64() -> None:
    """Break caught: bootstrap bypasses Instaloader's interactive/2FA flow or returns raw bytes."""
    loader = FakeBootstrapLoader()

    encoded = bootstrap_session("archive_owner", loader_factory=lambda **_options: loader)

    assert loader.interactive_calls == ["archive_owner"]
    assert loader.two_factor_codes == ["246810"]
    assert base64.b64decode(encoded, validate=True) == b"serialized\x00session"


def test_bootstrap_disables_instaloaders_internal_retries() -> None:
    """Break caught: interactive authentication retries a terminal failure inside Instaloader."""
    loader = FakeBootstrapLoader()
    factory_options: list[dict[str, int]] = []

    def factory(**options: int) -> FakeBootstrapLoader:
        factory_options.append(options)
        return loader

    encoded = bootstrap_session("archive_owner", loader_factory=factory)

    assert base64.b64decode(encoded, validate=True) == b"serialized\x00session"
    assert factory_options == [{"max_connection_attempts": 1}]


def test_bootstrap_uses_mode_0600_storage_and_deletes_it_after_encoding() -> None:
    """Break caught: the serialized credential is world-readable or survives successful encoding."""
    loader = FakeBootstrapLoader()

    bootstrap_session("archive_owner", loader_factory=lambda **_options: loader)

    assert loader.saved_path is not None
    assert loader.mode_during_save == 0o600
    assert not loader.saved_path.exists()


def test_bootstrap_rejects_an_identity_mismatch_without_writing_a_session() -> None:
    """Break caught: bootstrap emits credentials belonging to a different authenticated account."""
    loader = FakeBootstrapLoader(login_name="different_owner")

    with pytest.raises(LoginError, match="authenticated identity does not match"):
        bootstrap_session("archive_owner", loader_factory=lambda **_options: loader)

    assert loader.saved_path is None


def test_bootstrap_deletes_temporary_storage_when_encoding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a read/encoding failure leaves serialized credentials on disk."""
    loader = FakeBootstrapLoader()

    def fail_encode(_value: bytes) -> bytes:
        raise RuntimeError("injected encoding failure")

    monkeypatch.setattr("sync.bootstrap_session.base64.b64encode", fail_encode)

    with pytest.raises(RuntimeError, match="injected encoding failure"):
        bootstrap_session("archive_owner", loader_factory=lambda **_options: loader)

    assert loader.saved_path is not None
    assert not loader.saved_path.exists()


def test_bootstrap_translates_login_failures_without_leaking_credentials() -> None:
    """Break caught: interactive authentication exposes Instaloader's credential-bearing error."""
    loader = FakeBootstrapLoader(login_error=BadCredentialsException("password=secret"))

    with pytest.raises(LoginError) as captured:
        bootstrap_session("archive_owner", loader_factory=lambda **_options: loader)

    assert str(captured.value) == "Instagram login failed"
    assert captured.value.__cause__ is None


def test_bootstrap_does_not_change_the_process_umask() -> None:
    """Break caught: credential hardening mutates the caller's process-wide file creation policy."""
    loader = FakeBootstrapLoader()
    previous = os.umask(0)
    os.umask(previous)

    bootstrap_session("archive_owner", loader_factory=lambda **_options: loader)

    observed = os.umask(0)
    os.umask(observed)
    assert observed == previous
