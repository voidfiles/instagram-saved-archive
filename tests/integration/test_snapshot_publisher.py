"""Real Git integration tests for the sole permitted force-push destination."""

import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import SyncHarness

from sync.archive.models import MAX_GENERATED_FILE_BYTES
from sync.archive.store import SnapshotStore
from sync.instagram.errors import PublicationError, SizeError, ValidationError


class GitHarness:
    def __init__(self, root: Path) -> None:
        self.source = root / "source"
        self.source.mkdir()
        self.remote = root / "remote.git"
        self.home = root / "home"
        self.home.mkdir()
        self.url = "https://github.com/owner/archive.git"
        self.github_env = {
            **os.environ,
            "HOME": str(self.home),
            "GITHUB_REPOSITORY": "owner/archive",
        }
        self.commands: list[list[str]] = []
        self.git(["init", "--bare", str(self.remote)])
        self.git(["init", "-b", "main"], self.source)
        self.git(["config", "user.name", "Fixture"], self.source)
        self.git(["config", "user.email", "fixture@example.test"], self.source)
        (self.source / "source-code.txt").write_text("must remain on main only")
        self.git(["add", "."], self.source)
        self.git(["commit", "-m", "source"], self.source)
        self.git(["remote", "add", "origin", "git@github.com:owner/archive.git"], self.source)
        self.git(["config", "--global", f"url.{self.remote}.insteadOf", self.url])
        self.git(["push", self.url, "main"], self.source)

    def git(self, args: list[str], cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=self.github_env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def run(self, args, *, cwd: Path, env):
        self.commands.append(list(args))
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True)

    def rev_parse(self, ref: str) -> str:
        return self.git(["rev-parse", ref], self.remote)


@pytest.fixture
def harness(tmp_path: Path) -> GitHarness:
    return GitHarness(tmp_path)


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    sync = SyncHarness(tmp_path)
    sync.seed(1)
    return sync.snapshot


def publisher():
    try:
        return importlib.import_module("sync.archive.publisher").publish_snapshot
    except ModuleNotFoundError:
        pytest.fail("safe orphan snapshot publisher has not been implemented")


def publish(harness: GitHarness, snapshot: Path, **overrides):
    arguments = {
        "snapshot": snapshot,
        "source_repository": harness.source,
        "expected_repository": "owner/archive",
        "remote_url": harness.url,
        "env": harness.github_env,
        "run": harness.run,
    }
    arguments.update(overrides)
    return publisher()(**arguments)


def test_publication_creates_one_root_and_second_replaces_only_archive_data(
    harness: GitHarness, snapshot: Path
) -> None:
    (snapshot / "instagram.session").write_text("must not publish")
    before = harness.rev_parse("refs/heads/main")
    source_head = harness.git(["rev-parse", "HEAD"], harness.source)
    first = publish(harness, snapshot)
    assert first == harness.rev_parse("refs/heads/archive-data")
    assert harness.git(["rev-list", "--count", "archive-data"], harness.remote) == "1"
    assert (
        harness.git(["rev-list", "--parents", "-n", "1", "archive-data"], harness.remote) == first
    )
    assert harness.git(["ls-tree", "--name-only", "archive-data"], harness.remote).splitlines() == [
        "manifest.json",
        "media",
        "sync-state.json",
    ]
    store = SnapshotStore(snapshot)
    manifest, state = store.load()
    store.write_atomic(manifest, replace(state, backfill_complete=True))
    second = publish(harness, snapshot)
    assert second != first
    assert harness.git(["rev-list", "--count", "archive-data"], harness.remote) == "1"
    assert harness.rev_parse("refs/heads/main") == before
    assert harness.git(["rev-parse", "HEAD"], harness.source) == source_head
    assert harness.git(["status", "--porcelain"], harness.source) == ""
    pushes = [args for args in harness.commands if args[1] == "push"]
    assert pushes == [["git", "push", "--force", "origin", "HEAD:refs/heads/archive-data"]] * 2


@pytest.mark.parametrize(
    "mismatch", ["environment", "source", "destination", "malicious-host", "branch-url"]
)
def test_identity_mismatch_refuses_before_git_mutation(
    harness: GitHarness, snapshot: Path, mismatch: str
) -> None:
    overrides = {}
    if mismatch == "environment":
        overrides["env"] = {**harness.github_env, "GITHUB_REPOSITORY": "attacker/fork"}
    elif mismatch == "source":
        harness.git(
            ["remote", "set-url", "origin", "https://github.com/attacker/fork"], harness.source
        )
    else:
        overrides["remote_url"] = {
            "destination": "https://github.com/attacker/fork.git",
            "malicious-host": "https://github.com.attacker.test/owner/archive.git",
            "branch-url": "https://github.com/owner/archive.git#main",
        }[mismatch]
    with pytest.raises(PublicationError):
        publish(harness, snapshot, **overrides)
    assert not any(
        args[1] in {"init", "add", "commit", "push", "remote"} for args in harness.commands
    )
    assert harness.git(["for-each-ref", "--format=%(refname)"], harness.remote) == "refs/heads/main"


