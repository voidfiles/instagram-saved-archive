"""Publish a validated snapshot as the sole root of the literal archive-data ref."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from sync.archive.budget import check_budget
from sync.instagram.errors import PublicationError, SizeError, ValidationError
from sync.site_input.exporter import export_site_input, resolve_plain_path, validate_snapshot


class CommandRunner(Protocol):
    def __call__(
        self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    args: Sequence[str], *, cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run without a shell or inherited Git output in logs."""
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def publish_snapshot(
    snapshot: Path,
    source_repository: Path,
    expected_repository: str,
    remote_url: str,
    env: Mapping[str, str],
    run: CommandRunner = run_command,
) -> str:
    """Validate every boundary before Git mutation, then replace only archive-data.

    Callers serialize snapshot writers. HTTPS credentials may be supplied in the URL;
    otherwise Git uses the runner's ordinary credential configuration.
    """
    if (
        not _valid_identity(expected_repository)
        or env.get("GITHUB_REPOSITORY") != expected_repository
    ):
        raise PublicationError("repository identity does not match expected repository")
    if _remote_identity(remote_url).casefold() != expected_repository.casefold():
        raise PublicationError("publication remote does not match expected repository")
    try:
        snapshot = resolve_plain_path(snapshot)
        source_repository = resolve_plain_path(source_repository)
        if not source_repository.is_dir() or snapshot.is_relative_to(source_repository):
            raise PublicationError("snapshot must be outside the source worktree")
        validate_snapshot(snapshot)
        budget = check_budget(snapshot)
        if budget.level == "reject":
            raise SizeError("snapshot exceeds publication size budget", budget=budget)
    except (OSError, ValueError):
        raise ValidationError("snapshot paths or content are invalid") from None

    # Git repository redirects, injected config, traces, and author values must not
    # escape the temporary repository or leak credentials into runner logs.
    git_env = {key: value for key, value in env.items() if not key.startswith("GIT_")}
    git_env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Instagram Saved Archive",
            "GIT_AUTHOR_EMAIL": "archive@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "Instagram Saved Archive",
            "GIT_COMMITTER_EMAIL": "archive@users.noreply.github.com",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )

    def git(args: Sequence[str], cwd: Path) -> str:
        try:
            result = run(["git", *args], cwd=cwd, env=git_env)
            if result.returncode:
                raise PublicationError("Git publication operation failed")
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise PublicationError("Git publication operation failed") from None

    worktree = Path(git(["rev-parse", "--show-toplevel"], source_repository)).resolve(strict=True)
    if snapshot.is_relative_to(worktree):
        raise PublicationError("snapshot must be outside the source worktree")
    origin = git(["config", "--get", "remote.origin.url"], source_repository)
    if _remote_identity(origin).casefold() != expected_repository.casefold():
        raise PublicationError("source remote does not match expected repository")

    with tempfile.TemporaryDirectory(prefix="archive-publish-") as temporary:
        repository = Path(temporary) / "snapshot"
        try:
            export_site_input(snapshot, repository)
            budget = check_budget(repository)
            if budget.level == "reject":
                raise SizeError("snapshot exceeds publication size budget", budget=budget)
        except (OSError, ValueError):
            raise ValidationError("snapshot export failed validation") from None
        git(["init", "--template=", "-b", "archive-data"], repository)
        git(["config", "core.hooksPath", "/dev/null"], repository)
        git(["add", "--", "manifest.json", "sync-state.json", "media"], repository)
        git(["commit", "--no-gpg-sign", "-m", "Publish archive snapshot"], repository)
        sha = git(["rev-parse", "HEAD"], repository)
        git(["remote", "add", "origin", remote_url], repository)
        git(["push", "--force", "origin", "HEAD:refs/heads/archive-data"], repository)
        return sha


def _valid_identity(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)) and all(
        part not in {".", ".."} for part in value.split("/")
    )


def _remote_identity(url: str) -> str:
    if url.startswith("git@github.com:"):
        identity = url.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"https", "ssh"}
                or parsed.hostname != "github.com"
                or parsed.port is not None
                or parsed.query
                or parsed.fragment
                or (parsed.scheme == "ssh" and (parsed.username != "git" or parsed.password))
            ):
                raise ValueError
            identity = parsed.path.removeprefix("/")
        except ValueError:
            raise PublicationError(
                "Git remote must identify the expected GitHub repository"
            ) from None
    identity = identity.removesuffix(".git")
    if not _valid_identity(identity):
        raise PublicationError("Git remote must identify the expected GitHub repository")
    return identity
