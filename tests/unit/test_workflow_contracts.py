"""Offline CI and transactional deployment workflow contracts and shell probes."""

from __future__ import annotations

import base64
import copy
import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "test.yml"
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"
REQUIRED_RUN_STEPS = {
    "Check FFmpeg": (None, ("ffmpeg -version",)),
    "Install Python dependencies": (
        None,
        (
            "python -m pip install --upgrade pip",
            "python -m pip install --group dev -e .",
        ),
    ),
    "Run Python checks": (
        None,
        (
            "python -m ruff format --check .",
            "python -m ruff check .",
            "python -m mypy sync",
            "python -m pytest -q",
        ),
    ),
    "Generate and validate offline archive fixture": (
        None,
        (
            "python tests/fixtures/build_sample_archive.py --output .tmp/sample-archive",
            "python -m sync.cli validate-snapshot --snapshot .tmp/sample-archive",
            "python -m sync.cli prepare-site-input --snapshot .tmp/sample-archive --destination site/public/archive",
        ),
    ),
    "Install site dependencies": ("site", ("npm ci",)),
    "Check site": ("site", ("npm run check",)),
    "Test site": ("site", ("npm test",)),
    "Build site": ("site", ("npm run build",)),
    "Check static output": ("site", ("npm run check:static",)),
    "Install Playwright Chromium": ("site", ("npx playwright install chromium",)),
    "Run browser tests": ("site", ("npm run test:e2e",)),
}
EXPECTED_STEP_NAMES = (
    "Check out source",
    "Set up Python",
    "Set up Node",
    *REQUIRED_RUN_STEPS,
    "Upload Playwright diagnostics",
)


def _value(mapping: Mapping[object, object], key: str) -> object:
    """Read a YAML key despite PyYAML 1.1 coercing unquoted ``on`` to ``True``."""
    if key in mapping:
        return mapping[key]
    if key == "on" and True in mapping:
        return mapping[True]
    raise AssertionError(f"missing YAML key: {key}")


def _mapping(value: object) -> Mapping[object, object]:
    assert isinstance(value, Mapping)
    return value