@pytest.mark.parametrize("kind", ["inside", "root", "symlink", "invalid", "asset-link"])
def test_unsafe_snapshot_refuses_before_git_mutation(
    harness: GitHarness, snapshot: Path, kind: str
) -> None:
    if kind == "inside":
        snapshot = harness.source / "snapshot"
        SnapshotStore(snapshot).initialize()
    elif kind == "root":
        snapshot = harness.source
    elif kind == "symlink":
        link = snapshot.parent / "snapshot-link"
        link.symlink_to(snapshot, target_is_directory=True)
        snapshot = link
    elif kind == "invalid":
        (snapshot / "manifest.json").write_text("invalid secret")
    else:
        (snapshot / "media/link").symlink_to(harness.source / "source-code.txt")
    with pytest.raises((PublicationError, ValidationError)):
        publish(harness, snapshot)
    assert not any(
        args[1] in {"init", "add", "commit", "push", "remote"} for args in harness.commands
    )


def test_git_environment_cannot_redirect_mutation_into_main(
    harness: GitHarness, snapshot: Path
) -> None:
    before = harness.git(["rev-parse", "HEAD"], harness.source)
    hostile = {
        **harness.github_env,
        "GIT_DIR": str(harness.source / ".git"),
        "GIT_WORK_TREE": str(harness.source),
        "GIT_INDEX_FILE": str(harness.source / ".git/index"),
    }
    publish(harness, snapshot, env=hostile)
    assert harness.git(["rev-parse", "HEAD"], harness.source) == before
    assert harness.git(["status", "--porcelain"], harness.source) == ""


def test_oversized_snapshot_refuses_before_git_mutation(
    harness: GitHarness, snapshot: Path
) -> None:
    with (snapshot / "oversized.bin").open("wb") as handle:
        handle.truncate(MAX_GENERATED_FILE_BYTES + 1)
    with pytest.raises(SizeError):
        publish(harness, snapshot)
    assert not harness.commands


def test_snapshot_inside_worktree_cannot_hide_behind_source_subdirectory(
    harness: GitHarness, snapshot: Path
) -> None:
    child = harness.source / "subdirectory"
    child.mkdir()
    snapshot = harness.source / "snapshot"
    SnapshotStore(snapshot).initialize()
    with pytest.raises(PublicationError):
        publish(harness, snapshot, source_repository=child)
    assert not any(
        args[1] in {"init", "add", "commit", "push", "remote"} for args in harness.commands
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/archive.git",
        "ssh://git@github.com/owner/archive.git",
        "https://x-access-token:secret-token@github.com/owner/archive.git",
    ],
)
def test_matching_https_ssh_and_credential_urls_publish(
    harness: GitHarness, snapshot: Path, url: str
) -> None:
    harness.git(["config", "--global", "--add", f"url.{harness.remote}.insteadOf", url])
    sha = publish(harness, snapshot, remote_url=url)
    assert sha == harness.rev_parse("refs/heads/archive-data")


def test_failed_git_never_exposes_remote_credentials(harness: GitHarness, snapshot: Path) -> None:
    # A missing local target forces a genuine Git transport failure.
    remote = "https://x-access-token:secret-token@github.com/owner/archive.git"
    harness.git(
        ["config", "--global", "--add", f"url.{harness.remote / 'missing'}.insteadOf", remote]
    )
    with pytest.raises(PublicationError) as error:
        publish(harness, snapshot, remote_url=remote)
    assert "secret-token" not in str(error.value)
    assert error.value.__suppress_context__


def test_cli_publication_uses_real_default_runner_and_deterministic_root(
    harness: GitHarness, snapshot: Path
) -> None:
    before = harness.rev_parse("refs/heads/main")
    first = publish(harness, snapshot)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sync.cli",
            "publish-snapshot",
            "--snapshot",
            str(snapshot),
            "--source-repository",
            str(harness.source),
            "--expected-repository",
            "owner/archive",
            "--remote-url",
            harness.url,
        ],
        env=harness.github_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["commit"] == first
    assert harness.rev_parse("refs/heads/main") == before
    assert harness.git(["rev-list", "--count", "archive-data"], harness.remote) == "1"
