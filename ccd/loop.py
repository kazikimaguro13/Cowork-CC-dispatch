"""ccd loop — spec_039 FixLoop, the convergence loop around dispatch+verify.

Up to and including spec_038, the auto-fix and propose-mode loops
dispatched **once** per candidate and accepted the R5/R4/guard verdict
as final ("1 candidate, 1 try, merge or HALT" — spec_023 论点4). That is
safe but brittle: a single agent slip (forgot to fix a sibling, dropped
a test) burns the candidate for the night even if the agent could have
fixed it on a second pass with the failure made visible.

spec_039 wraps that single-shot model in a convergence loop. The
philosophy is borrowed from Anthropic's ralph-wiggum plugin ("keep
sending the agent back to the same job until the completion condition
is met"), but the completion oracle is NOT the agent's self-report
("done") — it is R5/R4/guard **machine verification**, the same
oracle the v2 loop has used since spec_023. Until verification is green
the loop iterates; once it is green the loop exits with
``converged=True``.

Halt conditions beyond "green":

1. ``max_iterations`` reached — bounded by
   :attr:`ccd.profile.SafetyConfig.loop_max_iterations` (default 1,
   allowed 1..5). 1 reproduces the spec_023〜038 single-shot behavior
   bit-for-bit.
2. **Wall-clock budget** — the per-candidate budget (40 min by default,
   inherited from spec_025's ``_AUTO_FIX_DISPATCH_TIMEOUT_S``) is
   applied to the *whole loop*, not per-iteration. Each iteration's
   bounded-dispatch timeout is ``budget - elapsed``. When the budget
   expires the loop halts with :data:`LOOP_HALT_BUDGET`.
3. **No-progress detection** — when two consecutive iterations produce
   the SAME failure signature ``(category, normalised R5 reason)`` the
   loop halts with :data:`LOOP_HALT_NO_PROGRESS` BEFORE the next
   dispatch. ralph's failure mode is blind token-burning; we don't
   trust self-reports, so we need an explicit "we are not making
   progress" stop rather than a promise of "almost done".
4. **Immediate-halt failure category** — categories that
   :mod:`ccd.retry` treats as immediate halt (``environment`` /
   ``merge_conflict``) and the ``blocked`` dispatch status do NOT loop.
   One iteration, then halt. Mirrors :func:`ccd.retry._is_retryable`.

Relationship to :func:`ccd.retry.dispatch_with_retry`
-----------------------------------------------------

``dispatch_with_retry`` (spec_011) is the chain-side self-healing retry
around smoke-failed dispatches. Its public behavior is unchanged. FixLoop
is the *nightly-side* convergence loop around R5/R4/guard verification.
The two share the retryable / immediate-halt category split via
:func:`ccd.retry.is_failure_immediate_halt` — extracted as a public
helper so FixLoop and ``dispatch_with_retry`` can't drift on what counts
as "give up".

Feedback file
-------------

When an iteration fails and the loop has more iterations to spend, the
loop writes a Markdown feedback file under
``<repo>/_ai_workspace/logs/<spec_id>.fix_loop.iter_<N>.feedback.md``
and passes its path to the next iteration's dispatcher via the optional
``feedback=`` kwarg of the :data:`ccd.nightly.FixDispatcher` protocol.
The default dispatcher built by :func:`ccd.nightly._build_default_fix_dispatcher`
forwards ``feedback`` through to :func:`ccd.retry.dispatch_with_retry`
which then to :func:`ccd.dispatch.dispatch_one` — so the agent's prompt
sees the R5/R4/guard reasons in its context window before retrying.

For ``max_iterations=1`` no feedback file is ever written (there is no
"next iteration" to consume it), preserving spec_038 disk layout.
"""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Halt-reason anchors. The morning brief and tests pin these substrings,
# so any rename is a single-place change followed by re-validating
# pinning callers (in line with the spec_025 anchor pattern).
LOOP_HALT_MAX_ITERATIONS = "fix-loop: max_iterations reached"
LOOP_HALT_BUDGET = "fix-loop: wall-clock budget exhausted"
LOOP_HALT_NO_PROGRESS = (
    "fix-loop: no-progress detected (same failure signature repeated)"
)
LOOP_HALT_IMMEDIATE = "fix-loop: immediate-halt category"