def _positive_timeout(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_workflow(raw: str) -> Mapping[object, object]:
    return _mapping(yaml.safe_load(raw))


def _workflow() -> Mapping[object, object]:
    assert WORKFLOW_PATH.is_file(), "offline CI workflow must exist"
    return _parse_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _steps(workflow: Mapping[object, object]) -> list[Mapping[object, object]]:
    jobs = _mapping(_value(workflow, "jobs"))
    assert list(jobs) == ["test"], "CI has one deterministic test job"
    job = _mapping(jobs["test"])
    assert str(_value(job, "runs-on")).startswith("ubuntu")
    assert _positive_timeout(_value(job, "timeout-minutes"))
    values = _value(job, "steps")
    assert isinstance(values, list)
    steps = [_mapping(step) for step in values]
    assert all(_positive_timeout(_value(step, "timeout-minutes")) for step in steps)
    assert tuple(_value(step, "name") for step in steps) == EXPECTED_STEP_NAMES
    return steps


def _step_by_name(steps: list[Mapping[object, object]], name: str) -> Mapping[object, object]:
    return next(step for step in steps if step.get("name") == name)


def _step_index(steps: list[Mapping[object, object]], name: str) -> int:
    return next(index for index, step in enumerate(steps) if step.get("name") == name)


def _assert_action_steps(steps: list[Mapping[object, object]]) -> None:
    actions = {
        "Check out source": ("actions/checkout@v7", {"persist-credentials": False}),
        "Set up Python": (
            "actions/setup-python@v7",
            {
                "python-version": "3.12",
                "cache": "pip",
                "cache-dependency-path": "pyproject.toml",
            },
        ),
        "Set up Node": (
            "actions/setup-node@v7",
            {
                "node-version": "24",
                "cache": "npm",
                "cache-dependency-path": "site/package-lock.json",
            },
        ),
    }
    for name, (use, options) in actions.items():
        step = _step_by_name(steps, name)
        assert _value(step, "uses") == use
        assert _value(step, "with") == options
        assert "if" not in step, f"{name} must always execute"


def _assert_run_steps(steps: list[Mapping[object, object]]) -> None:
    for name, (directory, lines) in REQUIRED_RUN_STEPS.items():
        step = _step_by_name(steps, name)
        assert "if" not in step, f"{name} must always execute"
        if directory is None:
            assert "working-directory" not in step
        else:
            assert _value(step, "working-directory") == directory
        run = _value(step, "run")
        assert isinstance(run, str)
        assert tuple(run.splitlines()) == lines


def _assert_order(steps: list[Mapping[object, object]]) -> None:
    expected_order = (
        "Check out source",
        "Set up Python",
        "Set up Node",
        "Install Python dependencies",
        "Run Python checks",
        "Generate and validate offline archive fixture",
        "Install site dependencies",
        "Check site",
        "Test site",
        "Build site",
        "Check static output",
        "Install Playwright Chromium",
        "Run browser tests",
    )
    positions = [_step_index(steps, name) for name in expected_order]
    assert positions == sorted(positions), "setup and offline gates must run in order"


def _assert_workflow_contract(workflow: Mapping[object, object]) -> None:
    triggers = _mapping(_value(workflow, "on"))
    assert set(triggers) == {"pull_request", "push"}, "only PR and main-push triggers are allowed"
    assert _value(triggers, "pull_request") is None
    push = _mapping(_value(triggers, "push"))
    assert push == {"branches": ["main"]}
    assert _value(workflow, "permissions") == {"contents": "read"}

    steps = _steps(workflow)
    _assert_action_steps(steps)
    _assert_run_steps(steps)
    _assert_order(steps)

    diagnostics = _step_by_name(steps, "Upload Playwright diagnostics")
    assert _value(diagnostics, "uses") == "actions/upload-artifact@v4"
    assert _value(diagnostics, "if") == "failure()"
    assert _value(diagnostics, "with") == {
        "name": "playwright-diagnostics",
        "path": "site/test-results\nsite/playwright-report\n",
        "if-no-files-found": "ignore",
        "retention-days": 3,
    }

    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    forbidden = ("INSTAGRAM_SESSION_B64", "password", "set -x", "printenv", "env |", "write")
    assert not any(value.lower() in raw.lower() for value in forbidden)


def test_workflow_enforces_the_offline_execution_boundary() -> None:
    """Break caught: a gate is skipped, mislocated, reordered, or loses its exact command."""
    _assert_workflow_contract(_workflow())


def test_editable_package_discovery_includes_only_the_sync_package_tree() -> None:
    """Break caught: flat-layout discovery accidentally packages the Astro site directory."""
    metadata = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    discovery = metadata["tool"]["setuptools"]["packages"]["find"]
    assert discovery == {"include": ["sync*"], "namespaces": False}


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("  pull_request:\n", "  pull_request:\n  workflow_dispatch:\n"),
        ("          cache: pip\n", "          cache: false\n"),
        (
            "      - name: Install site dependencies\n        working-directory: site\n        run: npm ci\n",
            "      - name: Install site dependencies\n        run: npm ci\n",
        ),
        (
            "      - name: Build site\n        working-directory: site\n        run: npm run build\n",
            "      - name: Build site\n        if: false\n        working-directory: site\n        run: npm run build\n",
        ),
        ("          python -m pytest -q\n", "          python -m pytest -q --disable-warnings\n"),
    ],
)
def test_workflow_contract_rejects_bypass_mutations(original: str, replacement: str) -> None:
    """Break caught: common textual mutations silently weaken a structured CI gate."""
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert raw.count(original) == 1
    mutated = _parse_workflow(raw.replace(original, replacement))
    with pytest.raises(AssertionError):
        _assert_workflow_contract(mutated)


