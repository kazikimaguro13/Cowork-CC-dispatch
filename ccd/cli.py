"""Command-line entry point for Cowork-CC-dispatch.

Subcommands are intentionally thin — each one parses arguments and delegates
to the existing core functions (`dispatch_one` / `run_chain` / `aggregate` +
`render_report`). The CLI persists run records to a JSON file so that
`ccd report` can read the most recent run, regardless of whether it came from
`dispatch` or `chain`.

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
from ccd.chain import ChainResult, run_chain
from ccd.dashboard import render_to as render_dashboard_to
from ccd.dispatch import dispatch_one
from ccd.integrate import DEFAULT_SMOKE_COMMANDS
from ccd.metrics import aggregate, render_report
from ccd.models import DispatchRecord, DispatchStatus
from ccd.protocol import parse_spec

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

    parser.print_help()
    return 0


def _cmd_dispatch(args: argparse.Namespace, runner: AgentRunner | None) -> int:
    repo = _resolve_repo(args.repo)
    spec = parse_spec(args.spec)
    runner = runner if runner is not None else ClaudeCodeRunner()

    record = dispatch_one(spec, runner, repo=repo)

    save_path = _resolve_save_path(args.save, repo)
    _save_run(save_path, records=[record], chain=None)

    print(_records_summary([record]))
    return 0 if record.status is DispatchStatus.DONE else 1


def _cmd_chain(
    args: argparse.Namespace,
    runner: AgentRunner | None,
    smoke_commands: Sequence[Sequence[str]] | None,
) -> int:
    repo = _resolve_repo(args.repo)
    specs = [parse_spec(p) for p in args.specs]
    runner = runner if runner is not None else ClaudeCodeRunner()
    smoke = smoke_commands if smoke_commands is not None else DEFAULT_SMOKE_COMMANDS

    result = run_chain(specs, runner, repo=repo, smoke_commands=smoke)
    records = [step.dispatch for step in result.steps]

    save_path = _resolve_save_path(args.save, repo)
    _save_run(save_path, records=records, chain=result)

    print(_records_summary(records))
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


def _save_run(
    path: Path,
    *,
    records: Sequence[DispatchRecord],
    chain: ChainResult | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 1,
        "saved_at": datetime.now(UTC).isoformat(),
        "records": [r.model_dump(mode="json") for r in records],
    }
    if chain is not None:
        payload["chain"] = {
            "success": chain.success,
            "halted_at": chain.halted_at,
            "halt_reason": chain.halt_reason,
            "branches": [step.branch for step in chain.steps],
        }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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