@dataclass(frozen=True)
class IterationVerification:
    """One iteration's R5/R4/guard verification result.

    The caller's verifier callback builds this object after a successful
    dispatch. The loop reads ``green`` to decide convergence and reads
    the per-gate booleans + ``r5_status`` + ``guard_reasons`` to build a
    failure signature for no-progress detection and a Markdown feedback
    file for the next iteration.

    ``diff`` is the diff captured between the candidate's branch and
    ``main`` (in the same workspace the dispatcher ran in). On the
    converged iteration this becomes ``merge_diff`` / ``proposal_diff``
    on the surfaced :class:`ccd.nightly.AutoFixOutcome`. On a failing
    iteration it is embedded into the feedback file so the agent can
    see the diff it produced alongside why it was rejected.

    ``suite_output`` is the tail of pytest's stdout/stderr; surfaced in
    the feedback file when R4 failed so the agent can see which test
    regressed.
    """

    r5_passed: bool
    r4_passed: bool
    guard_passed: bool
    r5_status: str
    guard_reasons: tuple[str, ...]
    diff: str
    suite_output: str = ""
    # spec_043 §2-4 — the R4 count summary ("collected N, passed N,
    # baseline N") on a pass, or the count-regression / unparseable
    # reason on a fail. Carried up onto :class:`ccd.nightly.AutoFixOutcome`
    # so the morning brief's §B verification evidence shows the dynamic
    # test-count check, not just a bare pass/fail. Empty when counts were
    # unavailable (fake runner / no baseline) — the brief then renders its
    # plain pass/fail line unchanged (spec_023〜042 外形保持).
    r4_detail: str = ""
    # spec_045 §2-1 — the R5 N-times stability one-liner ("killed (3/3 回
    # 安定)" on a stable pass, "R5 不安定: killed 3回中 2回のみ" on an
    # unstable fail). Carried up onto :class:`ccd.nightly.AutoFixOutcome`
    # so the morning brief's §B / §D surfaces RT-3's determinism check.
    # Empty at the default ``safety.r5_recheck_times == 1`` keeps the
    # spec_023〜044 brief 外形 bit-for-bit identical.
    r5_detail: str = ""

    @property
    def green(self) -> bool:
        """All three gates passed → loop converged this iteration."""
        return self.r5_passed and self.r4_passed and self.guard_passed


@dataclass(frozen=True)
class FixLoopOutcome:
    """The convergence loop's verdict for ONE candidate.

    Reading order for callers:

    - ``converged=True`` → the final iteration produced ``green``
      verification. ``final_verification`` is non-None;
      ``halt_reason`` is empty.
    - ``converged=False`` → loop halted for one of four reasons. The
      :data:`LOOP_HALT_*` anchors name which:

      * :data:`LOOP_HALT_MAX_ITERATIONS` — ran all ``max_iterations``
        and the last one still failed verification.
      * :data:`LOOP_HALT_BUDGET` — wall-clock budget exhausted.
      * :data:`LOOP_HALT_NO_PROGRESS` — same failure signature
        appeared twice in a row; loop halted before the next dispatch.
      * :data:`LOOP_HALT_IMMEDIATE` — dispatch reported an immediate-
        halt failure category (environment / merge_conflict / blocked).
        One iteration only.

    ``iterations`` is the actual count of dispatch attempts the loop
    made (1 ≤ iterations ≤ max_iterations). ``failure_signatures`` is
    the per-iteration ``(category, normalised reason)`` tuple list —
    spec_042 will consume it for the dashboard.
    """

    iterations: int
    converged: bool
    halt_reason: str
    final_verification: IterationVerification | None
    final_dispatch_status: str
    final_dispatch_halt_reason: str
    final_dispatched: bool
    failure_signatures: tuple[tuple[str, str], ...] = ()