DEPLOY_PATH = WORKFLOW_PATH.with_name("sync-and-deploy.yml")
DEPLOY_STEP_NAMES = (
    "Check out source",
    "Initialize runtime paths",
    *EXPECTED_STEP_NAMES[1:-1],
    "Restore archive snapshot",
    "Synchronize Instagram",
    "Validate production snapshot",
    "Prepare production site input",
    "Configure Pages",
    "Build production site",
    "Check production static output",
    "Publish archive snapshot",
    "Upload Pages artifact",
    "Deploy Pages",
)


def _deployment() -> Mapping[object, object]:
    assert DEPLOY_PATH.is_file(), "scheduled deployment workflow must exist"
    return _parse_workflow(DEPLOY_PATH.read_text(encoding="utf-8"))


def _deployment_steps(workflow: Mapping[object, object]) -> list[Mapping[object, object]]:
    jobs = _mapping(_value(workflow, "jobs"))
    assert list(jobs) == ["sync-and-deploy"]
    job = _mapping(jobs["sync-and-deploy"])
    assert job.get("if") == "github.ref == 'refs/heads/main'"
    assert job.get("runs-on") == "ubuntu-24.04"
    assert _positive_timeout(job.get("timeout-minutes"))
    assert job.get("environment") == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    assert job.get("defaults") == {"run": {"shell": "bash"}}
    assert "env" not in job
    assert "permissions" not in job and "continue-on-error" not in job
    values = _value(job, "steps")
    assert isinstance(values, list)
    steps = [_mapping(step) for step in values]
    assert tuple(step.get("name") for step in steps) == DEPLOY_STEP_NAMES
    runtime_paths = _step_by_name(steps, "Initialize runtime paths")
    assert runtime_paths.get("run") == (
        'printf \'SNAPSHOT=%s/archive-snapshot\\n\' "$RUNNER_TEMP" >> "$GITHUB_ENV"'
    )
    for step in steps:
        assert _positive_timeout(step.get("timeout-minutes"))
        assert "if" not in step and "continue-on-error" not in step
    return steps


