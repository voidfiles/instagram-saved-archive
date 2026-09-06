"""Contracts for the offline pull-request and main verification workflow."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "test.yml"


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


def _workflow() -> Mapping[object, object]:
    assert WORKFLOW_PATH.is_file(), "offline CI workflow must exist"
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return _mapping(parsed)


def _steps(workflow: Mapping[object, object]) -> list[Mapping[object, object]]:
    jobs = _mapping(_value(workflow, "jobs"))
    assert list(jobs) == ["test"], "CI has one deterministic test job"
    job = _mapping(jobs["test"])
    assert str(_value(job, "runs-on")).startswith("ubuntu")
    assert _positive_timeout(_value(job, "timeout-minutes"))
    steps = _value(job, "steps")
    assert isinstance(steps, list)
    normalized = [_mapping(step) for step in steps]
    assert all(_positive_timeout(_value(step, "timeout-minutes")) for step in normalized)
    return normalized


def _step_with_use(steps: list[Mapping[object, object]], use: str) -> Mapping[object, object]:
    return next(step for step in steps if step.get("uses") == use)


def test_workflow_runs_the_offline_archive_and_site_gates_in_deterministic_order() -> None:
    """Break caught: a PR can skip, reorder, or network-enable an archive/site verification gate."""
    workflow = _workflow()
    triggers = _mapping(_value(workflow, "on"))
    assert "pull_request" in triggers
    push = _mapping(_value(triggers, "push"))
    assert _value(push, "branches") == ["main"]

    steps = _steps(workflow)
    assert _step_with_use(steps, "actions/checkout@v7")
    python_setup = _step_with_use(steps, "actions/setup-python@v7")
    assert _value(_mapping(_value(python_setup, "with")), "python-version") == "3.12"
    assert (
        _value(_mapping(_value(python_setup, "with")), "cache-dependency-path") == "pyproject.toml"
    )
    node_setup = _step_with_use(steps, "actions/setup-node@v7")
    assert _value(_mapping(_value(node_setup, "with")), "node-version") == "24"
    assert (
        _value(_mapping(_value(node_setup, "with")), "cache-dependency-path")
        == "site/package-lock.json"
    )

    commands = "\n".join(str(step.get("run", "")) for step in steps)
    required = [
        "ffmpeg -version",
        'python -m pip install -e ".[dev]"',
        "python -m ruff format --check .",
        "python -m ruff check .",
        "python -m mypy sync",
        "python -m pytest -q",
        "python tests/fixtures/build_sample_archive.py --output .tmp/sample-archive",
        "python -m sync.cli validate-snapshot --snapshot .tmp/sample-archive",
        "python -m sync.cli prepare-site-input --snapshot .tmp/sample-archive --destination site/public/archive",
        "npm ci",
        "npm run check",
        "npm test",
        "npm run build",
        "npm run check:static",
        "npx playwright install chromium",
        "npm run test:e2e",
    ]
    positions = [commands.index(command) for command in required]
    assert positions == sorted(positions)
    assert "python -m sync.cli sync" not in commands


def test_workflow_uses_read_only_permissions_and_keeps_failure_diagnostics_short_lived() -> None:
    """Break caught: offline checks gain credentials, write privileges, or persistent browser artifacts."""
    workflow = _workflow()
    assert _value(workflow, "permissions") == {"contents": "read"}
    steps = _steps(workflow)
    diagnostics = _step_with_use(steps, "actions/upload-artifact@v4")
    assert _value(diagnostics, "if") == "failure()"
    diagnostic_options = _mapping(_value(diagnostics, "with"))
    assert _value(diagnostic_options, "retention-days") == 3

    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    forbidden = ("INSTAGRAM_SESSION_B64", "password", "set -x", "printenv", "env |")
    assert not any(value.lower() in raw.lower() for value in forbidden)
    assert "write" not in raw.lower()