# Failure categories that disable iteration. These mirror
# :data:`ccd.retry._HALT_ON_CATEGORIES` — see
# :func:`ccd.retry.is_failure_immediate_halt` for the canonical check.
# Kept as a string set so loop.py can answer "immediate halt?" without
# pulling in :mod:`pydantic` at import time.
_IMMEDIATE_HALT_CATEGORIES: frozenset[str] = frozenset(
    {"environment", "merge_conflict"}
)

# Categories the dispatcher might attach to a failed FixDispatchOutcome's
# ``halt_reason`` string. Used for substring-based extraction since the
# default dispatcher in :func:`ccd.nightly._build_default_fix_dispatcher`
# stores ``failure_category.value`` verbatim in ``halt_reason``.
_KNOWN_FAILURE_CATEGORIES: tuple[str, ...] = (
    "environment",
    "merge_conflict",
    "smoke_failed",
    "agent_misread",
    "transient",
    "interrupted",
    "spec_unclear",
)


def run_fix_loop(
    *,
    spec_path: Path,
    repo: Path,
    branch: str,
    dispatcher: Callable[..., Any],
    verifier: Callable[..., IterationVerification],
    max_iterations: int,
    wall_clock_budget_s: float,
    feedback_dir: Path | None = None,
    spec_id: str = "",
    clock: Callable[[], float] | None = None,
) -> FixLoopOutcome:
    """Run the convergence loop for ONE candidate.

    Inputs:

    - ``spec_path`` / ``repo`` / ``branch`` — forwarded to ``dispatcher``
      verbatim each iteration. The branch is assumed to already be
      checked out by the caller; the loop never branches.
    - ``dispatcher`` — :data:`ccd.nightly.FixDispatcher`. Called with
      ``feedback=<Path>`` from iteration 2 onward; iteration 1 calls it
      without ``feedback`` to preserve the existing fake-dispatcher call
      shape for K=1 tests.
    - ``verifier`` — callable ``(repo, branch) → IterationVerification``.
      Run only when dispatch succeeded; the caller closes over the
      template / finding / R5 / R4 / guard seams.
    - ``max_iterations`` — bound from
      :attr:`ccd.profile.SafetyConfig.loop_max_iterations`. ``1`` (the
      default) reproduces v2 single-shot behavior bit-for-bit.
    - ``wall_clock_budget_s`` — TOTAL loop budget. Each iteration's
      bounded-dispatch timeout is ``budget - elapsed``. Non-positive
      disables timeout (used by tests).
    - ``feedback_dir`` — where to write per-iteration feedback. Defaults
      to ``<repo>/_ai_workspace/logs``.
    - ``spec_id`` — used for the feedback filename so concurrent
      candidates do not collide.
    - ``clock`` — injectable monotonic clock for tests.

    Returns :class:`FixLoopOutcome`. Exceptions from ``dispatcher`` and
    ``verifier`` propagate — the caller (nightly's per-candidate body)
    is responsible for wrapping in HALT outcomes since it owns the repo
    restore path.
    """

    if max_iterations < 1:
        raise ValueError(
            f"max_iterations must be >= 1, got {max_iterations}"
        )

    now = clock if clock is not None else time.monotonic
    start = now()
    budget_enabled = wall_clock_budget_s is not None and wall_clock_budget_s > 0

    def _remaining() -> float:
        """Seconds left in the loop budget. Negative ⇒ budget exhausted."""
        return wall_clock_budget_s - (now() - start)

    signatures: list[tuple[str, str]] = []
    last_verification: IterationVerification | None = None
    last_dispatch_status = ""
    last_dispatch_halt_reason = ""
    last_dispatched = False
    feedback_path: Path | None = None

    for iteration in range(1, max_iterations + 1):
        # Pre-iteration budget check: if 0 ≤ remaining we still let
        # this iteration try (the per-call timeout will cap it); when
        # remaining ≤ 0 BEFORE the iteration starts, halt cleanly.
        if budget_enabled:
            remaining = _remaining()
            if remaining <= 0:
                return FixLoopOutcome(
                    iterations=max(iteration - 1, 0),
                    converged=False,
                    halt_reason=LOOP_HALT_BUDGET,
                    final_verification=last_verification,
                    final_dispatch_status=last_dispatch_status,
                    final_dispatch_halt_reason=last_dispatch_halt_reason,
                    final_dispatched=last_dispatched,
                    failure_signatures=tuple(signatures),
                )
        else:
            remaining = -1.0  # disabled

        dispatch_outcome = _bounded_dispatch(
            dispatcher=dispatcher,
            spec_path=spec_path,
            repo=repo,
            branch=branch,
            feedback=feedback_path,
            timeout_s=remaining if budget_enabled else 0.0,
        )
        last_dispatch_status = str(
            getattr(dispatch_outcome, "status", "") or ""
        )
        last_dispatch_halt_reason = str(
            getattr(dispatch_outcome, "halt_reason", "") or ""
        )
        last_dispatched = True

        if last_dispatch_status == "done":
            # Verifier runs only on a successful dispatch.
            verification = verifier(repo=repo, branch=branch)
            last_verification = verification
            if verification.green:
                return FixLoopOutcome(
                    iterations=iteration,
                    converged=True,
                    halt_reason="",
                    final_verification=verification,
                    final_dispatch_status=last_dispatch_status,
                    final_dispatch_halt_reason="",
                    final_dispatched=True,
                    failure_signatures=tuple(signatures),
                )
            sig: tuple[str, str] = (
                "verify",
                _normalise_verification_signature(verification),
            )
        else:
            # Dispatch failed / blocked / halted. Extract the failure
            # category for the signature and the immediate-halt check.
            category = _extract_category(
                last_dispatch_halt_reason, last_dispatch_status
            )
            if (
                last_dispatch_status == "blocked"
                or category in _IMMEDIATE_HALT_CATEGORIES
            ):
                return FixLoopOutcome(
                    iterations=iteration,
                    converged=False,
                    halt_reason=(
                        f"{LOOP_HALT_IMMEDIATE}: "
                        f"{category or last_dispatch_status}"
                    ),
                    final_verification=last_verification,
                    final_dispatch_status=last_dispatch_status,
                    final_dispatch_halt_reason=last_dispatch_halt_reason,
                    final_dispatched=True,
                    failure_signatures=tuple(signatures),
                )
            sig = (category or "dispatch", last_dispatch_halt_reason)

        signatures.append(sig)

        # No-progress: two consecutive identical signatures halt the
        # loop BEFORE the next dispatch. spec_039 §2-1 (3).
        if len(signatures) >= 2 and signatures[-1] == signatures[-2]:
            return FixLoopOutcome(
                iterations=iteration,
                converged=False,
                halt_reason=LOOP_HALT_NO_PROGRESS,
                final_verification=last_verification,
                final_dispatch_status=last_dispatch_status,
                final_dispatch_halt_reason=last_dispatch_halt_reason,
                final_dispatched=last_dispatched,
                failure_signatures=tuple(signatures),
            )

        # If we're about to enter another iteration, write feedback so
        # the next dispatch sees R5/R4/guard reasons in its prompt.
        if iteration < max_iterations:
            feedback_path = _write_loop_feedback(
                spec_id=spec_id,
                feedback_dir=feedback_dir,
                iteration=iteration,
                dispatch_status=last_dispatch_status,
                dispatch_halt_reason=last_dispatch_halt_reason,
                verification=last_verification,
                repo=repo,
            )

    return FixLoopOutcome(
        iterations=max_iterations,
        converged=False,
        halt_reason=LOOP_HALT_MAX_ITERATIONS,
        final_verification=last_verification,
        final_dispatch_status=last_dispatch_status,
        final_dispatch_halt_reason=last_dispatch_halt_reason,
        final_dispatched=last_dispatched,
        failure_signatures=tuple(signatures),
    )


