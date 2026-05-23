"""Command-line entry point for Cowork-CC-dispatch.

Subcommands are intentionally thin — each one parses arguments and delegates
to the existing core functions (`dispatch_one` / `run_chain` / `aggregate` +
`render_report`). The CLI persists run records to a JSON file so that
`ccd report` can read the most recent run, regardless of whether it came from
`dispatch` or `chain`.

spec_010 added crash-safe incremental persistence: the run JSON is updated
*before* each runner call (in-flight `RUNNING` marker) and again on
completion, atomically via `os.replace`. The orchestrator wraps every
spec in `try/except` so a `TimeoutExpired`, git error, or runner crash
becomes a `HALTED + INTERRUPTED` record on disk instead of a vanished run.

The runner is injectable on `main()` so tests can pass a `FakeAgentRunner`
without invoking the real `claude` CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ccd import __version__
from ccd.agent import AgentRunner, ClaudeCodeRunner
from ccd.chain import run_chain
from ccd.dashboard import render_to as render_dashboard_to
from ccd.dispatch import dispatch_one
from ccd.integrate import DEFAULT_SMOKE_COMMANDS
from ccd.metrics import aggregate, render_report
from ccd.models import DispatchRecord, DispatchStatus
from ccd.protocol import parse_spec
from ccd.run_writer import (
    RunWriter,
    halted_interrupted_record,
    reconcile_path,
)

DEFAULT_LAST_RUN_PATH = Path("_ai_workspace") / "logs" / "last_run.json"
DEFAULT_DASHBOARD_RUNS_PATH = Path("_ai_workspace") / "runs"
DEFAULT_DASHBOARD_OUTPUT_PATH = Path("docs") / "index.html"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccd",
        description="Cowork-CC-dispatch: orchestrate dispatches from one AI agent to another.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ccd {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_dispatch = sub.add_parser(
        "dispatch",
        help="Run a single spec through the agent.",
        description="Dispatch one spec to the agent and persist the resulting record.",
    )
    p_dispatch.add_argument("spec", type=Path, help="Path to a spec_NNN.md file.")
    p_dispatch.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repo working directory (default: current directory).",
    )
    p_dispatch.add_argument(
        "--save",
        type=Path,
        default=None,
        help=(
            "Where to write the run record JSON "
            f"(default: <repo>/{DEFAULT_LAST_RUN_PATH})."
        ),
    )
    p_dispatch.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Per-spec runner timeout in seconds (default: no timeout). "
            "Exceeding it produces a HALTED + INTERRUPTED record."
        ),
    )

    p_chain = sub.add_parser(
        "chain",
        help="Run multiple specs sequentially (halt on first failure).",
        description="Chain dispatch_one + integrate over multiple specs.",
    )
    p_chain.add_argument(
        "specs",
        nargs="+",
        type=Path,
        help="Paths to spec_NNN.md files, in execution order.",
    )
    p_chain.add_argument("--repo", type=Path, default=None)
    p_chain.add_argument(
        "--save",
        type=Path,
        default=None,
        help=(
            "Where to write the run record JSON "
            f"(default: <repo>/{DEFAULT_LAST_RUN_PATH})."
        ),
    )
    p_chain.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Per-spec runner timeout in seconds (default: no timeout). "
            "Exceeding it produces a HALTED + INTERRUPTED record and halts the chain."
        ),
    )

    p_report = sub.add_parser(
        "report",
        help="Render a metrics report from the most recent run.",
        description="Aggregate the saved run record into a Markdown metrics report.",
    )
    p_report.add_argument("--repo", type=Path, default=None)
    p_report.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        default=None,
        help=(
            "Path to the run JSON to read "
            f"(default: <repo>/{DEFAULT_LAST_RUN_PATH})."
        ),
    )

    p_dashboard = sub.add_parser(
        "dashboard",
        help="Render a static HTML dashboard from accumulated run JSON files.",
        description=(
            "Aggregate every run JSON under --runs-dir into a single "
            "self-contained HTML dashboard (inline SVG, no external resources)."
        ),
    )
    p_dashboard.add_argument("--repo", type=Path, default=None)
    p_dashboard.add_argument(
        "--runs-dir",
        dest="runs_dir",
        type=Path,
        default=None,
        help=(
            "Directory of run JSON files "
            f"(default: <repo>/{DEFAULT_DASHBOARD_RUNS_PATH})."
        ),
    )
    p_dashboard.add_argument(
        "--output",
        dest="output",
        type=Path,
        default=None,
        help=(
            "Output HTML path "
            f"(default: <repo>/{DEFAULT_DASHBOARD_OUTPUT_PATH})."
        ),
    )

    p_reconcile = sub.add_parser(
        "reconcile",
        help="Reconcile orphan RUNNING records to HALTED + INTERRUPTED.",
        description=(
            "Scan one run JSON file (or every *.json under a directory) "
            "and rewrite any 'running' record as 'halted' + 'interrupted'. "
            "finished_at is not invented."
        ),
    )
    p_reconcile.add_argument(
        "target",
        type=Path,
        help="Path to a run JSON file, or a directory of *.json files.",
    )

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: AgentRunner | None = None,
    smoke_commands: Sequence[Sequence[str]] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "dispatch":
        return _cmd_dispatch(args, runner)
    if args.command == "chain":
        return _cmd_chain(args, runner, smoke_commands)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "dashboard":
        return _cmd_dashboard(args)
    if args.command == "reconcile":
        return _cmd_reconcile(args)

    parser.print_help()
    return 0


def _cmd_dispatch(args: argparse.Namespace, runner: AgentRunner | None) -> int:
    repo = _resolve_repo(args.repo)
    spec = parse_spec(args.spec)
    timeout = getattr(args, "timeout", None)
    runner = runner if runner is not None else ClaudeCodeRunner(timeout=timeout)

    save_path = _resolve_save_path(args.save, repo)
    writer = RunWriter(save_path)
    writer.salvage_orphans()

    started_at = _now()
    writer.start(spec.id, started_at=started_at)
    try:
        record = dispatch_one(spec, runner, repo=repo)
    except Exception as exc:
        record = halted_interrupted_record(spec.id, started_at=started_at)
        writer.finish(record)
        print(_records_summary([record]))
        print(
            f"dispatch interrupted on {spec.id}: {_summarize_exception(exc)}",
            file=sys.stderr,
        )
        return 1

    writer.finish(record)
    print(_records_summary([record]))
    return 0 if record.status is DispatchStatus.DONE else 1


def _cmd_chain(
    args: argparse.Namespace,
    runner: AgentRunner | None,
    smoke_commands: Sequence[Sequence[str]] | None,
) -> int:
    repo = _resolve_repo(args.repo)
    specs = [parse_spec(p) for p in args.specs]
    timeout = getattr(args, "timeout", None)
    runner = runner if runner is not None else ClaudeCodeRunner(timeout=timeout)
    smoke = smoke_commands if smoke_commands is not None else DEFAULT_SMOKE_COMMANDS

    save_path = _resolve_save_path(args.save, repo)
    writer = RunWriter(save_path)
    writer.salvage_orphans()

    result = run_chain(
        specs,
        runner,
        repo=repo,
        smoke_commands=smoke,
        on_start=writer.start,
        on_finish=writer.finish,
    )
    writer.attach_chain(result)

    print(_records_summary([step.dispatch for step in result.steps]))
    if not result.success:
        reason = result.halt_reason or f"halted at {result.halted_at}"
        print(f"chain halted at {result.halted_at}: {reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo)
    path = args.from_path if args.from_path is not None else repo / DEFAULT_LAST_RUN_PATH
    path = path if path.is_absolute() else (repo / path).resolve()

    if not path.exists():
        print(f"no run record at {path}", file=sys.stderr)
        return 2

    records = _load_records(path)
    report = aggregate(records)
    print(render_report(report))
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo)
    runs_dir = _resolve_under_repo(args.runs_dir, repo, DEFAULT_DASHBOARD_RUNS_PATH)
    output = _resolve_under_repo(args.output, repo, DEFAULT_DASHBOARD_OUTPUT_PATH)

    written = render_dashboard_to(runs_dir, output)
    print(f"wrote {written}")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if not target.exists():
        print(f"no such file or directory: {target}", file=sys.stderr)
        return 2
    files, records = reconcile_path(target)
    print(f"reconciled {records} record(s) across {files} file(s)")
    return 0


def _resolve_under_repo(override: Path | None, repo: Path, default_rel: Path) -> Path:
    if override is None:
        return repo / default_rel
    override = Path(override)
    return override if override.is_absolute() else repo / override


def _resolve_repo(override: Path | None) -> Path:
    return Path(override).resolve() if override is not None else Path.cwd().resolve()


def _resolve_save_path(override: Path | None, repo: Path) -> Path:
    if override is None:
        return repo / DEFAULT_LAST_RUN_PATH
    override = Path(override)
    return override if override.is_absolute() else repo / override


def _now() -> datetime:
    return datetime.now(UTC)


def _summarize_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).strip()
    if not text:
        return name
    if len(text) > 200:
        text = text[:200] + "…"
    return f"{name}: {text}"


def _load_records(path: Path) -> list[DispatchRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("records", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'records' must be a list")
    return [DispatchRecord.model_validate(item) for item in raw]


def _records_summary(records: Sequence[DispatchRecord]) -> str:
    if not records:
        return "(no dispatches)"
    lines: list[str] = []
    for r in records:
        cat = f" [{r.failure_category.value}]" if r.failure_category is not None else ""
        lines.append(f"{r.spec_id}: {r.status.value}{cat}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