def _assert_deployment_contract(workflow: Mapping[object, object]) -> None:
    triggers = _mapping(_value(workflow, "on"))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 10 * * *"}]
    dispatch = _mapping(triggers["workflow_dispatch"])
    inputs = _mapping(dispatch.get("inputs"))
    assert set(inputs) == {"full_scan", "max_new_posts"}
    assert _mapping(inputs["full_scan"]) | {"description": ""} == {
        "description": "",
        "type": "boolean",
        "default": False,
        "required": False,
    }
    assert _mapping(inputs["max_new_posts"]) | {"description": ""} == {
        "description": "",
        "type": "number",
        "default": 50,
        "required": False,
    }
    assert workflow.get("permissions") == {
        "contents": "write",
        "pages": "write",
        "id-token": "write",
    }
    assert workflow.get("concurrency") == {
        "group": "instagram-saved-archive-sync-and-deploy",
        "cancel-in-progress": False,
    }
    assert "env" not in workflow and "defaults" not in workflow
    steps = _deployment_steps(workflow)
    checkout = _step_by_name(steps, "Check out source")
    assert checkout.get("uses") == "actions/checkout@v7"
    assert checkout.get("with") == {"ref": "${{ github.sha }}", "persist-credentials": False}
    # Reuse the offline action/cache and command contracts with only checkout ref added.
    offline = copy.deepcopy(steps)
    offline[0]["with"] = {"persist-credentials": False}
    _assert_action_steps(offline)
    _assert_run_steps(steps)
    commands = {
        "Validate production snapshot": (
            None,
            'python -m sync.cli validate-snapshot --snapshot "$SNAPSHOT"',
        ),
        "Prepare production site input": (
            None,
            'python -m sync.cli prepare-site-input --snapshot "$SNAPSHOT" --destination site/public/archive',
        ),
        "Build production site": ("site", "npm run build"),
    }
    for name, (directory, command) in commands.items():
        step = _step_by_name(steps, name)
        assert step.get("working-directory") == directory
        assert str(step.get("run")).strip() == command
    for name, action, options in (
        ("Configure Pages", "actions/configure-pages@v6", None),
        ("Upload Pages artifact", "actions/upload-pages-artifact@v5", {"path": "site/dist"}),
        ("Deploy Pages", "actions/deploy-pages@v5", None),
    ):
        step = _step_by_name(steps, name)
        assert step.get("uses") == action
        assert step.get("with") == options
    assert _step_by_name(steps, "Deploy Pages").get("id") == "deployment"
    assert _step_by_name(steps, "Configure Pages").get("id") == "pages"
    production_build = _step_by_name(steps, "Build production site")
    production_check = _step_by_name(steps, "Check production static output")
    assert production_check.get("working-directory") == "site"
    for step in (production_build, production_check):
        assert step.get("env") == {"ASTRO_BASE_PATH": "${{ steps.pages.outputs.base_path }}"}
    restore = _step_by_name(steps, "Restore archive snapshot")
    publish = _step_by_name(steps, "Publish archive snapshot")
    sync = _step_by_name(steps, "Synchronize Instagram")
    for step in (restore, publish):
        assert step.get("env") == {"GH_TOKEN": "${{ github.token }}"}
        assert "working-directory" not in step
    assert sync.get("env") == {
        "INSTAGRAM_USERNAME": "${{ vars.INSTAGRAM_USERNAME }}",
        "INSTAGRAM_SESSION_B64": "${{ secrets.INSTAGRAM_SESSION_B64 }}",
        "FULL_SCAN": "${{ inputs.full_scan == true }}",
        "MAX_NEW_POSTS": "${{ github.event_name == 'workflow_dispatch' && toJSON(inputs.max_new_posts) || '50' }}",
    }
    assert "working-directory" not in sync
    publish_lines = str(publish.get("run")).splitlines()
    for line in [
        'remote="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"',
        'python -m sync.cli publish-snapshot --snapshot "$SNAPSHOT" \\',
        '  --source-repository "$GITHUB_WORKSPACE" \\',
        '  --expected-repository "$GITHUB_REPOSITORY" --remote-url "$remote" \\',
    ]:
        assert line in publish_lines
    for step in steps:
        run = str(step.get("run", ""))
        assert "${{" not in run, "untrusted expressions must enter shell scripts through env"
        assert not any(text in run.lower() for text in ("set -x", "printenv", "password"))
        if step not in (restore, publish, sync, production_build, production_check):
            assert "env" not in step
        if step not in (sync, production_check, publish):
            assert "GITHUB_STEP_SUMMARY" not in run
    restore_run = str(restore.get("run"))
    assert "refs/heads/archive-data" in restore_run
    assert 'git archive FETCH_HEAD | tar -x -C "$SNAPSHOT"' in restore_run
    assert "refs/heads/main" not in restore_run
    sync_run = str(sync.get("run"))
    assert "umask 077" in sync_run
    assert 'session="$RUNNER_TEMP/instagram.session"' in sync_run
    assert 'chmod 600 "$session"' in sync_run
    assert "trap " in sync_run and " EXIT" in sync_run
    assert "SecretRedactor" in sync_run
    assert "redactor.redact(" in sync_run
    assert sync_run.splitlines()[0] == "printf '::add-mask::%s\\n' \"$INSTAGRAM_USERNAME\""
    assert 'removals="$GITHUB_WORKSPACE/config/removals.txt"' in sync_run
    assert sync_run.count("-m sync.cli sync") == 1
    assert "&\n" not in sync_run


def test_deployment_requires_serialized_secure_publication_after_all_gates() -> None:
    """Break caught: secrets, skipped checks, fixtures or extra triggers reach publication."""
    _assert_deployment_contract(_deployment())


