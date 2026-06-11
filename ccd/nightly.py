"""ccd nightly — scheduler with optional autonomous-fix / propose loop.

spec_020 wired the Phase-1 skeleton (discover → brief → mirror). spec_023
added the **autonomous-fix loop** for template A; spec_024 extended it
to template B; spec_025 adds the **cost / halt boundaries**
(``docs/DESIGN.md §9.6`` 论点8) so the loop is safe to run unattended
every night. spec_028 replaces the boolean ``profile.safety.autonomous_fix``
gate with a 3-value ``profile.safety.fix_mode`` (``"off"`` / ``"auto"`` /
``"propose"``). The orchestrator picks one of three behaviors after
discovery:

- ``fix_mode="off"`` — discovery + morning report only. No translate,
  no dispatch, no merge, no proposal (the spec_020 default behavior).
- ``fix_mode="auto"`` — the spec_023〜026 autonomous-fix loop, behavior
  unchanged from the old ``autonomous_fix=True``:

      discover → [select template → translate → dispatch → R5 verify →
                  R4 verify → guard → local merge] → brief

- ``fix_mode="propose"`` — spec_028 propose mode. Translate one
  candidate, run dispatch + R5 + R4 + guard **inside a disposable
  isolated clone** of the live repo, capture the verified diff as a
  patch file under ``_ai_workspace/nightly/proposals/``, surface it in
  the morning brief with a ``git apply`` one-liner. The live working
  tree, branches, and commits are NEVER touched — propose mode's
  invariant is "实 repo に何も残らない". Failed verification drops the
  proposal entirely and surfaces a one-line skip note in §D instead of
  polluting §B with an unverified diff.

The spec_025 cost/halt boundaries layered on top:

1. **PAUSE file** — ``<repo>/_ai_workspace/PAUSE`` is the operator's
   non-emergency brake. When present, ``run_nightly`` does **nothing**
   that night (no channels, no fix, no brief). The CLI surfaces this
   via ``result.paused=True`` and a stdout line.
2. **Un-pushed backlog cap** — when the local ``main`` has accumulated
   ``_AUTO_FIX_UNPUSHED_BACKLOG_LIMIT`` (3 by default) auto-merge
   commits not yet pushed to ``origin/main``, the auto-fix loop pauses
   new dispatches and the morning brief asks the operator to review
   and push. Discovery + brief still run so the operator sees what
   else is on the floor.
3. **Dispatch wall-clock cap** — a single ``claude``-dispatch can
   trigger a runaway. ``_AUTO_FIX_DISPATCH_TIMEOUT_S`` (40 min by
   default) bounds it; on timeout the candidate is marked failed and
   surfaced in the morning brief.
4. **Zero-finding normal exit** — a night with no actionable findings
   is not an error; ``run_nightly`` returns ``success=True`` and the
   brief simply says "今夜は何もなし".

The templates the loop is allowed to process are controlled by
``profile.safety.fix_templates`` (default ``["A"]``, spec_024 §2-3 staged
enablement):

- **Template A** (mutation-survivor → test-only fix) — `_AUTO_FIX_ALLOWED_FILES
  = ("tests/",)`. R5 = the target mutant is now killed. Structurally safest
  (cannot touch production code).
- **Template B** (adversarial ungraceful crash → production-fix +
  reproducer test) — allowed = one named production file + ``tests/``,
  R3 (production-diff bound) is enforced. R5 = the broken input now
  produces a CCD allow-listed exception (NOT silent acceptance, NOT the
  original ungraceful crash).

Safety-boundary level 2: local ``main`` merge, **no push**.

The gate is a per-profile field (spec_018 → spec_023): the default profile
is OFF so any newly-configured repo only does Phase 1 discovery + report.
CCD's own profile flips it on (论点1 tier).

Why a *single* candidate per night (论点3)
-----------------------------------------

The autonomous loop is the riskiest piece of v2 — it modifies the live
repo and merges to ``main``. We deliberately cap it at one candidate per
nightly so a single misfiring fix can't cascade into many merges before a
human sees the morning brief. The remaining actionable findings still
appear in the morning report; the operator decides whether to escalate.

What ``run_nightly`` does NOT do
--------------------------------
- It does not push (spec §3 — safety-boundary level 2).
- It does not retry the same candidate (论点4 layer 5 — 1 try, then halt
  and ask a human). Re-discovery the next night picks up survivors again,
  but the loop is not allowed to keep banging on the same finding.
- It does not run mutmut / claude / pytest in tests — the seams below
  let tests inject fakes. Production defaults shell out.
- It does not enable templates the profile didn't opt into. A profile
  with ``fix_templates=["A"]`` never picks up adversarial findings even
  when they are present in discover JSON.

Injection seams (tests / future)
--------------------------------

``run_nightly`` accepts seams so the test suite never shells out to real
``mutmut`` / ``claude`` / pytest / git:

- ``channel_runner`` / ``brief_runner`` / ``windows_mirror`` (spec_020).
- ``fix_dispatcher`` — invoked once per autonomous candidate to dispatch
  the spec_auto. Default wraps :func:`ccd.retry.dispatch_with_retry`.
- ``suite_runner`` — R4 (full suite green). Default shells out to
  ``pytest -q``.
- ``mutation_rechecker`` — R5 for template A (target mutation now killed).
  Default runs the production mutation channel against the target file
  alone.
- ``adversarial_rechecker`` — R5 for template B (the broken input now
  produces a graceful error, *not* silent acceptance). Default re-runs
  the named parser against the named adversarial case (spec_024).
- ``guard_inspector`` — wraps :func:`ccd.guard.inspect_diff`. Tests can
  substitute one that returns a canned GuardResult.
- ``git_ops`` — the four git operations the loop needs (create branch,
  diff vs main, merge to main, checkout main). Default uses subprocess.

The defaults are intentionally lightweight — they shell out and trust
the env. The loop is *exercised* only by tests with fakes; in production,
real ``ccd nightly`` runs the real defaults. ``docs/DESIGN.md §9.5`` and
spec_023 §6 document the open knobs (timeout / branch naming / etc.).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from ccd.agent import AgentRunner
from ccd.brief import BriefResult, run_brief
from ccd.discover import (
    DEFAULT_DISCOVER_DIR_REL,
    MutationRunner,
    MutmutRunner,
    _isolated_clone,
    run_channel,
    run_discovery,
)
from ccd.guard import GuardResult, inspect_diff
from ccd.loop import (
    LOOP_HALT_IMMEDIATE,
    LOOP_HALT_MAX_ITERATIONS,
    FixLoopOutcome,
    IterationVerification,
    run_fix_loop,
)
from ccd.profile import Profile, effective_mutation_config, load_profile
from ccd.protocol import parse_spec
from ccd.translate import Finding, translate_finding

# --------------------------------------------------------------------------- #
# Types for the injection seams
# --------------------------------------------------------------------------- #

ChannelRunner = Callable[..., Any]
"""Same shape as :func:`ccd.discover.run_channel`.

Loosely typed because each channel returns a different result class
(``DiscoveryResult`` / ``AdversarialResult`` / ``AIReviewResult``) and
the nightly orchestrator only inspects ``success`` / ``halt_reason`` /
``report_md_path`` / ``report_json_path`` — all common across them."""

BriefRunner = Callable[..., BriefResult]
"""Same shape as :func:`ccd.brief.run_brief`.

