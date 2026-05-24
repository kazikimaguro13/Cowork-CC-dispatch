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
    KNOWN_CHANNELS,
    Profile,
    load_profile,
    load_profile_with_source,
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
    """Unknown top-level fields (e.g. Phase 2 ``safety``) must error,
    not be silently dropped — operators must not rely on a field CCD
    doesn't yet honor."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "."
            safety = "branch-only"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "invalid profile" in msg
    assert "safety" in msg


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
