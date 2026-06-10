"""Tests for ``ccd/loop.py`` — spec_039 FixLoop, the convergence loop.

The loop wraps the per-candidate dispatch + verify cycle so that a
single agent slip can be re-tried with explicit R5/R4/guard feedback
rather than burning the candidate for the night. We pin:

- the K=1 / iter=1 default reproduces v2 single-shot behavior bit-for-bit
- a 1-then-green fake runner converges at iter 2
- a "same failure every time" fake halts on no-progress detection
  BEFORE the third iteration starts
- wall-clock budget exhausts → distinct halt reason
- immediate-halt categories (BLOCKED / environment) halt after 1 iter

Convergence is judged by the verifier, NOT by the dispatcher's
self-reported status — the verifier callback is the machine oracle and
the spec is explicit that self-report ("done") never counts as
convergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ccd.loop import (
    LOOP_HALT_BUDGET,
    LOOP_HALT_IMMEDIATE,
    LOOP_HALT_MAX_ITERATIONS,
    LOOP_HALT_NO_PROGRESS,
    IterationVerification,
    run_fix_loop,
)
from ccd.nightly import FixDispatchOutcome

# --------------------------------------------------------------------------- #
# Test fakes
# --------------------------------------------------------------------------- #


@dataclass
class _ScriptedDispatcher:
    """Records calls, returns a canned outcome per attempt.

    ``outcomes`` is a list of :class:`FixDispatchOutcome`. Iteration N
    returns ``outcomes[N-1]`` (1-indexed); when the list runs short the
    last element is returned, mirroring "the agent keeps doing the same
    thing" without a sentinel.
    """

    outcomes: list[FixDispatchOutcome] = field(
        default_factory=lambda: [
            FixDispatchOutcome(status="done", commits_made=1)
        ]
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        spec_path: Path,
        repo: Path,
        branch: str,
        feedback: Path | None = None,
    ) -> FixDispatchOutcome:
        self.calls.append(
            {
                "spec_path": spec_path,
                "repo": repo,
                "branch": branch,
                "feedback": feedback,
            }
        )
        idx = min(len(self.calls), len(self.outcomes)) - 1
        return self.outcomes[idx]


@dataclass
class _ScriptedVerifier:
    """Records calls, returns a canned IterationVerification per call.

    ``verifications`` is a list; iteration N receives
    ``verifications[N-1]`` (last element repeats). Set ``green_at`` to
    ``k`` to converge at iteration ``k`` with a synthetic green
    verification; earlier iterations get a guard-failing one with the
    fixed ``r5_status``.
    """

    verifications: list[IterationVerification] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        repo: Path,
        branch: str,
    ) -> IterationVerification:
        self.calls.append({"repo": repo, "branch": branch})
        if not self.verifications:
            return IterationVerification(
                r5_passed=True,
                r4_passed=True,
                guard_passed=True,
                r5_status="killed",
                guard_reasons=(),
                diff="diff --git a/tests/x.py b/tests/x.py\n",
            )
        idx = min(len(self.calls), len(self.verifications)) - 1
        return self.verifications[idx]


def _green_verification(
    diff: str = "diff --git a/tests/x.py b/tests/x.py\n",
) -> IterationVerification:
    return IterationVerification(
        r5_passed=True,
        r4_passed=True,
        guard_passed=True,
        r5_status="killed",
        guard_reasons=(),
        diff=diff,
    )


def _failing_verification(
    *,
    r5_status: str = "survived",
    guard_reason: str = "",
) -> IterationVerification:
    """R5-failing verification (target mutation still surviving)."""

    return IterationVerification(
        r5_passed=False,
        r4_passed=True,
        guard_passed=bool(not guard_reason),
        r5_status=r5_status,
        guard_reasons=((guard_reason,) if guard_reason else ()),
        diff="diff --git a/tests/x.py b/tests/x.py\n",
        suite_output="...",
    )


# --------------------------------------------------------------------------- #
# Default (K=1) backwards compat
# --------------------------------------------------------------------------- #


def test_default_one_iteration_green_converges(tmp_path: Path) -> None:
    """spec_039 §3-1 — ``max_iterations=1`` + green verification →
    converged=True, iterations=1, no feedback file written."""

    dispatcher = _ScriptedDispatcher()
    verifier = _ScriptedVerifier(verifications=[_green_verification()])

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=1,
        wall_clock_budget_s=60.0,
        spec_id="spec_auto_001",
    )

    assert out.iterations == 1
    assert out.converged is True
    assert out.halt_reason == ""
    assert out.final_dispatch_status == "done"
    assert out.final_verification is not None
    assert out.final_verification.green is True
    assert len(dispatcher.calls) == 1
    # No feedback should be passed on the first iteration.
    assert dispatcher.calls[0]["feedback"] is None
    # Verifier was called exactly once.
    assert len(verifier.calls) == 1


def test_default_one_iteration_failing_marks_max_iter_halt(
    tmp_path: Path,
) -> None:
    """spec_039 — ``max_iterations=1`` + failing verification → loop
    halts with LOOP_HALT_MAX_ITERATIONS (and converged=False)."""

    dispatcher = _ScriptedDispatcher()
    verifier = _ScriptedVerifier(
        verifications=[_failing_verification(r5_status="survived")]
    )

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=1,
        wall_clock_budget_s=60.0,
    )

    assert out.iterations == 1
    assert out.converged is False
    assert out.halt_reason == LOOP_HALT_MAX_ITERATIONS
    assert len(dispatcher.calls) == 1
    # No feedback file written for iter 1 → iter 2, because there is no
    # iter 2 at max_iterations=1.
    log_dir = tmp_path / "_ai_workspace" / "logs"
    assert not log_dir.exists() or not any(log_dir.iterdir())


# --------------------------------------------------------------------------- #
# Convergence loop happy path: 1 fail → 1 green
# --------------------------------------------------------------------------- #


def test_one_fail_then_green_converges_at_iter_2(
    tmp_path: Path,
) -> None:
    """spec_039 §3-2 — iter 1 R5 fail, iter 2 green → converged=True,
    iterations=2. Iter 2's dispatcher call carries a feedback path."""

    dispatcher = _ScriptedDispatcher(
        outcomes=[
            FixDispatchOutcome(status="done", commits_made=1),
            FixDispatchOutcome(status="done", commits_made=1),
        ]
    )
    verifier = _ScriptedVerifier(
        verifications=[
            _failing_verification(r5_status="survived"),
            _green_verification(),
        ]
    )

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=3,
        wall_clock_budget_s=60.0,
        spec_id="spec_auto_001",
    )

    assert out.iterations == 2
    assert out.converged is True
    assert out.halt_reason == ""
    assert len(dispatcher.calls) == 2
    # Iter 1: no feedback. Iter 2: feedback path is the one written
    # after iter 1 failed.
    assert dispatcher.calls[0]["feedback"] is None
    fb_path = dispatcher.calls[1]["feedback"]
    assert fb_path is not None
    assert Path(fb_path).exists()
    assert "fix_loop.iter_1.feedback.md" in str(fb_path)
    # The feedback body names the R5 failure status so the agent's
    # next attempt sees what went wrong.
    body = Path(fb_path).read_text(encoding="utf-8")
    assert "R5" in body
    assert "survived" in body


