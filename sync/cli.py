"""Stable JSON command boundary; human diagnostics never include upstream exceptions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from sync.archive.budget import BudgetStatus, check_budget
from sync.archive.publisher import publish_snapshot
from sync.archive.store import SnapshotStore
from sync.bootstrap_session import bootstrap_session
from sync.engine import SyncEngine, SyncOptions, recover_snapshot
from sync.instagram.client import InstaloaderClient
from sync.instagram.errors import ArchiveError, AuthenticationError, SizeError, ValidationError
from sync.instagram.retry import RetryPolicy, retry_transport
from sync.reporting import render_job_summary
from sync.site_input.exporter import export_site_input, resolve_plain_path, validate_snapshot


class _ArgumentsError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default error includes rejected argument values, possibly secrets.
        raise _ArgumentsError


def _bounded_integer(minimum: int, maximum: int | None = None) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            integer = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("expected integer") from None
        if integer < minimum or (maximum is not None and integer > maximum):
            raise argparse.ArgumentTypeError("integer is outside permitted bounds")
        return integer

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="instagram-saved-archive", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-session", allow_abbrev=False)
    bootstrap.add_argument("--username", required=True)
    for name in (
        "init-snapshot",
        "validate-snapshot",
        "sync",
        "prepare-site-input",
        "publish-snapshot",
    ):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--snapshot", type=Path, required=True)
        if name == "prepare-site-input":
            command.add_argument("--destination", type=Path, required=True)
        elif name == "publish-snapshot":
            command.add_argument("--source-repository", type=Path, required=True)
            command.add_argument("--expected-repository", required=True)
            command.add_argument("--remote-url", required=True)
        elif name == "sync":
            command.add_argument("--session", type=Path, required=True)
            command.add_argument("--username", default=os.environ.get("INSTAGRAM_USERNAME"))
            command.add_argument("--removals", type=Path, required=True)
            command.add_argument("--max-new-posts", type=_bounded_integer(1, 50), default=50)
            command.add_argument("--known-threshold", type=_bounded_integer(1), default=20)
            command.add_argument("--reconcile-limit", type=_bounded_integer(1, 20), default=20)
            command.add_argument("--full-scan", action="store_true")
    budget = commands.add_parser("check-budget", allow_abbrev=False)
    budget.add_argument("--root", type=Path, required=True)
    return parser


def _budget_json(budget: BudgetStatus) -> dict[str, object]:
    return {
        "level": budget.level,
        "total_bytes": budget.total_bytes,
        "largest": [
            {"path": item.path.as_posix(), "size_bytes": item.size_bytes} for item in budget.largest
        ],
    }


def _execute(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    result: dict[str, object] = {"status": "ok", "command": args.command}
    diagnostic = "Archive operation completed.\n"
    code = 0
    if args.command == "check-budget":
        root = resolve_plain_path(args.root)
        if not root.is_dir():
            raise ValidationError("budget root must be a directory")
        budget = check_budget(root)
        result["budget"] = _budget_json(budget)
        if budget.level == "reject":
            result["status"] = "error"
            code = 31
            diagnostic = "Publication size budget exceeded.\n"
        elif budget.level == "warning":
            diagnostic = "Warning: archive is approaching the publication size budget.\n"
    elif args.command == "init-snapshot":
        snapshot = resolve_plain_path(args.snapshot, must_exist=False)
        if snapshot.exists() and (not snapshot.is_dir() or any(snapshot.iterdir())):
            validate_snapshot(snapshot)
        else:
            snapshot.parent.resolve(strict=True)
            SnapshotStore(snapshot).initialize()
    elif args.command == "validate-snapshot":
        manifest, _ = validate_snapshot(args.snapshot)
        result["post_count"] = len(manifest.posts)
    elif args.command == "prepare-site-input":
        export_site_input(args.snapshot, args.destination)
    elif args.command == "publish-snapshot":
        result["commit"] = publish_snapshot(
            args.snapshot,
            args.source_repository,
            args.expected_repository,
            args.remote_url,
            os.environ,
        )
    elif args.command == "sync":
        snapshot = resolve_plain_path(args.snapshot, must_exist=False)
        recover_snapshot(snapshot)
        session = resolve_plain_path(args.session)
        removals = resolve_plain_path(args.removals)
        if not session.is_file() or not removals.is_file():
            raise ValidationError("session and removals must be files")
        validate_snapshot(snapshot)
        if not args.username:
            raise AuthenticationError("Instagram username is required")
        client = InstaloaderClient(session)
        policy = RetryPolicy()
        retry_transport(lambda: client.validate_identity(args.username), policy)
        options = SyncOptions(
            args.max_new_posts, args.full_scan, args.known_threshold, args.reconcile_limit
        )
        engine = SyncEngine(client)
        report = retry_transport(
            lambda: engine.run(snapshot, removals, datetime.now(UTC), options), policy
        )
        report_json = asdict(report)
        report_json.pop("snapshot_budget")
        result["report"] = report_json
        if report.snapshot_budget is not None:
            result["budget"] = _budget_json(report.snapshot_budget)
        diagnostic = render_job_summary(report)
    result["exit_code"] = code
    return result, diagnostic, code


def main(argv: list[str] | None = None) -> int:
    """Execute one command; argument failures are 2, operational failures use ArchiveError codes."""
    arguments = sys.argv[1:] if argv is None else argv
    is_bootstrap = arguments[:1] == ["bootstrap-session"]
    try:
        args = _parser().parse_args(arguments)
        if is_bootstrap:
            # Instaloader prints interactive prompts and save notices to stdout.
            # Keep the secret pipe reserved for the completed encoded credential.
            try:
                with redirect_stdout(sys.stderr):
                    encoded = bootstrap_session(args.username)
            except (EOFError, KeyboardInterrupt):
                raise AuthenticationError() from None
            print(encoded)
            return 0
        result, diagnostic, code = _execute(args)
    except _ArgumentsError:
        code = 2
        result = {"status": "error", "exit_code": code}
        diagnostic = "Invalid command arguments. Use --help for accepted arguments and bounds.\n"
    except ArchiveError as failure:
        code = failure.exit_code
        result = {"status": "error", "exit_code": code}
        diagnostic = render_job_summary(None, failure)
        if isinstance(failure, SizeError) and failure.budget is not None:
            result["budget"] = _budget_json(failure.budget)
    except (OSError, ValueError):
        code = 30
        result = {"status": "error", "exit_code": code}
        diagnostic = render_job_summary(None, ValidationError())
    except Exception:  # noqa: BLE001 -- command boundary must not leak upstream secrets in tracebacks.
        code = 1
        result = {"status": "error", "exit_code": code}
        diagnostic = "Archive operation failed unexpectedly.\n"
    if not is_bootstrap:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(diagnostic, file=sys.stderr, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