spec_025: the nightly orchestrator forwards the night's
:class:`AutoFixOutcome` to the brief via the ``auto_fix`` keyword
argument so the brief can upgrade §B to the Phase 2 version (昨夜の
自律修正・diff・push コマンド) when the loop actually merged a fix."""


UnpushedCounter = Callable[[Path], int]
"""``(repo) → int``. spec_025 §2-1(b): count auto-merge commits that
exist on local ``main`` but not on ``origin/main`` (i.e. the autonomous
fix has merged locally but the operator hasn't reviewed and pushed yet).

The default counter shells out to ``git log origin/main..main`` and
matches subjects starting with ``auto-merge:`` — that is the prefix
:class:`SubprocessGitOps` uses when the loop merges a feature branch.
Repos without an ``origin/main`` ref (fresh clone, no remote) return 0
— there is nothing to push, so the backlog gate does not apply."""

WindowsMirror = Callable[[Path], Path | None]
"""Copy a WSL report path to a Windows-visible location.

Returns the destination path (or ``None`` if no mirror was performed —
e.g. the destination root does not exist on this host)."""


@dataclass(frozen=True)
class FixDispatchOutcome:
    """What :data:`FixDispatcher` returns to the autonomous-fix loop.

    The loop reads ``status`` to decide whether dispatch succeeded enough
    to proceed with R4 / R5 / guard, and ``halt_reason`` to surface a
    failed dispatch in the morning brief. ``commits_made`` is a sanity
    field — a "done" status with 0 commits is treated as a halt.
    """

    status: str  # "done" / "failed" / "blocked" / "halted"
    halt_reason: str = ""
    commits_made: int = 0


FixDispatcher = Callable[..., FixDispatchOutcome]
"""``(spec_auto_path, repo, branch) → FixDispatchOutcome``.

The dispatcher is responsible for running the fix on the **already
checked-out feature branch**. Production wraps
:func:`ccd.retry.dispatch_with_retry`."""


@dataclass(frozen=True)
class SuiteOutcome:
    """What :data:`SuiteRunner` returns.

    ``output`` is the head + tail of stdout/stderr; the morning brief
    will surface ~the first 800 chars on a failure.

    spec_043 §2-1 — ``collected`` / ``passed_count`` carry the parsed
    pytest test counts so R4 can verify the suite did not *shrink* after
    a fix lands (the RT-1/4/7 defence: skip / deselect / collection-hook /
    import-alias muting all collapse into the single observable fact
    "fewer tests ran than before"). Both are ``None`` when the runner
    could not parse a count — the parser is deliberately lenient
    (spec_009 流儀): an unreadable summary returns ``None`` and the
    caller (:func:`_r4_verdict`) decides how to treat "件数不明" safely
    (偽陽性は許す・偽陰性は許さない)."""

    passed: bool
    output: str = ""
    collected: int | None = None
    passed_count: int | None = None


SuiteRunner = Callable[..., SuiteOutcome]
"""``(repo) → SuiteOutcome``. R4 (spec_023 §2-2): the full suite must
stay green after the fix lands. Default shells out to ``pytest -q``."""


MutationRechecker = Callable[..., str]
"""``(repo, file, line, mutation, signature) → "killed"|"survived"|"unknown"``.

R5 for template A (spec_023 §2-2): the target mutant — identified by
signature — must have flipped from ``survived`` to ``killed`` after the
fix lands. The default runs the production mutation channel against the
target file alone (spec_019's iso-venv) and reads the cache for the
signature."""


AdversarialRechecker = Callable[..., str]
"""``(repo, parser, case_name) → "graceful_error"|"graceful_success"|"ungraceful"|"unknown"``.

R5 for template B (spec_024 §2-2): the target (parser × adversarial case)
must now produce a **graceful error** — a CCD allow-listed exception
(``ValueError`` / ``pydantic.ValidationError`` / ``json.JSONDecodeError`` /
``FileNotFoundError``). The other three statuses all fail R5:

- ``"graceful_success"`` — the parser silently accepted the broken input
  (spec_024 §3 forbids this; the fix must error, not succeed).
- ``"ungraceful"`` — the original crash (or a different ungraceful
  exception) still leaks.
- ``"unknown"`` — the rechecker could not locate the parser/case (parser
  name mistyped, fixture catalog drifted) — loop halts conservatively.

The default rebuilds the named fixture from ``ccd.adversarial.default_cases()``
and calls the named parser in-process. False positives are preferred over
false negatives — when in doubt, return ``"unknown"`` and let the loop
halt."""


GuardInspector = Callable[..., GuardResult]
"""``(diff, allowed_files, template) → GuardResult``. Default wraps
:func:`ccd.guard.inspect_diff`. Tests substitute a stub returning a
canned GuardResult so they can pin guard-pass vs guard-HALT branches
independently of the parser."""


PatchApplier = Callable[..., None]
"""``(repo, patch_text, spec_auto_id) → None`` — spec_040 Integrator seam.

Applies the worker's verified patch to ``repo`` as a single new commit
on the currently-checked-out branch, raising on any failure (the
Integrator catches the exception and HALTs cleanly with
``_HALT_INTEGRATOR_APPLY_FAILED``). The default
:func:`_default_patch_applier` shells out to ``git apply --index`` +
``git commit -m``; tests inject a fake that records calls and is
configurable to raise.

The Integrator is the ONLY caller of this seam — workers never apply
patches (they only produce them). spec_040 §2-2 last bullet: live
writes belong exclusively to the Integrator."""


IsolatedWorkspace = Callable[[Path], Any]
"""``(live_repo) → ContextManager[Path]`` — spec_028 propose-mode seam.

Yields a disposable workspace path the propose loop runs the dispatcher,
R5/R4 verifiers, and the guard against. The default wraps
:func:`ccd.discover._isolated_clone` (spec_014 disposable clone with all
git remotes stripped). Tests inject a stub that yields a tmp dir without
copying the full tree so the live repo is provably never touched.

The yielded path must be a directory that already has a ``.git``
directory if the dispatcher / git_ops will call git inside it — the
default ``_isolated_clone`` provides this. Tests using fake git_ops
do not need a real git checkout."""


class GitOps(Protocol):
    """The git operations the autonomous-fix loop needs.

    Production is :class:`SubprocessGitOps` (subprocess wrappers around
    ``git``). Tests pass a :class:`FakeGitOps` that just records calls.
    The loop never invokes ``git`` directly — every git side-effect goes
    through this seam so a misconfigured test environment cannot poison
    the live repo.

    spec_026 §2-2 adds two more methods (``discard_local_changes`` and
    ``delete_branch``) so the loop can fully restore the working tree on
    every HALT path — without them, a halted run would leave the auto
    branch + uncommitted edits on the live repo and break the next
    night's pre-flight.
    """

    def create_and_checkout_branch(self, *, repo: Path, branch: str) -> None: ...

    def diff(self, *, repo: Path, base: str, head: str) -> str: ...

    def merge_branch_into_main(
        self, *, repo: Path, branch: str
    ) -> None: ...

    def checkout(self, *, repo: Path, ref: str) -> None: ...

    def discard_local_changes(self, *, repo: Path) -> None: ...

    def delete_branch(self, *, repo: Path, branch: str) -> None: ...


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChannelOutcome:
    """One channel's contribution to a nightly run.

    ``report_md_path`` / ``report_json_path`` come straight from the
    underlying channel result and may be ``None`` when the channel
    halted before producing a report (e.g. mutation canary halt from
    spec_019). ``halt_reason`` is the empty string when the channel
    succeeded.
    """

    channel: str
    success: bool
    halt_reason: str
    report_md_path: Path | None
    report_json_path: Path | None


@dataclass(frozen=True)
class AutoFixOutcome:
    """One autonomous-fix attempt's outcome (spec_023 §2-5; mode by spec_028).

    Always populated on :class:`NightlyResult` so the morning brief and
    tests can read the same shape regardless of what happened. The
    ``skipped`` flag distinguishes "the loop ran but bailed early"
    (no candidate, gate off, etc.) from "the loop ran end-to-end and
    either merged or halted".

    When ``skipped`` is True, only ``skip_reason`` (and ``mode``) carry
    meaning; other fields are zero / empty / False. When False:

    - ``spec_auto_id`` / ``spec_auto_path`` — the translated fix-spec.
    - ``finding_signature`` — the target mutation's signature.
    - ``branch`` — the feature branch the fix ran on.
    - ``dispatch_status`` — "done" / "failed" / "blocked".
    - ``r5_killed`` — the target mutation is now killed (spec §2-2).
    - ``r4_suite_passed`` — the full suite is green (spec §2-2).
    - ``guard_passed`` / ``guard_halt_reasons`` — static-guard verdict
      (spec §2-3).
    - ``merged`` — local ``main`` merge happened (auto mode only).
    - ``halt_reason`` — non-empty iff the loop ran but did NOT
      merge / produce a proposal.

    spec_028 adds:

    - ``mode`` — ``"auto"`` (default — bit-for-bit spec_023 behavior),
      ``"propose"`` (new), or ``"off"`` (gate off, not invoked).
    - ``proposed`` — propose mode produced a verified proposal (R5 + R4
      + guard all passed, diff captured).
    - ``proposal_patch_path`` — where the patch file was written under
      ``<live_repo>/_ai_workspace/nightly/proposals/``. ``None`` when
      no proposal was produced (or in auto mode).
    - ``proposal_diff`` — the verified diff embedded in the morning
      brief's §B propose variant. Empty in auto mode.
    """

    skipped: bool
    skip_reason: str = ""
    spec_auto_id: str = ""
    spec_auto_path: Path | None = None
    finding_signature: str = ""
    candidate_count: int = 0
    template: str = ""
    branch: str = ""
    dispatched: bool = False
    dispatch_status: str = ""
    r5_killed: bool = False
    r4_suite_passed: bool = False
    # spec_043 §2-4 — the R4 dynamic-count summary ("collected N, passed N,
    # baseline N") or, on a count-driven R4 fail, the regression reason.
    # Surfaced verbatim in the morning brief's §B verification evidence.
    # Empty keeps the spec_023〜042 brief外形 bit-for-bit identical when
    # counts were unavailable (fake runner / no baseline).
    r4_detail: str = ""
    guard_passed: bool = False
    guard_halt_reasons: tuple[str, ...] = ()
    merged: bool = False
    halt_reason: str = ""
    # spec_025 — diff captured at merge time, surfaced by the morning
    # brief's Phase 2 §B (``run_brief(..., auto_fix=...)``). Empty when
    # the loop halted before the diff step or when the fix was skipped.
    merge_diff: str = ""
    # spec_028 — mode + propose-mode artifacts.
    mode: str = "auto"
    proposed: bool = False
    proposal_patch_path: Path | None = None
    proposal_diff: str = ""
    # spec_039 — FixLoop telemetry. ``iterations`` counts how many
    # dispatch attempts the convergence loop made (0 for skipped
    # candidates, ≥ 1 once the loop body started). ``converged`` is
    # True iff the LAST iteration's R5/R4/guard verification was all
    # green. ``loop_halt_reason`` carries the structural reason the
    # loop ended when it did NOT converge — one of the LOOP_HALT_*
    # anchors from :mod:`ccd.loop`. At the default
    # ``loop_max_iterations=1`` this collapses to ``iterations=1`` and
    # ``converged`` mirrors whether the single iteration was green,
    # keeping the v2 dispatch count + brief layout bit-for-bit
    # identical (spec_039 §3-1). spec_042 consumes these fields for
    # the convergence dashboard.
    iterations: int = 0
    converged: bool = False
    loop_halt_reason: str = ""
    # spec_041 — per-worker telemetry. Empty strings keep the v2 /
    # spec_038〜040 外形 bit-for-bit identical when the new fields are
    # not populated (skip outcomes, gate off, off mode). Populated only
    # for outcomes that flowed through a WorkerPool (auto mode + propose
    # mode when parallelism>1). Timestamps are ISO-8601 with UTC offset.
    worker_id: str = ""
    worker_started_at: str = ""
    worker_finished_at: str = ""


@dataclass
class NightlyResult:
    """``run_nightly`` return value.

    ``success`` is ``True`` only when pre-flight passed *and* the brief
    rendered. Channel-level halts and autonomous-fix HALTs do not flip
    it — Phase 1 wants the operator to still get the morning report when
    (say) the mutation channel canary-halted or the fix didn't merge.

    ``brief_report_wsl`` / ``brief_report_windows`` are populated when
    the brief ran; the Windows mirror may be ``None`` when the mirror
    callback declined to copy (e.g. ``/mnt/c`` not present on this host).

    ``auto_fix`` is always populated when the gate is on (it may have
    ``skipped=True`` if there was no candidate); ``None`` when the gate
    is off (spec_020 behavior unchanged).
    """

    success: bool
    profile: Profile
    channels_run: tuple[ChannelOutcome, ...] = field(default_factory=tuple)
    brief_report_wsl: Path | None = None
    brief_report_windows: Path | None = None
    halt_reason: str = ""
    auto_fix: AutoFixOutcome | None = None
    # spec_038 §2-3 — additional per-candidate outcomes when the profile
    # raises ``safety.max_candidates_per_night`` above 1. Empty tuple at
    # the default K=1 (and when the loop is off / paused), keeping the
    # v2 NightlyResult外形 bit-for-bit identical for default profiles.
    # ``auto_fix`` carries the first candidate's outcome (or the single
    # skip outcome); ``auto_fix_extras`` carries the rest.
    auto_fix_extras: tuple[AutoFixOutcome, ...] = field(default_factory=tuple)
    # spec_025 §2-1(c) — manual kill switch via ``_ai_workspace/PAUSE``.
    # When True, ``run_nightly`` returned without invoking any channel,
    # the auto-fix loop, or the brief.
    paused: bool = False
    # spec_041 — parallelism telemetry surfaced for the morning brief
    # 夜サマリ + spec_042's convergence dashboard. ``parallelism`` is the
    # P that was used (clamped to the profile's bounded value);
    # ``achieved_max_concurrency`` is how many workers were *actually*
    # in-flight at the same time during the run (≤ P; equal to ``1``
    # at the default P=1 or whenever K < P). Both default to ``1`` so
    # the spec_023〜040 外形 stays bit-for-bit identical for default
    # profiles. ``drop_reasons`` collects the gate trip reasons (PAUSE,
    # backlog cap, max_merges cap, wall-clock window) so the morning
    # brief can show drop reasons grouped, and tests can assert the
    # gate that fired by name.
    parallelism: int = 1
    achieved_max_concurrency: int = 1
    drop_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def channels_executed(self) -> tuple[str, ...]:
        """Ordered tuple of channel names actually invoked (for tests + stdout)."""
        return tuple(co.channel for co in self.channels_run)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_nightly(
    *,
    repo: Path,
    profile: Profile | None = None,
    profile_path: Path | None = None,
    channel_runner: ChannelRunner | None = None,
    brief_runner: BriefRunner | None = None,
    windows_mirror: WindowsMirror | None = None,
    today: date | None = None,
    # spec_023 autonomous-fix seams
    agent_runner: AgentRunner | None = None,
    mutation_runner: MutationRunner | None = None,
    fix_dispatcher: FixDispatcher | None = None,
    suite_runner: SuiteRunner | None = None,
    mutation_rechecker: MutationRechecker | None = None,
    # spec_024 — template B R5 seam
    adversarial_rechecker: AdversarialRechecker | None = None,
    guard_inspector: GuardInspector | None = None,
    git_ops: GitOps | None = None,
    # spec_025 — cost / halt boundaries
    unpushed_counter: UnpushedCounter | None = None,
    unpushed_backlog_limit: int | None = None,
    dispatch_timeout_s: float | None = None,
    # spec_028 — propose-mode workspace seam (spec_040 reuses the same
    # seam for auto mode — both modes now run their write-bearing steps
    # inside a fresh disposable clone).
    isolated_workspace: IsolatedWorkspace | None = None,
    # spec_040 — Integrator's patch applier seam. Default shells out to
    # ``git apply --index`` + ``git commit``; tests inject a fake.
    apply_patch: PatchApplier | None = None,
    # spec_029 — per-policy output redirection. The sweep entry point
    # (``ccd nightly-all``) passes CCD-side per-policy directories so the
    # target repo never sees a write (the propose/off invariant) and
    # multi-policy runs keep their artifacts siloed. Default ``None``
    # preserves the single-policy flat layout under ``<repo>/_ai_workspace/``.
    discover_dir: Path | None = None,
    brief_dir: Path | None = None,
    proposal_dir: Path | None = None,
    # spec_042 — per-night v3 snapshot directory. ``None`` keeps the
    # default ``<repo>/_ai_workspace/nightly/records/``. The sweep entry
    # point passes a per-policy directory so client repos never see a
    # write (mirrors the proposal_dir invariant).
    record_dir: Path | None = None,
    # spec_030 — profile-driven adversarial parser injection +
    # synthetic channel-skip surfacing. ``adversarial_parsers`` is a
    # tuple of resolved adversarial parsers (see
    # :func:`ccd.adversarial.resolve_parser_targets`); ``None`` means
    # ``run_channel`` uses the CCD-default fallback (single-CLI path).
    # ``channel_skips`` maps a channel name to the reason it was NOT
    # invoked for this policy — the sweep populates it when a profile
    # opts out (e.g. adversarial channel without
    # ``[discovery.adversarial.parsers]``); the nightly orchestrator
    # then records them as :class:`ChannelOutcome` entries so the
    # morning brief surfaces them in §D rather than the
    # indistinguishable "未実行" line.
    adversarial_parsers: Any = None,
    channel_skips: dict[str, str] | None = None,
) -> NightlyResult:
    """Drive one nightly orchestration end-to-end.

    The linear flow (spec_020 + spec_023 + spec_028):

    1. **Profile** — accept an injected ``profile`` for tests, otherwise
       load via :func:`ccd.profile.load_profile`.
    2. **Pre-flight** — light Phase-1 safety check; halt before any
       channel runs on failure.
    3. **Discovery channels** — one call per enabled channel in
       ``profile.discovery.channels`` order. Per-channel halts are
       recorded but do not stop the loop.
    4. **Fix loop** — dispatched by ``profile.safety.fix_mode``:

       - ``"off"`` → ``auto_fix=None``, preserves spec_020 behavior
         bit-for-bit (no translate / dispatch / merge).
       - ``"auto"`` → spec_023〜026 autonomous-fix loop (translate →
         branch → dispatch → R5 → R4 → guard → local merge). Behavior
         bit-for-bit identical to the old ``autonomous_fix=True``.
       - ``"propose"`` → spec_028 propose loop. Translate → run
         dispatch + R5 + R4 + guard inside a disposable isolated clone
         → capture diff as a patch under ``_ai_workspace/nightly/proposals/``.
         The live working tree is NEVER written to.
    5. **Brief** — render the morning report.
    6. **Windows mirror** — copy to ``/mnt/c/...`` (soft fail).
    """

    repo = Path(repo).resolve()
    effective_profile = (
        profile
        if profile is not None
        else load_profile(repo, profile_path)
    )

    # spec_025 §2-1(c) — manual kill switch. The PAUSE file is checked
    # BEFORE pre-flight; an operator who set PAUSE doesn't want the
    # orchestrator probing the filesystem at all.
    if _pause_file_present(repo):
        return NightlyResult(
            success=True,
            profile=effective_profile,
            paused=True,
            halt_reason=_HALT_PAUSED,
        )

    pre_halt = _pre_flight(repo)
    if pre_halt:
        return NightlyResult(
            success=False,
            profile=effective_profile,
            halt_reason=pre_halt,
        )

    run_channel_fn = channel_runner if channel_runner is not None else run_channel
    run_brief_fn = brief_runner if brief_runner is not None else run_brief
    mirror_fn = windows_mirror if windows_mirror is not None else _default_mirror

    # spec_030 — channels the profile opted out of (e.g. adversarial
    # without ``[discovery.adversarial.parsers]`` in sweep mode) appear
    # as synthetic skip outcomes BEFORE the executed-channel list, so
    # the morning brief's §D surfaces them with a real reason. The skip
    # list is filtered out of the executed list to avoid running them.
    skip_map: dict[str, str] = dict(channel_skips or {})
    executed_channels = [
        c for c in effective_profile.discovery.channels if c not in skip_map
    ]
    # spec_032 — derive the effective mutation config (either the new
    # ``[discovery.mutation]`` block or a wrapper around the legacy
    # ``discovery.mutation_paths``) so the runner gets cwd / tests_dir /
    # extra_args when the profile supplies them.
    mut_cfg = effective_mutation_config(effective_profile.discovery)
    channel_outcomes = _run_channels(
        channels=executed_channels,
        mutation_paths=list(mut_cfg.mutation_paths),
        mutation_config=mut_cfg,
        repo=repo,
        run_channel_fn=run_channel_fn,
        discover_dir=discover_dir,
        adversarial_parsers=adversarial_parsers,
    )
    for skipped_channel, reason in skip_map.items():
        channel_outcomes.append(
            ChannelOutcome(
                channel=skipped_channel,
                success=False,
                halt_reason=reason,
                report_md_path=None,
                report_json_path=None,
            )
        )

    auto_fix: AutoFixOutcome | None = None
    auto_fix_extras: tuple[AutoFixOutcome, ...] = ()
    fix_mode = effective_profile.safety.fix_mode
    # spec_038 — per-night candidate cap (K). Default K=1 keeps the
    # spec_023〜026 single-candidate behavior bit-for-bit.
    max_k = int(effective_profile.safety.max_candidates_per_night)
    # spec_039 — per-candidate FixLoop iteration cap. Default 1 keeps
    # the spec_023〜038 single-shot behavior bit-for-bit.
    loop_max_iters = int(effective_profile.safety.loop_max_iterations)
    # spec_041 — WorkerPool size P + per-night merge cap.
    p_workers = int(effective_profile.safety.parallelism)
    max_merges_n = int(effective_profile.safety.max_merges_per_night)
    # spec_041 — surfaced into NightlyResult so the brief / spec_042
    # aggregator can read "P / 達成同時実行数 / drop reasons" without
    # introspecting individual outcomes.
    achieved_concurrency = 1
    night_drop_reasons: tuple[str, ...] = ()
    if fix_mode == "auto":
        (
            auto_fix,
            auto_fix_extras,
            p_workers,
            achieved_concurrency,
            night_drop_reasons,
        ) = _run_auto_fix_loop(
            repo=repo,
            channels=channel_outcomes,
            fix_templates=tuple(effective_profile.safety.fix_templates),
            today=today,
            agent_runner=agent_runner,
            mutation_runner=mutation_runner,
            fix_dispatcher=fix_dispatcher,
            suite_runner=suite_runner,
            mutation_rechecker=mutation_rechecker,
            adversarial_rechecker=adversarial_rechecker,
            guard_inspector=guard_inspector,
            git_ops=git_ops,
            unpushed_counter=unpushed_counter,
            unpushed_backlog_limit=(
                unpushed_backlog_limit
                if unpushed_backlog_limit is not None
                else _AUTO_FIX_UNPUSHED_BACKLOG_LIMIT
            ),
            dispatch_timeout_s=(
                dispatch_timeout_s
                if dispatch_timeout_s is not None
                else _AUTO_FIX_DISPATCH_TIMEOUT_S
            ),
            isolated_workspace=isolated_workspace,
            apply_patch=apply_patch,
            max_candidates=max_k,
            loop_max_iterations=loop_max_iters,
            parallelism=p_workers,
            max_merges_per_night=max_merges_n,
            proposal_dir=proposal_dir,
        )
    elif fix_mode == "propose":
        auto_fix, auto_fix_extras = _run_propose_loop(
            repo=repo,
            channels=channel_outcomes,
            fix_templates=tuple(effective_profile.safety.fix_templates),
            today=today,
            agent_runner=agent_runner,
            mutation_runner=mutation_runner,
            fix_dispatcher=fix_dispatcher,
            suite_runner=suite_runner,
            mutation_rechecker=mutation_rechecker,
            adversarial_rechecker=adversarial_rechecker,
            guard_inspector=guard_inspector,
            git_ops=git_ops,
            unpushed_counter=unpushed_counter,
            unpushed_backlog_limit=(
                unpushed_backlog_limit
                if unpushed_backlog_limit is not None
                else _AUTO_FIX_UNPUSHED_BACKLOG_LIMIT
            ),
            isolated_workspace=isolated_workspace,
            dispatch_timeout_s=(
                dispatch_timeout_s
                if dispatch_timeout_s is not None
                else _AUTO_FIX_DISPATCH_TIMEOUT_S
            ),
            proposal_dir=proposal_dir,
            max_candidates=max_k,
            loop_max_iterations=loop_max_iters,
        )

    brief_result = run_brief_fn(
        repo=repo,
        today=today,
        auto_fix=auto_fix,
        auto_fix_extras=auto_fix_extras,
        brief_dir=brief_dir,
        discover_dir=discover_dir,
        channel_outcomes=tuple(channel_outcomes),
        parallelism=p_workers if fix_mode == "auto" else 1,
        achieved_max_concurrency=(
            achieved_concurrency if fix_mode == "auto" else 1
        ),
        drop_reasons=night_drop_reasons if fix_mode == "auto" else (),
    )
    brief_md = brief_result.report_path if brief_result.success else None

    windows_path: Path | None = None
    if brief_md is not None:
        try:
            windows_path = mirror_fn(brief_md)
        except OSError:
            windows_path = None

    nightly_result = NightlyResult(
        success=brief_result.success,
        profile=effective_profile,
        channels_run=tuple(channel_outcomes),
        brief_report_wsl=brief_md,
        brief_report_windows=windows_path,
        halt_reason=brief_result.halt_reason if not brief_result.success else "",
        auto_fix=auto_fix,
        auto_fix_extras=auto_fix_extras,
        parallelism=p_workers if fix_mode == "auto" else 1,
        achieved_max_concurrency=(
            achieved_concurrency if fix_mode == "auto" else 1
        ),
        drop_reasons=(
            night_drop_reasons if fix_mode == "auto" else ()
        ),
    )

    # spec_042 — persist the v3 snapshot (best-effort; a write failure
    # never blocks the nightly's overall success — the brief is the
    # operator-facing artifact, the snapshot is the metric feedstock).
    snapshot_root = (
        Path(record_dir).resolve()
        if record_dir is not None
        else repo / _NIGHTLY_RECORDS_DIR_REL
    )
    night_id_str = (today if today is not None else _utc_today()).isoformat()
    try:
        save_night_snapshot(
            nightly_result,
            night_id=night_id_str,
            record_dir=snapshot_root,
        )
    except OSError:
        pass

    return nightly_result


# --------------------------------------------------------------------------- #
# Pre-flight (Phase 1 light version)
# --------------------------------------------------------------------------- #


def _pre_flight(repo: Path) -> str:
    """Return non-empty halt reason iff the orchestrator should refuse to run.

    Phase 1 checks (deliberately minimal — see module docstring for why):

    - ``repo`` exists and is a directory
    - ``<repo>/_ai_workspace`` can be created (or already exists) and is
      writable
    """

    if not repo.exists():
        return f"pre-flight failed: repo path does not exist: {repo}"
    if not repo.is_dir():
        return f"pre-flight failed: repo path is not a directory: {repo}"

    workspace = repo / "_ai_workspace"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"pre-flight failed: cannot create _ai_workspace: {exc}"
    if not os.access(workspace, os.W_OK):
        return f"pre-flight failed: _ai_workspace is not writable: {workspace}"
    return ""


def _pause_file_present(repo: Path) -> bool:
    """spec_025 §2-1(c) — true iff ``<repo>/_ai_workspace/PAUSE`` exists.

    A *file* check, deliberately tolerant: an empty PAUSE file pauses
    the loop just as well as one with explanatory text. Symlinks /
    directories also count — anything at that path is a halt signal
    from the operator. The check never raises; an unreadable
    ``_ai_workspace`` returns False (pre-flight will then surface the
    deeper problem instead).
    """

    try:
        return (repo / _AUTO_FIX_PAUSE_REL).exists()
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Channel execution
# --------------------------------------------------------------------------- #


def _run_channels(
    *,
    channels: list[str],
    mutation_paths: list[str],
    repo: Path,
    run_channel_fn: ChannelRunner,
    discover_dir: Path | None = None,
    adversarial_parsers: Any = None,
    mutation_config: Any = None,
) -> list[ChannelOutcome]:
    """Invoke each enabled channel and collect the four shared fields.

    spec_029: when ``discover_dir`` is provided (per-policy sweep mode),
    forward it to each channel so the discover JSON lands in CCD's
    per-policy workspace instead of the target repo's. None preserves
    the spec_020 flat layout under ``<repo>/_ai_workspace/discover/``.

    spec_030: ``adversarial_parsers`` (when provided) is forwarded to
    the adversarial channel so the sweep can inject profile-driven
    parsers instead of the CCD-default hard-coded set.

    spec_032: ``mutation_config`` carries the cwd / tests_dir /
    extra_args that the profile injects into the mutmut invocation.
    Forwarded only to the mutation channel; other channels ignore it.
    """

    out: list[ChannelOutcome] = []
    for channel in channels:
        paths = mutation_paths if channel == "mutation" else None
        kwargs: dict[str, Any] = {"repo": repo, "paths": paths}
        if discover_dir is not None:
            kwargs["discover_dir"] = discover_dir
        if channel == "adversarial" and adversarial_parsers is not None:
            kwargs["adversarial_parsers"] = adversarial_parsers
        if channel == "mutation" and mutation_config is not None:
            kwargs["mutation_config"] = mutation_config
        try:
            result = run_channel_fn(channel, **kwargs)
        except Exception as exc:
            out.append(
                ChannelOutcome(
                    channel=channel,
                    success=False,
                    halt_reason=(
                        f"{type(exc).__name__}: {exc}".strip() or type(exc).__name__
                    ),
                    report_md_path=None,
                    report_json_path=None,
                )
            )
            continue

        out.append(
            ChannelOutcome(
                channel=channel,
                success=bool(getattr(result, "success", False)),
                halt_reason=str(getattr(result, "halt_reason", "") or ""),
                report_md_path=_coerce_path(getattr(result, "report_md_path", None)),
                report_json_path=_coerce_path(
                    getattr(result, "report_json_path", None)
                ),
            )
        )
    return out


def _coerce_path(p: Any) -> Path | None:
    if p is None:
        return None
    return Path(p)


# --------------------------------------------------------------------------- #
# Autonomous-fix loop (spec_023)
# --------------------------------------------------------------------------- #


# Anchor strings for the morning brief / tests to grep for. Lifted to
# module-level constants so a rename is one place, mirroring the
# constraint-phrase pattern in ccd/translate.py.
# Kept as the historical spec_023 anchor (test still pins this exact
# substring for template-A only profiles). Composed via
# ``_compose_no_candidate_reason`` to extend cleanly to template B.
_HALT_NO_CANDIDATE = "no template-A candidate available"
_HALT_GUARD_HALT = "guard halted the fix"
_HALT_R5_FAILED = "R5 failed: target mutation not killed"
_HALT_R5_FAILED_B = (
    "R5 failed: adversarial case did not become a graceful error"
)
_HALT_R5_FAILED_B_SILENT = (
    "R5 failed: parser silently accepted the broken input "
    "(spec_024 §3 — fix must error gracefully, not succeed)"
)
_HALT_R4_FAILED = "R4 failed: full suite not green"
_HALT_DISPATCH_FAILED = "dispatch failed"
# spec_025 anchors.
_HALT_PAUSED = "paused: _ai_workspace/PAUSE present"
_HALT_DISPATCH_TIMEOUT = "dispatch timed out"
# spec_040 Integrator anchors (auto mode only — the Integrator runs on
# live AFTER the worker phase finishes inside a disposable clone). These
# distinguish "worker said the patch was verified-good but live disagrees"
# (apply / re-verify failure) from worker-side halts. Phrased so the
# morning brief's §D one-liner can name which integrator gate dropped
# the candidate.
_HALT_INTEGRATOR_PREFIX = "integrator"
_HALT_INTEGRATOR_BRANCH_FAILED = (
    "integrator: live feat branch creation failed"
)
_HALT_INTEGRATOR_APPLY_FAILED = "integrator: live patch apply failed"
_HALT_INTEGRATOR_GUARD_FAILED = "integrator: live guard re-check failed"
_HALT_INTEGRATOR_R4_FAILED = "integrator: live suite re-check failed"
_HALT_INTEGRATOR_MERGE_FAILED = "integrator: local merge failed"
_HALT_INTEGRATOR_EMPTY_PATCH = "integrator: verified patch was empty"
# Phrasing the morning brief surfaces verbatim when the un-pushed backlog
# hits the cap. The test for (b) pins this substring so a rename here
# also re-acknowledges the operator-facing wording.
_HALT_UNPUSHED_BACKLOG_PREFIX = (
    "un-pushed autonomous-fix commits at or above limit"
)

# spec_038 §2-3 — when the multi-candidate loop bails mid-night because
# PAUSE appeared or the backlog cap was re-tripped, the remaining
# candidates are summarised as one synthetic skip outcome whose
# ``skip_reason`` starts with this prefix. The morning brief and tests
# pin this substring.
_HALT_REMAINING_SKIPPED_PREFIX = "remaining candidate(s) skipped"

# spec_041 — gate anchors for the Integration queue. Each fires when the
# named gate re-evaluates positive immediately before the next pending
# integration; the remaining verified patches are退避 to proposals/ as a
# rollup skip with one of these prefixes in `skip_reason`.
_HALT_MAX_MERGES_REACHED_PREFIX = "max merges per night cap reached"
_HALT_NIGHT_WALL_CLOCK_PREFIX = "night wall-clock budget exhausted"
# spec_041 — anchor for the AutoFixOutcome a worker emits when its
# spawn / dispatch raised an unhandled exception inside the
# ThreadPoolExecutor. Captures the original exception so the morning
# brief can name "which worker" + "which error" without the parent
# loop having to introspect Future internals.
_HALT_WORKER_CRASHED_PREFIX = "worker crashed"

# spec_025 §2-1 — cost / halt thresholds.
#
# These live as module constants (not profile fields) on purpose:
# - they're a safety invariant of the loop's *physics*, not a per-repo
#   knob (CCD itself = ON, future client repos = OFF — but both share
#   the same wall-clock limit and the same un-pushed-review cap);
# - the docstring at the top of the module is the canonical
#   documentation of the values, so an operator that wants to widen
#   them edits the constant + adds a CHANGELOG note rather than
#   silently changing safety behavior via a profile flip.
#
# Callers (``run_nightly`` kwargs) can still override them for tests
# without touching the constant — the kwargs default to ``None`` and
# fall back to these values, so production paths read the constants and
# tests pass concrete numbers.
_AUTO_FIX_DISPATCH_TIMEOUT_S: float = 40 * 60  # 40 minutes
_AUTO_FIX_UNPUSHED_BACKLOG_LIMIT: int = 3

# spec_025 §2-1(c) — manual kill switch. The PAUSE file is intentionally
# under ``_ai_workspace/`` (already gitignored) so it never lands in a
# commit by accident; the operator drops it manually when something
# looks off, and the next morning ``ccd nightly`` is a no-op until the
# file is removed.
_AUTO_FIX_PAUSE_REL: Path = Path("_ai_workspace") / "PAUSE"

# Template A allowed-file set (test-only). Fixed at module level so the
# loop physically cannot be coerced to widen it for template A — template
# B computes its allowed set dynamically from the finding's target file.
_AUTO_FIX_ALLOWED_FILES_A: tuple[str, ...] = ("tests/",)

# spec_022 kept the constant name ``_AUTO_FIX_ALLOWED_FILES``. We keep the
# old name pointing at template A's set as a backwards-compatible alias so
# any downstream debug code referencing it still works; new code should
# use ``_AUTO_FIX_ALLOWED_FILES_A`` for explicit-ness.
_AUTO_FIX_ALLOWED_FILES = _AUTO_FIX_ALLOWED_FILES_A


def _run_auto_fix_loop(
    *,
    repo: Path,
    channels: list[ChannelOutcome],
    fix_templates: tuple[str, ...],
    today: date | None,
    agent_runner: AgentRunner | None,
    mutation_runner: MutationRunner | None,
    fix_dispatcher: FixDispatcher | None,
    suite_runner: SuiteRunner | None,
    mutation_rechecker: MutationRechecker | None,
    adversarial_rechecker: AdversarialRechecker | None,
    guard_inspector: GuardInspector | None,
    git_ops: GitOps | None,
    unpushed_counter: UnpushedCounter | None,
    unpushed_backlog_limit: int,
    isolated_workspace: IsolatedWorkspace | None,
    apply_patch: PatchApplier | None,
    dispatch_timeout_s: float,
    max_candidates: int = 1,
    loop_max_iterations: int = 1,
    parallelism: int = 1,
    max_merges_per_night: int = 3,
    night_wall_clock_budget_s: float | None = None,
    proposal_dir: Path | None = None,
) -> tuple[
    AutoFixOutcome,
    tuple[AutoFixOutcome, ...],
    int,
    int,
    tuple[str, ...],
]:
    """Drive the autonomous-fix loop for up to ``max_candidates``
    candidates (spec_023 §2-1〜§2-4 + spec_024 + spec_025 cost/halt
    boundaries + spec_038 top-K + spec_041 WorkerPool).

    Returns ``(primary, extras, parallelism, achieved_max_concurrency,
    drop_reasons)``. The trailing three fields surface the spec_041
    parallelism telemetry that ``run_nightly`` records on
    :class:`NightlyResult`; the AutoFixOutcome shape is unchanged.

    Per-candidate work is split into two phases (spec_040):

    - **Worker phase (clone)** — translate → enter disposable clone →
      ``run_fix_loop`` (dispatch + R5/R4/guard) → produce a verified
      diff. K workers run concurrently inside a ``ThreadPoolExecutor``
      of size ``parallelism`` (spec_041 §2-2). Workers do NOT write to
      the live working tree; each worker's clone is rmtree-d on context
      exit.
    - **Integrator phase (live)** — completed verified diffs are
      drained **serially in completion order** through
      :func:`_integrate_one_candidate` (spec_041 §2-3). Before each
      integration (after the first) the loop re-evaluates four gates:
      PAUSE, un-pushed backlog cap, ``max_merges_per_night`` cap, and
      the optional night wall-clock budget. Any tripped gate causes
      the remaining verified patches to be退避 to ``proposals/`` and
      a single rollup skip outcome is appended (spec_041 §2-3).

    Default profile (P=1, K=1) yields ``parallelism=1`` /
    ``achieved_max_concurrency=1`` / ``drop_reasons=()``, with
    submission order = integration order = source-JSON order, keeping
    the spec_023〜040 外形 (dispatch count, brief layout, recorded
    outcome shape) bit-for-bit identical (spec_041 §3-1).
    """

    # spec_025 §2-1(b) — un-pushed backlog cap.
    count_unpushed = (
        unpushed_counter
        if unpushed_counter is not None
        else _default_unpushed_counter
    )

    def _backlog_skip_reason() -> str:
        """Return non-empty skip reason iff the cap is currently tripped."""
        try:
            unpushed_now = int(count_unpushed(repo))
        except Exception:
            # Counter failing (git missing / weird state) is treated as
            # "we can't tell" — fall through. The morning brief surfaces
            # git errors elsewhere.
            return ""
        if unpushed_now >= unpushed_backlog_limit:
            return (
                f"{_HALT_UNPUSHED_BACKLOG_PREFIX} "
                f"({unpushed_now} un-pushed, "
                f"limit {unpushed_backlog_limit}); "
                "review and `git push origin main` before the loop resumes"
            )
        return ""

    p_clamped = max(1, min(4, int(parallelism or 1)))
    initial_skip = _backlog_skip_reason()
    if initial_skip:
        return (
            AutoFixOutcome(skipped=True, skip_reason=initial_skip),
            (),
            p_clamped,
            1,
            (),
        )

    # spec_038 — clamp top-K to a positive integer, defaulting to 1.
    # The profile validator already constrains 1..5 at load time; this
    # second-line defence keeps direct in-process callers safe too.
    limit = max(1, int(max_candidates or 1))
    candidates = _select_candidates(
        channels=channels,
        repo=repo,
        fix_templates=fix_templates,
        limit=limit,
    )
    if not candidates:
        return (
            AutoFixOutcome(
                skipped=True,
                skip_reason=_compose_no_candidate_reason(fix_templates),
            ),
            (),
            p_clamped,
            1,
            (),
        )

    # Resolve seams once for all candidates — there is no per-candidate
    # difference in which dispatcher / suite / rechecker / git_ops is
    # used; only the inputs change.
    gops = git_ops if git_ops is not None else SubprocessGitOps()
    dispatcher = (
        fix_dispatcher
        if fix_dispatcher is not None
        else _build_default_fix_dispatcher(agent_runner)
    )
    run_suite = (
        suite_runner if suite_runner is not None else _default_suite_runner
    )
    recheck_mutation = (
        mutation_rechecker
        if mutation_rechecker is not None
        else _build_default_mutation_rechecker(mutation_runner)
    )
    recheck_adversarial = (
        adversarial_rechecker
        if adversarial_rechecker is not None
        else _default_adversarial_rechecker
    )
    inspect = (
        guard_inspector
        if guard_inspector is not None
        else _default_guard_inspector
    )
    workspace_factory = (
        isolated_workspace
        if isolated_workspace is not None
        else _default_isolated_workspace
    )
    applier = (
        apply_patch if apply_patch is not None else _default_patch_applier
    )

    # spec_041 §2-2 — pre-translate ALL candidates serially BEFORE
    # workers are spawned. spec_auto IDs come from a monotonic counter
    # (:func:`ccd.translate._next_spec_auto_seq`) that is process-local
    # but not thread-safe; running translate serially in the main
    # thread guarantees each worker sees a distinct spec_auto.md.
    pre_translated: list[
        tuple[str, Finding, Path | None, int, Any, str, str]
    ] = []
    for idx, (template, finding, source_report, candidate_count) in enumerate(
        candidates
    ):
        tr = translate_finding(
            finding,
            repo=repo,
            source_report=str(source_report) if source_report else "",
            today=today,
        )
        worker_id = f"w{idx + 1}"
        if not tr.success:
            translated_branch = ""
        elif tr.spec_auto_id:
            translated_branch = f"auto/{tr.spec_auto_id}"
        else:
            translated_branch = ""
        pre_translated.append(
            (
                template,
                finding,
                source_report,
                candidate_count,
                tr,
                worker_id,
                translated_branch,
            )
        )

    return _drain_worker_pool(
        pre_translated=pre_translated,
        repo=repo,
        today=today,
        gops=gops,
        dispatcher=dispatcher,
        run_suite=run_suite,
        recheck_mutation=recheck_mutation,
        recheck_adversarial=recheck_adversarial,
        inspect=inspect,
        workspace_factory=workspace_factory,
        applier=applier,
        dispatch_timeout_s=dispatch_timeout_s,
        loop_max_iterations=loop_max_iterations,
        parallelism=p_clamped,
        max_merges_per_night=max(1, int(max_merges_per_night or 1)),
        night_wall_clock_budget_s=night_wall_clock_budget_s,
        proposal_dir=proposal_dir,
        backlog_skip_reason=_backlog_skip_reason,
    )


@dataclass(frozen=True)
class _WorkerPhaseResult:
    """spec_041 — what :func:`_run_worker_phase` hands the integration queue.

    Either:

    - the worker phase converged and produced a verified patch, in
      which case ``verified_diff`` / ``worker_verification`` / ``fl``
      are populated and ``halt_outcome`` is None — the Integrator will
      run; OR
    - the worker phase halted (translate failed, dispatch crashed,
      didn't converge, etc.), in which case ``halt_outcome`` carries
      the final :class:`AutoFixOutcome` and the patch fields are None
      — no Integrator runs.

    Always populated: ``worker_id``, ``template``, ``finding``,
    ``source_report``, ``candidate_count``, ``spec_auto_id``,
    ``spec_auto_path``, ``branch``, ``allowed_files``,
    ``started_at``, ``finished_at``. The timestamps are ISO-8601
    strings with UTC offset captured around the worker body so the
    morning brief can render duration + parallel efficiency without
    introducing a clock seam in the test layer.
    """

    worker_id: str
    template: str
    finding: Finding
    source_report: Path | None
    candidate_count: int
    spec_auto_id: str
    spec_auto_path: Path | None
    branch: str
    allowed_files: tuple[str, ...]
    started_at: str
    finished_at: str
    verified_diff: str | None
    worker_verification: IterationVerification | None
    fl: FixLoopOutcome | None
    halt_outcome: AutoFixOutcome | None


def _now_iso_utc() -> str:
    """spec_041 — ISO-8601 wall-clock now with UTC offset.

    Centralised so the per-worker timestamps recorded on
    :class:`AutoFixOutcome` are uniformly formatted (the morning brief
    and spec_042's parallel-efficiency aggregation read these as
    strings, never as comparable times — wall-clock is for human
    inspection and audit, not for arithmetic).
    """

    return datetime.now(UTC).isoformat()


def _stamp_worker(
    outcome: AutoFixOutcome,
    *,
    worker_id: str,
    started_at: str,
    finished_at: str,
) -> AutoFixOutcome:
    """spec_041 — attach worker telemetry to an :class:`AutoFixOutcome`.

    :class:`AutoFixOutcome` is a frozen dataclass; the helper
    rebuilds via :func:`dataclasses.replace` so every halt / merge /
    skip outcome can carry ``worker_id`` + start/finish timestamps in
    a single place.
    """

    from dataclasses import replace

    return replace(
        outcome,
        worker_id=worker_id,
        worker_started_at=started_at,
        worker_finished_at=finished_at,
    )


def _run_worker_phase(
    *,
    worker_id: str,
    template: str,
    finding: Finding,
    source_report: Path | None,
    candidate_count: int,
    tr: Any,
    repo: Path,
    gops: GitOps,
    dispatcher: FixDispatcher,
    run_suite: SuiteRunner,
    recheck_mutation: MutationRechecker,
    recheck_adversarial: AdversarialRechecker,
    inspect: GuardInspector,
    workspace_factory: IsolatedWorkspace,
    dispatch_timeout_s: float,
    loop_max_iterations: int,
) -> _WorkerPhaseResult:
    """spec_041 §2-2 — execute one candidate's worker phase inside a
    disposable clone.

    The worker phase mirrors what spec_040 carved out of the old
    inline flow: enter the clone, copy spec_auto.md, create the feat
    branch, run :func:`ccd.loop.run_fix_loop`, snapshot the verified
    diff. **No live writes happen** here — the structural invariant
    pinned by spec_040 §2-2 carries over verbatim.

    Per spec_041 §2-4, exceptions inside the worker body never escape;
    they become a :class:`AutoFixOutcome` HALT with
    ``_HALT_WORKER_CRASHED_PREFIX`` so a single thread crashing does
    not poison its sibling workers' results in the parent
    ``ThreadPoolExecutor``.
    """

    started_at = _now_iso_utc()

    def _finish() -> str:
        return _now_iso_utc()

    if not tr.success:
        finished_at = _finish()
        halt = _stamp_worker(
            AutoFixOutcome(
                skipped=True,
                skip_reason=tr.halt_reason,
                finding_signature=finding.signature,
                candidate_count=candidate_count,
            ),
            worker_id=worker_id,
            started_at=started_at,
            finished_at=finished_at,
        )
        return _WorkerPhaseResult(
            worker_id=worker_id,
            template=template,
            finding=finding,
            source_report=source_report,
            candidate_count=candidate_count,
            spec_auto_id=tr.spec_auto_id or "",
            spec_auto_path=tr.spec_auto_path,
            branch="",
            allowed_files=(),
            started_at=started_at,
            finished_at=finished_at,
            verified_diff=None,
            worker_verification=None,
            fl=None,
            halt_outcome=halt,
        )

    assert tr.spec_auto_path is not None  # success ⇒ path exists

    if template == "A":
        allowed_files = list(_AUTO_FIX_ALLOWED_FILES_A)
    else:  # "B"
        allowed_files = [finding.file, "tests/"]

    branch = f"auto/{tr.spec_auto_id}"

    def _wrap_halt(
        outcome: AutoFixOutcome,
    ) -> _WorkerPhaseResult:
        finished_at = _finish()
        return _WorkerPhaseResult(
            worker_id=worker_id,
            template=template,
            finding=finding,
            source_report=source_report,
            candidate_count=candidate_count,
            spec_auto_id=tr.spec_auto_id,
            spec_auto_path=tr.spec_auto_path,
            branch=branch,
            allowed_files=tuple(allowed_files),
            started_at=started_at,
            finished_at=finished_at,
            verified_diff=None,
            worker_verification=None,
            fl=None,
            halt_outcome=_stamp_worker(
                outcome,
                worker_id=worker_id,
                started_at=started_at,
                finished_at=finished_at,
            ),
        )

    try:
        with workspace_factory(repo) as clone:
            clone_path = Path(clone)
            spec_auto_in_clone = _copy_spec_auto_into_clone(
                spec_auto_live=tr.spec_auto_path,
                live_repo=repo,
                clone=clone_path,
            )

            try:
                gops.create_and_checkout_branch(
                    repo=clone_path, branch=branch
                )
            except Exception as exc:
                return _wrap_halt(
                    AutoFixOutcome(
                        skipped=False,
                        spec_auto_id=tr.spec_auto_id,
                        spec_auto_path=tr.spec_auto_path,
                        finding_signature=finding.signature,
                        candidate_count=candidate_count,
                        template=template,
                        branch=branch,
                        halt_reason=(
                            "worker: branch creation failed in clone: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                )

            # spec_043 §2-2 — measure the R4 baseline on the *unmodified*
            # clone, BEFORE the fix is dispatched. The feat branch is at
            # HEAD-of-main content here, so this is the pre-fix
            # (collected, passed) count the post-fix verifier compares
            # against. Measured inside the disposable clone — live is
            # never touched. A runner that raises (env breakage) yields a
            # ``None`` baseline, which disengages the count gate for this
            # candidate (the post-fix exit-0 check still applies); the
            # cost is one extra pytest run per candidate (spec §2-2 note).
            try:
                r4_baseline: SuiteOutcome | None = run_suite(repo=clone_path)
            except Exception:
                r4_baseline = None

            def _verifier(
                *, repo: Path, branch: str
            ) -> IterationVerification:
                return _verify_iteration_auto(
                    template=template,
                    finding=finding,
                    allowed_files=allowed_files,
                    repo=repo,
                    branch=branch,
                    gops=gops,
                    baseline=r4_baseline,
                    run_suite=run_suite,
                    recheck_mutation=recheck_mutation,
                    recheck_adversarial=recheck_adversarial,
                    inspect=inspect,
                )

            try:
                fl: FixLoopOutcome = run_fix_loop(
                    spec_path=spec_auto_in_clone,
                    repo=clone_path,
                    branch=branch,
                    dispatcher=dispatcher,
                    verifier=_verifier,
                    max_iterations=loop_max_iterations,
                    wall_clock_budget_s=dispatch_timeout_s,
                    feedback_dir=clone_path / "_ai_workspace" / "logs",
                    spec_id=tr.spec_auto_id,
                )
            except Exception as exc:
                return _wrap_halt(
                    AutoFixOutcome(
                        skipped=False,
                        spec_auto_id=tr.spec_auto_id,
                        spec_auto_path=tr.spec_auto_path,
                        finding_signature=finding.signature,
                        candidate_count=candidate_count,
                        template=template,
                        branch=branch,
                        dispatched=False,
                        halt_reason=(
                            f"{_HALT_DISPATCH_FAILED}: "
                            f"{type(exc).__name__}: {exc}".strip()
                        ),
                        iterations=0,
                        converged=False,
                        loop_halt_reason=LOOP_HALT_IMMEDIATE,
                    )
                )

            verif = fl.final_verification

            # Worker HALT A: dispatch never produced a verifiable state.
            if verif is None:
                dispatch_status = fl.final_dispatch_status or "failed"
                reason = (
                    fl.final_dispatch_halt_reason
                    or fl.halt_reason
                    or _HALT_DISPATCH_FAILED
                )
                return _wrap_halt(
                    AutoFixOutcome(
                        skipped=False,
                        spec_auto_id=tr.spec_auto_id,
                        spec_auto_path=tr.spec_auto_path,
                        finding_signature=finding.signature,
                        candidate_count=candidate_count,
                        template=template,
                        branch=branch,
                        dispatched=fl.final_dispatched,
                        dispatch_status=dispatch_status,
                        halt_reason=f"{_HALT_DISPATCH_FAILED}: {reason}",
                        iterations=fl.iterations,
                        converged=False,
                        loop_halt_reason=fl.halt_reason,
                    )
                )

            # Worker HALT B: ran out of iterations without converging.
            if not fl.converged:
                halt_reason = _compose_halt_reason(
                    template=template,
                    r5_killed=verif.r5_passed,
                    r5_status=verif.r5_status,
                    r4_passed=verif.r4_passed,
                    guard_passed=verif.guard_passed,
                    guard_reasons=verif.guard_reasons,
                    r4_detail=verif.r4_detail,
                )
                if (
                    fl.halt_reason
                    and fl.halt_reason != LOOP_HALT_MAX_ITERATIONS
                    and loop_max_iterations > 1
                ):
                    halt_reason = f"{halt_reason} [{fl.halt_reason}]"
                elif (
                    fl.halt_reason
                    and fl.halt_reason == LOOP_HALT_MAX_ITERATIONS
                    and loop_max_iterations > 1
                ):
                    halt_reason = (
                        f"{halt_reason} [fix-loop exhausted "
                        f"{loop_max_iterations} iterations]"
                    )
                return _wrap_halt(
                    AutoFixOutcome(
                        skipped=False,
                        spec_auto_id=tr.spec_auto_id,
                        spec_auto_path=tr.spec_auto_path,
                        finding_signature=finding.signature,
                        candidate_count=candidate_count,
                        template=template,
                        branch=branch,
                        dispatched=True,
                        dispatch_status=fl.final_dispatch_status,
                        r5_killed=verif.r5_passed,
                        r4_suite_passed=verif.r4_passed,
                        r4_detail=verif.r4_detail,
                        guard_passed=verif.guard_passed,
                        guard_halt_reasons=verif.guard_reasons,
                        merged=False,
                        halt_reason=halt_reason,
                        iterations=fl.iterations,
                        converged=False,
                        loop_halt_reason=fl.halt_reason,
                    )
                )

            # Worker phase converged: snapshot the verified state.
            verified_diff = verif.diff
            verified_verification = verif
            verified_fl = fl
    except Exception as exc:
        return _wrap_halt(
            AutoFixOutcome(
                skipped=False,
                spec_auto_id=tr.spec_auto_id,
                spec_auto_path=tr.spec_auto_path,
                finding_signature=finding.signature,
                candidate_count=candidate_count,
                template=template,
                branch=branch,
                halt_reason=(
                    "worker: workspace setup failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        )

    finished_at = _finish()
    return _WorkerPhaseResult(
        worker_id=worker_id,
        template=template,
        finding=finding,
        source_report=source_report,
        candidate_count=candidate_count,
        spec_auto_id=tr.spec_auto_id,
        spec_auto_path=tr.spec_auto_path,
        branch=branch,
        allowed_files=tuple(allowed_files),
        started_at=started_at,
        finished_at=finished_at,
        verified_diff=verified_diff,
        worker_verification=verified_verification,
        fl=verified_fl,
        halt_outcome=None,
    )


def _drain_worker_pool(
    *,
    pre_translated: list[
        tuple[str, Finding, Path | None, int, Any, str, str]
    ],
    repo: Path,
    today: date | None,
    gops: GitOps,
    dispatcher: FixDispatcher,
    run_suite: SuiteRunner,
    recheck_mutation: MutationRechecker,
    recheck_adversarial: AdversarialRechecker,
    inspect: GuardInspector,
    workspace_factory: IsolatedWorkspace,
    applier: PatchApplier,
    dispatch_timeout_s: float,
    loop_max_iterations: int,
    parallelism: int,
    max_merges_per_night: int,
    night_wall_clock_budget_s: float | None,
    proposal_dir: Path | None,
    backlog_skip_reason: Callable[[], str],
) -> tuple[
    AutoFixOutcome,
    tuple[AutoFixOutcome, ...],
    int,
    int,
    tuple[str, ...],
]:
    """spec_041 §2-2/§2-3 — dispatch K candidates through a worker pool
    of size P, then drain completed verified patches serially through
    the Integrator.

    Workers are submitted **dynamically** (initial fill up to P, then
    one more after each completion) rather than all-at-once, so that
    gate-triggered drop cleanly suppresses dispatches for unprocessed
    candidates — preserving the spec_038 "no useless dispatch after
    backlog cap trips" semantic at P=1 (bit-for-bit) and extending it
    naturally to P > 1.

    Returns ``(primary, extras, parallelism, achieved_max_concurrency,
    drop_reasons)``. ``achieved_max_concurrency`` is the max number of
    workers actually in-flight at the same time (≤ P, equal to 1 at
    the default P=1).
    """

    total_K = len(pre_translated)
    queue = list(pre_translated)
    in_flight: dict[concurrent.futures.Future, str] = {}
    achieved_max_concurrency = 1
    night_start = time.monotonic()

    # Per the spec_041 §2-3 gate model, the integration queue is
    # serialised — only the main thread invokes the Integrator. The
    # lock object below is documented for future reference (worker
    # threads never integrate), keeping the design self-documenting
    # even though in current code only the main thread integrates.
    integration_lock = threading.Lock()
    del integration_lock  # not yet exercised by tests; future-proofing only

    def _evaluate_gates(*, merges_done: int) -> str:
        """spec_041 §2-3 — return non-empty reason iff a gate trips."""
        if _pause_file_present(repo):
            return "PAUSE: `_ai_workspace/PAUSE` が現れた"
        bsr = backlog_skip_reason()
        if bsr:
            return bsr
        if merges_done >= max_merges_per_night:
            return (
                f"{_HALT_MAX_MERGES_REACHED_PREFIX} "
                f"({merges_done} merges, limit {max_merges_per_night})"
            )
        if (
            night_wall_clock_budget_s is not None
            and night_wall_clock_budget_s > 0
        ):
            elapsed = time.monotonic() - night_start
            if elapsed >= night_wall_clock_budget_s:
                return (
                    f"{_HALT_NIGHT_WALL_CLOCK_PREFIX} "
                    f"({elapsed:.0f}s elapsed, "
                    f"budget {night_wall_clock_budget_s:.0f}s)"
                )
        return ""

    outcomes: list[AutoFixOutcome] = []
    drop_reasons: list[str] = []
    processed_count = 0  # workers whose result we routed (merge / halt / etc.)
    merges_done = 0
    gate_tripped = ""

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=parallelism, thread_name_prefix="ccd-worker"
    )

    def _submit_next() -> None:
        if not queue:
            return
        record = queue.pop(0)
        (
            template,
            finding,
            source_report,
            candidate_count,
            tr,
            worker_id,
            _branch_hint,
        ) = record
        fut = executor.submit(
            _run_worker_phase,
            worker_id=worker_id,
            template=template,
            finding=finding,
            source_report=source_report,
            candidate_count=candidate_count,
            tr=tr,
            repo=repo,
            gops=gops,
            dispatcher=dispatcher,
            run_suite=run_suite,
            recheck_mutation=recheck_mutation,
            recheck_adversarial=recheck_adversarial,
            inspect=inspect,
            workspace_factory=workspace_factory,
            dispatch_timeout_s=dispatch_timeout_s,
            loop_max_iterations=loop_max_iterations,
        )
        in_flight[fut] = worker_id

    def _maybe_save_dropped(wpr: _WorkerPhaseResult) -> None:
        """spec_041 §2-3 — 退避 a verified-but-dropped patch."""
        if wpr.verified_diff:
            _save_dropped_patch(
                live_repo=repo,
                spec_auto_id=wpr.spec_auto_id
                or f"dropped_{wpr.worker_id}",
                diff_text=wpr.verified_diff,
                today=today,
                proposal_dir=proposal_dir,
            )

    def _gate_check(*, processed_already: int) -> bool:
        """spec_041 §2-3 — set ``gate_tripped`` iff any gate fires.

        Called BOTH before each integration (after the first) AND before
        each new submission (after the first ``parallelism`` initial
        fills). Two-point checking preserves the spec_038 "no useless
        dispatch after backlog cap trips" semantic at P=1 — the
        integration drain alone cannot prevent the next worker's
        ``claude`` subprocess from spawning, so the spec_041 gate also
        gates submission.
        """
        nonlocal gate_tripped
        if gate_tripped:
            return True
        if processed_already <= 0:
            return False  # pre-loop check already ran
        reason = _evaluate_gates(merges_done=merges_done)
        if reason:
            gate_tripped = reason
            return True
        return False

    try:
        # Initial fill up to P workers (no per-submission gate check —
        # the pre-loop check in ``_run_auto_fix_loop`` already ran).
        for _ in range(parallelism):
            if not queue:
                break
            _submit_next()
            if len(in_flight) > achieved_max_concurrency:
                achieved_max_concurrency = len(in_flight)

        while in_flight:
            if len(in_flight) > achieved_max_concurrency:
                achieved_max_concurrency = len(in_flight)
            done_set, _pending = concurrent.futures.wait(
                in_flight.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done_set:
                worker_id = in_flight.pop(fut)
                try:
                    wpr = fut.result()
                except BaseException as exc:  # noqa: BLE001 — record + continue
                    # spec_041 §2-4 — record the crash and keep going.
                    wpr = _build_crash_worker_result(
                        worker_id=worker_id, exc=exc
                    )

                # spec_041 §2-3 gate-before-integration (after first).
                if _gate_check(processed_already=processed_count):
                    _maybe_save_dropped(wpr)
                    continue

                # No gate tripped — route this worker's result.
                if wpr.halt_outcome is not None:
                    outcomes.append(wpr.halt_outcome)
                else:
                    integrated = _integrate_one_candidate(
                        spec_auto_id=wpr.spec_auto_id,
                        spec_auto_path=wpr.spec_auto_path,
                        finding=wpr.finding,
                        template=wpr.template,
                        allowed_files=list(wpr.allowed_files),
                        candidate_count=wpr.candidate_count,
                        branch=wpr.branch,
                        diff_text=wpr.verified_diff or "",
                        worker_verification=wpr.worker_verification,
                        fl=wpr.fl,
                        loop_max_iterations=loop_max_iterations,
                        live_repo=repo,
                        gops=gops,
                        run_suite=run_suite,
                        inspect=inspect,
                        apply_patch=applier,
                    )
                    integrated = _stamp_worker(
                        integrated,
                        worker_id=worker_id,
                        started_at=wpr.started_at,
                        finished_at=wpr.finished_at,
                    )
                    outcomes.append(integrated)
                    if integrated.merged:
                        merges_done += 1
                processed_count += 1

                # spec_041 §2-3 gate-before-submission. Re-evaluating
                # gates here keeps the spec_038 dispatch-count
                # semantic (the next worker's ``claude`` subprocess
                # does NOT spawn after backlog / PAUSE / max_merges
                # trips) — at P=1 this is bit-for-bit identical to
                # the pre-spec_041 sequential loop's behavior.
                if queue and not _gate_check(
                    processed_already=processed_count
                ):
                    _submit_next()
                    if len(in_flight) > achieved_max_concurrency:
                        achieved_max_concurrency = len(in_flight)
    finally:
        executor.shutdown(wait=True, cancel_futures=False)

    if gate_tripped:
        remaining = total_K - processed_count
        if remaining > 0:
            outcomes.append(
                AutoFixOutcome(
                    skipped=True,
                    skip_reason=(
                        f"{_HALT_REMAINING_SKIPPED_PREFIX}: "
                        f"{remaining} 件 ({gate_tripped})"
                    ),
                )
            )
        drop_reasons.append(gate_tripped)

    if not outcomes:
        # All K workers consumed silently (only possible if total_K==0,
        # already handled by the caller — defence in depth so the
        # caller never has to special-case the empty tuple).
        return (
            AutoFixOutcome(
                skipped=True,
                skip_reason="auto loop produced no outcomes",
            ),
            (),
            parallelism,
            achieved_max_concurrency,
            tuple(drop_reasons),
        )

    primary = outcomes[0]
    extras = tuple(outcomes[1:])
    return (
        primary,
        extras,
        parallelism,
        achieved_max_concurrency,
        tuple(drop_reasons),
    )


def _build_crash_worker_result(
    *,
    worker_id: str,
    exc: BaseException,
) -> _WorkerPhaseResult:
    """spec_041 §2-4 — wrap a ``ThreadPoolExecutor``-level exception.

    The worker body itself catches everything (see :func:`_run_worker_phase`),
    so reaching here means the executor itself raised — extremely rare,
    e.g. the worker thread was killed by a signal. We still record the
    candidate as a HALT so the morning brief surfaces ``worker crashed``
    rather than silently dropping the entry.
    """

    stamp = _now_iso_utc()
    return _WorkerPhaseResult(
        worker_id=worker_id,
        template="",
        # Build a placeholder Finding so the brief / record JSON has
        # the same shape — we have no candidate context once the executor
        # fails (futures map only stores worker_id).
        finding=Finding(
            channel="",
            file="",
            line=0,
            mutation="",
            signature=f"crashed-{worker_id}",
            status="",
            source_report="",
        ),
        source_report=None,
        candidate_count=0,
        spec_auto_id="",
        spec_auto_path=None,
        branch="",
        allowed_files=(),
        started_at=stamp,
        finished_at=stamp,
        verified_diff=None,
        worker_verification=None,
        fl=None,
        halt_outcome=_stamp_worker(
            AutoFixOutcome(
                skipped=False,
                halt_reason=(
                    f"{_HALT_WORKER_CRASHED_PREFIX}: "
                    f"{type(exc).__name__}: {exc}".strip()
                ),
            ),
            worker_id=worker_id,
            started_at=stamp,
            finished_at=stamp,
        ),
    )


def _save_dropped_patch(
    *,
    live_repo: Path,
    spec_auto_id: str,
    diff_text: str,
    today: date | None,
    proposal_dir: Path | None,
) -> Path:
    """spec_041 §2-3 — stash a verified-but-dropped patch under
    ``_ai_workspace/nightly/proposals/`` with a ``dropped_`` filename
    prefix so the morning brief can surface it alongside propose-mode
    artifacts (same shape, different reason).
    """

    today_d = today if today is not None else _utc_today()
    proposals_dir = (
        Path(proposal_dir).resolve()
        if proposal_dir is not None
        else live_repo / _PROPOSAL_DIR_REL
    )
    proposals_dir.mkdir(parents=True, exist_ok=True)
    patch_path = proposals_dir / (
        f"dropped_{today_d.isoformat()}_{spec_auto_id}.patch"
    )
    body = diff_text if diff_text.endswith("\n") else diff_text + "\n"
    patch_path.write_text(body, encoding="utf-8")
    return patch_path


def _process_one_auto_fix_candidate(
    *,
    template: str,
    finding: Finding,
    source_report: Path | None,
    candidate_count: int,
    repo: Path,
    today: date | None,
    gops: GitOps,
    dispatcher: FixDispatcher,
    run_suite: SuiteRunner,
    recheck_mutation: MutationRechecker,
    recheck_adversarial: AdversarialRechecker,
    inspect: GuardInspector,
    workspace_factory: IsolatedWorkspace,
    apply_patch: PatchApplier,
    dispatch_timeout_s: float,
    loop_max_iterations: int = 1,
) -> AutoFixOutcome:
    """spec_040 §2-1〜§2-2 — per-candidate body, unified clone-and-patch.

    Auto mode no longer mutates the live working tree during the worker
    phase. The two-phase flow is:

    Worker phase (in a disposable clone):

    1. Translate the finding to ``spec_auto.md`` against the LIVE repo
       (audit artifact; lives under ``_ai_workspace/bridge/inbox/`` so
       the operator can always inspect what the agent was asked to do).
    2. Enter a fresh ``isolated_workspace`` clone (spec_014's
       ``_isolated_clone`` strips all git remotes — clone cannot push).
    3. Copy ``spec_auto.md`` into the clone (``_ai_workspace`` is
       excluded from clone copy, so we re-introduce just the spec).
    4. Create a feat branch ``auto/<spec_auto_id>`` in the clone.
    5. Run :func:`ccd.loop.run_fix_loop` — dispatch + R5 + R4 + guard,
       iterated up to ``loop_max_iterations`` until verification is
       green (spec_039). All writes go to the clone; if every iteration
       fails the candidate is dropped without touching live.

    Integrator phase (on live — the ONLY writer to live):

    6. Hand the verified diff to :func:`_integrate_one_candidate` which
       creates the same feat branch on live, applies the patch as a
       single commit, re-runs guard + R4 against live, and (if both
       pass) merges into ``main``. Any failure → drop + live restore
       (no rebase, no re-dispatch — spec_040 §2-2).

    Spec_040 §2-2 invariant: every live-side write the Integrator does
    goes through ``gops``, every worker-side write goes through the
    same ``gops`` but with ``repo=<clone_path>``. The structural test
    :func:`test_worker_phase_does_not_write_to_live` pins this.
    """

    # 1. Translate against LIVE (the spec_auto.md is the shared audit
    # artifact — both modes route their translator through the live
    # bridge dir).
    tr = translate_finding(
        finding,
        repo=repo,
        source_report=str(source_report) if source_report else "",
        today=today,
    )
    if not tr.success:
        return AutoFixOutcome(
            skipped=True,
            skip_reason=tr.halt_reason,
            finding_signature=finding.signature,
            candidate_count=candidate_count,
        )

    assert tr.spec_auto_path is not None  # success ⇒ path exists

    # Per-template guard config — used in both worker and Integrator
    # phases (Integrator re-runs guard against live).
    if template == "A":
        allowed_files = list(_AUTO_FIX_ALLOWED_FILES_A)
    else:  # "B"
        allowed_files = [finding.file, "tests/"]

    branch = f"auto/{tr.spec_auto_id}"

    # 2. Worker phase — runs entirely inside a fresh disposable clone.
    # The clone is rmtree-d on context exit, so even mid-flight crashes
    # cannot leave debris on live.
    try:
        with workspace_factory(repo) as clone:
            clone_path = Path(clone)
            spec_auto_in_clone = _copy_spec_auto_into_clone(
                spec_auto_live=tr.spec_auto_path,
                live_repo=repo,
                clone=clone_path,
            )

            try:
                gops.create_and_checkout_branch(
                    repo=clone_path, branch=branch
                )
            except Exception as exc:
                return AutoFixOutcome(
                    skipped=False,
                    spec_auto_id=tr.spec_auto_id,
                    spec_auto_path=tr.spec_auto_path,
                    finding_signature=finding.signature,
                    candidate_count=candidate_count,
                    template=template,
                    branch=branch,
                    halt_reason=(
                        "worker: branch creation failed in clone: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

            def _verifier(
                *, repo: Path, branch: str
            ) -> IterationVerification:
                return _verify_iteration_auto(
                    template=template,
                    finding=finding,
                    allowed_files=allowed_files,
                    repo=repo,
                    branch=branch,
                    gops=gops,
                    run_suite=run_suite,
                    recheck_mutation=recheck_mutation,
                    recheck_adversarial=recheck_adversarial,
                    inspect=inspect,
                )

            try:
                fl: FixLoopOutcome = run_fix_loop(
                    spec_path=spec_auto_in_clone,
                    repo=clone_path,
                    branch=branch,
                    dispatcher=dispatcher,
                    verifier=_verifier,
                    max_iterations=loop_max_iterations,
                    wall_clock_budget_s=dispatch_timeout_s,
                    feedback_dir=clone_path / "_ai_workspace" / "logs",
                    spec_id=tr.spec_auto_id,
                )
            except Exception as exc:
                return AutoFixOutcome(
                    skipped=False,
                    spec_auto_id=tr.spec_auto_id,
                    spec_auto_path=tr.spec_auto_path,
                    finding_signature=finding.signature,
                    candidate_count=candidate_count,
                    template=template,
                    branch=branch,
                    dispatched=False,
                    halt_reason=(
                        f"{_HALT_DISPATCH_FAILED}: "
                        f"{type(exc).__name__}: {exc}".strip()
                    ),
                    iterations=0,
                    converged=False,
                    loop_halt_reason=LOOP_HALT_IMMEDIATE,
                )

            verif = fl.final_verification

            # Worker HALT A: dispatch never produced a verifiable state.
            if verif is None:
                dispatch_status = fl.final_dispatch_status or "failed"
                reason = (
                    fl.final_dispatch_halt_reason
                    or fl.halt_reason
                    or _HALT_DISPATCH_FAILED
                )
                return AutoFixOutcome(
                    skipped=False,
                    spec_auto_id=tr.spec_auto_id,
                    spec_auto_path=tr.spec_auto_path,
                    finding_signature=finding.signature,
                    candidate_count=candidate_count,
                    template=template,
                    branch=branch,
                    dispatched=fl.final_dispatched,
                    dispatch_status=dispatch_status,
                    halt_reason=f"{_HALT_DISPATCH_FAILED}: {reason}",
                    iterations=fl.iterations,
                    converged=False,
                    loop_halt_reason=fl.halt_reason,
                )

            # Worker HALT B: ran out of iterations without converging.
            if not fl.converged:
                halt_reason = _compose_halt_reason(
                    template=template,
                    r5_killed=verif.r5_passed,
                    r5_status=verif.r5_status,
                    r4_passed=verif.r4_passed,
                    guard_passed=verif.guard_passed,
                    guard_reasons=verif.guard_reasons,
                    r4_detail=verif.r4_detail,
                )
                if (
                    fl.halt_reason
                    and fl.halt_reason != LOOP_HALT_MAX_ITERATIONS
                    and loop_max_iterations > 1
                ):
                    halt_reason = f"{halt_reason} [{fl.halt_reason}]"
                elif (
                    fl.halt_reason
                    and fl.halt_reason == LOOP_HALT_MAX_ITERATIONS
                    and loop_max_iterations > 1
                ):
                    halt_reason = (
                        f"{halt_reason} [fix-loop exhausted "
                        f"{loop_max_iterations} iterations]"
                    )
                return AutoFixOutcome(
                    skipped=False,
                    spec_auto_id=tr.spec_auto_id,
                    spec_auto_path=tr.spec_auto_path,
                    finding_signature=finding.signature,
                    candidate_count=candidate_count,
                    template=template,
                    branch=branch,
                    dispatched=True,
                    dispatch_status=fl.final_dispatch_status,
                    r5_killed=verif.r5_passed,
                    r4_suite_passed=verif.r4_passed,
                    r4_detail=verif.r4_detail,
                    guard_passed=verif.guard_passed,
                    guard_halt_reasons=verif.guard_reasons,
                    merged=False,
                    halt_reason=halt_reason,
                    iterations=fl.iterations,
                    converged=False,
                    loop_halt_reason=fl.halt_reason,
                )

            # Worker phase converged: snapshot the verified state so it
            # outlives the clone (which is about to be rmtree-d).
            verified_diff = verif.diff
            verified_verification = verif
            verified_fl = fl
    except Exception as exc:
        return AutoFixOutcome(
            skipped=False,
            spec_auto_id=tr.spec_auto_id,
            spec_auto_path=tr.spec_auto_path,
            finding_signature=finding.signature,
            candidate_count=candidate_count,
            template=template,
            branch=branch,
            halt_reason=(
                "worker: workspace setup failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    # Clone has been rmtree-d; only the verified diff + verification
    # metadata survive into the Integrator phase.

    # 3. Integrator phase — the ONLY code path that writes to live.
    return _integrate_one_candidate(
        spec_auto_id=tr.spec_auto_id,
        spec_auto_path=tr.spec_auto_path,
        finding=finding,
        template=template,
        allowed_files=allowed_files,
        candidate_count=candidate_count,
        branch=branch,
        diff_text=verified_diff,
        worker_verification=verified_verification,
        fl=verified_fl,
        loop_max_iterations=loop_max_iterations,
        live_repo=repo,
        gops=gops,
        run_suite=run_suite,
        inspect=inspect,
        apply_patch=apply_patch,
    )


# spec_043 §2-4 — anchor the morning brief / tests pin when R4 fails
# because the post-fix suite ran *fewer* tests than the pre-fix baseline.
# The dynamic count gate is the主防御 against test-muting (RT-1/4/7); this
# string names it so §B/§D surface "実行テスト数が baseline を下回った".
_R4_COUNT_REGRESSION = "executed test count fell below baseline"
_R4_COUNT_UNAVAILABLE = (
    "post-fix test counts unparseable while baseline was known "
    "— conservative R4 fail (件数不明は減少を否定できない)"
)


def _r4_count_summary(
    after: SuiteOutcome, baseline: SuiteOutcome | None
) -> str:
    """Human-readable "collected N, passed N, baseline N" for the brief.

    Only includes the fields that are known; an all-``None`` outcome
    (fake runner / unparseable) renders the empty string so the brief
    falls back to its plain ``pass/fail`` line (spec_043 §2-4)."""

    parts: list[str] = []
    if after.collected is not None:
        parts.append(f"collected {after.collected}")
    if after.passed_count is not None:
        parts.append(f"passed {after.passed_count}")
    if baseline is not None and baseline.passed_count is not None:
        parts.append(f"baseline {baseline.passed_count}")
    return ", ".join(parts)


def _r4_verdict(
    after: SuiteOutcome, baseline: SuiteOutcome | None
) -> tuple[bool, str]:
    """spec_043 §2-2 — R4 verdict = "green AND did not shrink".

    Returns ``(passed, detail)``. ``detail`` is the count summary on a
    pass (surfaced in §B) and the failure reason on a fail (surfaced in
    the halt reason).

    Layering:

    1. The legacy "pytest exit 0" condition is still必須 — a red suite
       fails R4 regardless of counts.
    2. The count non-regression gate engages **only when we have a
       baseline with known counts**. The baseline is measured on the
       *unmodified* clone before the fix is dispatched, so an attacker's
       fix cannot influence it; in production
       :func:`_default_suite_runner` reliably yields counts (so the gate
       is always live there). When the baseline itself is unparseable
       (``None`` counts — only happens with test fakes or a genuinely
       broken pytest, never from a fix) there is no number to regress
       below, so the gate is skipped and only the exit-0 condition
       applies. This keeps the spec_023〜042 R4 tests bit-for-bit green
       while closing the偽陰性 where it actually matters.
    3. Once a known baseline exists, a ``None`` *post-fix* count is the
       realistic muting signal (a fix that breaks collection / parsing)
       → conservative fail. A drop in either ``passed`` or ``collected``
       → fail with the count-regression anchor.

    spec_043 §2-2 wording is "いずれかが None なら R4 fail"; we scope the
    conservative-on-None rule to the post-fix side (the side a fix
    controls) and treat a missing *baseline* as "gate not engaged"
    rather than a hard fail, so the loop is not bricked on repos whose
    pytest output we cannot parse. See result_043 §honest for the
    rationale.
    """

    if not after.passed:
        return False, "full suite not green"

    # Gate engages only with a baseline carrying known counts.
    if (
        baseline is None
        or baseline.collected is None
        or baseline.passed_count is None
    ):
        return True, _r4_count_summary(after, baseline)

    if after.collected is None or after.passed_count is None:
        return False, _R4_COUNT_UNAVAILABLE

    if after.passed_count < baseline.passed_count:
        return (
            False,
            f"{_R4_COUNT_REGRESSION} "
            f"(passed {after.passed_count} < baseline {baseline.passed_count})",
        )
    if after.collected < baseline.collected:
        return (
            False,
            f"{_R4_COUNT_REGRESSION} "
            f"(collected {after.collected} < "
            f"baseline {baseline.collected})",
        )
    return True, _r4_count_summary(after, baseline)


def _verify_iteration_auto(
    *,
    template: str,
    finding: Finding,
    allowed_files: list[str],
    repo: Path,
    branch: str,
    gops: GitOps,
    run_suite: SuiteRunner,
    recheck_mutation: MutationRechecker,
    recheck_adversarial: AdversarialRechecker,
    inspect: GuardInspector,
    baseline: SuiteOutcome | None = None,
) -> IterationVerification:
    """Run R5 + R4 + guard for a single iteration in auto mode.

    Extracted so the propose loop and the auto loop share the same
    verifier internals, and so tests can spot-check one iteration
    without mounting the whole FixLoop.

    Exception handling mirrors the pre-spec_039 inline flow: R5 inside
    :func:`_verify_r5` already absorbs rechecker exceptions; R4 / guard
    do their own try/except so a single misbehaving seam degrades to
    "this iteration failed" rather than propagating out of FixLoop.
    """

    r5_passed, r5_status = _verify_r5(
        template=template,
        finding=finding,
        recheck_mutation=recheck_mutation,
        recheck_adversarial=recheck_adversarial,
        repo=repo,
    )

    r4_detail = ""
    try:
        suite_outcome = run_suite(repo=repo)
        r4_passed, r4_detail = _r4_verdict(suite_outcome, baseline)
        suite_output = str(getattr(suite_outcome, "output", "") or "")
    except Exception:
        r4_passed = False
        suite_output = ""

    diff_text = ""
    try:
        diff_text = gops.diff(repo=repo, base="main", head=branch)
        guard_result = inspect(
            diff=diff_text,
            allowed_files=allowed_files,
            template=template,
        )
        guard_passed = bool(guard_result.passed)
        guard_reasons: tuple[str, ...] = tuple(guard_result.halt_reasons)
    except Exception as exc:
        guard_passed = False
        guard_reasons = (
            f"guard inspection failed: {type(exc).__name__}: {exc}",
        )

    return IterationVerification(
        r5_passed=r5_passed,
        r4_passed=r4_passed,
        guard_passed=guard_passed,
        r5_status=r5_status,
        guard_reasons=guard_reasons,
        diff=diff_text,
        suite_output=suite_output,
        r4_detail=r4_detail,
    )


# --------------------------------------------------------------------------- #
# Propose loop (spec_028)
# --------------------------------------------------------------------------- #


# Where verified proposal patches land under the live repo. Per spec §2-2
# step 4 / §6, ``_ai_workspace/nightly/proposals/`` is the recommended
# location and the morning brief points the operator at this directory.
_PROPOSAL_DIR_REL: Path = Path("_ai_workspace") / "nightly" / "proposals"

# spec_042 — per-night JSON snapshot directory. One file per nightly
# orchestration (``night_<date>.json``) carrying the v3 metric fields
# (FixLoop start count, iterations_to_green, worker_intervals, drop_reasons,
# total dispatch seconds). ``ccd report --v3`` / ``ccd dashboard`` consume
# this directory. Per-policy sweeps redirect via ``record_dir`` so client
# repos never receive a write.
_NIGHTLY_RECORDS_DIR_REL: Path = Path("_ai_workspace") / "nightly" / "records"

# Halt anchors specific to propose mode. Auto-mode anchors above stay
# unchanged so spec_023〜026 tests keep matching their exact substrings.
_HALT_PROPOSE_DISPATCH_FAILED = "proposal dispatch failed"
_HALT_PROPOSE_R5_FAILED = "proposal R5 failed"
_HALT_PROPOSE_R4_FAILED = "proposal R4 failed"
_HALT_PROPOSE_GUARD_HALT = "proposal guard halted"
_HALT_PROPOSE_NO_DIFF = (
    "proposal produced no diff — dispatcher did not modify the clone"
)


@contextmanager
def _default_isolated_workspace(repo: Path) -> Iterator[Path]:
    """Production default for :data:`IsolatedWorkspace` — wraps spec_014's
    ``_isolated_clone``.

    The clone copies everything under ``repo`` except the
    ``_ISOLATION_IGNORE`` set (``_ai_workspace``, caches, venvs); git
    remotes are stripped so the clone cannot push. Returns the clone
    path; the caller is responsible for copying the spec_auto.md into
    the clone explicitly (because ``_ai_workspace`` is excluded from
    the copy).
    """

    with _isolated_clone(repo) as workspace:
        yield workspace


def _run_propose_loop(
    *,
    repo: Path,
    channels: list[ChannelOutcome],
    fix_templates: tuple[str, ...],
    today: date | None,
    agent_runner: AgentRunner | None,
    mutation_runner: MutationRunner | None,
    fix_dispatcher: FixDispatcher | None,
    suite_runner: SuiteRunner | None,
    mutation_rechecker: MutationRechecker | None,
    adversarial_rechecker: AdversarialRechecker | None,
    guard_inspector: GuardInspector | None,
    git_ops: GitOps | None,
    unpushed_counter: UnpushedCounter | None,
    unpushed_backlog_limit: int,
    isolated_workspace: IsolatedWorkspace | None,
    dispatch_timeout_s: float,
    proposal_dir: Path | None = None,
    max_candidates: int = 1,
    loop_max_iterations: int = 1,
) -> tuple[AutoFixOutcome, tuple[AutoFixOutcome, ...]]:
    """Drive the propose-mode loop for up to ``max_candidates``
    candidates in series (spec_028 §2-2 + spec_038 top-K extension).

    Returns ``(primary_outcome, extras)`` — at ``max_candidates=1`` the
    extras tuple is always empty, keeping the v2外形 bit-for-bit
    identical for default profiles (spec_038 §3-1).

    Each candidate's write-bearing steps run **inside a fresh disposable
    isolated clone** of the live repo (one clone per candidate so
    proposals never cross-contaminate); on success the diff is captured
    as a patch file under ``<repo>/_ai_workspace/nightly/proposals/``.

    Core invariant (spec_028 §1, §2-2): the live working tree, branches,
    and commits are NEVER touched. All dispatch / verification / guard
    operations target the clone; the only live-repo writes are the
    patch files under ``proposals/`` (and the spec_auto.md the
    translator already writes to the live ``bridge/inbox/`` — that is
    the audit trail and is shared with auto mode).

    Failed verification or guard HALT → the proposal is discarded
    (no patch written) and surfaces in §D, NOT §B. The promise of
    propose mode is "動くと確認済みの修正案だけを出す" (spec_028 §2-3) —
    surfacing an unverified diff as a "proposal" would break that.

    spec_038 §2-3 — between candidates the propose loop re-evaluates
    BOTH operator brakes (PAUSE file AND the un-pushed backlog cap),
    matching the auto-mode behaviour and following the spec text
    literally. Propose mode never merges, so in normal operation the
    backlog counter stays at 0 and the check is a no-op; but the
    operator may still push a stuck backlog they want to clear before
    the propose loop continues, and a profile shared between auto and
    propose runs should respect the same brake semantics.
    """

    # spec_038 §2-3 — un-pushed backlog cap (re-evaluated between
    # candidates, identical wiring to the auto loop).
    count_unpushed = (
        unpushed_counter
        if unpushed_counter is not None
        else _default_unpushed_counter
    )

    def _backlog_skip_reason() -> str:
        """Return non-empty skip reason iff the cap is currently tripped."""
        try:
            unpushed_now = int(count_unpushed(repo))
        except Exception:
            return ""
        if unpushed_now >= unpushed_backlog_limit:
            return (
                f"{_HALT_UNPUSHED_BACKLOG_PREFIX} "
                f"({unpushed_now} un-pushed, "
                f"limit {unpushed_backlog_limit}); "
                "review and `git push origin main` before the loop resumes"
            )
        return ""

    # spec_038 — clamp top-K to a positive integer (defaults already
    # validated in profile but keep a second-line defence for in-process
    # callers).
    limit = max(1, int(max_candidates or 1))
    candidates = _select_candidates(
        channels=channels,
        repo=repo,
        fix_templates=fix_templates,
        limit=limit,
    )
    if not candidates:
        return (
            AutoFixOutcome(
                skipped=True,
                skip_reason=_compose_no_candidate_reason(fix_templates),
                mode="propose",
            ),
            (),
        )

    # Resolve seams once for all candidates.
    gops = git_ops if git_ops is not None else SubprocessGitOps()
    dispatcher = (
        fix_dispatcher
        if fix_dispatcher is not None
        else _build_default_fix_dispatcher(agent_runner)
    )
    run_suite = (
        suite_runner if suite_runner is not None else _default_suite_runner
    )
    recheck_mutation = (
        mutation_rechecker
        if mutation_rechecker is not None
        else _build_default_mutation_rechecker(mutation_runner)
    )
    recheck_adversarial = (
        adversarial_rechecker
        if adversarial_rechecker is not None
        else _default_adversarial_rechecker
    )
    inspect = (
        guard_inspector
        if guard_inspector is not None
        else _default_guard_inspector
    )
    workspace_factory = (
        isolated_workspace
        if isolated_workspace is not None
        else _default_isolated_workspace
    )

    outcomes: list[AutoFixOutcome] = []
    for i, (template, finding, source_report, candidate_count) in enumerate(
        candidates
    ):
        # spec_038 §2-3 — between candidates re-evaluate BOTH PAUSE and
        # un-pushed backlog cap (matches the auto loop verbatim). The
        # spec text is unambiguous: 各候補の処理開始前に未push バックログ
        # cap と PAUSE を再評価. Propose mode never merges, so the
        # backlog branch is a no-op in normal operation — keeping the
        # check in line with auto keeps the brake semantics uniform.
        if i > 0:
            remaining = len(candidates) - i
            if _pause_file_present(repo):
                outcomes.append(
                    AutoFixOutcome(
                        skipped=True,
                        skip_reason=(
                            f"{_HALT_REMAINING_SKIPPED_PREFIX}: "
                            f"{remaining} 件 (PAUSE: "
                            f"`_ai_workspace/PAUSE` が現れた)"
                        ),
                        mode="propose",
                    )
                )
                break
            backlog_skip = _backlog_skip_reason()
            if backlog_skip:
                outcomes.append(
                    AutoFixOutcome(
                        skipped=True,
                        skip_reason=(
                            f"{_HALT_REMAINING_SKIPPED_PREFIX}: "
                            f"{remaining} 件 ({backlog_skip})"
                        ),
                        mode="propose",
                    )
                )
                break

        outcomes.append(
            _process_one_propose_candidate(
                template=template,
                finding=finding,
                source_report=source_report,
                candidate_count=candidate_count,
                repo=repo,
                today=today,
                gops=gops,
                dispatcher=dispatcher,
                run_suite=run_suite,
                recheck_mutation=recheck_mutation,
                recheck_adversarial=recheck_adversarial,
                inspect=inspect,
                workspace_factory=workspace_factory,
                dispatch_timeout_s=dispatch_timeout_s,
                proposal_dir=proposal_dir,
                loop_max_iterations=loop_max_iterations,
            )
        )

    primary = outcomes[0]
    extras = tuple(outcomes[1:])
    return primary, extras


def _process_one_propose_candidate(
    *,
    template: str,
    finding: Finding,
    source_report: Path | None,
    candidate_count: int,
    repo: Path,
    today: date | None,
    gops: GitOps,
    dispatcher: FixDispatcher,
    run_suite: SuiteRunner,
    recheck_mutation: MutationRechecker,
    recheck_adversarial: AdversarialRechecker,
    inspect: GuardInspector,
    workspace_factory: IsolatedWorkspace,
    dispatch_timeout_s: float,
    proposal_dir: Path | None,
    loop_max_iterations: int = 1,
) -> AutoFixOutcome:
    """spec_038 §2-3 — per-candidate body, extended by spec_039 §2-3 to
    run the dispatch + verify cycle through :func:`ccd.loop.run_fix_loop`
    inside the disposable clone.

    Translates against the LIVE repo (the spec_auto.md is the audit
    artifact for both auto and propose modes), then runs all the
    write-bearing steps — including the convergence loop's feedback
    files — inside the fresh disposable clone. At
    ``loop_max_iterations=1`` the loop dispatches exactly once and the
    spec_028 propose semantics carry through unchanged.
    """

    # 1. Translate against the LIVE repo.
    tr = translate_finding(
        finding,
        repo=repo,
        source_report=str(source_report) if source_report else "",
        today=today,
    )
    if not tr.success:
        return AutoFixOutcome(
            skipped=True,
            skip_reason=tr.halt_reason,
            finding_signature=finding.signature,
            candidate_count=candidate_count,
            mode="propose",
        )

    assert tr.spec_auto_path is not None  # success ⇒ path exists

    if template == "A":
        allowed_files = list(_AUTO_FIX_ALLOWED_FILES_A)
    else:
        allowed_files = [finding.file, "tests/"]

    branch = f"propose/{tr.spec_auto_id}"

    # 2. Enter a fresh disposable clone. All writes from here on land
    # in the clone, which is rmtree-d on context exit (spec_014 §2-1).
    try:
        with workspace_factory(repo) as clone:
            clone_path = Path(clone)
            spec_auto_in_clone = _copy_spec_auto_into_clone(
                spec_auto_live=tr.spec_auto_path,
                live_repo=repo,
                clone=clone_path,
            )

            try:
                gops.create_and_checkout_branch(
                    repo=clone_path, branch=branch
                )
            except Exception as exc:
                return _propose_halt_outcome(
                    template=template,
                    finding=finding,
                    tr=tr,
                    candidate_count=candidate_count,
                    branch=branch,
                    halt_reason=(
                        "propose: branch creation failed in clone: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

            # spec_039 — build the iteration verifier closure and run
            # the convergence loop inside the clone. The verifier
            # mirrors the auto-mode one; ``repo`` it receives is the
            # clone path.
            def _verifier(
                *, repo: Path, branch: str
            ) -> IterationVerification:
                return _verify_iteration_auto(
                    template=template,
                    finding=finding,
                    allowed_files=allowed_files,
                    repo=repo,
                    branch=branch,
                    gops=gops,
                    run_suite=run_suite,
                    recheck_mutation=recheck_mutation,
                    recheck_adversarial=recheck_adversarial,
                    inspect=inspect,
                )

            try:
                fl: FixLoopOutcome = run_fix_loop(
                    spec_path=spec_auto_in_clone,
                    repo=clone_path,
                    branch=branch,
                    dispatcher=dispatcher,
                    verifier=_verifier,
                    max_iterations=loop_max_iterations,
                    wall_clock_budget_s=dispatch_timeout_s,
                    feedback_dir=clone_path / "_ai_workspace" / "logs",
                    spec_id=tr.spec_auto_id,
                )
            except Exception as exc:
                return _propose_halt_outcome(
                    template=template,
                    finding=finding,
                    tr=tr,
                    candidate_count=candidate_count,
                    branch=branch,
                    halt_reason=(
                        f"{_HALT_PROPOSE_DISPATCH_FAILED}: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

            dispatch_status = fl.final_dispatch_status
            verif = fl.final_verification

            # 5a. Dispatch failed entirely → propose HALT outcome.
            if verif is None:
                reason = (
                    fl.final_dispatch_halt_reason
                    or fl.halt_reason
                    or "no reason"
                )
                halt_outcome = _propose_halt_outcome(
                    template=template,
                    finding=finding,
                    tr=tr,
                    candidate_count=candidate_count,
                    branch=branch,
                    dispatched=fl.final_dispatched,
                    dispatch_status=dispatch_status or "failed",
                    halt_reason=f"{_HALT_PROPOSE_DISPATCH_FAILED}: {reason}",
                )
                return _attach_loop_meta(
                    outcome=halt_outcome,
                    iterations=fl.iterations,
                    converged=False,
                    loop_halt_reason=fl.halt_reason,
                )

            r5_killed = verif.r5_passed
            r4_passed = verif.r4_passed
            r4_detail = verif.r4_detail
            guard_passed = verif.guard_passed
            guard_reasons = verif.guard_reasons
            diff_text = verif.diff

            # 5b. Verification rejected → propose HALT (no patch saved).
            if not fl.converged:
                halt_reason = _compose_propose_halt_reason(
                    r5_killed=r5_killed,
                    r4_passed=r4_passed,
                    guard_passed=guard_passed,
                    guard_reasons=guard_reasons,
                )
                if (
                    fl.halt_reason
                    and fl.halt_reason != LOOP_HALT_MAX_ITERATIONS
                    and loop_max_iterations > 1
                ):
                    halt_reason = f"{halt_reason} [{fl.halt_reason}]"
                return AutoFixOutcome(
                    skipped=False,
                    spec_auto_id=tr.spec_auto_id,
                    spec_auto_path=tr.spec_auto_path,
                    finding_signature=finding.signature,
                    candidate_count=candidate_count,
                    template=template,
                    branch=branch,
                    dispatched=True,
                    dispatch_status=dispatch_status,
                    r5_killed=r5_killed,
                    r4_suite_passed=r4_passed,
                    r4_detail=r4_detail,
                    guard_passed=guard_passed,
                    guard_halt_reasons=guard_reasons,
                    merged=False,
                    halt_reason=halt_reason,
                    mode="propose",
                    proposed=False,
                    iterations=fl.iterations,
                    converged=False,
                    loop_halt_reason=fl.halt_reason,
                )

            # 7. Diff must be non-empty — a "verified proposal" with no
            # actual changes is incoherent (guard would normally catch
            # this via R0, but defend in depth).
            if not diff_text.strip():
                return AutoFixOutcome(
                    skipped=False,
                    spec_auto_id=tr.spec_auto_id,
                    spec_auto_path=tr.spec_auto_path,
                    finding_signature=finding.signature,
                    candidate_count=candidate_count,
                    template=template,
                    branch=branch,
                    dispatched=True,
                    dispatch_status=dispatch_status,
                    r5_killed=r5_killed,
                    r4_suite_passed=r4_passed,
                    r4_detail=r4_detail,
                    guard_passed=guard_passed,
                    guard_halt_reasons=guard_reasons,
                    merged=False,
                    halt_reason=_HALT_PROPOSE_NO_DIFF,
                    mode="propose",
                    proposed=False,
                    iterations=fl.iterations,
                    converged=False,
                    loop_halt_reason=fl.halt_reason,
                )

            # 8. Save the patch — the only live-repo write of propose
            # mode (and it lands under ``_ai_workspace/`` which is
            # gitignored, so the live git state is untouched).
            patch_path = _save_proposal_patch(
                live_repo=repo,
                spec_auto_id=tr.spec_auto_id,
                diff_text=diff_text,
                today=today,
                proposal_dir=proposal_dir,
            )

            return AutoFixOutcome(
                skipped=False,
                spec_auto_id=tr.spec_auto_id,
                spec_auto_path=tr.spec_auto_path,
                finding_signature=finding.signature,
                candidate_count=candidate_count,
                template=template,
                branch=branch,
                dispatched=True,
                dispatch_status=dispatch_status,
                r5_killed=r5_killed,
                r4_suite_passed=r4_passed,
                r4_detail=r4_detail,
                guard_passed=guard_passed,
                guard_halt_reasons=guard_reasons,
                merged=False,
                halt_reason="",
                mode="propose",
                proposed=True,
                proposal_patch_path=patch_path,
                proposal_diff=diff_text,
                iterations=fl.iterations,
                converged=True,
                loop_halt_reason="",
            )
    except Exception as exc:
        return _propose_halt_outcome(
            template=template,
            finding=finding,
            tr=tr,
            candidate_count=candidate_count,
            branch=branch,
            halt_reason=(
                f"propose: workspace setup failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        )


def _attach_loop_meta(
    *,
    outcome: AutoFixOutcome,
    iterations: int,
    converged: bool,
    loop_halt_reason: str,
) -> AutoFixOutcome:
    """Return a copy of ``outcome`` with the spec_039 loop telemetry set.

    Convenience helper: :class:`AutoFixOutcome` is a frozen dataclass,
    so we rebuild via :func:`dataclasses.replace`. Used by propose-mode
    halt paths (where the helper :func:`_propose_halt_outcome` is
    already loop-unaware) to fold in the FixLoop telemetry without
    touching the helper's signature.
    """

    from dataclasses import replace

    return replace(
        outcome,
        iterations=iterations,
        converged=converged,
        loop_halt_reason=loop_halt_reason,
    )


def _copy_spec_auto_into_clone(
    *,
    spec_auto_live: Path,
    live_repo: Path,
    clone: Path,
) -> Path:
    """Copy the translated spec_auto.md from the live ``bridge/inbox/``
    into the clone's matching location.

    Necessary because ``_isolated_clone`` excludes ``_ai_workspace`` (the
    clone never inherits the live bridge content). Returns the in-clone
    path the dispatcher should be pointed at.
    """

    try:
        rel = spec_auto_live.relative_to(live_repo)
    except ValueError:
        rel = Path("_ai_workspace") / "bridge" / "inbox" / spec_auto_live.name
    target = clone / rel
    # spec_040 — when the test's isolated_workspace factory yields the
    # live repo itself as the "clone" (a degenerate test fixture, not
    # production), the source and destination are literally the same
    # file. Treat that as a no-op so the test harness does not need to
    # special-case it. Production always yields a separate tmpdir so
    # this branch is purely a test-ergonomics convenience.
    try:
        same_file = (
            target.exists() and target.resolve() == spec_auto_live.resolve()
        )
    except OSError:
        same_file = False
    if same_file:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(spec_auto_live, target)
    return target


def _save_proposal_patch(
    *,
    live_repo: Path,
    spec_auto_id: str,
    diff_text: str,
    today: date | None,
    proposal_dir: Path | None = None,
) -> Path:
    """Write the verified diff under ``_ai_workspace/nightly/proposals/``.

    Filename includes both the date and the spec_auto id so multiple
    proposals on the same day (rare under the weekly cadence, possible
    under nightly) don't collide. Adds a trailing newline if the diff
    didn't already have one — ``git apply`` tolerates either, but a
    canonical newline keeps editor diffs clean.

    spec_029: when ``proposal_dir`` is provided, the patch lands there
    instead of ``<live_repo>/_ai_workspace/nightly/proposals/``. The
    sweep entry point uses this to keep client-repo writes off the
    target repo and instead segregate proposals by policy under CCD's
    own ``_ai_workspace/``.
    """

    today_d = today if today is not None else _utc_today()
    proposals_dir = (
        Path(proposal_dir).resolve()
        if proposal_dir is not None
        else live_repo / _PROPOSAL_DIR_REL
    )
    proposals_dir.mkdir(parents=True, exist_ok=True)
    patch_path = proposals_dir / (
        f"proposal_{today_d.isoformat()}_{spec_auto_id}.patch"
    )
    body = diff_text if diff_text.endswith("\n") else diff_text + "\n"
    patch_path.write_text(body, encoding="utf-8")
    return patch_path


def _compose_propose_halt_reason(
    *,
    r5_killed: bool,
    r4_passed: bool,
    guard_passed: bool,
    guard_reasons: tuple[str, ...],
) -> str:
    """Compose the §D one-liner when propose verification failed.

    Mirrors :func:`_compose_halt_reason` but uses propose-specific
    anchors so the brief can distinguish "auto loop halted before
    merge" from "propose loop generated a candidate but verification
    rejected it".
    """

    parts: list[str] = []
    if not guard_passed:
        suffix = f": {guard_reasons[0]}" if guard_reasons else ""
        parts.append(f"{_HALT_PROPOSE_GUARD_HALT}{suffix}")
    if not r5_killed:
        parts.append(_HALT_PROPOSE_R5_FAILED)
    if not r4_passed:
        parts.append(_HALT_PROPOSE_R4_FAILED)
    return "; ".join(parts) or "propose: verification failed"


def _propose_halt_outcome(
    *,
    template: str,
    finding: Finding,
    tr: Any,
    candidate_count: int,
    branch: str,
    halt_reason: str,
    dispatched: bool = False,
    dispatch_status: str = "",
) -> AutoFixOutcome:
    """Build a propose-mode HALT outcome (no merge, no proposal)."""

    return AutoFixOutcome(
        skipped=False,
        spec_auto_id=tr.spec_auto_id,
        spec_auto_path=tr.spec_auto_path,
        finding_signature=finding.signature,
        candidate_count=candidate_count,
        template=template,
        branch=branch,
        dispatched=dispatched,
        dispatch_status=dispatch_status,
        merged=False,
        halt_reason=halt_reason,
        mode="propose",
        proposed=False,
    )


def _verify_r5(
    *,
    template: str,
    finding: Finding,
    recheck_mutation: MutationRechecker,
    recheck_adversarial: AdversarialRechecker,
    repo: Path,
) -> tuple[bool, str]:
    """Run the template-specific R5 verification.

    Returns ``(passed, status)`` — ``status`` is the raw rechecker output
    (or an error sentinel) so the morning brief can show *why* R5 failed
    (e.g. ``"graceful_success"`` ≠ ``"ungraceful"``, both fail R5 but for
    structurally different reasons).
    """

    if template == "A":
        try:
            status = recheck_mutation(
                repo=repo,
                file=finding.file,
                line=finding.line,
                mutation=finding.mutation,
                signature=finding.signature,
            )
        except Exception as exc:
            return False, f"error: {type(exc).__name__}: {exc}"
        return status == "killed", status

    # template == "B"
    try:
        status = recheck_adversarial(
            repo=repo,
            parser=finding.parser,
            case_name=finding.case_name,
        )
    except Exception as exc:
        return False, f"error: {type(exc).__name__}: {exc}"
    # ONLY "graceful_error" passes — "graceful_success" is silent
    # acceptance (spec_024 §3 forbids it).
    return status == "graceful_error", status


def _select_candidates(
    *,
    channels: list[ChannelOutcome],
    repo: Path,
    fix_templates: tuple[str, ...],
    limit: int,
) -> list[tuple[str, Finding, Path | None, int]]:
    """spec_038 — pick up to ``limit`` candidates honoring the profile's
    enabled templates.

    Returns a list of ``(template, finding, source_path,
    total_actionable_count)`` tuples in priority order. Empty list when
    no enabled template has a candidate available — the caller
    distinguishes that case via ``len(...) == 0``.

    Priority order is **A before B**: template A is structurally safer
    (cannot touch production code), so when both are enabled we exhaust A
    candidates before considering B. This matches spec_024 §2-3's "A を
    一定期間信用してから B を足す" intent — even with B enabled, we don't
    starve A. Within each template, source-JSON order is preserved
    (spec_038 §2-2: "fix_templates の宣言順、テンプレ A → B").

    ``limit`` is the per-night cap from
    :attr:`ccd.profile.SafetyConfig.max_candidates_per_night`. The
    default ``limit=1`` reduces this helper to single-candidate selection
    bit-for-bit identical to spec_023〜026 behavior.
    """

    if limit <= 0:
        return []

    out: list[tuple[str, Finding, Path | None, int]] = []
    for template in fix_templates:
        if len(out) >= limit:
            break
        if template == "A":
            matches, source, count = _select_template_a_candidates(
                channels=channels,
                repo=repo,
            )
        elif template == "B":
            matches, source, count = _select_template_b_candidates(
                channels=channels,
                repo=repo,
            )
        else:
            continue
        for finding in matches:
            if len(out) >= limit:
                break
            out.append((template, finding, source, count))
    return out


def _compose_no_candidate_reason(fix_templates: tuple[str, ...]) -> str:
    """Build the "no candidate available" skip reason.

    Composes a reason that names *which* templates were attempted so the
    morning brief can distinguish "loop tried A and there was nothing"
    from "loop tried A+B and both came up empty". For backwards
    compatibility with spec_023 tests, the template-A-only case still
    surfaces the historical exact phrase ``_HALT_NO_CANDIDATE``.
    """

    if fix_templates == ("A",):
        return _HALT_NO_CANDIDATE
    parts = [f"template-{t}" for t in fix_templates]
    joined = " or ".join(parts)
    return f"no {joined} candidate available"


def _select_template_a_candidates(
    *,
    channels: list[ChannelOutcome],
    repo: Path,
) -> tuple[list[Finding], Path | None, int]:
    """Pick all template-A candidates from this night's findings, in
    source-JSON order (spec_038 §2-2 generalisation of the spec_023
    single-pick helper).

    Resolution order:

    1. The mutation channel outcome's ``report_json_path``.
    2. The latest ``discover_NNN.json`` under ``<repo>/_ai_workspace/discover/``.

    Returns ``(matches, source_path, total_actionable_count)``. The
    count is the number of actionable findings in the source JSON (so
    the brief can report "x of N picked"); ``matches`` is the subset
    that passed the pre-filter (file / line / mutation / status="survived").
    """

    source = _resolve_mutation_report_path(channels=channels, repo=repo)
    if source is None or not source.exists():
        return [], None, 0

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], source, 0

    actionable = payload.get("actionable") or []
    if not isinstance(actionable, list):
        return [], source, 0

    count = len(actionable)
    matches: list[Finding] = []
    for entry in actionable:
        if not isinstance(entry, dict):
            continue
        finding = Finding.from_dict(
            entry,
            channel="mutation",
            source_report=str(source),
        )
        # Translation's own template-fit check is the canonical filter;
        # we just pre-filter on the obvious required fields so a
        # half-shaped row doesn't consume one of the K per-night slots.
        if (
            finding.file
            and finding.line > 0
            and finding.mutation
            and finding.status == "survived"
        ):
            matches.append(finding)

    return matches, source, count


def _resolve_mutation_report_path(
    *,
    channels: list[ChannelOutcome],
    repo: Path,
) -> Path | None:
    """Find the mutation channel's discover JSON, falling back to disk.

    Disk fallback only picks files whose contents announce ``channel ==
    "mutation"`` (mutation JSONs do not carry a top-level ``channel`` key
    in the spec_013 schema, so we conservatively treat the *absence* of a
    ``channel`` key as "mutation" since adversarial JSON explicitly sets
    ``channel: "adversarial"``). That keeps an adversarial latest report
    from being mistaken for a mutation report under fallback.
    """

    for co in channels:
        if co.channel == "mutation" and co.report_json_path is not None:
            return co.report_json_path

    return _latest_discover_json(repo=repo, want_channel="mutation")


def _select_template_b_candidates(
    *,
    channels: list[ChannelOutcome],
    repo: Path,
) -> tuple[list[Finding], Path | None, int]:
    """Pick all template-B candidates from this night's findings, in
    source-JSON order (spec_038 §2-2 generalisation).

    Mirrors :func:`_select_template_a_candidates` but reads the adversarial
    channel's discover JSON (``findings`` list instead of ``actionable``).
    Returns ``(matches, source_path, total_finding_count)``. Pre-filters
    on the obvious required fields (parser / case / exception_type /
    file) so an ill-shaped entry never consumes one of the K per-night
    slots.
    """

    source = _resolve_adversarial_report_path(channels=channels, repo=repo)
    if source is None or not source.exists():
        return [], None, 0

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], source, 0

    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        return [], source, 0

    count = len(findings)
    matches: list[Finding] = []
    for entry in findings:
        if not isinstance(entry, dict):
            continue
        finding = Finding.from_dict(
            entry,
            channel="adversarial",
            source_report=str(source),
        )
        # Mirror translate's _why_template_b_does_not_fit so a half-shaped
        # entry never consumes a spec_auto seq.
        if (
            finding.parser
            and finding.case_name
            and finding.exception_type
            and finding.file
        ):
            matches.append(finding)

    return matches, source, count


def _resolve_adversarial_report_path(
    *,
    channels: list[ChannelOutcome],
    repo: Path,
) -> Path | None:
    """Find the adversarial channel's discover JSON, falling back to disk."""

    for co in channels:
        if co.channel == "adversarial" and co.report_json_path is not None:
            return co.report_json_path

    return _latest_discover_json(repo=repo, want_channel="adversarial")


def _latest_discover_json(*, repo: Path, want_channel: str) -> Path | None:
    """Walk ``_ai_workspace/discover/`` and return the latest JSON whose
    contents match ``want_channel``.

    "match" means: top-level ``channel`` key equals ``want_channel`` for
    adversarial; or absent (mutation JSON has no such key) for mutation.
    Files that fail to load JSON-cleanly are skipped (not raised) — the
    loop runs as best-effort.
    """

    discover_dir = repo / DEFAULT_DISCOVER_DIR_REL
    if not discover_dir.exists():
        return None
    matches: list[tuple[int, Path]] = []
    for p in discover_dir.glob("discover_*.json"):
        m = p.stem.removeprefix("discover_")
        if not m.isdigit():
            continue
        n = int(m)
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        channel = payload.get("channel")
        if want_channel == "mutation":
            # Mutation JSONs have no top-level "channel" key (spec_013).
            if channel is not None and channel != "mutation":
                continue
        else:
            if channel != want_channel:
                continue
        matches.append((n, p))
    if not matches:
        return None
    matches.sort(key=lambda t: t[0])
    return matches[-1][1]


def _compose_halt_reason(
    *,
    template: str,
    r5_killed: bool,
    r5_status: str,
    r4_passed: bool,
    guard_passed: bool,
    guard_reasons: tuple[str, ...],
    r4_detail: str = "",
) -> str:
    """Build the morning-brief-friendly halt reason for a non-merged fix.

    Template A surfaces the spec_023 anchor ``_HALT_R5_FAILED`` ("target
    mutation not killed"). Template B distinguishes silent acceptance
    (the parser succeeded, spec_024 §3 says this is forbidden) from
    "still ungraceful / unknown" — both fail R5 but for structurally
    different reasons the operator wants to see in the morning brief.
    """

    parts: list[str] = []
    if not guard_passed:
        suffix = f": {guard_reasons[0]}" if guard_reasons else ""
        parts.append(f"{_HALT_GUARD_HALT}{suffix}")
    if not r5_killed:
        if template == "B":
            if r5_status == "graceful_success":
                parts.append(_HALT_R5_FAILED_B_SILENT)
            else:
                # "ungraceful" / "unknown" / "error: ..." all surface here.
                parts.append(f"{_HALT_R5_FAILED_B} (status={r5_status!r})")
        else:
            parts.append(_HALT_R5_FAILED)
    if not r4_passed:
        # spec_043 §2-4 — when the dynamic count gate is what dropped R4
        # (vs a plain red suite), name the reason so the operator sees
        # "実行テスト数が baseline を下回った（after < base）" rather than
        # the generic "full suite not green".
        if r4_detail and _R4_COUNT_REGRESSION in r4_detail:
            parts.append(f"{_HALT_R4_FAILED} — {r4_detail}")
        elif r4_detail and _R4_COUNT_UNAVAILABLE in r4_detail:
            parts.append(f"{_HALT_R4_FAILED} — {r4_detail}")
        else:
            parts.append(_HALT_R4_FAILED)
    return "; ".join(parts) or "auto-fix did not merge"


def _restore_repo_after_halt(*, gops: GitOps, repo: Path, branch: str) -> None:
    """spec_026 §2-2 — restore the repo to its pre-execution state on HALT.

    Every HALT path of the autonomous-fix loop funnels through this
    helper so the next night's pre-flight sees a clean working tree on
    ``main`` (论点7: pre-flight assumes "repo is clean"). The three
    steps run in fixed order and each is wrapped in try/except so a
    partial failure in one step does not block the others ── if git is
    in a deeply weird state the morning brief still surfaces the
    original halt_reason and the operator can sort it out by hand.

    The order matters:

    1. ``discard_local_changes`` — wipe uncommitted edits on the auto
       branch BEFORE moving off it. ``git reset --hard`` + ``git clean
       -fd`` together purge tracked-but-modified and untracked files.
    2. ``checkout("main")`` — leave the working tree on main so the
       next git operation does not start from a deleted branch.
    3. ``delete_branch`` — wipe the auto feature branch so it does not
       linger as a debris commit chain.

    See spec_026 §1: prior to this helper, a HALT (偽 HALT in particular)
    left the repo dirty and the next ``ccd nightly`` ran on a polluted
    main — exactly the failure mode this restoration removes.
    """

    try:
        gops.discard_local_changes(repo=repo)
    except Exception:
        pass
    try:
        gops.checkout(repo=repo, ref="main")
    except Exception:
        pass
    try:
        gops.delete_branch(repo=repo, branch=branch)
    except Exception:
        pass


def _delete_feature_branch_after_merge(
    *, gops: GitOps, repo: Path, branch: str
) -> None:
    """spec_026 §2-2 — best-effort cleanup of the merged auto branch.

    The success path's ``merge_branch_into_main`` already leaves the
    working tree on ``main`` with the merge commit in place, so we only
    need to delete the now-redundant feature branch. Wrapped in
    try/except — losing the cleanup is non-fatal (the merge commit
    is still on main); the next pre-flight tolerates stale auto/
    branches gracefully.
    """

    try:
        gops.delete_branch(repo=repo, branch=branch)
    except Exception:
        pass


def _dispatch_with_timeout(
    *,
    dispatcher: FixDispatcher,
    spec_path: Path,
    repo: Path,
    branch: str,
    timeout_s: float,
) -> FixDispatchOutcome:
    """spec_025 §2-1(a) — wall-clock-bounded dispatch.

    The auto-fix loop spends most of its time inside this one call —
    a misbehaving ``claude`` subprocess can hang for hours. We wrap the
    dispatcher in a worker thread and let the main thread bail at
    ``timeout_s``; on timeout the loop records a failed dispatch with
    a halt_reason that names the cap so the morning brief can surface
    it.

    Caveat — Python cannot force-kill threads, so the underlying
    subprocess (if any) may continue running until it hits its own
    timeout or the OS reaps the parent on schedule rollover. The loop
    *contract* is "one candidate per night, dispatch_status=failed on
    timeout" — that contract holds, even if the orphan subprocess
    lingers a little longer.

    Non-positive ``timeout_s`` disables the timeout (used by tests that
    explicitly opt out — production callers go through ``run_nightly``
    which always supplies a positive default).
    """

    if timeout_s is None or timeout_s <= 0:
        return dispatcher(spec_path=spec_path, repo=repo, branch=branch)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="ccd-dispatch"
    ) as executor:
        future = executor.submit(
            dispatcher,
            spec_path=spec_path,
            repo=repo,
            branch=branch,
        )
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return FixDispatchOutcome(
                status="failed",
                halt_reason=(
                    f"{_HALT_DISPATCH_TIMEOUT} after {timeout_s:.0f}s "
                    "(spec_025 §2-1(a))"
                ),
                commits_made=0,
            )


def _default_patch_applier(
    *, repo: Path, patch_text: str, spec_auto_id: str
) -> None:
    """spec_040 — production default for :data:`PatchApplier`.

    Applies the worker's verified diff to ``repo`` as a single new
    commit on the currently-checked-out branch:

    1. ``git apply --index --whitespace=nowarn -`` (patch via stdin) —
       stages every hunk in the index. ``--3way`` is intentionally not
       used: the worker computed the diff from this same baseline (the
       Integrator just freshly created the live feat branch off ``main``
       and the worker's clone started from a copy of ``main`` too), so
       a plain non-3way apply succeeds when the live tree matches the
       clone's snapshot and FAILS LOUDLY when live has diverged (drift
       → drop, per spec_040 §2-2: "apply 衝突 (既に live が変わっていて
       patch が当たらない) で drop").
    2. ``git commit -m "auto-fix: <spec_auto_id>"`` — a single commit
       carrying the entire verified diff. The commit subject is stable
       and operator-greppable; tests pin it.

    Raises :class:`RuntimeError` on either step failing — the Integrator
    catches and HALTs with the canonical anchor.
    """

    completed = subprocess.run(
        ["git", "apply", "--index", "--whitespace=nowarn", "-"],
        cwd=str(repo),
        input=patch_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"git apply failed: {detail[:500]}")

    commit_msg = f"auto-fix: {spec_auto_id}"
    completed = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"git commit failed: {detail[:500]}")


def _integrate_one_candidate(
    *,
    spec_auto_id: str,
    spec_auto_path: Path,
    finding: Finding,
    template: str,
    allowed_files: list[str],
    candidate_count: int,
    branch: str,
    diff_text: str,
    worker_verification: IterationVerification,
    fl: FixLoopOutcome,
    loop_max_iterations: int,
    live_repo: Path,
    gops: GitOps,
    run_suite: SuiteRunner,
    inspect: GuardInspector,
    apply_patch: PatchApplier,
) -> AutoFixOutcome:
    """spec_040 — apply the worker's verified patch to live + re-verify + merge.

    The Integrator is the ONLY code path allowed to write to the live
    working tree / ``.git``. It is invoked AFTER the worker phase has
    produced a verified patch inside its disposable clone. Sequence:

    1. ``gops.create_and_checkout_branch(repo=live, branch=...)`` — fresh
       feat branch on live, branched off ``main``.
    2. ``apply_patch(repo=live, patch_text=...)`` — single commit
       carrying the worker's verified diff.
    3. ``inspect(...)`` — guard re-check on live (drift detection).
    4. ``run_suite(repo=live)`` — R4 re-check on live (full suite).
    5. ``gops.merge_branch_into_main(repo=live, branch=...)`` — local
       non-ff merge into ``main``. NO push (spec_023 invariant).
    6. ``gops.delete_branch(repo=live, branch=...)`` — clean up feat
       branch.

    Any failure between (1) and (5) → drop: restore live via
    :func:`_restore_repo_after_halt` (best-effort), no rebase, no
    re-dispatch. Spec_040 §2-2: "rebase も再 dispatch もせず drop".
    The morning brief surfaces a one-line halt reason in §D with the
    canonical ``_HALT_INTEGRATOR_*`` anchor.

    R5 is NOT re-run on live — the worker's clone ran R5 against the
    same baseline content (it copied from live), so re-running on live
    would just duplicate R5's verdict. Drift is caught by guard + R4
    re-check, which are the cheap checks; R5 (mutation re-run /
    adversarial parser) is expensive and adds no signal in the common
    case where live hasn't shifted since the worker started.
    """

    if not diff_text.strip():
        # Defense in depth — the worker should already have caught this.
        return AutoFixOutcome(
            skipped=False,
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding_signature=finding.signature,
            candidate_count=candidate_count,
            template=template,
            branch=branch,
            dispatched=True,
            dispatch_status=fl.final_dispatch_status,
            r5_killed=worker_verification.r5_passed,
            r4_suite_passed=worker_verification.r4_passed,
            r4_detail=worker_verification.r4_detail,
            guard_passed=worker_verification.guard_passed,
            guard_halt_reasons=worker_verification.guard_reasons,
            merged=False,
            halt_reason=_HALT_INTEGRATOR_EMPTY_PATCH,
            iterations=fl.iterations,
            converged=fl.converged,
            loop_halt_reason=fl.halt_reason,
        )

    # 1. Create live feat branch.
    try:
        gops.create_and_checkout_branch(repo=live_repo, branch=branch)
    except Exception as exc:
        _restore_repo_after_halt(gops=gops, repo=live_repo, branch=branch)
        return _integrator_halt_outcome(
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding=finding,
            template=template,
            candidate_count=candidate_count,
            branch=branch,
            worker_verification=worker_verification,
            fl=fl,
            halt_reason=(
                f"{_HALT_INTEGRATOR_BRANCH_FAILED}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    # 2. Apply the verified patch.
    try:
        apply_patch(
            repo=live_repo,
            patch_text=diff_text,
            spec_auto_id=spec_auto_id,
        )
    except Exception as exc:
        _restore_repo_after_halt(gops=gops, repo=live_repo, branch=branch)
        return _integrator_halt_outcome(
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding=finding,
            template=template,
            candidate_count=candidate_count,
            branch=branch,
            worker_verification=worker_verification,
            fl=fl,
            halt_reason=(
                f"{_HALT_INTEGRATOR_APPLY_FAILED}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    # 3. Guard re-check on live.
    live_diff_text = ""
    try:
        live_diff_text = gops.diff(
            repo=live_repo, base="main", head=branch
        )
        guard_result = inspect(
            diff=live_diff_text,
            allowed_files=allowed_files,
            template=template,
        )
    except Exception as exc:
        _restore_repo_after_halt(gops=gops, repo=live_repo, branch=branch)
        return _integrator_halt_outcome(
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding=finding,
            template=template,
            candidate_count=candidate_count,
            branch=branch,
            worker_verification=worker_verification,
            fl=fl,
            halt_reason=(
                f"{_HALT_INTEGRATOR_GUARD_FAILED}: "
                f"{type(exc).__name__}: {exc}"
            ),
            guard_passed=False,
        )
    if not guard_result.passed:
        _restore_repo_after_halt(gops=gops, repo=live_repo, branch=branch)
        suffix = (
            f": {guard_result.halt_reasons[0]}"
            if guard_result.halt_reasons
            else ""
        )
        return _integrator_halt_outcome(
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding=finding,
            template=template,
            candidate_count=candidate_count,
            branch=branch,
            worker_verification=worker_verification,
            fl=fl,
            halt_reason=f"{_HALT_INTEGRATOR_GUARD_FAILED}{suffix}",
            guard_passed=False,
            guard_reasons=tuple(guard_result.halt_reasons),
        )

    # 4. R4 (suite) re-check on live.
    try:
        suite_outcome = run_suite(repo=live_repo)
        r4_passed = bool(suite_outcome.passed)
    except Exception as exc:
        _restore_repo_after_halt(gops=gops, repo=live_repo, branch=branch)
        return _integrator_halt_outcome(
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding=finding,
            template=template,
            candidate_count=candidate_count,
            branch=branch,
            worker_verification=worker_verification,
            fl=fl,
            halt_reason=(
                f"{_HALT_INTEGRATOR_R4_FAILED}: "
                f"{type(exc).__name__}: {exc}"
            ),
            r4_passed=False,
        )
    if not r4_passed:
        _restore_repo_after_halt(gops=gops, repo=live_repo, branch=branch)
        return _integrator_halt_outcome(
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding=finding,
            template=template,
            candidate_count=candidate_count,
            branch=branch,
            worker_verification=worker_verification,
            fl=fl,
            halt_reason=_HALT_INTEGRATOR_R4_FAILED,
            r4_passed=False,
        )

    # 5. Merge into main (local, no push).
    try:
        gops.merge_branch_into_main(repo=live_repo, branch=branch)
    except Exception as exc:
        _restore_repo_after_halt(gops=gops, repo=live_repo, branch=branch)
        return _integrator_halt_outcome(
            spec_auto_id=spec_auto_id,
            spec_auto_path=spec_auto_path,
            finding=finding,
            template=template,
            candidate_count=candidate_count,
            branch=branch,
            worker_verification=worker_verification,
            fl=fl,
            halt_reason=(
                f"{_HALT_INTEGRATOR_MERGE_FAILED}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    # 6. Cleanup.
    _delete_feature_branch_after_merge(
        gops=gops, repo=live_repo, branch=branch
    )

    # Success — surface the diff captured AFTER apply on live (it's the
    # canonical "what landed on main"). When live and clone are at the
    # same baseline this equals the worker's diff; on rare cases where
    # whitespace normalisation differs, the live diff is the truthful
    # representation.
    surfaced_diff = live_diff_text or diff_text

    return AutoFixOutcome(
        skipped=False,
        spec_auto_id=spec_auto_id,
        spec_auto_path=spec_auto_path,
        finding_signature=finding.signature,
        candidate_count=candidate_count,
        template=template,
        branch=branch,
        dispatched=True,
        dispatch_status=fl.final_dispatch_status,
        r5_killed=worker_verification.r5_passed,
        r4_suite_passed=True,
        r4_detail=worker_verification.r4_detail,
        guard_passed=True,
        guard_halt_reasons=(),
        merged=True,
        halt_reason="",
        merge_diff=surfaced_diff,
        iterations=fl.iterations,
        converged=True,
        loop_halt_reason=fl.halt_reason,
    )


def _integrator_halt_outcome(
    *,
    spec_auto_id: str,
    spec_auto_path: Path,
    finding: Finding,
    template: str,
    candidate_count: int,
    branch: str,
    worker_verification: IterationVerification,
    fl: FixLoopOutcome,
    halt_reason: str,
    guard_passed: bool = True,
    guard_reasons: tuple[str, ...] = (),
    r4_passed: bool = True,
) -> AutoFixOutcome:
    """spec_040 — Integrator-side HALT outcome.

    Surfaces the worker's R5/R4/guard verdict (which was green at the
    end of the worker phase — otherwise the Integrator would not have
    been invoked) BUT overrides ``r4_suite_passed`` / ``guard_passed``
    when the Integrator's live re-check is what dropped the candidate.
    ``halt_reason`` carries the canonical ``_HALT_INTEGRATOR_*`` anchor
    so the morning brief's §D can render a one-liner with the right
    cause. ``merged=False`` always — a HALT here means nothing landed
    on main.
    """

    return AutoFixOutcome(
        skipped=False,
        spec_auto_id=spec_auto_id,
        spec_auto_path=spec_auto_path,
        finding_signature=finding.signature,
        candidate_count=candidate_count,
        template=template,
        branch=branch,
        dispatched=True,
        dispatch_status=fl.final_dispatch_status,
        r5_killed=worker_verification.r5_passed,
        r4_suite_passed=r4_passed and worker_verification.r4_passed,
        r4_detail=worker_verification.r4_detail,
        guard_passed=guard_passed and worker_verification.guard_passed,
        guard_halt_reasons=(
            guard_reasons if guard_reasons else worker_verification.guard_reasons
        ),
        merged=False,
        halt_reason=halt_reason,
        iterations=fl.iterations,
        converged=fl.converged,
        loop_halt_reason=fl.halt_reason,
    )


def _default_unpushed_counter(repo: Path) -> int:
    """spec_025 §2-1(b) production default for :data:`UnpushedCounter`.

    Counts commits on local ``main`` that haven't been pushed to
    ``origin/main`` whose subject begins with ``"auto-merge:"`` — the
    prefix :class:`SubprocessGitOps` uses when merging an autonomous-fix
    feature branch.

    Failure modes that return 0 (i.e., "don't gate the loop"):
    - ``git`` is not installed
    - the repo has no ``origin/main`` ref (fresh clone, no remote)
    - the subprocess otherwise exits non-zero

    The morning brief surfaces the count separately via
    :data:`AutoFixOutcome.skip_reason` so an operator that intentionally
    runs without a remote does not get a confusing "0 un-pushed" line —
    they get the normal Phase 1 §B because there's no backlog to
    surface.
    """

    try:
        completed = subprocess.run(
            ["git", "log", "origin/main..main", "--pretty=format:%s"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 0
    if completed.returncode != 0:
        return 0
    lines = (completed.stdout or "").strip().splitlines()
    return sum(1 for line in lines if line.startswith("auto-merge:"))


# --------------------------------------------------------------------------- #
# Default seam implementations
# --------------------------------------------------------------------------- #


def _build_default_fix_dispatcher(
    agent_runner: AgentRunner | None,
) -> FixDispatcher:
    """Wrap :func:`ccd.retry.dispatch_with_retry` as a FixDispatcher.

    The default uses ``max_attempts=1`` — the spec is "1晩1候補, 1試行"
    (论点4 layer 5). Retry feedback would let the agent try multiple
    times against the same finding, which contradicts "merge or HALT,
    no infinite retries". Operators who want retry semantics in their
    own loop can pass an alternative ``fix_dispatcher``.
    """

    from ccd.agent import ClaudeCodeRunner  # lazy: avoid mandatory import
    from ccd.retry import dispatch_with_retry

    runner = agent_runner if agent_runner is not None else ClaudeCodeRunner()

    def _dispatcher(
        *,
        spec_path: Path,
        repo: Path,
        branch: str,  # noqa: ARG001 — branch is implicit (already checked out)
        feedback: Path | None = None,
    ) -> FixDispatchOutcome:
        # spec_039 — ``feedback`` (when set by the convergence loop) is
        # forwarded as ``initial_feedback`` so the next attempt's
        # ``dispatch_one`` embeds it in the agent prompt verbatim. At
        # ``loop_max_iterations=1`` no feedback is ever supplied,
        # preserving v2 prompt shape bit-for-bit.
        spec = parse_spec(spec_path)
        record = dispatch_with_retry(
            spec,
            runner,
            repo=repo,
            max_attempts=1,
            initial_feedback=feedback,
        )
        return FixDispatchOutcome(
            status=record.status.value,
            halt_reason=(
                record.failure_category.value
                if record.failure_category is not None
                else ""
            ),
            commits_made=0,  # dispatch_with_retry doesn't surface this
        )

    return _dispatcher


# spec_043 §2-1 — lenient pytest count parsers. We read two independent
# facts out of ``pytest -q`` output:
#
# - ``collected N items`` (the collection line pytest prints once it has
#   gathered the test set — present across pytest 6/7/8). This is the
#   *collected* count: skip / xfail / deselect all still appear here, so a
#   drop in this number means tests were physically removed from
#   collection (collect_ignore, collection-hook deselection-before-collect,
#   deleted/renamed test files).
# - ``N passed`` (from the terminal summary line). This is the *executed-
#   and-passed* count: a test that was skip-marked or deselected is no
#   longer counted as passed, so muting a previously-passing test drops
#   this number even when ``collected`` is unchanged.
#
# Both regexes are intentionally forgiving — they ``search`` anywhere in
# the combined stdout/stderr and grab the last match (the summary line is
# emitted last). When neither matches, the count is ``None`` and the R4
# verdict treats "件数不明" conservatively. We do NOT try to reconstruct
# the count from skipped/deselected arithmetic; the two raw numbers above
# are enough to detect any shrinkage and keeping the parser tiny is what
# makes it survive pytest-version drift (spec_009 流儀).
_PYTEST_COLLECTED_RE = re.compile(r"collected\s+(\d+)\s+item")
_PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed")


def _parse_pytest_counts(output: str) -> tuple[int | None, int | None]:
    """Parse ``(collected, passed_count)`` out of ``pytest -q`` output.

    Returns ``None`` for either field that cannot be read (no matching
    line). Lenient by design: a pytest version whose summary wording we
    do not recognise yields ``None`` rather than a wrong number, and the
    caller fails R4 conservatively on ``None`` (spec_043 §2-1 second
    bullet — 偽陽性は許す・偽陰性は許さない)."""

    collected: int | None = None
    passed: int | None = None
    col = _PYTEST_COLLECTED_RE.findall(output or "")
    if col:
        try:
            collected = int(col[-1])
        except ValueError:
            collected = None
    pas = _PYTEST_PASSED_RE.findall(output or "")
    if pas:
        try:
            passed = int(pas[-1])
        except ValueError:
            passed = None
    return collected, passed


def _default_suite_runner(*, repo: Path) -> SuiteOutcome:
    """Run ``pytest -q`` in ``repo`` and return pass/fail + counts + tail.

    spec_043 §2-1 — ``-p no:cacheprovider`` is added so the run does not
    depend on (or write) ``.pytest_cache``; the worker baseline run and
    the post-fix run then observe the same collection deterministically.
    The full output is parsed for the collected / passed counts BEFORE
    truncating ``output`` to its tail, so a long suite whose summary line
    scrolls past the 2 KB tail window is still counted correctly."""

    try:
        completed = subprocess.run(
            ["pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return SuiteOutcome(passed=False, output=f"pytest not found: {exc}")
    full = (completed.stdout or "") + (completed.stderr or "")
    collected, passed_count = _parse_pytest_counts(full)
    return SuiteOutcome(
        passed=completed.returncode == 0,
        output=full[-2048:],
        collected=collected,
        passed_count=passed_count,
    )


def _build_default_mutation_rechecker(
    mutation_runner: MutationRunner | None,
) -> MutationRechecker:
    """Re-run the mutation channel against the target file and return
    the signature's new status (`killed` / `survived` / `unknown`)."""

    runner_obj = mutation_runner if mutation_runner is not None else MutmutRunner()

    def _recheck(
        *,
        repo: Path,
        file: str,
        line: int,  # noqa: ARG001 — signature is the canonical key
        mutation: str,  # noqa: ARG001 — signature is the canonical key
        signature: str,
    ) -> str:
        result = run_discovery(runner_obj, repo=repo, paths=[file])
        for m in result.actionable_mutants:
            if m.signature == signature:
                return "survived"
        for m in result.blocklisted_mutants:
            if m.signature == signature:
                return "survived"
        # If the signature isn't in the survivor lists, mutmut killed it
        # (it now appears only under `status=killed` in the run's cache).
        # The run itself may also have failed before reaching results —
        # in that case we don't know, so report "unknown" so the loop
        # halts conservatively rather than merging on a false positive.
        if not result.success and result.summary.mutants_total == 0:
            return "unknown"
        return "killed"

    return _recheck


def _default_guard_inspector(
    *, diff: str, allowed_files: list[str], template: str
) -> GuardResult:
    """Default wraps :func:`ccd.guard.inspect_diff` 1:1."""

    return inspect_diff(
        diff=diff,
        allowed_files=allowed_files,
        template=template,
    )


def _default_adversarial_rechecker(
    *,
    repo: Path,  # noqa: ARG001 — present for signature parity with the seam
    parser: str,
    case_name: str,
) -> str:
    """Production default for the template-B R5 recheck (spec_024).

    Reconstructs the named adversarial fixture from
    :func:`ccd.adversarial.default_cases` and calls the named parser in
    process. The four-way classification matches the seam contract:

    - allowlist exception (``ValueError`` / ``ValidationError`` /
      ``json.JSONDecodeError`` / ``FileNotFoundError``) → ``"graceful_error"``
    - any ``UnicodeError`` subclass → ``"ungraceful"`` (spec_015's
      override — codec-layer leak is below the parser's intent)
    - any other ``Exception`` → ``"ungraceful"``
    - the parser returned without raising → ``"graceful_success"``
      (silent acceptance — spec_024 §3 forbids this; R5 will fail)
    - the named parser or case is not in the production catalog →
      ``"unknown"`` (loop halts conservatively).

    Uses :class:`tempfile.TemporaryDirectory` so the fixture cleanup
    happens deterministically; no live-repo write is ever performed.
    """

    import tempfile

    from ccd.adversarial import (
        GRACEFUL_EXCEPTIONS,
        UNGRACEFUL_OVERRIDES,
        default_cases,
        default_parsers,
    )

    parser_fn = None
    for p in default_parsers():
        if p.name == parser:
            parser_fn = p.fn
            break
    if parser_fn is None:
        return "unknown"

    case = None
    for c in default_cases():
        if c.name == case_name:
            case = c
            break
    if case is None:
        return "unknown"

    with tempfile.TemporaryDirectory(prefix="ccd_r5_b_") as tmp_str:
        fixture = Path(tmp_str) / f"{case.name}.bin"
        fixture.write_bytes(case.content)
        try:
            parser_fn(fixture)
        except UNGRACEFUL_OVERRIDES:
            return "ungraceful"
        except GRACEFUL_EXCEPTIONS:
            return "graceful_error"
        except Exception:
            return "ungraceful"
        else:
            return "graceful_success"


@dataclass
class SubprocessGitOps:
    """Default :data:`GitOps` — shells out to ``git``.

    Each method is a thin subprocess wrapper that lets the loop pretend
    the four operations it needs (create-and-checkout, diff, merge,
    checkout-anything) are a single seam. Exceptions propagate; the loop
    converts them into ``halt_reason`` on the AutoFixOutcome.
    """

    def create_and_checkout_branch(self, *, repo: Path, branch: str) -> None:
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )

    def diff(self, *, repo: Path, base: str, head: str) -> str:
        completed = subprocess.run(
            ["git", "diff", f"{base}..{head}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout or ""

    def merge_branch_into_main(self, *, repo: Path, branch: str) -> None:
        # First, ``main``; then merge the feature branch. ``--no-ff`` so
        # the morning brief / git log clearly show the autonomous merge
        # as a single commit-shaped event rather than a fast-forward.
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "merge",
                "--no-ff",
                "-m",
                f"auto-merge: {branch}",
                branch,
            ],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )

    def checkout(self, *, repo: Path, ref: str) -> None:
        subprocess.run(
            ["git", "checkout", ref],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )

    def discard_local_changes(self, *, repo: Path) -> None:
        """spec_026 §2-2 — restore the working tree to the current commit.

        ``git reset --hard HEAD`` reverts tracked-file edits (staged or
        unstaged) and ``git clean -fd`` removes untracked files /
        directories that the halted fix-task may have left behind. The
        combination returns the working tree to its pre-execution state
        on the *current* branch — callers follow with ``checkout("main")``
        and ``delete_branch`` to fully unwind the auto branch.
        """
        subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )

    def delete_branch(self, *, repo: Path, branch: str) -> None:
        """spec_026 §2-2 — force-delete a local feature branch.

        ``-D`` (capital) deletes even if the branch is not fully merged
        — which is the HALT case we care about (the fix didn't merge,
        but we still want the branch gone from the working tree). For
        the success path we still use ``-D`` for symmetry; the merge
        commit lives on ``main`` so no work is lost.
        """
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )


# --------------------------------------------------------------------------- #
# Windows mirror
# --------------------------------------------------------------------------- #

_WINDOWS_MOUNT = Path("/mnt/c")
_WINDOWS_MIRROR_ENV = "CCD_WINDOWS_MIRROR_ROOT"


def _default_mirror(report_md_path: Path) -> Path | None:
    """Default ``windows_mirror`` — copy under ``/mnt/c/Users/<user>/...``.

    Returns ``None`` if ``/mnt/c`` (and therefore the Windows side) is
    not mounted, e.g. on a Linux dev host or CI runner. This is a soft
    fail by design: the WSL copy is still authoritative.
    """

    dest_root = _resolve_windows_mirror_root()
    if dest_root is None:
        return None
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    dest = dest_root / report_md_path.name
    try:
        shutil.copy2(report_md_path, dest)
    except OSError:
        return None
    return dest


def _resolve_windows_mirror_root() -> Path | None:
    """Pick the directory we copy the morning report into.

    Resolution order:
    1. ``$CCD_WINDOWS_MIRROR_ROOT`` if set — full operator override.
    2. ``/mnt/c/Users/$WIN_USER/ccd-nightly`` if ``/mnt/c`` is mounted
       and ``WIN_USER`` (a custom env operators commonly export from
       ``~/.bashrc``) is present.
    3. ``/mnt/c/Users/$USER/ccd-nightly`` as the final fallback when
       only ``/mnt/c`` is available.
    4. ``None`` if ``/mnt/c`` is absent (mirror declined).
    """

    override = os.environ.get(_WINDOWS_MIRROR_ENV)
    if override:
        return Path(override)
    if not _WINDOWS_MOUNT.is_dir():
        return None
    win_user = os.environ.get("WIN_USER") or os.environ.get("USER")
    if not win_user:
        return None
    return _WINDOWS_MOUNT / "Users" / win_user / "ccd-nightly"


# --------------------------------------------------------------------------- #
# Utilities the CLI uses for stdout
# --------------------------------------------------------------------------- #


def _utc_today() -> date:
    """Today's date in UTC. Exposed for parity with `ccd brief`."""
    return datetime.now(UTC).date()


# --------------------------------------------------------------------------- #
# spec_042 — per-night v3 snapshot persistence
# --------------------------------------------------------------------------- #


def build_night_snapshot(
    result: NightlyResult,
    *,
    night_id: str,
) -> dict[str, Any]:
    """Project :class:`NightlyResult` onto the v3 :class:`NightSnapshot` shape.

    Returned as a plain dict so :func:`ccd.metrics.NightSnapshot.model_validate`
    can consume it directly (or so callers can persist it as JSON without
    importing the schema). The fields mirror ``ccd.metrics.NightSnapshot``:

    - ``fix_loop_starts`` — outcomes that actually entered the FixLoop
      (``skipped=False and iterations >= 1``). Skipped outcomes do not
      count toward convergence_rate's denominator (生存バイアス対策,
      spec_042 §2-1).
    - ``converged`` / ``iterations_to_green`` — read from ``AutoFixOutcome``
      (spec_039 fields).
    - ``merges`` — outcomes with ``merged=True``.
    - ``parallelism`` / ``achieved_max_concurrency`` / ``drop_reasons`` —
      surfaced from :class:`NightlyResult` (spec_041 fields).
    - ``worker_intervals`` — per-worker ISO-8601 timestamps (spec_041)
      for outcomes that flowed through a WorkerPool (``worker_id``
      populated). Outcomes without a worker_id contribute nothing — they
      predate the WorkerPool or were skip outcomes.
    """

    outcomes: list[AutoFixOutcome] = []
    if result.auto_fix is not None:
        outcomes.append(result.auto_fix)
    outcomes.extend(result.auto_fix_extras)

    fix_loop_starts = sum(
        1 for o in outcomes if not o.skipped and o.iterations >= 1
    )
    converged = sum(1 for o in outcomes if o.converged)
    iterations_to_green = tuple(
        o.iterations for o in outcomes if o.converged and o.iterations >= 1
    )
    merges = sum(1 for o in outcomes if o.merged)

    intervals: list[dict[str, Any]] = []
    for o in outcomes:
        if not o.worker_id:
            continue
        intervals.append(
            {
                "worker_id": o.worker_id,
                "started_at": o.worker_started_at,
                "finished_at": o.worker_finished_at,
                "merged": bool(o.merged),
            }
        )

    return {
        "night_id": night_id,
        "fix_loop_starts": fix_loop_starts,
        "converged": converged,
        "iterations_to_green": list(iterations_to_green),
        "merges": merges,
        "parallelism": int(result.parallelism),
        "achieved_max_concurrency": int(result.achieved_max_concurrency),
        "drop_reasons": list(result.drop_reasons),
        "worker_intervals": intervals,
    }


def save_night_snapshot(
    result: NightlyResult,
    *,
    night_id: str,
    record_dir: Path,
) -> Path:
    """Write :func:`build_night_snapshot` to ``<record_dir>/night_<id>.json``.

    The file format is a single JSON object with the keys the v3
    :class:`ccd.metrics.NightSnapshot` model expects. Writes are
    idempotent — re-running on the same night overwrites the prior
    snapshot (the snapshot represents the most recent run).
    """

    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    payload = build_night_snapshot(result, night_id=night_id)
    out = record_dir / f"night_{night_id}.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return out


__all__ = [
    "AdversarialRechecker",
    "AutoFixOutcome",
    "ChannelOutcome",
    "FixDispatchOutcome",
    "FixDispatcher",
    "GitOps",
    "GuardInspector",
    "IsolatedWorkspace",
    "MutationRechecker",
    "NightlyResult",
    "PatchApplier",
    "SubprocessGitOps",
    "SuiteOutcome",
    "SuiteRunner",
    "UnpushedCounter",
    "build_night_snapshot",
    "run_nightly",
    "save_night_snapshot",
]