# --------------------------------------------------------------------------- #
# No-progress detection
# --------------------------------------------------------------------------- #


def test_two_consecutive_same_failures_halt_on_no_progress(
    tmp_path: Path,
) -> None:
    """spec_039 §3-3 — same failure signature twice in a row → halt
    BEFORE iteration 3 starts (max_iterations=5 here)."""

    dispatcher = _ScriptedDispatcher(
        outcomes=[FixDispatchOutcome(status="done", commits_made=1)]
    )
    # Two identical failing verifications, then a hypothetical 3rd we
    # never reach.
    verifier = _ScriptedVerifier(
        verifications=[
            _failing_verification(r5_status="survived"),
            _failing_verification(r5_status="survived"),
            _green_verification(),  # never invoked
        ]
    )

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=5,
        wall_clock_budget_s=60.0,
    )

    assert out.iterations == 2
    assert out.converged is False
    assert out.halt_reason == LOOP_HALT_NO_PROGRESS
    # Dispatcher was called 2x; the 3rd iteration was NOT started.
    assert len(dispatcher.calls) == 2
    assert len(verifier.calls) == 2
    # The failure-signature list captures both occurrences.
    assert len(out.failure_signatures) == 2
    assert out.failure_signatures[0] == out.failure_signatures[1]


def test_different_failure_signatures_continue_iterating(
    tmp_path: Path,
) -> None:
    """The no-progress halt only fires on TWO CONSECUTIVE identical
    failures — a failure that differs from the previous one keeps the
    loop going. We use r5_status to make the signatures differ."""

    dispatcher = _ScriptedDispatcher(
        outcomes=[FixDispatchOutcome(status="done", commits_made=1)]
    )
    verifier = _ScriptedVerifier(
        verifications=[
            _failing_verification(r5_status="survived"),
            _failing_verification(r5_status="unknown"),
            _green_verification(),
        ]
    )

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=5,
        wall_clock_budget_s=60.0,
    )

    assert out.iterations == 3
    assert out.converged is True


# --------------------------------------------------------------------------- #
# Wall-clock budget
# --------------------------------------------------------------------------- #


