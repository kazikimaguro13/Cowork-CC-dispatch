"""Tests for ``ccd/profile.py`` and the ``ccd profile`` CLI (spec_018).

The profile is the v2 Phase-1 configuration substrate: model + loader.
These tests cover the loader (TOML round-trip, graceful missing file,
clear errors on malformed input, partial overrides, determinism) and
the ``ccd profile`` CLI wrapper that surfaces the effective profile.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from ccd import cli
from ccd.profile import (
    DEFAULT_PROFILE_REL,
    KNOWN_CADENCES,
    KNOWN_CHANNELS,
    KNOWN_WEEKDAYS,
    Profile,
    load_profile,
    load_profile_with_source,
    render_profile,
    resolve_profile_path,
)

# --------------------------------------------------------------------------- #
# Loader — happy path
# --------------------------------------------------------------------------- #


def test_load_profile_reads_all_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "./client_repo"

            [discovery]
            channels = ["mutation", "ai"]
            mutation_paths = ["client_repo/src", "client_repo/lib"]

            [schedule]
            nightly_at = "03:30"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.repo == "./client_repo"
    assert profile.discovery.channels == ["mutation", "ai"]
    assert profile.discovery.mutation_paths == ["client_repo/src", "client_repo/lib"]
    assert profile.schedule.nightly_at == "03:30"


def test_load_profile_with_source_reports_path(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text('repo = "."\n', encoding="utf-8")

    result = load_profile_with_source(tmp_path)

    assert result.source == profile_path.resolve()
    assert result.expected_path == profile_path.resolve()
    assert result.profile.repo == "."


# --------------------------------------------------------------------------- #
# Loader — missing file is graceful
# --------------------------------------------------------------------------- #


def test_missing_profile_returns_all_defaults(tmp_path: Path) -> None:
    profile = load_profile(tmp_path)

    assert profile.repo == "."
    assert profile.discovery.channels == list(KNOWN_CHANNELS)
    assert profile.discovery.mutation_paths == ["ccd"]
    assert profile.schedule.nightly_at == "02:00"


def test_missing_profile_with_source_signals_defaults(tmp_path: Path) -> None:
    result = load_profile_with_source(tmp_path)

    assert result.source is None
    assert result.expected_path == (tmp_path / DEFAULT_PROFILE_REL).resolve()
    assert result.profile == Profile()


# --------------------------------------------------------------------------- #
# Loader — partial profile inherits defaults
# --------------------------------------------------------------------------- #


def test_partial_profile_inherits_defaults_for_missing_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [schedule]
            nightly_at = "04:15"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.repo == "."  # default
    assert profile.discovery.channels == list(KNOWN_CHANNELS)  # default
    assert profile.discovery.mutation_paths == ["ccd"]  # default
    assert profile.schedule.nightly_at == "04:15"  # overridden


def test_partial_discovery_only_inherits_schedule(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [discovery]
            channels = ["mutation"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.discovery.channels == ["mutation"]
    assert profile.discovery.mutation_paths == ["ccd"]  # default
    assert profile.schedule.nightly_at == "02:00"  # default


# --------------------------------------------------------------------------- #
# Loader — errors must be clear (no silent fallback)
# --------------------------------------------------------------------------- #


def test_invalid_toml_raises_value_error(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    # Unterminated string — invalid TOML.
    profile_path.write_text('repo = "unterminated\n', encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "invalid TOML" in msg
    assert str(profile_path) in msg


def test_unknown_field_raises_value_error(tmp_path: Path) -> None:
    """Unknown top-level fields (e.g. an unreserved Phase-2 knob) must
    error, not be silently dropped — operators must not rely on a field
    CCD doesn't yet honor."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "."
            mystery_knob = "wat"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "invalid profile" in msg
    assert "mystery_knob" in msg


