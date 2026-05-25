"""ccd nightly — scheduler with optional autonomous-fix loop.

spec_020 wired the Phase-1 skeleton (discover → brief → mirror). spec_023
adds the **autonomous-fix loop** (论点 of `docs/DESIGN.md §9.5/§9.7`):
when ``profile.safety.autonomous_fix`` is True, the orchestrator inserts a
single-candidate fix cycle between discovery and the morning report:

    discover → [translate → dispatch → R5 verify → R4 verify → guard →
                local merge] → brief

The loop is **template A only** (mutation-survivor → test-only fix) ──
template B is spec_024. Safety-boundary level 2: local ``main`` merge,
**no push**.

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
- It does not implement template B. Template-B findings (adversarial
  ungraceful crashes) flow through translate as "not fitting template A"
  and stay report-only here.
- It does not run mutmut / claude / pytest in tests — the seams below
  let tests inject fakes. Production defaults shell out.

Injection seams (tests / future)
--------------------------------

``run_nightly`` accepts seams so the test suite never shells out to real
``mutmut`` / ``claude`` / pytest / git:

- ``channel_runner`` / ``brief_runner`` / ``windows_mirror`` (spec_020).
- ``fix_dispatcher`` — invoked once per autonomous candidate to dispatch
  the spec_auto. Default wraps :func:`ccd.retry.dispatch_with_retry`.
- ``suite_runner`` — R4 (full suite green). Default shells out to
  ``pytest -q``.
- ``mutation_rechecker`` — R5 (target mutation now killed). Default runs
  the production mutation channel against the target file alone.
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

import json
import os
import shutil
import subprocess
from collections.abc import Callable
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
    run_channel,
    run_discovery,
)
from ccd.guard import GuardResult, inspect_diff
from ccd.profile import Profile, load_profile
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
"""Same shape as :func:`ccd.brief.run_brief`."""

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
    will surface ~the first 800 chars on a failure."""

    passed: bool
    output: str = ""


SuiteRunner = Callable[..., SuiteOutcome]
"""``(repo) → SuiteOutcome``. R4 (spec_023 §2-2): the full suite must
stay green after the fix lands. Default shells out to ``pytest -q``."""


MutationRechecker = Callable[..., str]
"""``(repo, file, line, mutation, signature) → "killed"|"survived"|"unknown"``.

R5 (spec_023 §2-2): the target mutant — identified by signature — must
have flipped from ``survived`` to ``killed`` after the fix lands. The
default runs the production mutation channel against the target file
alone (spec_019's iso-venv) and reads the cache for the signature."""


GuardInspector = Callable[..., GuardResult]
"""``(diff, allowed_files, template) → GuardResult``. Default wraps
:func:`ccd.guard.inspect_diff`. Tests substitute a stub returning a
canned GuardResult so they can pin guard-pass vs guard-HALT branches
independently of the parser."""


