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
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ccd import __version__
from ccd.agent import AgentRunner, ClaudeCodeRunner
from ccd.brief import run_brief
from ccd.chain import run_chain
from ccd.dashboard import render_to as render_dashboard_to
from ccd.discover import (
    DEFAULT_CHANNEL,
    SUPPORTED_CHANNELS,
    MutationRunner,
    run_channel,
)
from ccd.guard import DEFAULT_PROD_DIFF_LIMIT, fetch_diff, inspect_diff
from ccd.integrate import DEFAULT_SMOKE_COMMANDS
from ccd.metrics import aggregate, render_report
from ccd.models import DispatchRecord, DispatchStatus
from ccd.nightly import BriefRunner, ChannelRunner, WindowsMirror, run_nightly
from ccd.profile import load_profile_with_source, render_profile
from ccd.protocol import parse_spec
from ccd.retrospect import DEFAULT_LIMIT as DEFAULT_RETROSPECT_LIMIT
from ccd.retrospect import run_retrospect
from ccd.retry import dispatch_with_retry
from ccd.run_writer import (
    RunWriter,
    halted_interrupted_record,
    reconcile_path,
)

DEFAULT_CLI_MAX_ATTEMPTS = 3

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
    p_dispatch.add_argument(
        "--max-attempts",
        dest="max_attempts",
        type=int,
        default=DEFAULT_CLI_MAX_ATTEMPTS,
        help=(
            f"Maximum dispatch attempts (default: {DEFAULT_CLI_MAX_ATTEMPTS}). "
            "Retryable failures (smoke_failed / agent_misread / transient / "
            "interrupted) feed a feedback file into the next attempt's "
            "prompt. environment / merge_conflict / BLOCKED halt immediately."
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
    p_chain.add_argument(
        "--max-attempts",
        dest="max_attempts",
        type=int,
        default=DEFAULT_CLI_MAX_ATTEMPTS,
        help=(
            f"Maximum dispatch attempts per spec (default: "
            f"{DEFAULT_CLI_MAX_ATTEMPTS}). Same retryable / halt boundary "
            "as `ccd dispatch --max-attempts`."
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

    p_retrospect = sub.add_parser(
        "retrospect",
        help=(
            "Run a ccd self-retrospective — analyze dispatch history and "
            "emit improvement-proposal seeds (human-in-the-loop)."
        ),
        description=(
            "Gather run JSON / result_*.md / recent git history into a "
            "review-task spec, dispatch it through the same AgentRunner "
            "used for normal dispatches, and verify the agent produced "
            "_ai_workspace/retro/retro_NNN.md + proposals/*.md. The "
            "proposals are seeds — they are not auto-promoted to "
            "_ai_workspace/bridge/inbox/ and not auto-dispatched."
        ),
    )
    p_retrospect.add_argument("--repo", type=Path, default=None)
    p_retrospect.add_argument(
        "--runs-dir",
        dest="runs_dir",
        type=Path,
        default=None,
        help=(
            "Directory of run JSON files to scan "
            f"(default: <repo>/{DEFAULT_DASHBOARD_RUNS_PATH}). "
            "Legacy <repo>/_ai_workspace/logs/*_run.json are also picked up."
        ),
    )
    p_retrospect.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RETROSPECT_LIMIT,
        help=(
            f"Max recent commits to include in the evidence bundle "
            f"(default: {DEFAULT_RETROSPECT_LIMIT})."
        ),
    )

    p_discover = sub.add_parser(
        "discover",
        help=(
            "Run a mutation-testing discovery batch against ccd's code "
            "(v2 Phase 1, human-triggered)."
        ),
        description=(
            "Invoke the mutation tool (mutmut by default), normalize its "
            "raw output into stable Mutant records, compute a deterministic "
            "factual summary, split survivors against "
            "_ai_workspace/discover/blocklist.txt, and write a discovery "
            "report (_ai_workspace/discover/discover_NNN.md + .json). "
            "Phase 1 only — no auto-fix, no scheduler. The report surfaces "
            "test gaps for a human to read."
        ),
    )
    p_discover.add_argument("--repo", type=Path, default=None)
    p_discover.add_argument(
        "--channel",
        choices=SUPPORTED_CHANNELS,
        default=DEFAULT_CHANNEL,
        help=(
            f"Discovery channel (default: {DEFAULT_CHANNEL}). "
            "`mutation` runs mutmut against ccd's tests (spec_013). "
            "`adversarial` feeds CCD's parsers a curated catalog of broken "
            "inputs and reports any ungraceful crash (spec_015). "
            "`ai` asks an AI agent to read ccd/ and surface semantic concerns "
            "(spec_016, report-only — claims, not verified facts)."
        ),
    )
    p_discover.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help=(
            "Paths to mutate (default: ccd). Mutation channel only — "
            "ignored for `--channel adversarial`."
        ),
    )

    p_brief = sub.add_parser(
        "brief",
        help=(
            "Render the Phase-1 morning report from accumulated discover_NNN "
            "JSON files (spec_017)."
        ),
        description=(
            "Pure renderer (no channel execution): collect the latest "
            "discover_NNN.json per channel (mutation / adversarial / ai), "
            "compute a deterministic cross-channel summary, and write a "
            "6-section morning report to "
            "_ai_workspace/nightly/report_YYYY-MM-DD.md. "
            "Mechanical findings (facts) and AI-inference findings (claims) "
            "are visually separated; the report states explicitly that "
            "Phase 1 performs no autonomous fixes."
        ),
    )
    p_brief.add_argument("--repo", type=Path, default=None)
    p_brief.add_argument(
        "--inputs",
        nargs="+",
        type=Path,
        default=None,
        help=(
            "Explicit discover_NNN.json paths (one or more). When omitted, "
            "the brief auto-discovers the latest report per channel under "
            "<repo>/_ai_workspace/discover/."
        ),
    )

    p_profile = sub.add_parser(
        "profile",
        help=(
            "Load and display the effective ccd profile (spec_018, v2 "
            "Phase 1)."
        ),
        description=(
            "Read the TOML profile (default: "
            "<repo>/_ai_workspace/ccd_profile.toml) and print the effective "
            "profile with all defaults filled in. Reports whether the "
            "profile was loaded from a file or assembled from defaults. "
            "Profile parse / schema errors are surfaced with a non-zero "
            "exit. The profile is consumed by the scheduler (spec_019); "
            "existing subcommands are not rewired by spec_018."
        ),
    )
    p_profile.add_argument("--repo", type=Path, default=None)
    p_profile.add_argument(
        "--profile",
        dest="profile_path",
        type=Path,
        default=None,
        help=(
            "Explicit profile TOML path. When omitted, the loader looks "
            "at <repo>/_ai_workspace/ccd_profile.toml."
        ),
    )

    p_nightly = sub.add_parser(
        "nightly",
        help=(
            "Run the Phase-1 nightly orchestration: discovery channels + "
            "morning report + Windows mirror (spec_020)."
        ),
        description=(
            "Linear orchestrator that drives the profile's enabled "
            "discovery channels (mutation / adversarial / ai) in order, "
            "then renders the morning report (ccd brief) and mirrors it to "
            "a Windows-visible path so the operator can read it without "
            "entering WSL. Phase 1 is discovery-only — nightly does NOT "
            "merge, push, or rewrite history; pre-flight is intentionally "
            "light (the full HEAD/clean checks belong in Phase 2 where "
            "auto-fix writes to the live repo)."
        ),
    )
    p_nightly.add_argument("--repo", type=Path, default=None)
    p_nightly.add_argument(
        "--profile",
        dest="profile_path",
        type=Path,
        default=None,
        help=(
            "Explicit profile TOML path. When omitted, the loader looks "
            "at <repo>/_ai_workspace/ccd_profile.toml; if that's absent "
            "too, the all-defaults profile (mutation + adversarial + ai) "
            "is used."
        ),
    )

    p_guard = sub.add_parser(
        "guard",
        help=(
            "Inspect a git diff for fraudulent-fix patterns "
            "(v2 Phase 2 — spec_021)."
        ),
        description=(
            "Run the static guard against `git diff <base>..<head>`: "
            "R1 (file allowlist), R2 (tests/ is append-only — no removed "
            "lines, no skip/xfail markers), R3 (template B production "
            "diff bound). A hardcoded self-protection denylist (the "
            "guard itself, scheduler modules, CI config, packaging, "
            "discovery config) overrides any caller allowlist. The "
            "dynamic rules R4 (suite green) / R5 (target test killed) "
            "are NOT in this command — they live in the loop wiring "
            "(spec_023). Exit code is 0 on pass and 1 on HALT."
        ),
    )
    p_guard.add_argument("--repo", type=Path, default=None)
    p_guard.add_argument(
        "--base",
        default="main",
        help="git ref to diff from (default: main).",
    )
    p_guard.add_argument(
        "--head",
        default="HEAD",
        help="git ref to diff to (default: HEAD).",
    )
    p_guard.add_argument(
        "--template",
        choices=["A", "B"],
        required=True,
        help=(
            "Fix template. A = tests-only (allowed_files conventionally "
            "tests/). B = one named production file + tests/."
        ),
    )
    p_guard.add_argument(
        "--allowed",
        nargs="+",
        default=[],
        help=(
            "Allowed file(s) / directory prefix(es). Example: "
            "`--allowed tests/` for template A, "
            "`--allowed ccd/foo.py tests/` for template B."
        ),
    )
    p_guard.add_argument(
        "--max-prod-diff-lines",
        dest="max_prod_diff_lines",
        type=int,
        default=DEFAULT_PROD_DIFF_LIMIT,
        help=(
            f"R3 threshold for template B (default: "
            f"{DEFAULT_PROD_DIFF_LIMIT} +/- lines summed across all "
            "non-test files)."
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
    mutation_runner: MutationRunner | None = None,
    smoke_commands: Sequence[Sequence[str]] | None = None,
    channel_runner: ChannelRunner | None = None,
    brief_runner: BriefRunner | None = None,
    windows_mirror: WindowsMirror | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "dispatch":
        return _cmd_dispatch(args, runner, smoke_commands)
    if args.command == "chain":
        return _cmd_chain(args, runner, smoke_commands)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "dashboard":
        return _cmd_dashboard(args)
    if args.command == "retrospect":
        return _cmd_retrospect(args, runner)
    if args.command == "discover":
        return _cmd_discover(args, mutation_runner, runner)
    if args.command == "brief":
        return _cmd_brief(args)
    if args.command == "profile":
        return _cmd_profile(args)
    if args.command == "nightly":
        return _cmd_nightly(
            args,
            channel_runner=channel_runner,
            brief_runner=brief_runner,
            windows_mirror=windows_mirror,
        )
    if args.command == "guard":
        return _cmd_guard(args)
    if args.command == "reconcile":
        return _cmd_reconcile(args)

    parser.print_help()
    return 0


def _cmd_dispatch(
    args: argparse.Namespace,
    runner: AgentRunner | None,
    smoke_commands: Sequence[Sequence[str]] | None,
) -> int:
    repo = _resolve_repo(args.repo)
    spec = parse_spec(args.spec)
    timeout = getattr(args, "timeout", None)
    max_attempts = getattr(args, "max_attempts", DEFAULT_CLI_MAX_ATTEMPTS)
    runner = runner if runner is not None else ClaudeCodeRunner(timeout=timeout)
    smoke = smoke_commands if smoke_commands is not None else DEFAULT_SMOKE_COMMANDS

    save_path = _resolve_save_path(args.save, repo)
    writer = RunWriter(save_path)
    writer.salvage_orphans()

    started_at = _now()
    writer.start(spec.id, started_at=started_at)
    try:
        record = dispatch_with_retry(
            spec,
            runner,
            repo=repo,
            max_attempts=max_attempts,
            smoke_commands=smoke,
        )
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
    max_attempts = getattr(args, "max_attempts", DEFAULT_CLI_MAX_ATTEMPTS)
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
        max_attempts=max_attempts,
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


def _cmd_retrospect(
    args: argparse.Namespace,
    runner: AgentRunner | None,
) -> int:
    repo = _resolve_repo(args.repo)
    runs_dir = args.runs_dir
    if runs_dir is not None and not Path(runs_dir).is_absolute():
        runs_dir = repo / runs_dir
    limit = getattr(args, "limit", DEFAULT_RETROSPECT_LIMIT)
    runner = runner if runner is not None else ClaudeCodeRunner()

    result = run_retrospect(
        runner,
        repo=repo,
        runs_dir=runs_dir,
        limit=limit,
    )

    print(f"review spec: {result.review_spec_path}")
    print(
        "factual summary: "
        f"runs={result.summary.runs_scanned} "
        f"records={result.summary.records_total} "
        f"results={result.summary.result_files} "
        f"commits={result.summary.recent_commits}"
    )
    if result.retro_path is not None:
        print(f"retrospective: {result.retro_path}")
    for pp in result.proposal_paths:
        print(f"proposal: {pp}")

    if not result.success:
        print(f"retrospect halted: {result.halt_reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_discover(
    args: argparse.Namespace,
    mutation_runner: MutationRunner | None,
    agent_runner: AgentRunner | None,
) -> int:
    repo = _resolve_repo(args.repo)
    channel = getattr(args, "channel", DEFAULT_CHANNEL)
    paths = list(args.paths) if args.paths else None

    result = run_channel(
        channel,
        repo=repo,
        paths=paths,
        mutation_runner=mutation_runner,
        agent_runner=agent_runner,
    )

    if not result.success:
        print(f"discovery halted: {result.halt_reason}", file=sys.stderr)
        return 1

    assert result.report_md_path is not None
    assert result.report_json_path is not None
    print(f"discovery report: {result.report_md_path}")
    print(f"discovery json:   {result.report_json_path}")

    if channel == "mutation":
        summary = result.summary
        print(
            "factual summary: "
            f"mutants={summary.mutants_total} "
            f"survived={summary.survived_total} "
            f"actionable={summary.actionable_total} "
            f"blocklisted={summary.blocklisted_total}"
        )
        for m in result.actionable_mutants:
            print(f"actionable: {m.file}:{m.line} — {m.mutation}")
        return 0

    if channel == "adversarial":
        summary = result.summary
        print(
            "factual summary: "
            f"parsers={len(summary.parsers)} "
            f"cases={summary.cases_total} "
            f"evaluations={summary.evaluations_total} "
            f"graceful={summary.graceful_total} "
            f"ungraceful={summary.ungraceful_total}"
        )
        for f in result.findings:
            print(
                f"ungraceful: {f.parser} × {f.case_name} — "
                f"{f.exception_type}: {f.exception_message}"
            )
        return 0

    # ai channel (spec_016) — report-only, claims not facts.
    summary = result.summary
    print("note: ai channel is report-only — findings are claims, not verified facts")
    print(
        "factual summary: "
        f"target={summary.target_package} "
        f"files={summary.files_total} "
        f"findings={summary.findings_total} (non-deterministic)"
    )
    for f in result.findings:
        print(f"finding: {f.location} — {f.concern}")
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo)
    inputs = list(args.inputs) if args.inputs else None

    result = run_brief(repo=repo, inputs=inputs)

    if not result.success:
        print(f"brief halted: {result.halt_reason}", file=sys.stderr)
        return 1

    assert result.report_path is not None
    print(f"morning report: {result.report_path}")
    summary = result.summary
    print(
        "factual summary: "
        f"mechanical={summary.mechanical_findings_total} "
        f"(mutation={summary.mutation_actionable}, "
        f"adversarial={summary.adversarial_ungraceful}) "
        f"ai={summary.ai_findings} (report-only)"
    )
    if summary.channels_missing:
        missing = ", ".join(summary.channels_missing)
        print(f"channels not yet executed: {missing}")
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo)
    profile_path = getattr(args, "profile_path", None)
    try:
        result = load_profile_with_source(repo, profile_path)
    except ValueError as exc:
        print(f"profile error: {exc}", file=sys.stderr)
        return 1
    print(render_profile(result))
    return 0


def _cmd_nightly(
    args: argparse.Namespace,
    *,
    channel_runner: ChannelRunner | None,
    brief_runner: BriefRunner | None,
    windows_mirror: WindowsMirror | None,
) -> int:
    repo = _resolve_repo(args.repo)
    profile_path = getattr(args, "profile_path", None)

    result = run_nightly(
        repo=repo,
        profile_path=profile_path,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=windows_mirror,
    )

    print("channels executed: " + (", ".join(result.channels_executed) or "(none)"))
    for co in result.channels_run:
        status = "ok" if co.success else f"halted ({co.halt_reason or 'no reason'})"
        print(f"  - {co.channel}: {status}")

    if result.brief_report_wsl is not None:
        print(f"morning report (wsl):     {result.brief_report_wsl}")
    if result.brief_report_windows is not None:
        print(f"morning report (windows): {result.brief_report_windows}")
    elif result.brief_report_wsl is not None:
        print("morning report (windows): (mirror declined — /mnt/c unavailable)")

    if not result.success:
        print(f"nightly halted: {result.halt_reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_guard(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo)
    base = args.base
    head = args.head
    template = args.template
    allowed = list(args.allowed) if args.allowed else []
    max_prod = args.max_prod_diff_lines

    try:
        diff_text = fetch_diff(repo, base, head)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        print(f"guard halted: git diff failed: {stderr}", file=sys.stderr)
        return 1

    result = inspect_diff(
        diff=diff_text,
        allowed_files=allowed,
        template=template,
        max_prod_diff_lines=max_prod,
    )

    print(f"template: {template}")
    print(f"diff range: {base}..{head}")
    if result.files_touched:
        print("files touched:")
        for f in result.files_touched:
            print(f"  - {f}")
    else:
        print("files touched: (none)")

    if result.passed:
        print("guard: pass")
        return 0

    print(f"guard: HALT ({len(result.halt_reasons)} reason(s))", file=sys.stderr)
    for r in result.halt_reasons:
        print(f"  - {r}", file=sys.stderr)
    return 1


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
