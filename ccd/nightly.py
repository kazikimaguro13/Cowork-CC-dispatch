"""ccd nightly — scheduler skeleton (spec_020, v2 Phase 1 final).

The v2 Phase 1 last spec. Phase 1 surfaces three discovery channels
(``mutation`` / ``adversarial`` / ``ai``), a morning report renderer
(``ccd brief``), and a profile loader (``ccd profile``). What was
missing — and what this module adds — is the **linear orchestration**
that drives them unattended at night: pre-flight check → run each
profile-enabled discovery channel → render the morning report → mirror
the report to a Windows-side path so the operator can read it without
touching WSL.

This is *not* the full tick-controller state machine described in
``docs/DESIGN.md §9.6`` (논점7) — that machine has many stages
(``idle → discovering → translating → patching → verifying → ...``) and
only makes sense once Phase 2 brings auto-fix into the loop. Phase 1
does discovery only, so the orchestration collapses to a straight
sequence. ``run_nightly`` is exactly that sequence, kept small so the
Phase-2 controller can be a clean rewrite rather than a refactor.

What ``run_nightly`` does NOT do
--------------------------------
- It does not implement the full pre-flight (``HEAD == main`` /
  ``working tree clean`` / un-pushed-backlog threshold). That check
  matters when auto-fix is writing to the live repo; Phase 1 is
  discovery-only — the mutation channel runs in spec_014's isolated
  clone, the adversarial channel uses an in-process temp dir, the AI
  channel only reads source, and ``ccd brief`` writes only under
  ``_ai_workspace/nightly/`` (gitignored). A light pre-flight (repo
  accessible, ``_ai_workspace`` writable) is sufficient for Phase 1.
- It does not push, merge, or rewrite history. The Phase 1 charter is
  "discovery only — no autonomous changes".
- It does not implement the cost ceilings or PAUSE-file kill switch
  described in §9.6; those are Phase 2 levers and the profile schema
  reserves them as future fields (see ``ccd/profile.py``).

Injection seams (tests / future)
--------------------------------
``run_nightly`` accepts three callable seams so the test suite never
shells out to real ``mutmut`` / ``claude`` / Windows file operations:

- ``channel_runner`` — invoked once per enabled channel. The default
  delegates to :func:`ccd.discover.run_channel`, which is the same
  router ``ccd discover`` uses.
- ``brief_runner`` — invoked once after discovery. The default
  delegates to :func:`ccd.brief.run_brief`.
- ``windows_mirror`` — invoked once per generated report path. The
  default copies the report to a Windows-visible location under
  ``/mnt/c/...`` derived from the live repo's basename
  (matching the ``auto_dispatch`` mirror convention in
  ``axis-knowledge-rag``: ``cp <wsl> <win>``). Tests pass a stub that
  writes to a tmp directory.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ccd.brief import BriefResult, run_brief
from ccd.discover import run_channel
from ccd.profile import Profile, load_profile

# --------------------------------------------------------------------------- #
# Types for the injection seams
# --------------------------------------------------------------------------- #

ChannelRunner = Callable[..., Any]
"""Same shape as :func:`ccd.discover.run_channel`.

We type it loosely because each channel returns a different result
class (``DiscoveryResult`` / ``AdversarialResult`` / ``AIReviewResult``)
and the nightly orchestrator only inspects ``success`` / ``halt_reason``
/ ``report_md_path`` / ``report_json_path`` — all of which are common
across the three classes."""

BriefRunner = Callable[..., BriefResult]
"""Same shape as :func:`ccd.brief.run_brief`."""

WindowsMirror = Callable[[Path], Path | None]
"""Copy a WSL report path to a Windows-visible location.