class GitOps(Protocol):
    """The four git operations the autonomous-fix loop needs.

    Production is :class:`SubprocessGitOps` (subprocess wrappers around
    ``git``). Tests pass a :class:`FakeGitOps` that just records calls.
    The loop never invokes ``git`` directly — every git side-effect goes
    through this seam so a misconfigured test environment cannot poison
    the live repo.
    """

    def create_and_checkout_branch(self, *, repo: Path, branch: str) -> None: ...

    def diff(self, *, repo: Path, base: str, head: str) -> str: ...

    def merge_branch_into_main(
        self, *, repo: Path, branch: str
    ) -> None: ...

    def checkout(self, *, repo: Path, ref: str) -> None: ...


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
    """One autonomous-fix attempt's outcome (spec_023 §2-5).

    Always populated on :class:`NightlyResult` so the morning brief and
    tests can read the same shape regardless of what happened. The
    ``skipped`` flag distinguishes "the loop ran but bailed early"
    (no candidate, gate off, etc.) from "the loop ran end-to-end and
    either merged or halted".

    When ``skipped`` is True, only ``skip_reason`` carries meaning;
    other fields are zero / empty / False. When False:

    - ``spec_auto_id`` / ``spec_auto_path`` — the translated fix-spec.
    - ``finding_signature`` — the target mutation's signature.
    - ``branch`` — the feature branch the fix ran on.
    - ``dispatch_status`` — "done" / "failed" / "blocked".
    - ``r5_killed`` — the target mutation is now killed (spec §2-2).
    - ``r4_suite_passed`` — the full suite is green (spec §2-2).
    - ``guard_passed`` / ``guard_halt_reasons`` — static-guard verdict
      (spec §2-3).
    - ``merged`` — local ``main`` merge happened (spec §2-4).
    - ``halt_reason`` — non-empty iff the loop ran but did NOT merge.
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
    guard_passed: bool = False
    guard_halt_reasons: tuple[str, ...] = ()
    merged: bool = False
    halt_reason: str = ""


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
    guard_inspector: GuardInspector | None = None,
    git_ops: GitOps | None = None,
) -> NightlyResult:
    """Drive one nightly orchestration end-to-end.

    The linear flow (spec_020 + spec_023):

    1. **Profile** — accept an injected ``profile`` for tests, otherwise
       load via :func:`ccd.profile.load_profile`.
    2. **Pre-flight** — light Phase-1 safety check; halt before any
       channel runs on failure.
    3. **Discovery channels** — one call per enabled channel in
       ``profile.discovery.channels`` order. Per-channel halts are
       recorded but do not stop the loop.
    4. **Autonomous-fix loop (spec_023)** — runs *only* when
       ``profile.safety.autonomous_fix`` is True. One template-A
       candidate per night: translate → branch → dispatch →
       R5 (mutation killed) → R4 (suite green) → guard → local merge.
       Any failure halts before merge; the result is recorded as
       :class:`AutoFixOutcome`. Gate off ⇒ ``auto_fix=None``,
       preserving the spec_020 behavior bit-for-bit.
    5. **Brief** — render the morning report.
    6. **Windows mirror** — copy to ``/mnt/c/...`` (soft fail).
    """

    repo = Path(repo).resolve()
    effective_profile = (
        profile
        if profile is not None
        else load_profile(repo, profile_path)
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

    channel_outcomes = _run_channels(
        channels=effective_profile.discovery.channels,
        mutation_paths=list(effective_profile.discovery.mutation_paths),
        repo=repo,
        run_channel_fn=run_channel_fn,
    )

    auto_fix: AutoFixOutcome | None = None
    if effective_profile.safety.autonomous_fix:
        auto_fix = _run_auto_fix_loop(
            repo=repo,
            channels=channel_outcomes,
            today=today,
            agent_runner=agent_runner,
            mutation_runner=mutation_runner,
            fix_dispatcher=fix_dispatcher,
            suite_runner=suite_runner,
            mutation_rechecker=mutation_rechecker,
            guard_inspector=guard_inspector,
            git_ops=git_ops,
        )

    brief_result = run_brief_fn(repo=repo, today=today)
    brief_md = brief_result.report_path if brief_result.success else None

    windows_path: Path | None = None
    if brief_md is not None:
        try:
            windows_path = mirror_fn(brief_md)
        except OSError:
            windows_path = None

    return NightlyResult(
        success=brief_result.success,
        profile=effective_profile,
        channels_run=tuple(channel_outcomes),
        brief_report_wsl=brief_md,
        brief_report_windows=windows_path,
        halt_reason=brief_result.halt_reason if not brief_result.success else "",
        auto_fix=auto_fix,
    )


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


# --------------------------------------------------------------------------- #
# Channel execution
# --------------------------------------------------------------------------- #


def _run_channels(
    *,
    channels: list[str],
    mutation_paths: list[str],
    repo: Path,
    run_channel_fn: ChannelRunner,
) -> list[ChannelOutcome]:
    """Invoke each enabled channel and collect the four shared fields."""

    out: list[ChannelOutcome] = []
    for channel in channels:
        paths = mutation_paths if channel == "mutation" else None
        try:
            result = run_channel_fn(channel, repo=repo, paths=paths)
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
_HALT_NO_CANDIDATE = "no template-A candidate available"
_HALT_GUARD_HALT = "guard halted the fix"
_HALT_R5_FAILED = "R5 failed: target mutation not killed"
_HALT_R4_FAILED = "R4 failed: full suite not green"
_HALT_DISPATCH_FAILED = "dispatch failed"
_AUTO_FIX_ALLOWED_FILES: tuple[str, ...] = ("tests/",)


def _run_auto_fix_loop(
    *,
    repo: Path,
    channels: list[ChannelOutcome],
    today: date | None,
    agent_runner: AgentRunner | None,
    mutation_runner: MutationRunner | None,
    fix_dispatcher: FixDispatcher | None,
    suite_runner: SuiteRunner | None,
    mutation_rechecker: MutationRechecker | None,
    guard_inspector: GuardInspector | None,
    git_ops: GitOps | None,
) -> AutoFixOutcome:
    """Drive one template-A autonomous-fix attempt (spec_023 §2-1〜§2-4).

    Returns an :class:`AutoFixOutcome` describing exactly what happened:
    skipped (no candidate / translate downgrade), dispatched + halted at
    R4/R5/guard, or dispatched + merged.

    One candidate per night (论点3): the first actionable mutation finding
    that fits template A. The remaining findings stay in the discover JSON
    and surface in the morning brief.
    """

    finding, source_report, candidate_count = _select_template_a_candidate(
        channels=channels,
        repo=repo,
    )
    if finding is None:
        return AutoFixOutcome(
            skipped=True,
            skip_reason=_HALT_NO_CANDIDATE,
        )

    # 1. Translate
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

    # 2. Resolve seams to defaults
    gops = git_ops if git_ops is not None else SubprocessGitOps()
    dispatcher = (
        fix_dispatcher
        if fix_dispatcher is not None
        else _build_default_fix_dispatcher(agent_runner)
    )
    run_suite = suite_runner if suite_runner is not None else _default_suite_runner
    recheck_mutation = (
        mutation_rechecker
        if mutation_rechecker is not None
        else _build_default_mutation_rechecker(mutation_runner)
    )
    inspect = guard_inspector if guard_inspector is not None else _default_guard_inspector

    # 3. Branch
    branch = f"auto/{tr.spec_auto_id}"
    try:
        gops.create_and_checkout_branch(repo=repo, branch=branch)
    except Exception as exc:
        return AutoFixOutcome(
            skipped=False,
            spec_auto_id=tr.spec_auto_id,
            spec_auto_path=tr.spec_auto_path,
            finding_signature=finding.signature,
            candidate_count=candidate_count,
            template="A",
            branch=branch,
            halt_reason=f"branch creation failed: {type(exc).__name__}: {exc}",
        )

    # 4. Dispatch the fix
    try:
        dispatch_outcome = dispatcher(
            spec_path=tr.spec_auto_path,
            repo=repo,
            branch=branch,
        )
    except Exception as exc:
        _safe_checkout_main(gops, repo)
        return AutoFixOutcome(
            skipped=False,
            spec_auto_id=tr.spec_auto_id,
            spec_auto_path=tr.spec_auto_path,
            finding_signature=finding.signature,
            candidate_count=candidate_count,
            template="A",
            branch=branch,
            dispatched=False,
            halt_reason=(
                f"{_HALT_DISPATCH_FAILED}: "
                f"{type(exc).__name__}: {exc}".strip()
            ),
        )

    dispatch_status = dispatch_outcome.status
    dispatch_ok = dispatch_status == "done"
    if not dispatch_ok:
        _safe_checkout_main(gops, repo)
        reason = dispatch_outcome.halt_reason or _HALT_DISPATCH_FAILED
        return AutoFixOutcome(
            skipped=False,
            spec_auto_id=tr.spec_auto_id,
            spec_auto_path=tr.spec_auto_path,
            finding_signature=finding.signature,
            candidate_count=candidate_count,
            template="A",
            branch=branch,
            dispatched=True,
            dispatch_status=dispatch_status,
            halt_reason=f"{_HALT_DISPATCH_FAILED}: {reason}",
        )

    # 5. R5: target mutation now killed?
    try:
        recheck_status = recheck_mutation(
            repo=repo,
            file=finding.file,
            line=finding.line,
            mutation=finding.mutation,
            signature=finding.signature,
        )
    except Exception as exc:
        recheck_status = f"error: {type(exc).__name__}: {exc}"
    r5_killed = recheck_status == "killed"

    # 6. R4: full suite green?
    try:
        suite_outcome = run_suite(repo=repo)
        r4_passed = bool(suite_outcome.passed)
    except Exception:
        r4_passed = False

    # 7. Guard
    try:
        diff_text = gops.diff(repo=repo, base="main", head=branch)
        guard_result = inspect(
            diff=diff_text,
            allowed_files=list(_AUTO_FIX_ALLOWED_FILES),
            template="A",
        )
        guard_passed = bool(guard_result.passed)
        guard_reasons = tuple(guard_result.halt_reasons)
    except Exception as exc:
        guard_passed = False
        guard_reasons = (
            f"guard inspection failed: {type(exc).__name__}: {exc}",
        )

    # 8. Decide: merge or halt
    merged = False
    halt_reason = ""
    if r5_killed and r4_passed and guard_passed:
        try:
            gops.merge_branch_into_main(repo=repo, branch=branch)
            merged = True
        except Exception as exc:
            halt_reason = (
                f"local merge failed: {type(exc).__name__}: {exc}"
            )
    else:
        halt_reason = _compose_halt_reason(
            r5_killed=r5_killed,
            r4_passed=r4_passed,
            guard_passed=guard_passed,
            guard_reasons=guard_reasons,
        )

    if not merged:
        _safe_checkout_main(gops, repo)

    return AutoFixOutcome(
        skipped=False,
        spec_auto_id=tr.spec_auto_id,
        spec_auto_path=tr.spec_auto_path,
        finding_signature=finding.signature,
        candidate_count=candidate_count,
        template="A",
        branch=branch,
        dispatched=True,
        dispatch_status=dispatch_status,
        r5_killed=r5_killed,
        r4_suite_passed=r4_passed,
        guard_passed=guard_passed,
        guard_halt_reasons=guard_reasons,
        merged=merged,
        halt_reason=halt_reason,
    )


def _select_template_a_candidate(
    *,
    channels: list[ChannelOutcome],
    repo: Path,
) -> tuple[Finding | None, Path | None, int]:
    """Pick the first template-A candidate from this night's findings.

    Resolution order:

    1. The mutation channel outcome's ``report_json_path``.
    2. The latest ``discover_NNN.json`` under ``<repo>/_ai_workspace/discover/``.

    Returns ``(finding, source_path, total_actionable_count)``. The
    count is the number of actionable findings in the source JSON (so
    the brief can report "1 of N picked").
    """

    source = _resolve_mutation_report_path(channels=channels, repo=repo)
    if source is None or not source.exists():
        return None, None, 0

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, source, 0

    actionable = payload.get("actionable") or []
    if not isinstance(actionable, list):
        return None, source, 0

    count = len(actionable)
    for entry in actionable:
        if not isinstance(entry, dict):
            continue
        finding = Finding.from_dict(
            entry,
            channel="mutation",
            source_report=str(source),
        )
        # Translation's own template-fit check is the canonical filter;
        # we just need the first finding here and let translate_finding
        # downgrade if it really doesn't fit. But we cheaply pre-filter
        # on the obvious required fields so a half-shaped row doesn't
        # consume the "one candidate per night" slot.
        if (
            finding.file
            and finding.line > 0
            and finding.mutation
            and finding.status == "survived"
        ):
            return finding, source, count

    return None, source, count


def _resolve_mutation_report_path(
    *,
    channels: list[ChannelOutcome],
    repo: Path,
) -> Path | None:
    """Find the mutation channel's discover JSON, falling back to disk."""

    for co in channels:
        if co.channel == "mutation" and co.report_json_path is not None:
            return co.report_json_path

    discover_dir = repo / DEFAULT_DISCOVER_DIR_REL
    if not discover_dir.exists():
        return None
    latest: tuple[int, Path] | None = None
    for p in discover_dir.glob("discover_*.json"):
        m = p.stem.removeprefix("discover_")
        if not m.isdigit():
            continue
        n = int(m)
        if latest is None or n > latest[0]:
            latest = (n, p)
    return latest[1] if latest is not None else None


