"""Integration step: smoke-test the dispatched work and merge it to main.

`integrate(record, ...)` runs the configured smoke commands (default:
`ruff check .` then `pytest -q`) in the repo. If smoke passes *and* the
dispatch finished with status DONE, it merges the named feature branch into
`main` with `git merge --no-ff`. Otherwise it returns a failed
`IntegrateResult` — the chain orchestrator halts on the first failure so
nothing broken is merged on top.

v1 never runs `git push`; the operator pushes manually after reviewing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import DispatchRecord, DispatchStatus, FailureCategory

DEFAULT_SMOKE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("ruff", "check", "."),
    ("pytest", "-q"),
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of running the smoke commands. Stops at the first non-zero exit."""

    passed: bool
    commands: tuple[CommandResult, ...] = ()


@dataclass(frozen=True)
class IntegrateResult:
    spec_id: str
    success: bool
    merged: bool
    smoke: SmokeResult | None
    failure_category: FailureCategory | None = None
    detail: str = ""


def integrate(
    record: DispatchRecord,
    *,
    repo: Path,
    branch: str,
    main_branch: str = "main",
    smoke_commands: Sequence[Sequence[str]] = DEFAULT_SMOKE_COMMANDS,
) -> IntegrateResult:
    """Smoke + merge for one dispatched spec. No-op merge when dispatch failed."""

    repo = Path(repo)

    if record.status is not DispatchStatus.DONE:
        return IntegrateResult(
            spec_id=record.spec_id,
            success=False,
            merged=False,
            smoke=None,
            failure_category=record.failure_category,
            detail=f"dispatch status was {record.status.value}",
        )

    smoke = _run_smoke(repo, smoke_commands)
    if not smoke.passed:
        return IntegrateResult(
            spec_id=record.spec_id,
            success=False,
            merged=False,
            smoke=smoke,
            failure_category=FailureCategory.SMOKE_FAILED,
            detail="smoke commands failed",
        )

    merge_error = _merge_to_main(repo, branch=branch, main_branch=main_branch)
    if merge_error is not None:
        return IntegrateResult(
            spec_id=record.spec_id,
            success=False,
            merged=False,
            smoke=smoke,
            failure_category=FailureCategory.MERGE_CONFLICT,
            detail=merge_error[:1000],
        )

    return IntegrateResult(
        spec_id=record.spec_id,
        success=True,
        merged=True,
        smoke=smoke,
        failure_category=None,
        detail="",
    )


def _run_smoke(repo: Path, commands: Sequence[Sequence[str]]) -> SmokeResult:
    results: list[CommandResult] = []
    passed = True
    for argv in commands:
        argv_list = list(argv)
        completed = subprocess.run(
            argv_list,
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        results.append(
            CommandResult(
                argv=tuple(argv_list),
                returncode=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        )
        if completed.returncode != 0:
            passed = False
            break
    return SmokeResult(passed=passed, commands=tuple(results))


def _merge_to_main(repo: Path, *, branch: str, main_branch: str) -> str | None:
    """Merge `branch` into `main_branch`. Returns error text on failure, None on success.

    On merge failure we attempt `git merge --abort` so that `main` stays clean
    rather than left mid-merge.
    """

    co = subprocess.run(
        ["git", "checkout", main_branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if co.returncode != 0:
        return (co.stderr or co.stdout or "git checkout failed").strip()

    merge = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"merge: {branch}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if merge.returncode != 0:
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        return (merge.stderr or merge.stdout or "git merge failed").strip()
    return None