def _normalise_verification_signature(
    verification: IterationVerification,
) -> str:
    """Build a stable failure-signature string from a verification result.

    Goal: catch "same failure as last time" robustly without being so
    granular it never matches (e.g. line-numbered tracebacks vary
    between runs). We focus on:

    - the R5 status string (the agent's structural mistake, most often)
    - WHICH gates failed (r5 / r4 / guard) — flipping which gate fails
      counts as progress, even if the agent didn't actually fix anything
    - the first guard reason (when guard is the failing gate) — guard
      reasons are R-anchored ("R1: …", "R2: …", "R3: …") and stable
      across runs

    Two iterations whose normalised signatures match are treated as "no
    progress" and trigger the early halt.
    """

    parts: list[str] = []
    if not verification.r5_passed:
        parts.append(f"r5={verification.r5_status or 'unknown'}")
    if not verification.r4_passed:
        parts.append("r4=fail")
    if not verification.guard_passed:
        head = verification.guard_reasons[0] if verification.guard_reasons else ""
        # Keep only the first ~80 chars to dodge per-run path noise.
        head = head[:80]
        parts.append(f"guard={head or 'fail'}")
    return "|".join(parts) or "verify=unknown"


def _extract_category(halt_reason: str, status: str) -> str:
    """Extract a FailureCategory-like string from a dispatch outcome.

    The default :func:`ccd.nightly._build_default_fix_dispatcher` stores
    ``record.failure_category.value`` verbatim in
    ``FixDispatchOutcome.halt_reason`` when the dispatch failed —
    ``"smoke_failed"`` / ``"agent_misread"`` / etc. Custom dispatchers
    may free-form the halt reason; we match by substring so common
    phrasings (``"dispatch failed: environment"``) still classify.

    Returns ``""`` when the category can't be determined; the loop
    treats that as a non-halt category so iteration continues.
    """

    if status == "blocked":
        return "blocked"
    needle = halt_reason.lower()
    for cat in _KNOWN_FAILURE_CATEGORIES:
        if cat in needle:
            return cat
    return ""