def _compose_halt_reason(
    *,
    r5_killed: bool,
    r4_passed: bool,
    guard_passed: bool,
    guard_reasons: tuple[str, ...],
) -> str:
    """Build the morning-brief-friendly halt reason for a non-merged fix."""

    parts: list[str] = []
    if not guard_passed:
        suffix = f": {guard_reasons[0]}" if guard_reasons else ""
        parts.append(f"{_HALT_GUARD_HALT}{suffix}")
    if not r5_killed:
        parts.append(_HALT_R5_FAILED)
    if not r4_passed:
        parts.append(_HALT_R4_FAILED)
    return "; ".join(parts) or "auto-fix did not merge"


def _safe_checkout_main(gops: GitOps, repo: Path) -> None:
    """Best-effort: leave the working tree on ``main`` after a halt.

    We do not let a checkout failure cascade — if git is in a state
    where ``main`` cannot be checked out, the morning brief will surface
    the original halt_reason and the operator can sort the state out by
    hand. Silently swallowing the error keeps the brief writeable.
    """

    try:
        gops.checkout(repo=repo, ref="main")
    except Exception:
        pass


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
    ) -> FixDispatchOutcome:
        spec = parse_spec(spec_path)
        record = dispatch_with_retry(spec, runner, repo=repo, max_attempts=1)
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


def _default_suite_runner(*, repo: Path) -> SuiteOutcome:
    """Run ``pytest -q`` in ``repo`` and return pass/fail + tail of output."""

    try:
        completed = subprocess.run(
            ["pytest", "-q"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return SuiteOutcome(passed=False, output=f"pytest not found: {exc}")
    tail = (completed.stdout or "") + (completed.stderr or "")
    return SuiteOutcome(passed=completed.returncode == 0, output=tail[-2048:])


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


__all__ = [
    "AutoFixOutcome",
    "ChannelOutcome",
    "FixDispatchOutcome",
    "FixDispatcher",
    "GitOps",
    "GuardInspector",
    "MutationRechecker",
    "NightlyResult",
    "SubprocessGitOps",
    "SuiteOutcome",
    "SuiteRunner",
    "run_nightly",
]