@pytest.mark.parametrize(
    "mutation",
    [
        "cancel",
        "permissions",
        "trigger",
        "ref",
        "credentials",
        "skip",
        "ignore_failure",
        "cache",
        "directory",
        "order",
        "fixture_publish",
        "artifact",
        "summary",
        "secret_scope",
        "pages_order",
        "build_base",
        "checker_base",
    ],
)
def test_deployment_contract_rejects_security_and_order_mutations(mutation: str) -> None:
    workflow = copy.deepcopy(_deployment())
    job = workflow["jobs"]["sync-and-deploy"]
    steps = job["steps"]
    if mutation == "cancel":
        workflow["concurrency"]["cancel-in-progress"] = True
    elif mutation == "permissions":
        workflow["permissions"]["actions"] = "write"
    elif mutation == "trigger":
        _value(workflow, "on")["pull_request"] = None
    elif mutation == "ref":
        steps[0]["with"]["ref"] = "main"
    elif mutation == "credentials":
        steps[0]["with"]["persist-credentials"] = True
    elif mutation == "skip":
        _step_by_name(steps, "Build production site")["if"] = False
    elif mutation == "ignore_failure":
        _step_by_name(steps, "Run browser tests")["continue-on-error"] = True
    elif mutation == "cache":
        steps[2]["with"]["cache-dependency-path"] = "package-lock.json"
    elif mutation == "directory":
        _step_by_name(steps, "Check production static output")["working-directory"] = "."
    elif mutation == "order":
        i = _step_index(steps, "Publish archive snapshot")
        steps[i - 1], steps[i] = steps[i], steps[i - 1]
    elif mutation == "fixture_publish":
        publish = _step_by_name(steps, "Publish archive snapshot")
        publish["run"] = publish["run"].replace('"$SNAPSHOT"', ".tmp/sample-archive")
    elif mutation == "artifact":
        _step_by_name(steps, "Upload Pages artifact")["with"]["path"] = "."
    elif mutation == "summary":
        _step_by_name(steps, "Build production site")["run"] += (
            '\necho raw >> "$GITHUB_STEP_SUMMARY"'
        )
    elif mutation == "secret_scope":
        job["env"] = {"INSTAGRAM_SESSION_B64": "${{ secrets.INSTAGRAM_SESSION_B64 }}"}
    elif mutation == "pages_order":
        i = _step_index(steps, "Configure Pages")
        steps[i], steps[i + 1] = steps[i + 1], steps[i]
    elif mutation == "build_base":
        _step_by_name(steps, "Build production site")["env"] = {}
    elif mutation == "checker_base":
        _step_by_name(steps, "Check production static output")["env"] = {}
    with pytest.raises(AssertionError):
        _assert_deployment_contract(workflow)


def _run_deployment_shell(
    tmp_path: Path,
    name: str,
    extra_env: dict[str, str],
    fake: str,
    *,
    run_override: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the actual workflow shell with only external commands replaced offline."""
    step = _step_by_name(_deployment_steps(_deployment()), name)
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    for command in ("python", "git", "npm"):
        executable = bin_path / command
        executable.write_text(f"#!{sys.executable}\n" + fake, encoding="utf-8")
        executable.chmod(0o700)
    runner = tmp_path / "runner"
    runner.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{bin_path}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner),
        "SNAPSHOT": str(runner / "archive-snapshot"),
        "GITHUB_WORKSPACE": str(REPOSITORY_ROOT),
        "GITHUB_REPOSITORY": "example/archive",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GH_TOKEN": "synthetic-github-token",
        **extra_env,
    }
    return subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            run_override if run_override is not None else str(step["run"]),
        ],
        cwd=REPOSITORY_ROOT / str(step.get("working-directory", ".")),
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("remote_status,fetch_status", [(0, 0), (2, 0), (128, 0), (0, 128)])
def test_restore_initializes_only_absent_branches_and_never_on_transport_failure(
    tmp_path: Path,
    remote_status: int,
    fetch_status: int,
) -> None:
    fake = """import io, os, sys, tarfile
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["RUNNER_TEMP"])
if args[:1] == ["ls-remote"]:
    assert args[-1] == "refs/heads/archive-data"
    sys.exit(int(os.environ["REMOTE_STATUS"]))
if args[:1] == ["fetch"]:
    assert args[-1] == "refs/heads/archive-data"
    sys.exit(int(os.environ["FETCH_STATUS"]))