def _bounded_dispatch(
    *,
    dispatcher: Callable[..., Any],
    spec_path: Path,
    repo: Path,
    branch: str,
    feedback: Path | None,
    timeout_s: float,
) -> Any:
    """Run ``dispatcher`` with a wall-clock cap and optional feedback.

    Mirrors :func:`ccd.nightly._dispatch_with_timeout` but adds the
    feedback path injection and uses the LOOP's remaining-budget
    timeout instead of the per-iteration cap. The dispatcher is only
    handed a ``feedback=`` kwarg when ``feedback`` is non-None, so
    iteration-1 calls (which never have feedback) preserve the v2
    test-fake call shape bit-for-bit — existing K=1 / iter=1 tests
    keep their fakes unchanged.

    On timeout we construct a synthetic :class:`FixDispatchOutcome` with
    ``status="failed"`` and a halt_reason naming the budget. The class
    is imported lazily to avoid a module-level dependency on
    :mod:`ccd.nightly` (loop.py is meant to be importable on its own).
    """

    def _call() -> Any:
        kwargs: dict[str, Any] = {
            "spec_path": spec_path,
            "repo": repo,
            "branch": branch,
        }
        if feedback is not None:
            kwargs["feedback"] = feedback
        return dispatcher(**kwargs)

    if timeout_s is None or timeout_s <= 0:
        return _call()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="ccd-fix-loop"
    ) as executor:
        future = executor.submit(_call)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            from ccd.nightly import (  # lazy: avoid module-level cycle
                _HALT_DISPATCH_TIMEOUT,
                FixDispatchOutcome,
            )
            # spec_025 §2-1(a) is the original wall-clock anchor; spec_039
            # generalises it from per-dispatch to per-loop. Tests pin the
            # spec_025 marker so we keep it verbatim and append the
            # spec_039 marker so log readers know this came through the
            # FixLoop budget rather than the old single-shot helper.
            return FixDispatchOutcome(
                status="failed",
                halt_reason=(
                    f"{_HALT_DISPATCH_TIMEOUT} after {timeout_s:.0f}s "
                    "(spec_025 §2-1(a), spec_039 fix-loop budget)"
                ),
                commits_made=0,
            )


