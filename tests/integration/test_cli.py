"""CLI process contracts and real sync composition without Instagram access."""

import base64
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import FakeInstagramClient, public_image

from sync.archive.models import MAX_GENERATED_FILE_BYTES
from sync.archive.store import SnapshotStore
from sync.instagram.errors import (
    AuthenticationError,
    PublicationError,
    SizeError,
    ThrottleError,
    TransientExhaustionError,
    ValidationError,
)


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sync.cli", *args], capture_output=True, text=True, check=False
    )


def module():
    try:
        return importlib.import_module("sync.cli")
    except ModuleNotFoundError:
        pytest.fail("archive CLI has not been implemented")


def test_init_validate_export_and_budget_are_machine_readable(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    destination = tmp_path / "new-parent/site"
    for arguments in [
        ["init-snapshot", "--snapshot", str(snapshot)],
        ["validate-snapshot", "--snapshot", str(snapshot)],
        ["prepare-site-input", "--snapshot", str(snapshot), "--destination", str(destination)],
        ["check-budget", "--root", str(destination)],
    ]:
        result = cli(*arguments)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "ok"
        assert result.stderr.strip()
    assert (destination / "sync-state.json").is_file()


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--max-new-posts", "0"),
        ("--max-new-posts", "51"),
        ("--max-new-posts", "1.5"),
        ("--known-threshold", "0"),
        ("--reconcile-limit", "0"),
        ("--reconcile-limit", "21"),
    ],
)
def test_cli_rejects_invalid_sync_bounds_before_work(tmp_path: Path, flag: str, value: str) -> None:
    result = cli(
        "sync",
        "--snapshot",
        str(tmp_path / "missing"),
        "--session",
        "missing",
        "--removals",
        "missing",
        "--username",
        "someone",
        flag,
        value,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["exit_code"] == 2
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "missing").exists()


def test_argument_errors_do_not_echo_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTAGRAM_SESSION_B64", "secret-cookie-value")
    result = cli("--unknown=secret-cookie-value")
    assert result.returncode == 2
    assert "secret-cookie-value" not in result.stdout + result.stderr


