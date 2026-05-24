"""ccd profile — profile model + loader (spec_018, v2 Phase 1).

The v2 loop is **profile-driven from day one** (``docs/DESIGN.md §9.3``).
A profile bundles per-repository configuration — target repo, enabled
discovery channels, the nightly schedule — so adding a new client repo
becomes "add another profile" rather than "fork the codebase".

spec_018 ships the **model and the loader**. The Phase 1 surface is
deliberately small (repo path, discovery channels, mutation paths, the
nightly slot). Phase 2 fields (`safety`, cost ceilings, un-pushed
backlog thresholds) are *reserved* — documented below but **not
implemented** in this spec (YAGNI; ``docs/DESIGN.md §9.7``).

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

Phase 2 reserved fields (NOT implemented in this spec)
------------------------------------------------------
- ``safety``: "branch-only" vs "push" — controls whether the
  autonomous loop is allowed to push merged work.
- Cost ceilings: per-night and per-week token / dollar budgets.
- Un-pushed backlog threshold: stop the loop when N un-pushed commits
  accumulate, to force a human review point.

When Phase 2 lands, add these to the pydantic model with their own
defaults and update the scheduler to consume them. Until then they
should NOT be in the TOML — ``extra="forbid"`` will reject them so
operators don't silently rely on a field CCD doesn't yet honor.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

KNOWN_CHANNELS: tuple[str, ...] = ("mutation", "adversarial", "ai")

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
    """Scheduler settings (spec_018 §2-2, consumed by spec_019)."""

    model_config = ConfigDict(extra="forbid")

    nightly_at: str = "02:00"

    @field_validator("nightly_at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM_RE.match(v):
            raise ValueError(
                f"nightly_at must be 'HH:MM' (00:00–23:59); got {v!r}"
            )
        return v


class Profile(BaseModel):
    """The Phase 1 profile (spec_018 §2-2).

    Every field has a sensible default; an absent profile file therefore
    yields a fully-populated ``Profile()``. Unknown fields are rejected
    (``extra="forbid"``) so typos and Phase 2 fields surface as errors
    instead of silently being ignored.
    """

    model_config = ConfigDict(extra="forbid")

    repo: str = "."
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)


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
    return "\n".join(lines)


def _toml_str_list(items: list[str]) -> str:
    """Render a list[str] as a TOML inline array."""
    body = ", ".join(f'"{s}"' for s in items)
    return f"[{body}]"