def test_unknown_channel_raises_value_error(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [discovery]
            channels = ["mutation", "telepathy"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "telepathy" in msg


def test_invalid_nightly_at_raises_value_error(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [schedule]
            nightly_at = "25:99"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    assert "nightly_at" in str(excinfo.value)


def test_wrong_type_raises_value_error(tmp_path: Path) -> None:
    """A wrong-type field (string where list expected) must error."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [discovery]
            channels = "mutation"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_profile(tmp_path)


# --------------------------------------------------------------------------- #
# Loader — determinism and explicit path
# --------------------------------------------------------------------------- #


def test_defaults_are_deterministic(tmp_path: Path) -> None:
    """Same (missing) input → same Profile, comparable by equality."""

    a = load_profile(tmp_path)
    b = load_profile(tmp_path)

    assert a == b
    assert a.discovery == b.discovery
    assert a.schedule == b.schedule


def test_loaded_profile_is_deterministic(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "./xx"

            [discovery]
            channels = ["mutation", "adversarial"]
            mutation_paths = ["src"]

            [schedule]
            nightly_at = "01:00"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    assert load_profile(tmp_path) == load_profile(tmp_path)


def test_explicit_path_overrides_default_location(tmp_path: Path) -> None:
    elsewhere = tmp_path / "custom" / "my_profile.toml"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text('repo = "./elsewhere"\n', encoding="utf-8")

    profile = load_profile(tmp_path, path=elsewhere)

    assert profile.repo == "./elsewhere"


def test_resolve_profile_path_default(tmp_path: Path) -> None:
    assert resolve_profile_path(tmp_path) == (tmp_path / DEFAULT_PROFILE_REL).resolve()


def test_resolve_profile_path_relative(tmp_path: Path) -> None:
    """A relative explicit path is resolved under repo."""

    assert (
        resolve_profile_path(tmp_path, Path("custom.toml"))
        == (tmp_path / "custom.toml").resolve()
    )


# --------------------------------------------------------------------------- #
# CLI — ``ccd profile``
# --------------------------------------------------------------------------- #


def test_cli_profile_displays_defaults_when_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no profile file at" in out
    assert "using all defaults" in out
    assert 'repo = "."' in out
    assert "[discovery]" in out
    assert "[schedule]" in out
    assert 'nightly_at = "02:00"' in out
    for ch in KNOWN_CHANNELS:
        assert f'"{ch}"' in out


def test_cli_profile_displays_loaded_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "./client"

            [schedule]
            nightly_at = "05:00"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "loaded from:" in out
    assert str(profile_path.resolve()) in out
    assert 'repo = "./client"' in out
    assert 'nightly_at = "05:00"' in out
    # Defaults filled in for unspecified fields.
    assert 'mutation_paths = ["ccd"]' in out


def test_cli_profile_explicit_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elsewhere = tmp_path / "custom.toml"
    elsewhere.write_text('repo = "./elsewhere"\n', encoding="utf-8")

    rc = cli.main(
        [
            "profile",
            "--repo",
            str(tmp_path),
            "--profile",
            str(elsewhere),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert str(elsewhere.resolve()) in out
    assert 'repo = "./elsewhere"' in out


def test_cli_profile_invalid_exits_non_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [discovery]
            channels = ["mutation", "telepathy"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "profile error" in captured.err
    assert "telepathy" in captured.err


def test_cli_profile_invalid_toml_exits_non_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text('repo = "broken\n', encoding="utf-8")

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "profile error" in captured.err
    assert "invalid TOML" in captured.err


# --------------------------------------------------------------------------- #
# Phase 2 — safety.fix_mode gate (spec_023 → 3-value by spec_028)
# --------------------------------------------------------------------------- #


def test_safety_default_fix_mode_is_off() -> None:
    """spec_028 §2-1: an absent profile / freshly-built ``Profile()``
    must default to ``safety.fix_mode="off"`` (safe — newly configured
    profiles do not auto-fix and do not produce proposals by surprise)."""

    profile = Profile()

    assert profile.safety.fix_mode == "off"


def test_safety_fix_mode_auto_via_toml(tmp_path: Path) -> None:
    """spec_028: operator opts into auto by writing
    ``fix_mode = "auto"`` — the loader surfaces it on ``safety.fix_mode``."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_mode = "auto"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.safety.fix_mode == "auto"


def test_safety_fix_mode_propose_via_toml(tmp_path: Path) -> None:
    """spec_028: ``fix_mode = "propose"`` is accepted."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_mode = "propose"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.safety.fix_mode == "propose"


def test_safety_fix_mode_unknown_value_raises_value_error(
    tmp_path: Path,
) -> None:
    """spec_028 §2-1: unknown ``fix_mode`` values must error (no silent
    fallback to default)."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_mode = "suggest"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    assert "suggest" in str(excinfo.value)


def test_safety_legacy_autonomous_fix_field_now_rejected(
    tmp_path: Path,
) -> None:
    """spec_028 §2-1: the boolean ``autonomous_fix`` field was removed
    (no backwards-compatible alias). Any TOML still carrying it must
    surface as a clear load error via ``extra="forbid"`` so the
    migration is loud rather than silent. This pin keeps the
    'migration leak' from regressing into a silent ignore."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            autonomous_fix = true
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    assert "autonomous_fix" in str(excinfo.value)


def test_safety_unknown_subfield_raises_value_error(tmp_path: Path) -> None:
    """``[safety]`` is ``extra="forbid"`` so an unknown subfield
    (e.g. a reserved Phase-2 knob CCD doesn't yet honor) raises rather
    than silently being dropped."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_mode = "auto"
            push = "yes-please"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    assert "push" in str(excinfo.value)


def test_safety_section_appears_in_render_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ccd profile`` surfaces the safety gate in its TOML-shaped render
    so an operator can copy-paste the block as a starting point."""

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "[safety]" in out
    assert 'fix_mode = "off"' in out


def test_safety_section_renders_fix_mode_when_enabled(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[safety]\nfix_mode = "auto"\n',
        encoding="utf-8",
    )

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert 'fix_mode = "auto"' in out


# --------------------------------------------------------------------------- #
# Phase 2 — safety.fix_templates staged enablement (spec_024)
# --------------------------------------------------------------------------- #


def test_safety_fix_templates_default_is_a_only() -> None:
    """spec_024 §2-3: 'A を一定期間信用してから B を足す'. The default
    must keep template B disabled so a freshly-built ``Profile()`` runs
    only the structurally-safest autonomous edit (test-only)."""

    profile = Profile()

    assert profile.safety.fix_templates == ["A"]


def test_safety_fix_templates_a_and_b_enable_template_b(tmp_path: Path) -> None:
    """Operator opts into template B by writing
    ``fix_templates = ["A", "B"]`` — the loader must preserve order so
    the loop's priority (A before B) stays as written."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_mode = "auto"
            fix_templates = ["A", "B"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.safety.fix_templates == ["A", "B"]
    assert profile.safety.fix_mode == "auto"


def test_safety_fix_templates_unknown_value_raises_value_error(
    tmp_path: Path,
) -> None:
    """An unknown template letter (``"Q"``) must error rather than be
    silently dropped — same pattern as the discovery-channels validator."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_templates = ["A", "Q"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    assert "Q" in str(excinfo.value)


def test_safety_fix_templates_empty_list_raises_value_error(
    tmp_path: Path,
) -> None:
    """An empty list is rejected — disable the loop via
    ``fix_mode = "off"`` (spec_028), not by stripping the template list."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_templates = []
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    assert "at least one template" in str(excinfo.value)


def test_safety_fix_templates_duplicate_raises_value_error(
    tmp_path: Path,
) -> None:
    """``["A", "A"]`` is a typo, not intent — reject so the operator
    notices and rewrites."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [safety]
            fix_templates = ["A", "A"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    assert "duplicate" in str(excinfo.value)


def test_safety_fix_templates_appears_in_render_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ccd profile`` surfaces the staged-enablement knob alongside the
    gate so an operator can copy-paste the block as a starting point."""

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "[safety]" in out
    assert 'fix_templates = ["A"]' in out


def test_safety_fix_templates_renders_with_both_when_enabled(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[safety]\nfix_templates = ["A", "B"]\n',
        encoding="utf-8",
    )

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert 'fix_templates = ["A", "B"]' in out


# --------------------------------------------------------------------------- #
# Phase 3 — schedule.cadence + schedule.weekly_day (spec_027)
# --------------------------------------------------------------------------- #


def test_schedule_known_cadences_constant() -> None:
    """The module exposes the allow-list as a tuple so other modules
    (and tests) can reference it without re-typing the literals."""

    assert KNOWN_CADENCES == ("nightly", "weekly")


def test_schedule_known_weekdays_constant() -> None:
    """Full English weekday names — what PowerShell's
    ``New-ScheduledTaskTrigger -DaysOfWeek`` consumes (spec_027 §2-2)."""

    assert set(KNOWN_WEEKDAYS) == {
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }
    assert len(KNOWN_WEEKDAYS) == 7


def test_schedule_cadence_default_is_weekly() -> None:
    """spec_027 §2-1: the **default flips to weekly** because running
    autonomous-fix every night while the system is under active
    development means chasing a moving target."""

    profile = Profile()

    assert profile.schedule.cadence == "weekly"


def test_schedule_weekly_day_default_is_sunday() -> None:
    """spec_027 §2-1: default weekly trigger fires on Sunday."""

    profile = Profile()

    assert profile.schedule.weekly_day == "Sunday"


def test_schedule_nightly_at_default_unchanged_from_spec_018() -> None:
    """spec_027 §2-1: ``nightly_at`` is **NOT renamed** — it still
    means 'time of day' and cadence-independently defaults to 02:00."""

    profile = Profile()

    assert profile.schedule.nightly_at == "02:00"


def test_schedule_cadence_nightly_accepted(tmp_path: Path) -> None:
    """``cadence = "nightly"`` keeps the legacy every-night trigger
    available for the spec_021–026 operating mode."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[schedule]\ncadence = "nightly"\n',
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.schedule.cadence == "nightly"
    # Other fields keep defaults
    assert profile.schedule.weekly_day == "Sunday"
    assert profile.schedule.nightly_at == "02:00"


def test_schedule_cadence_weekly_accepted(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [schedule]
            cadence = "weekly"
            weekly_day = "Saturday"
            nightly_at = "03:30"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.schedule.cadence == "weekly"
    assert profile.schedule.weekly_day == "Saturday"
    assert profile.schedule.nightly_at == "03:30"


def test_schedule_cadence_unknown_value_raises_value_error(
    tmp_path: Path,
) -> None:
    """spec_027 §2-1: ``cadence`` outside {nightly, weekly} must raise
    (same flavour as the ``channels`` / ``fix_templates`` validators)."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[schedule]\ncadence = "daily"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "cadence" in msg
    assert "daily" in msg


def test_schedule_weekly_day_unknown_value_raises_value_error(
    tmp_path: Path,
) -> None:
    """spec_027 §2-1: garbage weekday names (``"Funday"``) must raise —
    we don't want a typo to silently fall through to PowerShell which
    would then fail at task-registration time."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[schedule]\nweekly_day = "Funday"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "weekly_day" in msg
    assert "Funday" in msg


def test_schedule_weekly_day_short_form_rejected(tmp_path: Path) -> None:
    """Short forms (``"Sun"``) are not accepted — PowerShell's
    ``-DaysOfWeek`` wants the full name. We reject early so the
    operator notices in ``ccd profile`` rather than at PS1 run time."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[schedule]\nweekly_day = "Sun"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_profile(tmp_path)


def test_schedule_weekly_day_case_insensitive_input(tmp_path: Path) -> None:
    """spec_027 §2-1 / §6: input is title-cased so ``"sunday"`` is
    accepted and stored as ``"Sunday"`` — what lands in the profile is
    always the canonical form PowerShell consumes."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[schedule]\nweekly_day = "sunday"\n',
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.schedule.weekly_day == "Sunday"


def test_schedule_weekly_day_all_uppercase_input(tmp_path: Path) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[schedule]\nweekly_day = "WEDNESDAY"\n',
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.schedule.weekly_day == "Wednesday"


def test_schedule_cadence_missing_in_toml_yields_weekly_default(
    tmp_path: Path,
) -> None:
    """**Backward-compatibility pin** (spec_027 §4): a TOML written
    against spec_018–026 (no ``cadence`` field) must load cleanly and
    end up with ``cadence="weekly"``. spec_028 updates this fixture to
    use ``fix_mode = "auto"`` (the post-spec_028 shape of the deployed
    ``_ai_workspace/ccd_profile.toml``)."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "."

            [discovery]
            channels = ["mutation"]
            mutation_paths = ["ccd/protocol.py"]

            [schedule]
            nightly_at = "02:00"

            [safety]
            fix_mode = "auto"
            fix_templates = ["A"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.schedule.nightly_at == "02:00"
    assert profile.schedule.cadence == "weekly"
    assert profile.schedule.weekly_day == "Sunday"


def test_schedule_weekly_day_harmless_under_nightly_cadence(
    tmp_path: Path,
) -> None:
    """spec_027 §2-1: ``weekly_day`` is ignored by the scheduler when
    ``cadence="nightly"``, but writing it in the TOML is still allowed
    so an operator can pre-set the day before flipping to weekly."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [schedule]
            cadence = "nightly"
            weekly_day = "Wednesday"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.schedule.cadence == "nightly"
    assert profile.schedule.weekly_day == "Wednesday"


def test_schedule_section_renders_cadence_and_weekly_day(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """spec_027 §2-3: ``ccd profile`` emits ``cadence`` and
    ``weekly_day`` alongside ``nightly_at`` under ``[schedule]``."""

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "[schedule]" in out
    assert 'nightly_at = "02:00"' in out
    assert 'cadence = "weekly"' in out
    assert 'weekly_day = "Sunday"' in out


def test_schedule_section_renders_overridden_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            [schedule]
            cadence = "nightly"
            weekly_day = "Friday"
            nightly_at = "04:15"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    rc = cli.main(["profile", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert 'cadence = "nightly"' in out
    assert 'weekly_day = "Friday"' in out
    assert 'nightly_at = "04:15"' in out


def test_register_nightly_ps1_supports_cadence() -> None:
    """spec_027 §2-2: the scheduler template must expose ``$Cadence`` /
    ``$WeeklyDay`` editing points, branch the trigger by cadence
    (``-Weekly -DaysOfWeek`` for weekly, ``-Daily`` for nightly), and
    halt on an unknown cadence rather than silently fall back to Daily.

    We don't execute the PS1 — only check the template text contains the
    expected anchors. The PS1 is a *template* (human edits + runs),
    so a text-level check is enough to catch a regression in the spec
    surface.

    Note: ``_ai_workspace/`` is gitignored (the PS1 ships as a working
    template, not a committed artifact), so when the file is missing
    from a clean checkout this test is skipped rather than failing —
    text checks would be meaningless against a non-existent file."""

    # ``register_nightly.ps1`` lives under the repo's ``_ai_workspace/``
    # directory. ``tests/`` is at ``<repo>/tests/`` so ``../`` is repo
    # root.
    ps1 = (
        Path(__file__).resolve().parent.parent
        / "_ai_workspace"
        / "register_nightly.ps1"
    )
    if not ps1.exists():
        pytest.skip(
            f"{ps1} not present (gitignored template); "
            "skip text-level cadence checks"
        )
    body = ps1.read_text(encoding="utf-8")

    # Editing points exist
    assert "$Cadence" in body
    assert "$WeeklyDay" in body

    # Both branches exist
    assert "-Weekly" in body
    assert "-DaysOfWeek" in body
    assert "-Daily" in body

    # Unknown cadence stops the script
    assert "Write-Error" in body

    # Default mirrors the profile default (spec_027 §2-1)
    assert '$Cadence     = "weekly"' in body
    assert '$WeeklyDay   = "Sunday"' in body


def test_render_profile_round_trip_preserves_schedule(tmp_path: Path) -> None:
    """spec_027 §2-4: rendering then re-loading the output yields an
    equal ``Profile`` — the renderer is a faithful TOML emitter and
    the new fields don't break the round-trip."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "./client"

            [discovery]
            channels = ["mutation", "adversarial"]
            mutation_paths = ["src", "lib"]

            [schedule]
            nightly_at = "03:30"
            cadence = "weekly"
            weekly_day = "Saturday"

            [safety]
            fix_mode = "auto"
            fix_templates = ["A", "B"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    original = load_profile_with_source(tmp_path)
    rendered = render_profile(original)

    # Strip the leading `# loaded from:` comment line so the body is
    # pure TOML, then write it to a new file and re-load.
    body = "\n".join(
        line for line in rendered.splitlines() if not line.startswith("#")
    )
    round_trip_path = tmp_path / "round_trip.toml"
    round_trip_path.write_text(body, encoding="utf-8")

    reloaded = load_profile(tmp_path, path=round_trip_path)

    assert reloaded == original.profile
