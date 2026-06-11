"""ccd profile — profile model + loader (spec_018, v2 Phase 1; spec_023/028 ext).

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

spec_028 (v2 Phase 3 — 2本目) replaces the boolean
``safety.autonomous_fix`` gate with a 3-value ``safety.fix_mode``:

- ``"auto"`` — the spec_023〜026 autonomous loop (translate → dispatch →
  R5/R4 → guard → local merge). Same behavior as the old
  ``autonomous_fix=True`` bit-for-bit.
- ``"propose"`` — **new**. Translate one finding per night, dispatch the
  fix inside a disposable isolated clone, run R5/R4 + guard against the
  clone, capture the diff as a patch file under
  ``_ai_workspace/nightly/proposals/``, surface it in the morning brief
  with a ``git apply`` one-liner — but **do NOT merge or touch the live
  working tree**.
- ``"off"`` — discovery + morning report only. Same as the old
  ``autonomous_fix=False``.

Default is ``"off"`` (safe). The legacy boolean ``autonomous_fix`` is
**removed**; ``extra="forbid"`` makes any stray ``autonomous_fix = ...``
in a TOML surface as a load error so the migration is loud.

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

- ``safety.fix_mode`` (spec_023→028) — the **3-value gate** that selects
  the nightly behavior in ``ccd nightly``: ``"off"`` (default —
  discovery + report only), ``"auto"`` (autonomous loop with local
  merge, CCD's own first-tier mode), or ``"propose"`` (generate a
  verified diff in an isolated clone and surface it in the brief, do
  not merge — the client-repo mode).
- ``safety.fix_templates`` (spec_024) — **which templates the loop is
  allowed to process** (``docs/DESIGN.md §9.7`` risk-tier ramp). Default
  ``["A"]`` (test-only, structurally safest). Operators flip to
  ``["A", "B"]`` once template A is trusted on the repo to let the loop
  also fix adversarial ungraceful crashes (one production file +
  ``tests/``, R3 prod-diff bound enforced). ``fix_templates`` applies
  to both ``"auto"`` and ``"propose"`` modes — the templates govern
  *which findings* are processable, the mode governs *what to do* with
  the verified outcome.

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
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

KNOWN_CHANNELS: tuple[str, ...] = ("mutation", "adversarial", "ai")

# spec_030 — adversarial parser ``import`` field validation. The string
# must be a Python-style fully-qualified attribute path: dotted segments
# of letters / digits / underscores, each segment starting with a letter
# or underscore. The regex deliberately rejects path separators, shell
# metachars, hyphens, and leading digits so a typo or shell injection
# attempt is caught at load time rather than after a long sweep.
_ADVERSARIAL_IMPORT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)

# Templates the autonomous-fix loop knows how to handle. ``"A"`` = mutation
# survivor → test-only fix (spec_022/spec_023). ``"B"`` = adversarial
# ungraceful crash → production-fix + reproducer test (spec_024). Future
# templates (e.g., ``"C"`` for AI-inference findings) get added here.
KNOWN_FIX_TEMPLATES: tuple[str, ...] = ("A", "B")

# Modes the nightly fix loop runs in (spec_028 §2-1). ``"off"`` =
# discovery + morning report only (no translate, no dispatch, no merge).
# ``"auto"`` = the spec_023〜026 autonomous loop (translate → dispatch →
# R5/R4 → guard → local merge). ``"propose"`` = generate one verified
# fix in a disposable isolated clone and surface its diff + patch path
# in the morning brief — DO NOT merge or touch the live working tree.
# Default is ``"off"`` so a freshly-configured profile never auto-fixes
# nor produces proposals by surprise.
KNOWN_FIX_MODES: tuple[str, ...] = ("auto", "propose", "off")

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

# spec_029 — multi-policy registry. ``_ai_workspace/profiles/*.toml`` is
# one TOML per *policy* (one client repo /施策); the filename without
# the ``.toml`` suffix is the policy name. When the directory exists the
# registry takes over; when it does not, the loader falls back to the
# legacy single-profile path (``_ai_workspace/ccd_profile.toml``) and
# the sole policy is named ``"ccd"``. Existing single-profile operation
# and existing tests are therefore bit-for-bit unchanged.
PROFILES_DIR_REL = Path("_ai_workspace") / "profiles"
DEFAULT_FALLBACK_POLICY_NAME = "ccd"

# Policy names appear in directory paths (per-policy discover / nightly
# sub-directories under CCD's ``_ai_workspace/``) and in the cross-policy
# index headlines, so we restrict the set of allowed characters to ones
# that are safe everywhere (no path separators, no shell metachars, no
# whitespace). The same set the spec_018 mutation_paths / channels
# validators accept for free-text-style fields, deliberately narrowed.
_POLICY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


# --------------------------------------------------------------------------- #
# Pydantic model
# --------------------------------------------------------------------------- #


class ParserTarget(BaseModel):
    """One adversarial parser entry (spec_030 §2-1).

    Operators write this as a TOML ``[[discovery.adversarial.parsers]]``
    array-of-tables entry — the registry-time analogue of the hard-coded
    ``default_parsers()`` list ``ccd/adversarial.py`` carried before
    spec_030. The string ``import`` is resolved at sweep time via a
    dotted-name lookup (``importlib`` + ``getattr``) so the profile
    decides which target repo's parsers the channel observes — CCD's own
    parsers are NOT used when this list is supplied, which prevents the
    sweep silently exercising the wrong code (the Phase 2.5 misfire).

    ``input_kind`` constrains how the channel feeds the fixture to the
    parser:

    - ``"path"`` (the spec_015 contract) — pass a ``Path`` pointing at a
      tmp file containing the fixture bytes. The CCD-default parsers all
      use this shape.
    - ``"bytes"`` — pass the raw fixture bytes directly. Use for parsers
      that accept ``bytes`` without writing to disk.
    - ``"str"`` — pass the fixture decoded as UTF-8 (``errors="replace"``
      so invalid-UTF-8 cases still reach the parser as adversarial input).
    """

    model_config = ConfigDict(extra="forbid")

    import_: str = Field(alias="import")
    input_kind: Literal["path", "bytes", "str"] = "path"

    @field_validator("import_")
    @classmethod
    def _import_well_formed(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                "ParserTarget.import must be a non-empty dotted import path"
            )
        v = v.strip()
        if not _ADVERSARIAL_IMPORT_RE.fullmatch(v):
            raise ValueError(
                f"ParserTarget.import {v!r} is not a valid dotted name — "
                "must match module(.sub)*.attr (letters/digits/underscore, "
                "non-numeric first character per segment, at least one dot)"
            )
        return v


class AdversarialConfig(BaseModel):
    """Adversarial-channel parser injection (spec_030 §2-1).

    The presence of this block in a profile means "this施策 owns its own
    adversarial parser list — use that instead of the CCD default". An
    absent block (``DiscoveryConfig.adversarial is None``) means "no
    adversarial parsers configured for this施策 — sweep mode skips the
    channel rather than silently running CCD's own parsers on the
    施策's behalf" (spec_030 §1-1: the Phase 2.5 misfire).

    An empty list is rejected (``ValueError``) — operators who really
    want to disable the channel should drop ``"adversarial"`` from
    ``DiscoveryConfig.channels`` instead. An empty parsers list is a
    typo, not an intent.
    """

    model_config = ConfigDict(extra="forbid")

    parsers: list[ParserTarget] = Field(default_factory=list)

    @field_validator("parsers")
    @classmethod
    def _parsers_non_empty(cls, v: list[ParserTarget]) -> list[ParserTarget]:
        if not v:
            raise ValueError(
                "discovery.adversarial.parsers must list at least one "
                "[[discovery.adversarial.parsers]] entry; "
                "to disable the channel drop 'adversarial' from "
                "discovery.channels instead"
            )
        return v


class MutationConfig(BaseModel):
    """Mutation-channel execution parameters (spec_032 §2-1).

    The mutmut CLI runs against a *cwd* inside the isolated clone, with
    mutation paths and a tests directory resolved *relative to that cwd*.
    For flat CCD-style repos a single ``mutation_paths`` setting at
    ``[discovery]`` is enough — the legacy spec_018 form. For nested
    repos like ``axis-knowledge-rag`` (``backend/src/...``, ``backend/
    tests/...``), mutmut 2.x does not consistently discover paths from
    the repo root and 0-mutants HALTs are the resulting symptom. The fix
    is to let the profile name the subdirectory mutmut should treat as
    its cwd:

    .. code-block:: toml

        [discovery.mutation]
        cwd = "backend"
        mutation_paths = ["src/normalizer.py"]
        tests_dir = "tests"
        extra_args = []

    Translates to (inside the iso-clone)::

        cd <clone>/backend && \\
            mutmut run --paths-to-mutate src/normalizer.py --tests-dir tests

    Fields:

    - ``mutation_paths`` (list[str], non-empty) — files / directories
      mutmut should mutate, expressed *relative to* ``cwd`` (or to the
      clone root when ``cwd`` is None).
    - ``cwd`` (str | None) — subdirectory of the iso-clone mutmut runs
      in. ``None`` keeps the legacy spec_018 behavior (mutmut runs at
      the clone root).
    - ``tests_dir`` (str | None) — value forwarded as ``--tests-dir``.
      ``None`` lets mutmut auto-discover. Resolved relative to ``cwd``.
    - ``extra_args`` (list[str]) — appended verbatim to the mutmut
      ``run`` command line. CCD does NOT whitelist the contents: a bad
      flag will surface as a mutmut non-zero exit (or the spec_030
      0-mutants HALT) — that is the existing防護網 (spec_032 §3
      "既存防護網に委譲").

    Existence checks for ``cwd`` / ``mutation_paths`` entries /
    ``tests_dir`` are NOT performed inside this model — they happen in
    :func:`_validate_mutation_config_paths` after the profile has been
    fully parsed and the target repo path is known.
    """

    model_config = ConfigDict(extra="forbid")

    mutation_paths: list[str] = Field(default_factory=list)
    cwd: str | None = None
    tests_dir: str | None = None
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("mutation_paths")
    @classmethod
    def _paths_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "discovery.mutation.mutation_paths must list at least one "
                "path; drop the [discovery.mutation] table to disable the "
                "channel-specific override"
            )
        if any(not p or not p.strip() for p in v):
            raise ValueError(
                "discovery.mutation.mutation_paths entries must be "
                "non-empty strings"
            )
        return v

    @field_validator("cwd", "tests_dir")
    @classmethod
    def _path_str_well_formed(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                "discovery.mutation cwd/tests_dir must be non-empty strings "
                "when set"
            )
        return v

    @field_validator("extra_args")
    @classmethod
    def _extra_args_strings(cls, v: list[str]) -> list[str]:
        if any(not isinstance(x, str) for x in v):
            raise ValueError(
                "discovery.mutation.extra_args entries must be strings"
            )
        return v


class DiscoveryConfig(BaseModel):
    """Discovery-channel settings (spec_018 §2-2; adversarial added by
    spec_030; per-channel ``mutation`` block added by spec_032)."""

    model_config = ConfigDict(extra="forbid")

    channels: list[str] = Field(
        default_factory=lambda: list(KNOWN_CHANNELS),
    )
    mutation_paths: list[str] = Field(
        default_factory=lambda: ["ccd"],
    )
    # spec_030 — opt-in per-policy adversarial parser injection. ``None``
    # means "the profile did not configure the adversarial channel"; the
    # sweep path then skips the channel rather than silently routing it
    # to CCD's hard-coded parsers (the Phase 2.5 misfire). Single-policy
    # ``ccd discover --channel adversarial`` invocations still fall back
    # to the hard-coded defaults so existing spec_015 behavior is
    # bit-for-bit unchanged.
    adversarial: AdversarialConfig | None = None
    # spec_032 — opt-in per-policy mutmut invocation parameters. ``None``
    # keeps spec_018 legacy behavior: ``mutation_paths`` at the top of
    # ``[discovery]`` is the sole knob, mutmut runs at the clone root,
    # ``--tests-dir`` is omitted, no extra flags. Setting this enables
    # the spec_032 nested-structure workaround (cwd / tests_dir /
    # extra_args).
    mutation: MutationConfig | None = None

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

    @model_validator(mode="before")
    @classmethod
    def _no_dual_mutation_config(cls, data):
        """spec_032 — reject ambiguous TOML where BOTH top-level
        ``mutation_paths`` AND ``[discovery.mutation]`` are set.

        Either form is fine on its own (legacy spec_018 vs spec_032
        nested), but writing both is almost certainly a typo or stale
        migration and "first-wins" semantics would silently choose one
        over the other. Loud is better than silent.
        """

        if isinstance(data, dict):
            if "mutation" in data and "mutation_paths" in data:
                raise ValueError(
                    "discovery.mutation_paths and [discovery.mutation] "
                    "are mutually exclusive — pick one. "
                    "[discovery.mutation] is the spec_032 form that "
                    "supports cwd / tests_dir / extra_args; the bare "
                    "mutation_paths key is the legacy spec_018 form."
                )
        return data


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
    """Phase 2 safety knobs (spec_023 §2-1; ``fix_templates`` by spec_024;
    ``fix_mode`` 3-value replacement by spec_028).

    ``fix_mode`` selects what ``ccd nightly`` does after discovery:

    - ``"off"`` (the safe default) — discovery + morning report only. No
      translate, no dispatch, no merge, no proposal.
    - ``"auto"`` — the spec_023〜026 autonomous loop. One candidate per
      night: translate → dispatch → R5 → R4 → guard → local merge into
      ``main`` (no push). This is CCD's own first-tier mode; client
      repos should NOT use ``"auto"`` until a long bake-in (spec §1).
    - ``"propose"`` — the spec_028 propose mode. Translate one candidate,
      dispatch the fix **inside a disposable isolated clone**, run R5 +
      R4 + guard against the clone, capture the diff as a patch file
      under ``_ai_workspace/nightly/proposals/``, surface the diff and a
      ``git apply`` one-liner in the morning brief. The live working
      tree, branches, and commits are NEVER touched — propose mode's
      core invariant is "实 repo に何も残らない". Failed verification
      drops the proposal and surfaces a one-line skip note in §D
      instead of polluting §B with an unverified diff.

    The default is ``"off"`` so a freshly-configured client repo never
    auto-fixes nor produces proposals by surprise. CCD's own profile
    flips to ``"auto"`` (论点1 tier); future client repos will flip to
    ``"propose"`` when wired up by spec_029.

    Migration note (spec_028): the previous boolean ``autonomous_fix``
    field has been **removed** (no alias kept). ``extra="forbid"`` will
    make any stray ``autonomous_fix = ...`` line in a TOML surface as a
    clear load error rather than silently being ignored.

    ``fix_templates`` controls **which templates the loop is allowed to
    process** (``docs/DESIGN.md §9.7`` risk-tier ramp). Template A
    (test-only) is structurally the safest autonomous edit; template B
    (one production file + tests/) is one step riskier because it edits
    live code. Per spec_024, the staged enablement is operator-controlled:

    - ``["A"]`` (default) — only template A. Adversarial findings stay
      report-only. This is the safe default — a client repo gets only
      the structurally-safest autonomous edit even after flipping
      ``fix_mode`` away from ``"off"``.
    - ``["A", "B"]`` — both templates. The loop processes mutation AND
      adversarial findings. Enable B only after A is trusted on this
      repo.
    - ``["B"]`` is allowed for completeness but unusual — operators
      would normally keep A enabled too.

    Empty lists are rejected; the gate is the right way to disable the
    loop, not an empty template list. ``fix_templates`` applies to both
    ``"auto"`` and ``"propose"`` modes — the templates govern *which
    findings* are processable, the mode governs *what to do* with the
    verified outcome.

    ``max_candidates_per_night`` (spec_038) lifts the spec_023〜026
    "1晩1候補" cap so the loop can serially process up to K candidates
    per night. Default ``1`` keeps the v2 外形 (dispatch count, brief
    layout, recorded outcome shape) bit-for-bit identical. Allowed range
    is ``1..5`` — values outside the range surface as a clear load
    error rather than silently being clamped. Parallelism is NOT
    introduced by this field (spec_041 territory); K candidates are
    processed strictly in series, applying every per-candidate gate
    (spec_auto seq, 40-min dispatch timeout, guard, halt collection)
    and re-evaluating the PAUSE / un-pushed-backlog cap between
    candidates.

    ``loop_max_iterations`` (spec_039) lifts the spec_023〜038 single-
    shot dispatch model to a convergence loop. The nightly loop now
    dispatches a candidate, runs R5/R4/guard verification, and if
    verification failed writes a feedback Markdown into
    ``_ai_workspace/logs/`` then re-dispatches with that feedback in
    the prompt — repeating until verification is green OR one of three
    halt conditions fires (max iterations reached, the per-candidate
    wall-clock budget is exhausted, or two consecutive iterations
    produced the same failure signature). Default ``1`` reproduces the
    v2 single-shot behavior bit-for-bit (only iteration-1 runs, no
    feedback file written). Allowed range is ``1..5`` — five gives the
    agent four feedback-augmented retries before halting, which is
    enough to cover transient agent slips without burning the 40-min
    budget on a stuck candidate.

    ``parallelism`` (spec_041) sets the worker-pool size P used by the
    nightly auto / propose loops. Workers run the dispatcher + verify
    cycle in parallel inside disposable clones; completed patches are
    drained serially through the Integrator. Default ``1`` keeps the
    v2 / spec_038〜040 外形 (dispatch count, brief layout, recorded
    outcome shape) bit-for-bit identical. Allowed range is ``1..4`` —
    above 4 the rate-limit分担 against the single Claude account starts
    to dominate dispatch latency before parallel speedup pays for it
    (the spec deliberately stops at 4 until the field数据 says
    otherwise).

    ``max_merges_per_night`` (spec_041) caps how many auto-mode merges
    can land on local ``main`` in one nightly run. Default ``3`` matches
    the un-pushed-backlog limit so the operator review queue stays
    bounded even when parallelism produces more verified patches than
    the cap. The Integrator re-evaluates this gate before each merge;
    once the cap is hit, the remaining verified patches are退避 to
    ``_ai_workspace/nightly/proposals/`` (same shape as propose-mode
    artifacts) and surfaced in the morning brief. Allowed range
    is ``1..10``.

    ``r5_recheck_times`` (spec_045 §2-1; レッドチーム RT-3 対策) is how
    many times the template-A R5 mutation recheck is repeated before the
    candidate is accepted. DESIGN §9.5 ルール5 already demands "決定的・
    N回" — this field wires that wording to behavior. Default ``1`` keeps
    the spec_023〜044 single-recheck behavior bit-for-bit (one mutmut
    re-run, current 外形). When ``>= 2`` the recheck is run N times and
    R5 passes ONLY if **every** run reports ``killed``; a single
    ``survived`` / ``error`` makes R5 fail as a non-deterministic signal
    (a flaky / timing-dependent test that occasionally "kills" a mutant
    is a false positive RT-3 wants to halt, not merge). The morning brief
    surfaces ``killed (N/N 回安定)`` on a stable pass and ``R5 不安定:
    killed N回中 M回のみ`` on an unstable fail. Allowed range is ``1..5``
    — mutmut re-runs are expensive, so the cap matches the other
    per-night N-knobs; enabling N>=2 is an operator decision worth taking
    only once spec_046's lightweight mutation subset makes the cost
    realistic.
    """

    model_config = ConfigDict(extra="forbid")

    fix_mode: str = "off"
    fix_templates: list[str] = Field(default_factory=lambda: ["A"])
    max_candidates_per_night: int = 1
    loop_max_iterations: int = 1
    parallelism: int = 1
    max_merges_per_night: int = 3
    r5_recheck_times: int = 1

    @field_validator("fix_mode")
    @classmethod
    def _fix_mode_known(cls, v: str) -> str:
        if v not in KNOWN_FIX_MODES:
            raise ValueError(
                f"unknown fix_mode {v!r}; allowed: {list(KNOWN_FIX_MODES)!r}"
            )
        return v

    @field_validator("max_candidates_per_night")
    @classmethod
    def _k_in_range(cls, v: int) -> int:
        # spec_038 — bound K to 1..5. Below 1 makes no sense (the loop
        # would never act); above 5 is well beyond the night's wall-clock
        # (5 × 40 min dispatch cap = 200 min just for dispatch). Out of
        # range loud-fails at load time, matching the SafetyConfig流儀.
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(
                f"max_candidates_per_night must be an int; got {type(v).__name__}"
            )
        if v < 1 or v > 5:
            raise ValueError(
                f"max_candidates_per_night must be in 1..5; got {v}"
            )
        return v

    @field_validator("loop_max_iterations")
    @classmethod
    def _loop_iterations_in_range(cls, v: int) -> int:
        # spec_039 — bound to 1..5. Below 1 makes no sense (a "0 iteration"
        # loop never dispatches); above 5 risks the per-candidate 40-min
        # budget catching every iteration mid-dispatch without ever
        # converging. Out-of-range loud-fails at load time.
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(
                f"loop_max_iterations must be an int; got {type(v).__name__}"
            )
        if v < 1 or v > 5:
            raise ValueError(
                f"loop_max_iterations must be in 1..5; got {v}"
            )
        return v

    @field_validator("parallelism")
    @classmethod
    def _parallelism_in_range(cls, v: int) -> int:
        # spec_041 — bound to 1..4. Below 1 makes no sense (a "0 worker"
        # pool never dispatches); above 4 the rate-limit分担 against
        # the single Claude account dominates dispatch latency before
        # parallel speedup pays for it.
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(
                f"parallelism must be an int; got {type(v).__name__}"
            )
        if v < 1 or v > 4:
            raise ValueError(
                f"parallelism must be in 1..4; got {v}"
            )
        return v

    @field_validator("max_merges_per_night")
    @classmethod
    def _max_merges_in_range(cls, v: int) -> int:
        # spec_041 — bound to 1..10. Below 1 makes no sense (a "0 merge"
        # cap never lands anything); above 10 is well beyond the
        # un-pushed review queue an operator can keep up with in one
        # morning.
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(
                f"max_merges_per_night must be an int; got {type(v).__name__}"
            )
        if v < 1 or v > 10:
            raise ValueError(
                f"max_merges_per_night must be in 1..10; got {v}"
            )
        return v

    @field_validator("r5_recheck_times")
    @classmethod
    def _r5_recheck_times_in_range(cls, v: int) -> int:
        # spec_045 — bound to 1..5. Below 1 makes no sense (a "0 recheck"
        # R5 would never verify the mutation kill); above 5 multiplies the
        # already-expensive mutmut re-run cost beyond the night's
        # wall-clock budget. Out-of-range loud-fails at load time, matching
        # the SafetyConfig流儀.
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(
                f"r5_recheck_times must be an int; got {type(v).__name__}"
            )
        if v < 1 or v > 5:
            raise ValueError(
                f"r5_recheck_times must be in 1..5; got {v}"
            )
        return v

    @field_validator("fix_templates")
    @classmethod
    def _templates_known(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "fix_templates must list at least one template; "
                'use safety.fix_mode="off" to disable the loop '
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
# Errors
# --------------------------------------------------------------------------- #


class ProfileError(ValueError):
    """Raised when a profile fails post-load validation (spec_032 §2-1).

    Subclasses :class:`ValueError` so existing callers that catch
    ``ValueError`` continue to work — pydantic schema violations have
    always been surfaced as ``ValueError`` from this module.
    """


# --------------------------------------------------------------------------- #
# Effective mutation config (spec_032 §2-1)
# --------------------------------------------------------------------------- #


def effective_mutation_config(discovery: DiscoveryConfig) -> MutationConfig:
    """Return a uniform :class:`MutationConfig` view of the profile.

    Two equivalent profile shapes (spec_032 §2-1):

    - **Legacy (spec_018)** — ``[discovery] mutation_paths = [...]`` at
      the top of the discovery table; ``discovery.mutation`` is None.
      Wrapped into a default :class:`MutationConfig` so call sites have
      one shape to read.
    - **spec_032** — ``[discovery.mutation]`` table with explicit
      ``mutation_paths`` + optional ``cwd`` / ``tests_dir`` /
      ``extra_args``.

    Call sites should NOT branch on ``discovery.mutation is None`` —
    use this helper.
    """

    if discovery.mutation is not None:
        return discovery.mutation
    return MutationConfig(mutation_paths=list(discovery.mutation_paths))


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

    # spec_032 §2-1 — post-parse path-existence validation for the new
    # [discovery.mutation] block. Legacy ``mutation_paths`` at the top
    # of ``[discovery]`` is NOT validated for existence (backward
    # compat with spec_018 deployments where the profile's repo may
    # not yet be checked out locally).
    _validate_mutation_config_paths(
        profile=profile,
        ccd_repo=repo,
        profile_path=expected,
    )

    return ProfileLoadResult(
        profile=profile,
        source=expected,
        expected_path=expected,
    )


def _resolve_target_repo(profile: Profile, ccd_repo: Path) -> Path:
    """Resolve ``profile.repo`` to an absolute target-repo path.

    Mirrors :func:`ccd.sweep._resolve_target_repo` (kept module-private
    there to avoid an import cycle from sweep.py — see spec_029
    architecture notes). Absolute paths pass through; relative paths
    resolve against the CCD repo where the profile file lives.
    """

    raw = profile.repo or "."
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return (Path(ccd_repo) / p).resolve()


def _validate_mutation_config_paths(
    *,
    profile: Profile,
    ccd_repo: Path,
    profile_path: Path,
) -> None:
    """spec_032 §2-1 — validate that paths declared in
    ``[discovery.mutation]`` actually exist on disk.

    Skipped entirely when the profile uses the legacy
    ``discovery.mutation_paths`` form (``profile.discovery.mutation is
    None``) — backward compat with spec_018 deployments.

    All failures are collected and surfaced in a single
    :class:`ProfileError` so the operator sees every missing piece in
    one go (same "全部チェックする" flavour as the spec_031 post-install
    validator).
    """

    cfg = profile.discovery.mutation
    if cfg is None:
        return

    target_repo = _resolve_target_repo(profile, ccd_repo)
    errors: list[str] = []

    if cfg.cwd:
        cwd_path = target_repo / cfg.cwd
        if not cwd_path.is_dir():
            errors.append(
                f"discovery.mutation.cwd directory not found: {cwd_path}"
            )

    base = target_repo / cfg.cwd if cfg.cwd else target_repo

    for entry in cfg.mutation_paths:
        candidate = base / entry
        if not candidate.exists():
            errors.append(
                f"discovery.mutation.mutation_paths entry not found: "
                f"{candidate}"
            )

    if cfg.tests_dir:
        tests_path = base / cfg.tests_dir
        if not tests_path.is_dir():
            errors.append(
                f"discovery.mutation.tests_dir not found: {tests_path}"
            )

    if errors:
        raise ProfileError(
            f"{profile_path}: invalid profile — "
            "discovery.mutation paths failed existence check:\n  - "
            + "\n  - ".join(errors)
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
    # spec_032 — emit either the legacy ``mutation_paths`` key OR the
    # new ``[discovery.mutation]`` table, never both (they are mutually
    # exclusive at load time).
    if p.discovery.mutation is None:
        lines.append(
            "mutation_paths = " + _toml_str_list(p.discovery.mutation_paths)
        )
    lines.append("")
    if p.discovery.mutation is not None:
        lines.append("[discovery.mutation]")
        lines.append(
            "mutation_paths = " + _toml_str_list(p.discovery.mutation.mutation_paths)
        )
        if p.discovery.mutation.cwd is not None:
            lines.append(f'cwd = "{p.discovery.mutation.cwd}"')
        if p.discovery.mutation.tests_dir is not None:
            lines.append(f'tests_dir = "{p.discovery.mutation.tests_dir}"')
        lines.append(
            "extra_args = " + _toml_str_list(p.discovery.mutation.extra_args)
        )
        lines.append("")
    if p.discovery.adversarial is not None:
        for parser in p.discovery.adversarial.parsers:
            lines.append("[[discovery.adversarial.parsers]]")
            lines.append(f'import = "{parser.import_}"')
            lines.append(f'input_kind = "{parser.input_kind}"')
            lines.append("")
    lines.append("[schedule]")
    lines.append(f'nightly_at = "{p.schedule.nightly_at}"')
    lines.append(f'cadence = "{p.schedule.cadence}"')
    lines.append(f'weekly_day = "{p.schedule.weekly_day}"')
    lines.append("")
    lines.append("[safety]")
    lines.append(f'fix_mode = "{p.safety.fix_mode}"')
    lines.append("fix_templates = " + _toml_str_list(p.safety.fix_templates))
    lines.append(
        f"max_candidates_per_night = {p.safety.max_candidates_per_night}"
    )
    lines.append(
        f"loop_max_iterations = {p.safety.loop_max_iterations}"
    )
    lines.append(f"parallelism = {p.safety.parallelism}")
    lines.append(
        f"max_merges_per_night = {p.safety.max_merges_per_night}"
    )
    lines.append(
        f"r5_recheck_times = {p.safety.r5_recheck_times}"
    )
    return "\n".join(lines)


def _toml_str_list(items: list[str]) -> str:
    """Render a list[str] as a TOML inline array."""
    body = ", ".join(f'"{s}"' for s in items)
    return f"[{body}]"


# --------------------------------------------------------------------------- #
# Profile registry — spec_029 multi-policy support
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PolicyEntry:
    """One row from the profile registry (spec_029 §2-1).

    The policy name is **the TOML filename stem** (so
    ``_ai_workspace/profiles/samurai.toml`` → ``name="samurai"``). The
    spec deliberately does NOT carry a ``name`` field inside the TOML to
    avoid drift between the filename and an embedded label.

    ``source`` is the absolute path of the TOML file the profile came
    from. In the single-profile fallback (``profiles/`` directory
    missing) ``source`` is ``None`` when the legacy
    ``ccd_profile.toml`` was also absent (defaults assembled) and the
    legacy path otherwise — mirrors :class:`ProfileLoadResult` so the
    cross-policy index can surface "loaded from" honestly.
    """

    name: str
    profile: Profile
    source: Path | None


def load_profile_registry(
    repo: Path,
    profiles_dir: Path | None = None,
) -> list[PolicyEntry]:
    """Load every policy under ``<repo>/_ai_workspace/profiles/``.

    Returns entries sorted by policy name (deterministic; the morning
    cross-policy index reads the registry in this order).

    Behaviour matrix:

    - ``profiles/`` directory is present (even when empty) → registry
      mode. Empty directory yields an empty list — operators who have
      migrated to the registry but not yet populated it see "no policy
      to sweep" instead of a silent fallback.
    - ``profiles/`` directory is absent → **fallback** to the legacy
      single-profile loader. Returns exactly one entry named ``"ccd"``
      (spec_029 §2-1 default). This keeps existing single-profile
      operation bit-for-bit unchanged.

    Validation:

    - The TOML filename stem must match ``[A-Za-z0-9_-]+`` (spec_029
      §2-1 "施策名のバリデーション"). Filenames containing other
      characters raise ``ValueError`` — silently skipping them would let
      a typo'd policy disappear from the sweep without notice.
    - TOML parse errors and pydantic schema violations are surfaced
      with the offending file path included (same loud-failure
      contract as :func:`load_profile`).
    """

    repo = Path(repo).resolve()
    resolved_dir = (
        Path(profiles_dir).resolve()
        if profiles_dir is not None
        else (repo / PROFILES_DIR_REL).resolve()
    )

    if not resolved_dir.is_dir():
        # Fallback path — registry directory absent. Hand off to the
        # single-profile loader so a profile-less repo continues to
        # produce a fully-defaulted Profile() exactly as before.
        single = load_profile_with_source(repo)
        return [
            PolicyEntry(
                name=DEFAULT_FALLBACK_POLICY_NAME,
                profile=single.profile,
                source=single.source,
            )
        ]

    entries: list[PolicyEntry] = []
    for toml_path in sorted(resolved_dir.iterdir()):
        if toml_path.suffix != ".toml":
            continue
        if not toml_path.is_file():
            continue
        name = toml_path.stem
        if not _POLICY_NAME_RE.fullmatch(name):
            raise ValueError(
                f"{toml_path}: invalid policy name {name!r} — "
                f"must match {_POLICY_NAME_RE.pattern!r} "
                "(English letters, digits, underscore, hyphen only)"
            )
        try:
            with toml_path.open("rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{toml_path}: invalid TOML — {exc}") from exc
        except OSError as exc:
            raise ValueError(
                f"{toml_path}: cannot read profile — {exc}"
            ) from exc
        try:
            profile = Profile.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(
                f"{toml_path}: invalid profile — {exc}"
            ) from exc
        # spec_032 — apply post-parse mutation-paths existence check
        # uniformly through the registry path too (sweep loads here).
        _validate_mutation_config_paths(
            profile=profile,
            ccd_repo=repo,
            profile_path=toml_path,
        )
        entries.append(
            PolicyEntry(name=name, profile=profile, source=toml_path)
        )

    entries.sort(key=lambda e: e.name)
    return entries
