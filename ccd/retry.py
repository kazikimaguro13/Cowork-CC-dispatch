"""Self-healing retry loop around `dispatch_one` (spec_011).

`dispatch_with_retry` is the "AI managing AI" loop: when a dispatch fails in
a retryable way (smoke regression, agent misread, transient/interrupted),
the failure is summarized to a Markdown feedback file and fed into the next
attempt's prompt so the agent can diagnose and fix its own previous attempt.

Boundary:

  - retryable (loop continues)   = smoke_failed / agent_misread / transient
                                    / interrupted / None (unclassified)
  - immediate halt (loop stops)  = environment / merge_conflict / BLOCKED

`subprocess.TimeoutExpired` raised through `dispatch_one` is treated as a
retryable interrupted failure (the loop notes "the previous attempt timed
out" in the feedback file). Any other unhandled exception propagates to the
caller (`run_chain` / `_cmd_dispatch`) so spec_010's exception-safe
`HALTED + INTERRUPTED` path keeps owning truly unexpected crashes.

The returned `DispatchRecord` reflects the *last* attempt's status /
failure_category. `attempts` is the actual number of tries. `started_at`
is the first attempt's start, `finished_at` is the last attempt's finish.
`intervention=False`: an automatic retry is not a human intervention —
keeping it False is what makes `autonomous_completion_rate` count
retry-recovered specs as autonomously completed, as intended.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .agent import AgentRunner
from .dispatch import dispatch_one
from .integrate import DEFAULT_SMOKE_COMMANDS, SmokeResult, run_smoke
from .models import DispatchRecord, DispatchStatus, FailureCategory, Spec

DEFAULT_FEEDBACK_DIR_REL = Path("_ai_workspace") / "logs"

_RETRYABLE_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.SMOKE_FAILED,
        FailureCategory.AGENT_MISREAD,
        FailureCategory.TRANSIENT,
        FailureCategory.INTERRUPTED,
    }
)

_HALT_ON_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.ENVIRONMENT,
        FailureCategory.MERGE_CONFLICT,
    }
)

_FEEDBACK_EXCERPT_LIMIT = 4 * 1024  # head + tail per command stream
_RESULT_BODY_EXCERPT = 800


def dispatch_with_retry(
    spec: Spec,
    runner: AgentRunner,
    *,
    repo: Path,
    max_attempts: int = 1,
    smoke_commands: Sequence[Sequence[str]] = DEFAULT_SMOKE_COMMANDS,
    feedback_dir: Path | None = None,
) -> DispatchRecord:
    """Run ``dispatch_one`` + smoke up to ``max_attempts`` times with feedback.

    The library-default ``max_attempts=1`` reproduces single-shot behavior
    (no retry) — callers (`run_chain`, `_cmd_dispatch`) opt in by passing
    a larger value. The CLI's ``--max-attempts`` default is 3.
    """

    repo = Path(repo)
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    fb_dir = (
        Path(feedback_dir)
        if feedback_dir is not None
        else repo / DEFAULT_FEEDBACK_DIR_REL
    )

    first_started_at: datetime | None = None
    last_record: DispatchRecord | None = None
    feedback_path_for_next: Path | None = None

    for attempt in range(1, max_attempts + 1):
        attempt_started_at = datetime.now(UTC)
        try:
            record = dispatch_one(
                spec, runner, repo=repo, feedback=feedback_path_for_next
            )
            timeout_failure = False
        except subprocess.TimeoutExpired:
            # Surface the timeout as a retryable interrupted failure: we know
            # *what* happened (the runner was killed by timeout), unlike a
            # crash from an unknown cause which we deliberately propagate.
            record = _timeout_record(spec.id, started_at=attempt_started_at)
            timeout_failure = True

        if first_started_at is None:
            first_started_at = record.started_at

        smoke_result: SmokeResult | None = None
        if record.status is DispatchStatus.DONE:
            smoke_result = run_smoke(repo, smoke_commands)
            if not smoke_result.passed:
                # Override the otherwise-DONE record with smoke_failed: the
                # agent claimed success but the tree doesn't pass.
                record = record.model_copy(
                    update={
                        "status": DispatchStatus.FAILED,
                        "failure_category": FailureCategory.SMOKE_FAILED,
                    }
                )

        last_record = record
        if record.status is DispatchStatus.DONE:
            break

        if attempt >= max_attempts:
            break

        if not _is_retryable(record):
            break

        feedback_path_for_next = _write_feedback(
            spec_id=spec.id,
            repo=repo,
            feedback_dir=fb_dir,
            attempt=attempt,
            record=record,
            smoke=smoke_result,
            timed_out=timeout_failure,
        )

    assert last_record is not None  # the loop body always assigns at least once
    assert first_started_at is not None

    return DispatchRecord(
        spec_id=spec.id,
        started_at=first_started_at,
        finished_at=last_record.finished_at,
        status=last_record.status,
        attempts=attempt,
        failure_category=last_record.failure_category,
        intervention=False,
    )


def _is_retryable(record: DispatchRecord) -> bool:
    if record.status is DispatchStatus.BLOCKED:
        return False
    cat = record.failure_category
    if cat is None:
        return True
    if cat in _HALT_ON_CATEGORIES:
        return False
    return cat in _RETRYABLE_CATEGORIES


def _timeout_record(spec_id: str, *, started_at: datetime) -> DispatchRecord:
    """Synthesize a HALTED + INTERRUPTED record for a runner timeout.

    ``finished_at`` is left ``None`` (we don't know exactly when the runner
    was killed; the timeout was the bound, not the actual finish).
    """

    return DispatchRecord(
        spec_id=spec_id,
        started_at=started_at,
        finished_at=None,
        status=DispatchStatus.HALTED,
        attempts=1,
        failure_category=FailureCategory.INTERRUPTED,
        intervention=False,
    )


def _write_feedback(
    *,
    spec_id: str,
    repo: Path,
    feedback_dir: Path,
    attempt: int,
    record: DispatchRecord,
    smoke: SmokeResult | None,
    timed_out: bool,
) -> Path:
    """Write a human-readable Markdown feedback file for the next attempt."""

    feedback_dir.mkdir(parents=True, exist_ok=True)
    path = feedback_dir / f"{spec_id}.feedback.md"

    cat = (
        record.failure_category.value
        if record.failure_category is not None
        else "(unclassified)"
    )

    lines: list[str] = [
        f"# Retry feedback for {spec_id}",
        "",
        f"- Attempt that just failed: {attempt} of N",
        f"- Status: {record.status.value}",
        f"- Failure category: {cat}",
        "",
    ]

    if timed_out:
        lines.extend(
            [
                "## Previous attempt timed out",
                "",
                "前回の試行は subprocess.TimeoutExpired により打ち切られました。",
                "実装に時間がかかりすぎたか、ハングした可能性があります。",
                "より小さい単位で進めるか、原因を切り分けてください。",
                "",
            ]
        )

    if smoke is not None and not smoke.passed:
        lines.append("## Smoke output")
        lines.append("")
        for cmd in smoke.commands:
            argv_str = " ".join(cmd.argv)
            lines.append(f"### `{argv_str}` (exit {cmd.returncode})")
            lines.append("")
            stdout_excerpt = _excerpt(cmd.stdout, _FEEDBACK_EXCERPT_LIMIT)
            stderr_excerpt = _excerpt(cmd.stderr, _FEEDBACK_EXCERPT_LIMIT)
            if stdout_excerpt:
                lines.extend(["**stdout**", "", "```", stdout_excerpt, "```", ""])
            if stderr_excerpt:
                lines.extend(["**stderr**", "", "```", stderr_excerpt, "```", ""])

    result_excerpt = _previous_result_excerpt(repo=repo, spec_id=spec_id)
    if result_excerpt:
        lines.extend(
            [
                "## Previous result file (excerpt)",
                "",
                "```",
                result_excerpt,
                "```",
                "",
            ]
        )

    lines.extend(
        [
            "## Next attempt",
            "",
            "前回の作業はフィーチャーブランチに残っているので、",
            "それを土台に原因を直してから再実装してください。",
            "（HEAD を捨てて最初からやり直す必要はありません — diff を読み、",
            "落ちた箇所だけ修正する形で進めてください。）",
            "",
        ]
    )

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _previous_result_excerpt(*, repo: Path, spec_id: str) -> str:
    """Return the first ~800 chars of the agent's previous result file, if any."""

    suffix = spec_id[len("spec_") :] if spec_id.startswith("spec_") else spec_id
    result_path = (
        repo / "_ai_workspace" / "bridge" / "outbox" / f"result_{suffix}.md"
    )
    if not result_path.exists():
        return ""
    try:
        text = result_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    text = text.strip()
    if len(text) <= _RESULT_BODY_EXCERPT:
        return text
    return text[:_RESULT_BODY_EXCERPT] + "\n…[truncated]…"


def _excerpt(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text.rstrip()
    half = limit // 2
    return text[:half].rstrip() + "\n…[truncated]…\n" + text[-half:].lstrip()
