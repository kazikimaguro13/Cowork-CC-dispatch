"""Single-shot dispatch: run one spec through an `AgentRunner` and classify.

`dispatch_one` is the v1 entry point. It invokes the runner exactly once (no
retry — that's spec_005's concern), then classifies the outcome by reading
the result Markdown the agent was supposed to write and counting the commits
the agent made between dispatch start and finish.

Classification trust order:
    1. Result file is present and parseable → trust its `status` /
       `failure_category`. (Override: status=done but zero commits ⇒ agent
       misread — the agent claimed success without producing any change.)
    2. Result file missing + runner exited non-zero → environment failure.
    3. Result file missing + runner exited zero → agent misread.
    4. Result file unparseable → agent misread.

"unknown" failure cause is represented as `failure_category=None` (the existing
enum has no UNKNOWN variant and DispatchRecord already allows None).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .agent import AgentOutcome, AgentRunner
from .models import DispatchRecord, DispatchStatus, FailureCategory, Spec
from .protocol import parse_result

_OUTBOX_REL = Path("_ai_workspace") / "bridge" / "outbox"


def dispatch_one(spec: Spec, runner: AgentRunner, *, repo: Path) -> DispatchRecord:
    """Run `runner` against `spec` once and return a classified `DispatchRecord`."""

    repo = Path(repo)
    started_at = _now()
    base_sha = _head_sha(repo)

    outcome = runner.run(spec, workdir=repo)

    finished_at = _now()
    commits_made = _count_commits_since(repo, base_sha)

    status, failure_category = _classify(
        spec=spec,
        outcome=outcome,
        repo=repo,
        commits_made=commits_made,
    )

    return DispatchRecord(
        spec_id=spec.id,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        attempts=1,
        failure_category=failure_category,
        intervention=False,
    )


def _classify(
    *,
    spec: Spec,
    outcome: AgentOutcome,
    repo: Path,
    commits_made: int,
) -> tuple[DispatchStatus, FailureCategory | None]:
    result_path = repo / _OUTBOX_REL / _result_filename(spec.id)

    if not result_path.exists():
        if outcome.exit_code != 0:
            return DispatchStatus.FAILED, FailureCategory.ENVIRONMENT
        return DispatchStatus.FAILED, FailureCategory.AGENT_MISREAD

    try:
        result = parse_result(result_path)
    except ValueError:
        return DispatchStatus.FAILED, FailureCategory.AGENT_MISREAD

    if result.status is DispatchStatus.DONE:
        if commits_made == 0:
            return DispatchStatus.FAILED, FailureCategory.AGENT_MISREAD
        return DispatchStatus.DONE, None

    if result.status is DispatchStatus.BLOCKED:
        return DispatchStatus.BLOCKED, result.failure_category or FailureCategory.SPEC_UNCLEAR

    if result.status is DispatchStatus.FAILED:
        return DispatchStatus.FAILED, result.failure_category

    return DispatchStatus.FAILED, None


def _result_filename(spec_id: str) -> str:
    suffix = spec_id[len("spec_") :] if spec_id.startswith("spec_") else spec_id
    return f"result_{suffix}.md"


def _now() -> datetime:
    return datetime.now(UTC)


def _head_sha(repo: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _count_commits_since(repo: Path, base_sha: str | None) -> int:
    if base_sha is None:
        argv = ["git", "rev-list", "--count", "HEAD"]
    else:
        argv = ["git", "rev-list", "--count", f"{base_sha}..HEAD"]
    completed = subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return 0
    return int(completed.stdout.strip() or "0")