if args[:1] == ["archive"]:
    assert args == ["archive", "FETCH_HEAD"]
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        entry = tarfile.TarInfo("restored")
        entry.size = 4
        archive.addfile(entry, io.BytesIO(b"real"))
elif args[:3] == ["-m", "sync.cli", "init-snapshot"]:
    Path(os.environ["SNAPSHOT"]).mkdir(exist_ok=True)
    (Path(os.environ["SNAPSHOT"]) / "initialized").touch()
else:
    sys.exit(99)
"""
    result = _run_deployment_shell(
        tmp_path,
        "Restore archive snapshot",
        {
            "REMOTE_STATUS": str(remote_status),
            "FETCH_STATUS": str(fetch_status),
        },
        fake,
    )
    snapshot = tmp_path / "runner/archive-snapshot"
    assert (result.returncode == 0) == (remote_status in (0, 2) and fetch_status == 0)
    assert (snapshot / "initialized").exists() == (remote_status == 2)
    assert (snapshot / "restored").exists() == (remote_status == 0 and fetch_status == 0)
    assert "synthetic-github-token" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "status,encoded,maximum,full_scan",
    [
        (0, None, "50", "false"),
        (20, None, "1", "true"),
        (0, "!not-base64!", "50", "false"),
        (0, None, "0", "false"),
        (0, None, "51", "false"),
        (0, None, "1.5", "false"),
        (0, None, "$(touch injected)", "false"),
        (0, None, "1", "invalid"),
    ],
)
def test_sync_shell_cleans_session_and_redacts_summary_even_on_failure(
    tmp_path: Path,
    status: int,
    encoded: str | None,
    maximum: str,
    full_scan: str,
) -> None:
    fake = """import os, stat, sys
from pathlib import Path
if sys.argv[1:4] != ["-m", "sync.cli", "sync"]:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
args = sys.argv[4:]
session = Path(args[args.index("--session") + 1])
assert session == Path(os.environ["RUNNER_TEMP"]) / "instagram.session"
assert stat.S_IMODE(session.stat().st_mode) == 0o600
assert session.read_text() == "synthetic-session"
assert args[args.index("--snapshot") + 1] == os.environ["SNAPSHOT"]
assert args[args.index("--max-new-posts") + 1] == os.environ["MAX_NEW_POSTS"]
assert ("--full-scan" in args) == (os.environ["FULL_SCAN"] == "true")
(Path(os.environ["RUNNER_TEMP"]) / "synced").touch()
print('{"status":"ok"}')
print("Aggregate sync result " + os.environ["INSTAGRAM_USERNAME"] + " " +
      os.environ["INSTAGRAM_SESSION_B64"], file=sys.stderr)
sys.exit(int(os.environ["SYNC_STATUS"]))
"""
    result = _run_deployment_shell(
        tmp_path,
        "Synchronize Instagram",
        {
            "INSTAGRAM_USERNAME": "synthetic_user",
            "INSTAGRAM_SESSION_B64": encoded or base64.b64encode(b"synthetic-session").decode(),
            "MAX_NEW_POSTS": maximum,
            "FULL_SCAN": full_scan,
            "SYNC_STATUS": str(status),
        },
        fake,
    )
    valid = encoded is None and maximum in {"1", "50"} and full_scan in {"true", "false"}
    assert (tmp_path / "runner/synced").exists() == valid
    assert not (tmp_path / "runner/instagram.session").exists()
    assert not (REPOSITORY_ROOT / "injected").exists()
    assert (result.returncode == 0) == (valid and status == 0)
    if valid:
        summary = (tmp_path / "summary").read_text()
        assert "Aggregate sync result" in summary and "[REDACTED]" in summary
        assert "synthetic_user" not in summary and "c3ludGhldGlj" not in summary
    assert "synthetic-session" not in result.stdout + result.stderr


@pytest.mark.parametrize("identity_matches", [True, False])
def test_workflow_sync_uses_cli_identity_gate_before_saved_feed_access(
    tmp_path: Path,
    identity_matches: bool,
) -> None:
    """Break caught: workflow bypasses CLI, or CLI enumerates before authenticating."""
    fake = """import os, sys
