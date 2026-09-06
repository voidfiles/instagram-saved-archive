"""Contracts for the offline pull-request and main verification workflow."""

from __future__ import annotations

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