def test_wall_clock_budget_exhausted_halts(tmp_path: Path) -> None:
    """spec_039 — when the loop's TOTAL budget is exhausted between
    iterations the loop halts with LOOP_HALT_BUDGET, distinct from
    LOOP_HALT_MAX_ITERATIONS."""

    # Inject a monotonic clock we control. The loop calls _remaining()
    # which is budget - (clock_now - start). We make the clock jump
    # past the budget after iter 1 so iter 2 never starts.
    clock_ticks = iter([0.0, 0.0, 100.0])  # start, iter1 pre, iter2 pre

    def _clock() -> float:
        try:
            return next(clock_ticks)
        except StopIteration:
            return 1000.0

    dispatcher = _ScriptedDispatcher(
        outcomes=[FixDispatchOutcome(status="done", commits_made=1)]
    )
    verifier = _ScriptedVerifier(
        verifications=[_failing_verification(r5_status="survived")]
    )

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=5,
        wall_clock_budget_s=10.0,
        clock=_clock,
    )

    # Iter 1 ran (dispatch + verify, failed). The clock jump pushed
    # remaining past 0, so iter 2 halted before starting.
    assert out.iterations == 1
    assert out.converged is False
    assert out.halt_reason == LOOP_HALT_BUDGET
    assert len(dispatcher.calls) == 1


# --------------------------------------------------------------------------- #
# Immediate-halt failure categories
# --------------------------------------------------------------------------- #


def test_blocked_dispatch_halts_after_one_iteration(
    tmp_path: Path,
) -> None:
    """spec_039 §3-5 — a ``blocked`` dispatch (the agent gave up
    explicitly) halts the loop immediately without writing feedback."""

    dispatcher = _ScriptedDispatcher(
        outcomes=[
            FixDispatchOutcome(
                status="blocked",
                halt_reason="agent declared BLOCKED in result body",
            ),
        ]
    )
    verifier = _ScriptedVerifier(
        verifications=[_green_verification()]  # never invoked
    )

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=5,
        wall_clock_budget_s=60.0,
    )

    assert out.iterations == 1
    assert out.converged is False
    assert LOOP_HALT_IMMEDIATE in out.halt_reason
    assert "blocked" in out.halt_reason
    # Verifier never ran (dispatch failed before R5/R4/guard).
    assert verifier.calls == []
    # No feedback file written.
    log_dir = tmp_path / "_ai_workspace" / "logs"
    assert not log_dir.exists() or not any(log_dir.iterdir())


def test_environment_failure_halts_after_one_iteration(
    tmp_path: Path,
) -> None:
    """spec_039 §3-5 — ``environment`` failure category (e.g. pytest
    runner missing) is also an immediate halt."""

    dispatcher = _ScriptedDispatcher(
        outcomes=[
            FixDispatchOutcome(
                status="failed",
                halt_reason="environment",
            ),
        ]
    )
    verifier = _ScriptedVerifier(verifications=[_green_verification()])

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=5,
        wall_clock_budget_s=60.0,
    )

    assert out.iterations == 1
    assert out.converged is False
    assert LOOP_HALT_IMMEDIATE in out.halt_reason
    assert "environment" in out.halt_reason
    assert verifier.calls == []


def test_smoke_failed_is_retryable(tmp_path: Path) -> None:
    """``smoke_failed`` is in retry.py's _RETRYABLE_CATEGORIES — the
    fix loop must NOT treat it as immediate halt; the agent gets a
    feedback file and another shot."""

    dispatcher = _ScriptedDispatcher(
        outcomes=[
            FixDispatchOutcome(
                status="failed",
                halt_reason="smoke_failed",
            ),
            FixDispatchOutcome(status="done", commits_made=1),
        ]
    )
    verifier = _ScriptedVerifier(
        verifications=[_green_verification()]  # iter 2 runs verifier
    )

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=3,
        wall_clock_budget_s=60.0,
    )

    assert out.iterations == 2
    assert out.converged is True
    # Two dispatches; feedback path passed to iter 2.
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[1]["feedback"] is not None


# --------------------------------------------------------------------------- #
# Failure signature normalisation
# --------------------------------------------------------------------------- #


def test_two_consecutive_dispatch_failures_with_same_category_halt(
    tmp_path: Path,
) -> None:
    """Two consecutive non-immediate dispatch failures with the same
    halt_reason are 'same signature' and trigger no-progress halt."""

    dispatcher = _ScriptedDispatcher(
        outcomes=[
            FixDispatchOutcome(status="failed", halt_reason="agent_misread"),
            FixDispatchOutcome(status="failed", halt_reason="agent_misread"),
            FixDispatchOutcome(status="done", commits_made=1),
        ]
    )
    verifier = _ScriptedVerifier(verifications=[_green_verification()])

    out = run_fix_loop(
        spec_path=tmp_path / "spec_auto.md",
        repo=tmp_path,
        branch="auto/spec_auto_001",
        dispatcher=dispatcher,
        verifier=verifier,
        max_iterations=5,
        wall_clock_budget_s=60.0,
    )

    assert out.iterations == 2
    assert out.converged is False
    assert out.halt_reason == LOOP_HALT_NO_PROGRESS
    assert verifier.calls == []