from pathlib import Path
if sys.argv[1:4] != ["-m", "sync.cli", "sync"]:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
import sync.cli as boundary
from sync.archive.store import SnapshotStore
from sync.instagram.errors import AuthenticationError
from sync.reporting import SyncReport
root = Path(os.environ["RUNNER_TEMP"])
SnapshotStore(Path(os.environ["SNAPSHOT"])).initialize()
class Client:
    def __init__(self, session):
        assert session.is_file()
    def validate_identity(self, username):
        assert username == "synthetic_user"
        if os.environ["IDENTITY_MATCHES"] != "true":
            raise AuthenticationError("synthetic-session")
        (root / "authenticated").touch()
class Engine:
    def __init__(self, client):
        assert (root / "authenticated").exists()
    def run(self, *args):
        (root / "enumerated").touch()
        return SyncReport()
boundary.InstaloaderClient = Client
boundary.SyncEngine = Engine
sys.exit(boundary.main(sys.argv[3:]))
"""
    result = _run_deployment_shell(
        tmp_path,
        "Synchronize Instagram",
        {
            "INSTAGRAM_USERNAME": "synthetic_user",
            "INSTAGRAM_SESSION_B64": base64.b64encode(b"synthetic-session").decode(),
            "MAX_NEW_POSTS": "1",
            "FULL_SCAN": "false",
            "IDENTITY_MATCHES": "true" if identity_matches else "false",
        },
        fake,
    )
    assert (result.returncode == 0) == identity_matches
    assert (tmp_path / "runner/enumerated").exists() == identity_matches
    assert not (tmp_path / "runner/instagram.session").exists()
    summary = (tmp_path / "summary").read_text()
    assert "synthetic-session" not in summary
    if not identity_matches:
        assert "Authentication failed" in summary


def _budget_shell_probe(tmp_path: Path, scope: str, level: str, mutation: str = "") -> None:
    """Exercise Python boundaries before npm installation; site tests cover the real checker."""
    name = "Synchronize Instagram" if scope == "Snapshot" else "Check production static output"
    fake = """import os, subprocess, sys
