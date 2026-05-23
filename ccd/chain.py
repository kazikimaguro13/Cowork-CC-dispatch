"""Chain orchestrator: run multiple specs as dispatch_one → integrate, halt on failure.

For each spec in order:
    1. Switch to `main_branch` and create a fresh feature branch
       (default name: `feat/<spec.id>`)
    2. `dispatch_one(spec, runner, repo=repo)`
    3. `integrate(record, repo=repo, branch=branch, ...)`

When a step fails, the chain halts and the remaining specs are not run.
`main` is never left in a broken state: if smoke fails or the dispatch
itself didn't reach DONE, no merge happens and the failed work stays
isolated on its feature branch.

spec_010: every spec is wrapped in `try/except Exception`. A git checkout
error, a `subprocess.TimeoutExpired` from the runner, or any other
unhandled exception becomes a ``HALTED + INTERRUPTED`` record (the
truthful "ccd started this dispatch but never observed it finish") and
halts the chain — instead of crashing through the orchestrator and
deleting the run JSON. Optional ``on_start`` / ``on_finish`` callbacks
let `cli.py` persist that record atomically before and after each
attempt.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .agent import AgentRunner
from .integrate import DEFAULT_SMOKE_COMMANDS, IntegrateResult, integrate
from .models import DispatchRecord, DispatchStatus, FailureCategory, Spec
from .retry import dispatch_with_retry


@dataclass(frozen=True)
class ChainStep:
    spec_id: str
    branch: str
    dispatch: DispatchRecord
    integrate: IntegrateResult


@dataclass(frozen=True)
class ChainResult:
    steps: tuple[ChainStep, ...]
    success: bool
    halted_at: str | None
    halt_reason: str = ""


def default_branch_for(spec: Spec) -> str:
    return f"feat/{spec.id}"


# Type aliases for the (optional) incremental-persistence hooks used by
# `cli.py:RunWriter`. They are deliberately simple callables so chain.py
# does not import from cli.py.
OnStart = Callable[[str], None] | Callable[..., None]
OnFinish = Callable[[DispatchRecord], None]


def run_chain(
    specs: Sequence[Spec],
    runner: AgentRunner,
    *,
    repo: Path,
    main_branch: str = "main",
    smoke_commands: Sequence[Sequence[str]] = DEFAULT_SMOKE_COMMANDS,
    branch_for: Callable[[Spec], str] = default_branch_for,
    on_start: Callable[..., None] | None = None,
    on_finish: Callable[[DispatchRecord], None] | None = None,
    max_attempts: int = 1,
) -> ChainResult:
    """Run specs sequentially as dispatch_one → integrate, stopping on first failure.

    ``on_start`` is invoked as ``on_start(spec_id, started_at=...)`` *before*
    each runner call so a writer can persist an in-flight ``RUNNING`` marker.
    ``on_finish`` is invoked with the final ``DispatchRecord`` whether the
    spec completed normally or was wrapped in a ``HALTED + INTERRUPTED``
    record due to an unhandled exception.

    ``max_attempts`` is forwarded to `dispatch_with_retry` for each spec.
    Library default is 1 (no retry — keeps existing run_chain tests passing
    unchanged). The CLI raises this to 3 via ``--max-attempts``.
    """

    repo = Path(repo)
    steps: list[ChainStep] = []

    for spec in specs:
        branch = branch_for(spec)
        started_at = _now()
        if on_start is not None:
            on_start(spec.id, started_at=started_at)

        try:
            _create_feature_branch(repo, branch=branch, main_branch=main_branch)
            record = dispatch_with_retry(
                spec,
                runner,
                repo=repo,
                max_attempts=max_attempts,
                smoke_commands=smoke_commands,
            )
        except Exception as exc:
            record = DispatchRecord(
                spec_id=spec.id,
                started_at=started_at,
                finished_at=None,
                status=DispatchStatus.HALTED,
                attempts=1,
                failure_category=FailureCategory.INTERRUPTED,
                intervention=False,
            )
            if on_finish is not None:
                on_finish(record)
            integrate_result = IntegrateResult(
                spec_id=spec.id,
                success=False,
                merged=False,
                smoke=None,
                failure_category=FailureCategory.INTERRUPTED,
                detail=_summarize_exception(exc),
            )
            steps.append(
                ChainStep(
                    spec_id=spec.id,
                    branch=branch,
                    dispatch=record,
                    integrate=integrate_result,
                )
            )
            return ChainResult(
                steps=tuple(steps),
                success=False,
                halted_at=spec.id,
                halt_reason=integrate_result.detail or f"halted at {spec.id}",
            )

        if on_finish is not None:
            on_finish(record)

        try:
            result = integrate(
                record,
                repo=repo,
                branch=branch,
                main_branch=main_branch,
                smoke_commands=smoke_commands,
            )
        except Exception as exc:
            result = IntegrateResult(
                spec_id=spec.id,
                success=False,
                merged=False,
                smoke=None,
                failure_category=FailureCategory.INTERRUPTED,
                detail=_summarize_exception(exc),
            )

        steps.append(
            ChainStep(spec_id=spec.id, branch=branch, dispatch=record, integrate=result)
        )

        if not result.success:
            return ChainResult(
                steps=tuple(steps),
                success=False,
                halted_at=spec.id,
                halt_reason=result.detail or f"halted at {spec.id}",
            )

    return ChainResult(steps=tuple(steps), success=True, halted_at=None)


def _create_feature_branch(repo: Path, *, branch: str, main_branch: str) -> None:
    """Switch to `main_branch` and create a fresh feature branch from it.

    Raises RuntimeError if either git command fails — the chain can't proceed
    without a clean feature branch to dispatch onto, and silently swallowing
    git errors would let the next spec run against the wrong tree.
    """

    co = subprocess.run(
        ["git", "checkout", main_branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if co.returncode != 0:
        raise RuntimeError(
            f"git checkout {main_branch} failed: "
            f"{(co.stderr or co.stdout or '').strip()}"
        )
    cb = subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if cb.returncode != 0:
        raise RuntimeError(
            f"git checkout -b {branch} failed: "
            f"{(cb.stderr or cb.stdout or '').strip()}"
        )


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