def _write_loop_feedback(
    *,
    spec_id: str,
    feedback_dir: Path | None,
    iteration: int,
    dispatch_status: str,
    dispatch_halt_reason: str,
    verification: IterationVerification | None,
    repo: Path,
) -> Path:
    """Write a Markdown feedback file describing what just failed.

    The next iteration's dispatcher embeds this path in the agent's
    prompt (via :func:`ccd.dispatch.dispatch_one`'s ``feedback`` param)
    so the agent sees R5/R4/guard reasons before retrying. Filename is
    versioned per iteration so multiple feedback files coexist in the
    logs directory for post-mortem.
    """

    fb_dir = (
        Path(feedback_dir)
        if feedback_dir is not None
        else repo / "_ai_workspace" / "logs"
    )
    fb_dir.mkdir(parents=True, exist_ok=True)
    name = spec_id or "fix_loop"
    path = fb_dir / f"{name}.fix_loop.iter_{iteration}.feedback.md"

    lines: list[str] = [
        f"# Fix-loop feedback for {name} (iteration {iteration})",
        "",
        "前回のイテレーションは **検証 (R5/R4/guard) を通らなかった** か、",
        "dispatch 自体が失敗しました。前回の作業はフィーチャーブランチに",
        "残っているので、diff を読み、落ちた箇所だけ直す形で再試行してください。",
        "（HEAD を捨てて最初からやり直す必要はありません。）",
        "",
    ]

    if dispatch_status and dispatch_status != "done":
        lines.extend(
            [
                "## 前回の dispatch 失敗",
                "",
                f"- status: `{dispatch_status}`",
                f"- 失敗カテゴリ / 理由: {dispatch_halt_reason or '(理由なし)'}",
                "",
            ]
        )

    if verification is not None:
        lines.extend(
            [
                "## 前回の検証結果",
                "",
                f"- R5 (target verification): "
                f"{'pass' if verification.r5_passed else 'fail'} "
                f"(status=`{verification.r5_status}`)",
                f"- R4 (`pytest -q` 全件 green): "
                f"{'pass' if verification.r4_passed else 'fail'}",
                f"- ガード (R1〜R3): "
                f"{'pass' if verification.guard_passed else 'fail'}",
                "",
            ]
        )
        if not verification.guard_passed and verification.guard_reasons:
            lines.append("### ガード halt 理由")
            lines.append("")
            for r in verification.guard_reasons:
                lines.append(f"- {r}")
            lines.append("")
        if not verification.r4_passed and verification.suite_output:
            tail = verification.suite_output[-2000:].rstrip()
            lines.extend(
                [
                    "### Suite 出力 (末尾)",
                    "",
                    "```",
                    tail,
                    "```",
                    "",
                ]
            )
        if verification.diff:
            excerpt = verification.diff[:4000].rstrip()
            lines.extend(
                [
                    "### 現在の diff (参考)",
                    "",
                    "```diff",
                    excerpt,
                    "```",
                    "",
                ]
            )

    lines.extend(
        [
            "## 次のイテレーション",
            "",
            "**diff を読み、落ちた箇所だけ直す形で進めてください。**",
            "前回の試行を捨てて最初からやり直す必要はありません。",
            "",
        ]
    )

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


__all__ = [
    "FixLoopOutcome",
    "IterationVerification",
    "LOOP_HALT_BUDGET",
    "LOOP_HALT_IMMEDIATE",
    "LOOP_HALT_MAX_ITERATIONS",
    "LOOP_HALT_NO_PROGRESS",
    "run_fix_loop",
]