from pathlib import Path
root = Path(os.environ["RUNNER_TEMP"])
if sys.argv[1:4] == ["-m", "sync.cli", "sync"]:
    sys.path.insert(0, os.environ["GITHUB_WORKSPACE"])
    import sync.cli as boundary
    import sync.engine as engine
    from sync.archive.budget import check_budget, PathSize
    from tests.integration.fakes import FakeInstagramClient, SyncHarness, public_image
    harness = SyncHarness(root)
    snapshot = Path(os.environ["SNAPSHOT"])
    harness.snapshot.rename(snapshot)
    client = FakeInstagramClient()
    client.saved = [public_image("PUBLIC"), *[public_image(f"PUBLIC{i}") for i in range(4)]]
    boundary.InstaloaderClient = lambda _session: client
    total = 850_000_000 if os.environ["BUDGET_LEVEL"] == "warning" else 900_000_000
    engine.check_budget = lambda _root: check_budget(_root, size_walker=lambda _: (
        PathSize(path.relative_to(_root), total // 10)
        for path in sorted((_root / "media").rglob("*.webp"))
    ))
    sys.exit(boundary.main(sys.argv[3:]))
if sys.argv[1:3] == ["run", "check:static"]:
    artifact = root / "artifact"
    media = artifact / "archive/media/PUBLIC"
    media.mkdir(parents=True)
    (artifact / "index.html").write_text("")
    size = 85_000_000 if os.environ["BUDGET_LEVEL"] == "warning" else 90_000_000
    for i in range(10):
        with (media / f"{i:02}.webp").open("wb") as file:
            file.truncate(size)
    result = subprocess.run([sys.executable, "-m", "sync.cli", "check-budget",
                             "--root", str(artifact)], capture_output=True, text=True)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    sys.exit(0 if result.returncode == 0 else 1)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
"""
    step = _step_by_name(_deployment_steps(_deployment()), name)
    run = str(step["run"])
    if mutation == "discard_summary":
        run = 'export GITHUB_STEP_SUMMARY="$RUNNER_TEMP/discarded-summary"\n' + run
    elif mutation == "swallow_refusal":
        run = "(\n" + run + "\n) || true"
    result = _run_deployment_shell(
        tmp_path,
        name,
        {
            "INSTAGRAM_USERNAME": "synthetic_user",
            "INSTAGRAM_SESSION_B64": base64.b64encode(b"synthetic-session").decode(),
            "MAX_NEW_POSTS": "5",
            "FULL_SCAN": "false",
            "BUDGET_LEVEL": level,
            "PYTHON": sys.executable,
        },
        fake,
        run_override=run,
    )
    expected_exit = 0 if level == "warning" else 31 if scope == "Snapshot" else 1
    assert result.returncode == expected_exit, result.stderr
    summary_path = tmp_path / "summary"
    assert summary_path.is_file(), "budget diagnostics never reached Actions summary"
    summary = summary_path.read_text()
    assert f"{scope} budget: {level}" in summary
    assert ("850000000" if level == "warning" else "900000000") in summary
    assert "media/PUBLIC/00.webp" in summary
    assert "Largest contributors" in summary
    assert "media/PUBLIC" not in result.stdout + result.stderr
    assert "synthetic-session" not in summary + result.stdout + result.stderr
    assert "c3ludGhldGlj" not in summary + result.stdout + result.stderr
    assert not (tmp_path / "runner/instagram.session").exists()


@pytest.mark.parametrize("scope", ["Snapshot", "Artifact"])
@pytest.mark.parametrize("level", ["warning", "reject"])
def test_production_budget_diagnostics_reach_summary_without_logging_paths(
    tmp_path: Path, scope: str, level: str
) -> None:
    """Break caught: production shell loses structured budget warnings/refusals or logs paths."""
    _budget_shell_probe(tmp_path, scope, level)


@pytest.mark.parametrize("scope", ["Snapshot", "Artifact"])
@pytest.mark.parametrize("mutation", ["discard_summary", "swallow_refusal"])
def test_budget_shell_probe_detects_summary_and_refusal_mutations(
    tmp_path: Path, scope: str, mutation: str
) -> None:
    with pytest.raises(AssertionError):
        _budget_shell_probe(tmp_path, scope, "reject", mutation)


def test_publication_budget_refusal_reaches_summary_without_logging_private_paths(
    tmp_path: Path,
) -> None:
    """Break caught: a final publisher budget refusal logs raw contributor paths after sync."""
    fake = """import os, sys
from pathlib import Path
if sys.argv[1:4] != ["-m", "sync.cli", "publish-snapshot"]:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
from functools import partial
import sync.cli as boundary
from sync.archive.publisher import publish_snapshot
from sync.archive.models import MAX_GENERATED_FILE_BYTES
from sync.archive.store import SnapshotStore
snapshot = Path(os.environ["SNAPSHOT"])
SnapshotStore(snapshot).initialize()
with (snapshot / "private_owner-PRIVATE_CODE.bin").open("wb") as file:
    file.truncate(MAX_GENERATED_FILE_BYTES + 1)
def forbidden_git(*args, **kwargs):
    raise AssertionError("Budget refusal must precede Git")
boundary.publish_snapshot = partial(publish_snapshot, run=forbidden_git)
sys.exit(boundary.main(sys.argv[3:]))
"""
    result = _run_deployment_shell(tmp_path, "Publish archive snapshot", {}, fake)
    assert result.returncode == 31
    summary_path = tmp_path / "summary"
    assert summary_path.is_file(), "publisher budget refusal never reached Actions summary"
    summary = summary_path.read_text()
    assert "Snapshot budget: reject" in summary
    assert "99614721 bytes" in summary
    for private in ("private_owner", "PRIVATE_CODE", "synthetic-github-token"):
        assert private not in summary + result.stdout + result.stderr