Returns the destination path (or ``None`` if no mirror was performed —
e.g. the destination root does not exist on this host)."""


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


@dataclass
class NightlyResult:
    """``run_nightly`` return value.

    ``success`` is ``True`` only when pre-flight passed *and* the brief
    rendered. Channel-level halts do not flip it — Phase 1 wants the
    operator to still get the morning report when (say) the mutation
    channel canary-halted, so they can see what the other channels found.

    ``brief_report_wsl`` / ``brief_report_windows`` are populated when
    the brief ran; the Windows mirror may be ``None`` when the mirror
    callback declined to copy (e.g. ``/mnt/c`` not present on this host).
    """

    success: bool
    profile: Profile
    channels_run: tuple[ChannelOutcome, ...] = field(default_factory=tuple)
    brief_report_wsl: Path | None = None
    brief_report_windows: Path | None = None
    halt_reason: str = ""

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
) -> NightlyResult:
    """Drive one nightly orchestration end-to-end.

    The linear flow (spec_020 §2-1):

    1. **Profile** — accept an injected ``profile`` for tests, otherwise
       load via :func:`ccd.profile.load_profile`. An absent profile
       file yields all-defaults — three channels enabled, mutation
       targets ``["ccd"]``.
    2. **Pre-flight** — light Phase-1 safety check (repo accessible +
       ``_ai_workspace`` writable). Failures halt the orchestrator
       *before* any channel runs; ``halt_reason`` carries the reason.
    3. **Discovery channels** — invoke ``channel_runner`` once per
       enabled channel, in ``profile.discovery.channels`` order. The
       mutation channel receives ``profile.discovery.mutation_paths``;
       other channels ignore the ``paths`` argument (parity with
       :func:`ccd.discover.run_channel`). Per-channel halts are
       recorded but do not stop the loop — the operator still wants
       the brief.
    4. **Brief** — invoke ``brief_runner`` (default
       :func:`ccd.brief.run_brief`). The brief auto-discovers the
       channels' latest ``discover_NNN.json`` from
       ``<repo>/_ai_workspace/discover/`` so the same report we just
       wrote feeds the morning brief.
    5. **Windows mirror** — copy the brief's markdown to a
       Windows-visible path so the operator can read it without
       opening WSL. The mirror callback returns the destination or
       ``None`` (mirror declined). Mirror failure does not flip
       ``success`` — a missing ``/mnt/c`` on a CI host is a soft fail.
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

    brief_result = run_brief_fn(repo=repo, today=today)
    brief_md = brief_result.report_path if brief_result.success else None

    windows_path: Path | None = None
    if brief_md is not None:
        try:
            windows_path = mirror_fn(brief_md)
        except OSError:
            # Mirror is best-effort; if /mnt/c is unavailable or
            # read-only the operator just reads the WSL copy. We log
            # nothing here — Phase 1 has no logging substrate yet.
            windows_path = None

    return NightlyResult(
        success=brief_result.success,
        profile=effective_profile,
        channels_run=tuple(channel_outcomes),
        brief_report_wsl=brief_md,
        brief_report_windows=windows_path,
        halt_reason=brief_result.halt_reason if not brief_result.success else "",
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

    The full pre-flight (HEAD == main, working tree clean, un-pushed
    backlog under threshold) belongs in Phase 2 where the autonomous fix
    loop writes into the live repo. Phase 1 discovery writes only into
    ``_ai_workspace/`` (gitignored) and runs in isolated clones / temp
    dirs, so a dirty working tree cannot be corrupted by it.
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
    """Invoke each enabled channel and collect the four shared fields.

    Phase 1 keeps ``channel_runner`` general — same signature as
    :func:`ccd.discover.run_channel` — so any future channel that
    plugs into the router gets picked up by the scheduler without a
    nightly-side change.
    """

    out: list[ChannelOutcome] = []
    for channel in channels:
        paths = mutation_paths if channel == "mutation" else None
        try:
            result = run_channel_fn(channel, repo=repo, paths=paths)
        except Exception as exc:
            # We never re-raise from inside the orchestrator — a single
            # broken channel must not lose us the other channels'
            # findings or the morning brief. Surface the exception as a
            # halt reason and continue.
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
# Windows mirror
# --------------------------------------------------------------------------- #

# Per `docs/DESIGN.md §9.6`, the morning report lives on the WSL side
# (gitignored under `_ai_workspace/nightly/`) and is mirrored to a
# Windows-visible path so the operator can read it without entering WSL.
# The convention mirrors what `auto_dispatch_controller.sh` in
# axis-knowledge-rag does for `auto_dispatch_state.json`: copy to
# `/mnt/c/Users/<user>/...`. We can't know the operator's preferred
# Desktop path here, so we mirror under the user's Windows home using
# the WIN_USER env var (or `$USER` as a fallback) — operators who want
# a different destination can pass their own ``windows_mirror`` callback
# (or set CCD_WINDOWS_MIRROR_ROOT in the env).
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
    "ChannelOutcome",
    "NightlyResult",
    "run_nightly",
]
