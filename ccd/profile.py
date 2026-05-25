"""ccd profile — profile model + loader (spec_018, v2 Phase 1; spec_023 ext).

The v2 loop is **profile-driven from day one** (``docs/DESIGN.md §9.3``).
A profile bundles per-repository configuration — target repo, enabled
discovery channels, the nightly schedule — so adding a new client repo
becomes "add another profile" rather than "fork the codebase".

spec_018 ships the **model and the loader**. The Phase 1 surface is
deliberately small (repo path, discovery channels, mutation paths, the
nightly slot). Phase 2 fields (`safety`, cost ceilings, un-pushed
backlog thresholds) are *reserved* — documented below but **not
implemented** in this spec (YAGNI; ``docs/DESIGN.md §9.7``).

spec_027 (v2 Phase 3 — first spec) adds **cadence** to ``[schedule]``:
``cadence`` (``"nightly"`` / ``"weekly"``, default ``"weekly"``) and
``weekly_day`` (full English weekday name, default ``"Sunday"``). The
default flips to weekly because nightly auto-fix is impractical while
the system is still under active development (the loop would chase a
moving target). The loop body itself (``ccd/nightly.py``) does not read
cadence — the scheduler template (``_ai_workspace/register_nightly.ps1``)
is what decides "how often"; ``ccd nightly`` still just runs one loop
when invoked.

The loader is graceful by design: if no profile file is present, an
all-defaults ``Profile`` is returned. CCD therefore continues to work
without any configuration, and a profile only needs to exist when an
operator wants to override one of the defaults. **Parse errors and
schema violations are surfaced as ``ValueError`` — the loader never
silently falls back to defaults when a profile *is* present but
malformed** (spec §2-1, "捏造しない").

The scheduler (spec_019) is the actual consumer of profiles. spec_018
only adds the model, the loader, and the ``ccd profile`` subcommand for
human inspection of the effective profile. Existing subcommands
(``dispatch`` / ``chain`` / ``report`` / ``dashboard`` / ``retrospect``
/ ``discover`` / ``brief`` / ``reconcile``) are NOT rewired here.

Phase 2 fields
--------------

- ``safety.autonomous_fix`` (spec_023) — the **gate** that ignites the
  autonomous-fix loop in ``ccd nightly``. Default ``False`` (safe) so a
  freshly-configured profile only does discovery + morning report; flip
  to ``True`` in CCD's own profile to let the loop translate one
  template finding per night and merge the fix locally (`docs/DESIGN.md
  §9.7` 论点1 tier: CCD itself = ON, future client repos = OFF).
- ``safety.fix_templates`` (spec_024) — **which templates the loop is
  allowed to process** (``docs/DESIGN.md §9.7`` risk-tier ramp). Default
  ``["A"]`` (test-only, structurally safest). Operators flip to
  ``["A", "B"]`` once template A is trusted on the repo to let the loop
  also fix adversarial ungraceful crashes (one production file +
  ``tests/``, R3 prod-diff bound enforced).

Phase 2 reserved fields (NOT implemented yet)
---------------------------------------------
- ``safety.push`` — "branch-only" vs "push" (spec_023 is level 2:
  local merge only, no push).
- Cost ceilings: per-night and per-week token / dollar budgets.
- Un-pushed backlog threshold: stop the loop when N un-pushed commits
  accumulate, to force a human review point.

When more Phase 2 fields land, add them to ``SafetyConfig`` with their
own defaults. Until then they should NOT be in the TOML —
``extra="forbid"`` will reject them so operators don't silently rely on
a field CCD doesn't yet honor.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

KNOWN_CHANNELS: tuple[str, ...] = ("mutation", "adversarial", "ai")

# Templates the autonomous-fix loop knows how to handle. ``"A"`` = mutation
# survivor → test-only fix (spec_022/spec_023). ``"B"`` = adversarial
# ungraceful crash → production-fix + reproducer test (spec_024). Future
# templates (e.g., ``"C"`` for AI-inference findings) get added here.
KNOWN_FIX_TEMPLATES: tuple[str, ...] = ("A", "B")

# Scheduler cadences (spec_027). ``"nightly"`` keeps the legacy
# every-night trigger; ``"weekly"`` (the new default) runs once per week
# on ``weekly_day``. The loop body in ``ccd/nightly.py`` does NOT read
# cadence — the scheduler template decides "how often"; ``ccd nightly``
# runs one loop per invocation regardless.
KNOWN_CADENCES: tuple[str, ...] = ("nightly", "weekly")

# Full English weekday names accepted by PowerShell's
# ``New-ScheduledTaskTrigger -DaysOfWeek`` (spec_027). We deliberately
# keep this to the seven full names — short forms (``"Sun"``) and
# locale-specific names are not accepted, so what lands in the profile
# is exactly what the scheduler trigger consumes.
KNOWN_WEEKDAYS: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

DEFAULT_PROFILE_REL = Path("_ai_workspace") / "ccd_profile.toml"

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


# --------------------------------------------------------------------------- #
# Pydantic model
# --------------------------------------------------------------------------- #


class DiscoveryConfig(BaseModel):
    """Discovery-channel settings (spec_018 §2-2)."""

    model_config = ConfigDict(extra="forbid")

    channels: list[str] = Field(
        default_factory=lambda: list(KNOWN_CHANNELS),
    )
    mutation_paths: list[str] = Field(
        default_factory=lambda: ["ccd"],
    )

    @field_validator("channels")
    @classmethod
    def _channels_known(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in KNOWN_CHANNELS]
        if bad:
            raise ValueError(
                f"unknown discovery channel(s): {bad!r}; "
                f"allowed: {list(KNOWN_CHANNELS)!r}"
            )
        return v

    @field_validator("mutation_paths")
    @classmethod
    def _paths_non_empty(cls, v: list[str]) -> list[str]:
        if any(not p or not p.strip() for p in v):
            raise ValueError("mutation_paths entries must be non-empty strings")
        return v


class ScheduleConfig(BaseModel):
    """Scheduler settings (spec_018 §2-2; cadence added by spec_027).

    Three fields:

    - ``nightly_at`` (``"HH:MM"``, default ``"02:00"``) — the **time of
      day** the trigger fires. The name is historical (spec_018 only
      had a nightly cadence): the value is cadence-independent — both
      ``"nightly"`` and ``"weekly"`` cadences fire at this clock time.
      We deliberately do **not** rename to e.g. ``run_at`` because
      ``extra="forbid"`` would then reject the existing
      ``[schedule] nightly_at = ...`` in deployed TOMLs (spec_027 §2-1).
    - ``cadence`` (``"nightly"`` / ``"weekly"``, default ``"weekly"``)
      — how often the scheduler fires (spec_027). ``"weekly"`` is the
      new default: running autonomous-fix every night while the system
      is under active development means chasing a moving target, so
      weekly is the realistic operating cadence. ``"nightly"`` remains
      available for the spec_021–026 legacy mode. Unknown values raise
      ``ValueError`` (no silent fallback to a different cadence).
    - ``weekly_day`` (full English weekday name, default ``"Sunday"``)
      — which day the weekly trigger fires on. Stored exactly as
      PowerShell's ``New-ScheduledTaskTrigger -DaysOfWeek`` expects
      (``"Monday"`` … ``"Sunday"``). Inputs are case-normalised to
      title case (so ``"sunday"`` is accepted and stored as
      ``"Sunday"``) — what lands in the profile is always the canonical
      form the trigger consumes. When ``cadence="nightly"`` the field
      is **ignored** by the scheduler template, but it still has its
      default and can sit in the TOML harmlessly so the operator can
      flip ``cadence`` to ``"weekly"`` later without re-adding it.
    """

    model_config = ConfigDict(extra="forbid")

    nightly_at: str = "02:00"
    cadence: str = "weekly"
    weekly_day: str = "Sunday"

    @field_validator("nightly_at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM_RE.match(v):
            raise ValueError(
                f"nightly_at must be 'HH:MM' (00:00–23:59); got {v!r}"
            )
        return v

    @field_validator("cadence")
    @classmethod
    def _cadence_known(cls, v: str) -> str:
        if v not in KNOWN_CADENCES:
            raise ValueError(
                f"unknown cadence {v!r}; allowed: {list(KNOWN_CADENCES)!r}"
            )
        return v

    @field_validator("weekly_day")
    @classmethod
    def _weekday_known(cls, v: str) -> str:
        # Accept any casing of a full English weekday name; store the
        # canonical title-case form that PowerShell's
        # ``-DaysOfWeek`` parameter consumes.
        normalised = v.title() if isinstance(v, str) else v
        if normalised not in KNOWN_WEEKDAYS:
            raise ValueError(
                f"unknown weekly_day {v!r}; "
                f"allowed (full English weekday names): "
                f"{list(KNOWN_WEEKDAYS)!r}"
            )
        return normalised


class SafetyConfig(BaseModel):
    """Phase 2 safety knobs (spec_023 §2-1; ``fix_templates`` by spec_024).

    ``autonomous_fix`` is the **gate** that ignites the autonomous-fix
    loop. When ``True``, ``ccd nightly`` runs
    ``discover → translate → dispatch → verify → guard → local-merge``
    for one finding per night. When ``False`` (the safe default), ``ccd
    nightly`` does Phase-1 discovery + morning report only — no
    translation, no dispatch, no merge. The default is OFF so a
    freshly-configured client repo never auto-fixes by surprise; only a
    profile that explicitly opts in flips it on.

    ``fix_templates`` controls **which templates the loop is allowed to
    process** (``docs/DESIGN.md §9.7`` risk-tier ramp). Template A
    (test-only) is structurally the safest autonomous edit; template B
    (one production file + tests/) is one step riskier because it edits
    live code. Per spec_024, the staged enablement is operator-controlled:

    - ``["A"]`` (default) — only template A. Adversarial findings stay
      report-only. This is the safe default — a client repo gets only
      the structurally-safest autonomous edit even after flipping
      ``autonomous_fix=True``.
    - ``["A", "B"]`` — both templates. The loop processes mutation AND
      adversarial findings. Enable B only after A is trusted on this
      repo.
    - ``["B"]`` is allowed for completeness but unusual — operators
      would normally keep A enabled too.

    Empty lists are rejected; the gate is the right way to disable the
    loop, not an empty template list.
    """

    model_config = ConfigDict(extra="forbid")

    autonomous_fix: bool = False
    fix_templates: list[str] = Field(default_factory=lambda: ["A"])

    @field_validator("fix_templates")
    @classmethod
    def _templates_known(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "fix_templates must list at least one template; "
                "use safety.autonomous_fix=false to disable the loop "
                "instead of providing an empty fix_templates list"
            )
        bad = [t for t in v if t not in KNOWN_FIX_TEMPLATES]
        if bad:
            raise ValueError(
                f"unknown fix_templates: {bad!r}; "
                f"allowed: {list(KNOWN_FIX_TEMPLATES)!r}"
            )
        # Reject duplicates: ["A","A","B"] is a typo, not intent.
        seen: list[str] = []
        for t in v:
            if t in seen:
                raise ValueError(
                    f"duplicate template {t!r} in fix_templates={v!r}"
                )
            seen.append(t)
        return v


class Profile(BaseModel):
    """The profile (spec_018 §2-2; ``safety`` added by spec_023).

    Every field has a sensible default; an absent profile file therefore
    yields a fully-populated ``Profile()``. Unknown fields are rejected
    (``extra="forbid"``) so typos surface as errors instead of silently
    being ignored.
    """

    model_config = ConfigDict(extra="forbid")

    repo: str = "."
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProfileLoadResult:
    """What ``load_profile_with_source`` returns.

    ``source`` is the file the profile was read from, or ``None`` when
    no file existed and the all-defaults ``Profile()`` was returned.
    ``expected_path`` is always the path that was *checked* (so the CLI
    can tell the operator where to drop a profile if they want one).
    """

    profile: Profile
    source: Path | None
    expected_path: Path


def resolve_profile_path(repo: Path, path: Path | None = None) -> Path:
    """Return the absolute path the loader would consult.

    - ``path=None`` → ``<repo>/_ai_workspace/ccd_profile.toml``
    - explicit absolute ``path`` → returned as-is
    - explicit relative ``path`` → resolved under ``repo``

    The returned path may or may not exist; callers should check.
    """

    if path is None:
        return (repo / DEFAULT_PROFILE_REL).resolve()
    p = Path(path)
    return p.resolve() if p.is_absolute() else (repo / p).resolve()


def load_profile(repo: Path, path: Path | None = None) -> Profile:
    """Load the effective profile.

    - If the profile file does not exist, return a fully-default
      ``Profile()`` (graceful — CCD works without any configuration).
    - TOML parse errors and pydantic schema violations are raised as
      ``ValueError`` with the offending file path included. The loader
      never silently falls back to defaults when a file is present but
      malformed (spec §2-1).
    """

    return load_profile_with_source(repo, path).profile


def load_profile_with_source(
    repo: Path,
    path: Path | None = None,
) -> ProfileLoadResult:
    """Load the profile and report where it came from.

    Used by ``ccd profile`` so the CLI can tell the operator whether the
    effective profile was read from a file or assembled from defaults.
    """

    expected = resolve_profile_path(repo, path)
    if not expected.exists():
        return ProfileLoadResult(
            profile=Profile(),
            source=None,
            expected_path=expected,
        )

    try:
        with expected.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{expected}: invalid TOML — {exc}") from exc
    except OSError as exc:
        raise ValueError(f"{expected}: cannot read profile — {exc}") from exc

    try:
        profile = Profile.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{expected}: invalid profile — {exc}") from exc

    return ProfileLoadResult(
        profile=profile,
        source=expected,
        expected_path=expected,
    )


# --------------------------------------------------------------------------- #
# Rendering — used by `ccd profile`
# --------------------------------------------------------------------------- #


def render_profile(result: ProfileLoadResult) -> str:
    """Render the effective profile as a human-readable, TOML-shaped block.

    The output is TOML-syntax-compatible so an operator can copy it
    verbatim into ``_ai_workspace/ccd_profile.toml`` as a starting
    point. A leading comment surfaces *where* the profile was loaded
    from (or that defaults were used because no file existed).
    """

    lines: list[str] = []
    if result.source is not None:
        lines.append(f"# loaded from: {result.source}")
    else:
        lines.append(f"# no profile file at {result.expected_path}")
        lines.append("# using all defaults")
    lines.append("")

    p = result.profile
    lines.append(f'repo = "{p.repo}"')
    lines.append("")
    lines.append("[discovery]")
    lines.append("channels = " + _toml_str_list(p.discovery.channels))
    lines.append("mutation_paths = " + _toml_str_list(p.discovery.mutation_paths))
    lines.append("")
    lines.append("[schedule]")
    lines.append(f'nightly_at = "{p.schedule.nightly_at}"')
    lines.append(f'cadence = "{p.schedule.cadence}"')
    lines.append(f'weekly_day = "{p.schedule.weekly_day}"')
    lines.append("")
    lines.append("[safety]")
    lines.append(f"autonomous_fix = {_toml_bool(p.safety.autonomous_fix)}")
    lines.append("fix_templates = " + _toml_str_list(p.safety.fix_templates))
    return "\n".join(lines)


def _toml_bool(value: bool) -> str:
    """Render a Python bool as TOML literal (``true`` / ``false``)."""
    return "true" if value else "false"


def _toml_str_list(items: list[str]) -> str:
    """Render a list[str] as a TOML inline array."""
    body = ", ".join(f'"{s}"' for s in items)
    return f"[{body}]"
