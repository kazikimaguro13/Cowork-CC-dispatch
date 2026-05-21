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
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .agent import AgentRunner
from .dispatch import dispatch_one
from .integrate import DEFAULT_SMOKE_COMMANDS, IntegrateResult, integrate
from .models import DispatchRecord, Spec


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


def run_chain(
    specs: Sequence[Spec],
    runner: AgentRunner,
    *,
    repo: Path,
    main_branch: str = "main",
    smoke_commands: Sequence[Sequence[str]] = DEFAULT_SMOKE_COMMANDS,
    branch_for: Callable[[Spec], str] = default_branch_for,
) -> ChainResult:
    """Run specs sequentially as dispatch_one → integrate, stopping on first failure."""

    repo = Path(repo)
    steps: list[ChainStep] = []

    for spec in specs:
        branch = branch_for(spec)
        _create_feature_branch(repo, branch=branch, main_branch=main_branch)

        record = dispatch_one(spec, runner, repo=repo)
        result = integrate(
            record,
            repo=repo,
            branch=branch,
            main_branch=main_branch,
            smoke_commands=smoke_commands,
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