def test_missing_snapshot_and_invalid_data_return_validation_exit(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    result = cli("validate-snapshot", "--snapshot", str(missing))
    assert result.returncode == 30
    assert not missing.exists()
    SnapshotStore(missing).initialize()
    (missing / "manifest.json").write_text("secret invalid content")
    result = cli("validate-snapshot", "--snapshot", str(missing))
    assert result.returncode == 30
    assert json.loads(result.stdout)["exit_code"] == 30
    assert "secret invalid content" not in result.stdout + result.stderr


def test_budget_rejection_reports_largest_files(tmp_path: Path) -> None:
    with (tmp_path / "too-large.mp4").open("wb") as handle:
        handle.truncate(MAX_GENERATED_FILE_BYTES + 1)
    result = cli("check-budget", "--root", str(tmp_path))
    assert result.returncode == 31
    report = json.loads(result.stdout)
    assert report["budget"]["level"] == "reject"
    assert report["budget"]["largest"][0]["path"] == "too-large.mp4"


def test_sync_composes_identity_client_engine_and_json_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    boundary = module()
    snapshot = tmp_path / "snapshot"
    SnapshotStore(snapshot).initialize()
    session = tmp_path / "session"
    session.write_text("private session")
    removals = tmp_path / "removals.txt"
    removals.write_text("")
    client = FakeInstagramClient()
    client.saved = [public_image("NEW001")]
    identities: list[str] = []
    monkeypatch.setattr(client, "validate_identity", identities.append)
    monkeypatch.setattr(boundary, "InstaloaderClient", lambda path: client)
    monkeypatch.setenv("INSTAGRAM_USERNAME", "private-account")
    assert (
        boundary.main(
            [
                "sync",
                "--snapshot",
                str(snapshot),
                "--session",
                str(session),
                "--removals",
                str(removals),
                "--max-new-posts",
                "1",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["report"]["new_count"] == 1
    assert len(SnapshotStore(snapshot).load()[0].posts) == 1
    assert identities == ["private-account"]
    assert "private-account" not in output.out + output.err
    assert "private session" not in output.out + output.err


@pytest.mark.parametrize(
    "failure",
    [
        AuthenticationError,
        ThrottleError,
        TransientExhaustionError,
        ValidationError,
        SizeError,
        PublicationError,
    ],
)
def test_cli_uses_stable_failure_codes_without_exception_or_environment_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure
) -> None:
    boundary = module()
    snapshot = tmp_path / "snapshot"
    SnapshotStore(snapshot).initialize()
    session = tmp_path / "session"
    session.write_text("session")
    removals = tmp_path / "removals.txt"
    removals.write_text("")

    def fail(path: Path):
        raise failure("unregistered secret from upstream")

    monkeypatch.setattr(boundary, "InstaloaderClient", fail)
    monkeypatch.setenv("PRIVATE_VALUE", "environment-must-not-be-dumped")
    code = boundary.main(
        [
            "sync",
            "--snapshot",
            str(snapshot),
            "--session",
            str(session),
            "--username",
            "secret-user",
            "--removals",
            str(removals),
        ]
    )
    output = capsys.readouterr()
    assert code == failure.exit_code
    assert json.loads(output.out)["exit_code"] == code
    for secret in ["unregistered secret", "secret-user", os.environ["PRIVATE_VALUE"]]:
        assert secret not in output.out + output.err
    assert output.err.strip()


def test_publisher_has_no_configurable_branch_argument() -> None:
    result = cli("publish-snapshot", "--branch", "main")
    assert result.returncode == 2
    assert "--branch" not in cli("publish-snapshot", "--help").stdout


def test_init_snapshot_supports_existing_empty_directory(tmp_path: Path) -> None:
    snapshot = tmp_path / "empty"
    snapshot.mkdir()
    result = cli("init-snapshot", "--snapshot", str(snapshot))
    assert result.returncode == 0, result.stderr
    assert SnapshotStore(snapshot).load()[0].posts == ()


def test_bootstrap_stdout_is_only_base64_and_interactive_output_goes_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Break caught: a prompt, saved-file notice, or JSON contaminates a secret pipe."""
    from sync.bootstrap_session import bootstrap_session

    class Loader:
        def interactive_login(self, username: str) -> None:
            assert username == "archive_owner"
            print("Interactive login and two-factor prompt")

        def test_login(self) -> str:
            print("Checking authenticated identity")
            return "archive_owner"

        def save_session_to_file(self, filename: str) -> None:
            Path(filename).write_bytes(b"synthetic-session")
            print("Saved session notice")

    boundary = module()
    monkeypatch.setattr(
        boundary,
        "bootstrap_session",
        lambda username: bootstrap_session(username, loader_factory=lambda **_: Loader()),
        raising=False,
    )
    assert boundary.main(["bootstrap-session", "--username", "archive_owner"]) == 0
    output = capsys.readouterr()
    assert output.out == base64.b64encode(b"synthetic-session").decode() + "\n"
    assert "Interactive login and two-factor prompt" in output.err
    assert "Checking authenticated identity" in output.err
    assert "Saved session notice" in output.err


@pytest.mark.parametrize(
    "failure,expected",
    [(AuthenticationError, 20), (RuntimeError, 1), (EOFError, 20), (KeyboardInterrupt, 20)],
)
def test_bootstrap_failure_has_empty_stdout_and_sanitized_stable_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure,
    expected: int,
) -> None:
    boundary = module()

    def fail(username: str) -> str:
        raise failure("synthetic-sensitive-value")

    monkeypatch.setattr(boundary, "bootstrap_session", fail, raising=False)
    assert boundary.main(["bootstrap-session", "--username", "archive_owner"]) == expected
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip()
    assert "synthetic-sensitive-value" not in output.err
    assert "Traceback" not in output.err


def test_bootstrap_missing_username_does_not_emit_json_into_secret_pipe() -> None:
    result = cli("bootstrap-session")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Invalid command arguments" in result.stderr
