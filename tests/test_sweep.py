"""Tests for ``ccd/sweep.py`` and the ``ccd nightly-all`` CLI (spec_029).

The sweep is a thin loop that:

1. loads ``_ai_workspace/profiles/*.toml`` into a list of named
   policies (falling back to a single ``"ccd"`` policy when the
   directory is absent),
2. runs ``run_nightly`` per policy with per-policy output redirection
   so client repos never receive a write,
3. isolates per-policy failures so 1 bad policy does not stop the
   rest,
4. writes a one-line-per-policy cross-policy index.

These tests inject fake nightly runners and fake registries so the
sweep is exercised end-to-end without invoking real ``mutmut`` /
``claude`` / git (spec_029 §2-5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from ccd import cli
from ccd.nightly import AutoFixOutcome, ChannelOutcome, NightlyResult
from ccd.profile import (
    DEFAULT_FALLBACK_POLICY_NAME,
    Profile,
    load_profile_registry,
)
from ccd.sweep import (
    PolicyOutcome,
    render_index,
    run_nightly_all,
)

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingNightlyRunner:
    """Fake ``run_nightly`` that records calls and returns canned results.

    ``per_policy_result`` lets a test pin a specific ``NightlyResult``
    per profile-name; otherwise a generic successful result is
    returned. ``raise_for`` causes the runner to raise ``Exception``
    for the named policies — used to exercise failure isolation.
    """

    per_policy_result: dict[str, NightlyResult] = field(default_factory=dict)
    raise_for: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> NightlyResult:
        self.calls.append(kwargs)
        profile: Profile = kwargs["profile"]
        # We key by `repo` field because that's the only stable per-
        # policy identifier the runner sees (the sweep does not pass
        # a `name` parameter through — only `profile`).
        for name, canned in self.per_policy_result.items():
            if profile.repo.endswith(name) or name == profile.repo:
                if name in self.raise_for:
                    raise RuntimeError(f"injected failure for {name}")
                return canned
        # Failure-by-name: if the policy's repo *ends* with one of the
        # raise_for entries we raise. This is robust to the sweep
        # resolving relative paths.
        for name in self.raise_for:
            if profile.repo.endswith(name) or profile.repo == name:
                raise RuntimeError(f"injected failure for {name}")
        # Default canned success.
        return _ok_result(profile=profile)


def _ok_result(
    *,
    profile: Profile,
    merged: bool = False,
    proposed: bool = False,
    report_path: Path | None = None,
) -> NightlyResult:
    """Build a generic successful NightlyResult."""

    auto_fix: AutoFixOutcome | None = None
    if merged:
        auto_fix = AutoFixOutcome(
            skipped=False,
            spec_auto_id="spec_auto_001",
            template="A",
            branch="auto/spec_auto_001",
            merged=True,
            mode="auto",
        )
    elif proposed:
        auto_fix = AutoFixOutcome(
            skipped=False,
            spec_auto_id="spec_auto_001",
            template="A",
            branch="propose/spec_auto_001",
            mode="propose",
            proposed=True,
            proposal_patch_path=Path("/tmp/example.patch"),
        )

    return NightlyResult(
        success=True,
        profile=profile,
        channels_run=(
            ChannelOutcome(
                channel="mutation",
                success=True,
                halt_reason="",
                report_md_path=None,
                report_json_path=None,
            ),
        ),
        brief_report_wsl=report_path,
        auto_fix=auto_fix,
    )


def _write_policy(
    profiles_dir: Path,
    name: str,
    *,
    repo: str = ".",
    fix_mode: str = "off",
    extra: str = "",
) -> Path:
    """Drop one ``<name>.toml`` into ``profiles_dir`` and return its path."""

    profiles_dir.mkdir(parents=True, exist_ok=True)
    body = dedent(
        f"""
        repo = "{repo}"

        [safety]
        fix_mode = "{fix_mode}"
        """
    ).strip() + "\n"
    if extra:
        body = body + extra + "\n"
    path = profiles_dir / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Registry — discovery
# --------------------------------------------------------------------------- #


def test_registry_reads_every_toml_in_profiles_dir(tmp_path: Path) -> None:
    """Every ``*.toml`` under ``_ai_workspace/profiles/`` becomes one
    named policy. Policy name == filename stem; ordering is
    deterministic (alphabetical)."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "ccd", fix_mode="auto")
    _write_policy(profiles_dir, "samurai", repo="./samurai", fix_mode="propose")
    _write_policy(profiles_dir, "travel-mail", repo="./travel", fix_mode="propose")

    registry = load_profile_registry(tmp_path)

    assert [e.name for e in registry] == ["ccd", "samurai", "travel-mail"]
    by_name = {e.name: e for e in registry}
    assert by_name["ccd"].profile.safety.fix_mode == "auto"
    assert by_name["samurai"].profile.safety.fix_mode == "propose"
    assert by_name["samurai"].profile.repo == "./samurai"
    assert by_name["travel-mail"].profile.safety.fix_mode == "propose"


def test_registry_falls_back_to_single_profile_when_dir_missing(
    tmp_path: Path,
) -> None:
    """No ``profiles/`` dir → loader falls back to the legacy single
    profile (``ccd_profile.toml``). Policy name is ``"ccd"``."""

    # Drop a legacy single profile.
    legacy = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        'repo = "./solo"\n[safety]\nfix_mode = "auto"\n',
        encoding="utf-8",
    )

    registry = load_profile_registry(tmp_path)

    assert len(registry) == 1
    entry = registry[0]
    assert entry.name == DEFAULT_FALLBACK_POLICY_NAME == "ccd"
    assert entry.profile.repo == "./solo"
    assert entry.profile.safety.fix_mode == "auto"
    assert entry.source == legacy.resolve()


def test_registry_fallback_returns_all_defaults_when_no_legacy_profile(
    tmp_path: Path,
) -> None:
    """No ``profiles/`` AND no ``ccd_profile.toml`` → fallback still
    returns one entry, with the all-defaults ``Profile()``."""

    registry = load_profile_registry(tmp_path)

    assert len(registry) == 1
    entry = registry[0]
    assert entry.name == "ccd"
    assert entry.profile == Profile()
    assert entry.source is None


def test_registry_empty_directory_yields_empty_registry(tmp_path: Path) -> None:
    """``profiles/`` exists but is empty → empty registry (NOT fallback).
    Operators who have migrated to the registry but not yet populated it
    see "nothing to sweep" instead of a silent fallback to the legacy
    single-profile path."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    profiles_dir.mkdir(parents=True)
    # Also drop a legacy profile to prove the fallback does NOT trigger.
    (tmp_path / "_ai_workspace" / "ccd_profile.toml").write_text(
        'repo = "./solo"\n', encoding="utf-8"
    )

    registry = load_profile_registry(tmp_path)

    assert registry == []


def test_registry_rejects_invalid_policy_name(tmp_path: Path) -> None:
    """Filename with chars outside ``[A-Za-z0-9_-]`` raises ``ValueError``
    instead of silently skipping the policy."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "samurai")
    # Drop a file with an invalid name (spaces / dot inside stem).
    bad = profiles_dir / "client one.toml"
    bad.write_text('repo = "."\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid policy name"):
        load_profile_registry(tmp_path)


def test_registry_rejects_malformed_toml(tmp_path: Path) -> None:
    """TOML parse errors are surfaced with the offending file path."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "broken.toml").write_text(
        "this is not = toml [\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="broken.toml.*invalid TOML"):
        load_profile_registry(tmp_path)


def test_registry_rejects_schema_violation(tmp_path: Path) -> None:
    """A TOML that parses but violates the Profile schema raises with
    the file path included (same contract as the single-profile loader)."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "wrong.toml").write_text(
        '[safety]\nfix_mode = "not-a-real-mode"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wrong.toml.*invalid profile"):
        load_profile_registry(tmp_path)


def test_registry_load_profile_unchanged_by_spec_029(tmp_path: Path) -> None:
    """spec_029 must not change the single-profile loader. A repo with
    only a legacy ``ccd_profile.toml`` and no registry directory still
    loads exactly as it did before (parity test for ``load_profile``)."""

    from ccd.profile import load_profile

    legacy = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('repo = "./client"\n', encoding="utf-8")

    profile = load_profile(tmp_path)

    assert profile.repo == "./client"
    # And the registry view returns the same profile.
    registry = load_profile_registry(tmp_path)
    assert registry[0].profile == profile


# --------------------------------------------------------------------------- #
# Sweep — multi-policy ordering + per-policy output redirection
# --------------------------------------------------------------------------- #


def test_sweep_runs_every_policy_in_order(tmp_path: Path) -> None:
    """The sweep invokes ``run_nightly`` once per policy in the registry
    in alphabetical order. Each call carries that policy's profile."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "alpha", repo=str(tmp_path / "alpha"))
    _write_policy(profiles_dir, "beta", repo=str(tmp_path / "beta"))
    _write_policy(profiles_dir, "gamma", repo=str(tmp_path / "gamma"))
    for d in ("alpha", "beta", "gamma"):
        (tmp_path / d).mkdir()

    runner = _RecordingNightlyRunner()
    sweep = run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )

    assert sweep.success is True
    assert [c["profile"].repo for c in runner.calls] == [
        str(tmp_path / "alpha"),
        str(tmp_path / "beta"),
        str(tmp_path / "gamma"),
    ]
    assert [p.name for p in sweep.policies] == ["alpha", "beta", "gamma"]
    for p in sweep.policies:
        assert p.success is True


def test_sweep_redirects_each_policy_outputs_under_ccd_workspace(
    tmp_path: Path,
) -> None:
    """Per-policy ``discover_dir`` / ``brief_dir`` / ``proposal_dir`` are
    computed by the sweep so client repos never receive a write
    (spec_029 §2-3 privacy / isolation)."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "samurai", repo=str(tmp_path / "samurai"))
    (tmp_path / "samurai").mkdir()

    runner = _RecordingNightlyRunner()
    run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )

    call = runner.calls[0]
    expected_discover = (
        tmp_path / "_ai_workspace" / "discover" / "samurai"
    ).resolve()
    expected_brief = (
        tmp_path / "_ai_workspace" / "nightly" / "samurai"
    ).resolve()
    expected_proposal = (expected_brief / "proposals").resolve()
    assert Path(call["discover_dir"]) == expected_discover
    assert Path(call["brief_dir"]) == expected_brief
    assert Path(call["proposal_dir"]) == expected_proposal
    # And the target repo is the policy's own repo, NOT the CCD repo.
    assert Path(call["repo"]) == (tmp_path / "samurai").resolve()


def test_sweep_fallback_preserves_legacy_flat_paths(tmp_path: Path) -> None:
    """Single-policy fallback (no ``profiles/`` dir) must keep the
    spec_020 flat layout — ``run_nightly`` is called WITHOUT
    ``discover_dir`` / ``brief_dir`` / ``proposal_dir`` overrides so
    existing tests + existing operation are unchanged."""

    # No profiles/ dir; only the legacy single profile.
    (tmp_path / "_ai_workspace").mkdir()
    (tmp_path / "_ai_workspace" / "ccd_profile.toml").write_text(
        'repo = "."\n', encoding="utf-8"
    )

    runner = _RecordingNightlyRunner()
    run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )

    assert len(runner.calls) == 1
    call = runner.calls[0]
    # The flat layout: no path overrides passed through.
    assert call["discover_dir"] is None
    assert call["brief_dir"] is None
    assert call["proposal_dir"] is None


# --------------------------------------------------------------------------- #
# Sweep — failure isolation (spec §2-2 论点4)
# --------------------------------------------------------------------------- #


def test_sweep_isolates_per_policy_failure_and_continues(
    tmp_path: Path,
) -> None:
    """When policy N raises, policies N+1..end still run AND the sweep
    returns success=True. The failed policy is recorded with
    ``success=False`` and the exception text."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "alpha", repo=str(tmp_path / "alpha"))
    _write_policy(profiles_dir, "beta_broken", repo=str(tmp_path / "beta_broken"))
    _write_policy(profiles_dir, "gamma", repo=str(tmp_path / "gamma"))
    for d in ("alpha", "beta_broken", "gamma"):
        (tmp_path / d).mkdir()

    runner = _RecordingNightlyRunner(raise_for={"beta_broken"})
    sweep = run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )

    # The sweep itself completed (every policy was *attempted*).
    assert sweep.success is True
    assert [p.name for p in sweep.policies] == ["alpha", "beta_broken", "gamma"]
    by_name = {p.name: p for p in sweep.policies}
    assert by_name["alpha"].success is True
    assert by_name["beta_broken"].success is False
    assert "injected failure" in by_name["beta_broken"].error
    assert by_name["gamma"].success is True
    # All three policies actually had ``run_nightly`` invoked even
    # though the middle one raised (i.e. the sweep moved on).
    assert len(runner.calls) == 3


def test_sweep_records_internal_halt_as_failure(tmp_path: Path) -> None:
    """A ``NightlyResult`` returned with ``success=False`` (internal halt
    like channel canary / brief render failure) is surfaced in the
    index as a policy failure but does NOT stop subsequent policies."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "alpha", repo=str(tmp_path / "alpha"))
    _write_policy(profiles_dir, "halted", repo=str(tmp_path / "halted"))
    (tmp_path / "alpha").mkdir()
    (tmp_path / "halted").mkdir()

    halted_result = NightlyResult(
        success=False,
        profile=Profile(repo=str(tmp_path / "halted")),
        halt_reason="pre-flight failed: simulated",
    )
    runner = _RecordingNightlyRunner(
        per_policy_result={"halted": halted_result},
    )
    sweep = run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )

    by_name = {p.name: p for p in sweep.policies}
    assert by_name["alpha"].success is True
    assert by_name["halted"].success is False
    assert "pre-flight failed" in by_name["halted"].error


# --------------------------------------------------------------------------- #
# Cross-policy index
# --------------------------------------------------------------------------- #


def test_sweep_writes_cross_policy_index(tmp_path: Path) -> None:
    """The sweep writes ``_ai_workspace/nightly/index_YYYY-MM-DD.md``
    with one line per policy and a link to the per-policy report."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "ccd", repo=str(tmp_path), fix_mode="auto")
    _write_policy(profiles_dir, "samurai", repo=str(tmp_path / "samurai"))
    (tmp_path / "samurai").mkdir()

    samurai_report = (
        tmp_path / "_ai_workspace" / "nightly" / "samurai" / "report_2026-05-25.md"
    )
    ccd_report = (
        tmp_path / "_ai_workspace" / "nightly" / "ccd" / "report_2026-05-25.md"
    )

    def _ok(**kwargs: Any) -> NightlyResult:
        profile: Profile = kwargs["profile"]
        if profile.repo.endswith("samurai"):
            report = samurai_report
            proposed = True
        else:
            report = ccd_report
            proposed = False
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# fake report\n", encoding="utf-8")
        return _ok_result(profile=profile, proposed=proposed, report_path=report)

    sweep = run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=_ok,
    )

    assert sweep.index_path is not None
    assert sweep.index_path.name == "index_2026-05-25.md"
    body = sweep.index_path.read_text(encoding="utf-8")
    # Index opens with a v2 Phase 3 banner.
    assert "横断インデックス 2026-05-25" in body
    # One bullet per policy with the policy name in code-fence ticks.
    assert "`ccd`" in body
    assert "`samurai`" in body
    # The propose-mode policy got the "修正案 1 件を生成" headline.
    assert "修正案 1 件を生成" in body
    # The relative report link from index is correct
    # (index sits at .../nightly/, samurai report sits at .../nightly/samurai/).
    assert "samurai/report_2026-05-25.md" in body
    assert "ccd/report_2026-05-25.md" in body


def test_sweep_index_marks_failed_policies(tmp_path: Path) -> None:
    """A policy whose ``run_nightly`` raised gets a "失敗 — ..." line
    in the index (论点4 visibility)."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "alpha", repo=str(tmp_path / "alpha"))
    _write_policy(profiles_dir, "broken", repo=str(tmp_path / "broken"))
    (tmp_path / "alpha").mkdir()
    (tmp_path / "broken").mkdir()

    runner = _RecordingNightlyRunner(raise_for={"broken"})
    sweep = run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )
    body = sweep.index_path.read_text(encoding="utf-8")
    assert "`broken`: **失敗**" in body
    assert "injected failure" in body
    # The succeeded policy is still in the index.
    assert "`alpha`" in body


def test_sweep_index_fallback_mode_carries_marker(tmp_path: Path) -> None:
    """In fallback mode the index includes a note that single-policy
    operation is active so the operator notices the migration path."""

    (tmp_path / "_ai_workspace").mkdir()
    (tmp_path / "_ai_workspace" / "ccd_profile.toml").write_text(
        'repo = "."\n', encoding="utf-8"
    )

    runner = _RecordingNightlyRunner()
    sweep = run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )
    body = sweep.index_path.read_text(encoding="utf-8")
    assert "単一プロファイル運用" in body
    assert "profiles/<施策名>.toml" in body


def test_index_empty_registry_renders_meaningful_message(tmp_path: Path) -> None:
    """An empty ``profiles/`` directory yields a 0-policy sweep — the
    index still renders with a "no policies to sweep" message rather
    than crashing or writing nothing."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    profiles_dir.mkdir(parents=True)

    runner = _RecordingNightlyRunner()
    sweep = run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )

    assert sweep.policies == []
    assert sweep.success is True
    body = sweep.index_path.read_text(encoding="utf-8")
    assert "処理対象の施策がありません" in body
    # And run_nightly was never called.
    assert runner.calls == []


def test_render_index_summarises_each_outcome_kind() -> None:
    """Direct test of the renderer: every variety of policy outcome
    becomes the right one-line summary."""

    merged_result = NightlyResult(
        success=True,
        profile=Profile(),
        auto_fix=AutoFixOutcome(
            skipped=False,
            spec_auto_id="spec_auto_001",
            template="A",
            branch="auto/spec_auto_001",
            merged=True,
            mode="auto",
        ),
    )
    proposed_result = NightlyResult(
        success=True,
        profile=Profile(),
        auto_fix=AutoFixOutcome(
            skipped=False,
            spec_auto_id="spec_auto_002",
            template="A",
            branch="propose/spec_auto_002",
            mode="propose",
            proposed=True,
        ),
    )
    halted_result = NightlyResult(
        success=True,
        profile=Profile(),
        auto_fix=AutoFixOutcome(
            skipped=False,
            spec_auto_id="spec_auto_003",
            template="A",
            branch="propose/spec_auto_003",
            mode="propose",
            proposed=False,
            halt_reason="proposal guard halted: R1 violation",
        ),
    )
    discover_only = NightlyResult(
        success=True,
        profile=Profile(),
        channels_run=(
            ChannelOutcome(
                channel="mutation",
                success=True,
                halt_reason="",
                report_md_path=None,
                report_json_path=None,
            ),
        ),
    )

    body = render_index(
        today=date(2026, 5, 25),
        policies=[
            PolicyOutcome(name="ccd", success=True, result=merged_result),
            PolicyOutcome(name="samurai", success=True, result=proposed_result),
            PolicyOutcome(name="travel-mail", success=True, result=halted_result),
            PolicyOutcome(name="docs-only", success=True, result=discover_only),
            PolicyOutcome(name="broken", success=False, error="boom"),
        ],
    )
    assert "`ccd`: 自律修正 1 件を merge" in body
    assert "`samurai`: 修正案 1 件を生成" in body
    assert "`travel-mail`: 提案モード HALT" in body
    assert "`docs-only`: 発見のみ" in body
    assert "`broken`: **失敗**" in body


# --------------------------------------------------------------------------- #
# CLI surface — `ccd nightly-all`
# --------------------------------------------------------------------------- #


def test_cli_nightly_all_invokes_sweep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ccd nightly-all --repo <path>`` invokes the sweep and prints a
    one-line-per-policy stdout summary plus the index path."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "policy_a", repo=str(tmp_path / "a"))
    _write_policy(profiles_dir, "policy_b", repo=str(tmp_path / "b"))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    runner = _RecordingNightlyRunner()
    rc = cli.main(
        ["nightly-all", "--repo", str(tmp_path)],
        nightly_runner=runner,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "policies processed: 2" in captured.out
    assert "policy_a: ok" in captured.out
    assert "policy_b: ok" in captured.out
    assert "cross-policy index:" in captured.out
    assert "index_" in captured.out


def test_cli_nightly_all_surfaces_failure_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failed policies print ``<name>: failed (<reason>)`` but the CLI
    still exits 0 (the sweep itself is a "successful round" even when a
    single policy failed)."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(profiles_dir, "alpha", repo=str(tmp_path / "alpha"))
    _write_policy(profiles_dir, "broken", repo=str(tmp_path / "broken"))
    (tmp_path / "alpha").mkdir()
    (tmp_path / "broken").mkdir()

    runner = _RecordingNightlyRunner(raise_for={"broken"})
    rc = cli.main(
        ["nightly-all", "--repo", str(tmp_path)],
        nightly_runner=runner,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha: ok" in out
    assert "broken: failed" in out
    assert "injected failure" in out


def test_cli_nightly_all_registry_error_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Registry-level errors (invalid policy name / parse / schema) are
    surfaced with exit code 1 — the operator must fix the registry
    before the sweep can proceed."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "client one.toml").write_text(
        'repo = "."\n', encoding="utf-8"
    )

    rc = cli.main(["nightly-all", "--repo", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "nightly-all halted" in err
    assert "invalid policy name" in err


def test_cli_nightly_all_keeps_nightly_subcommand_unchanged(
    tmp_path: Path,
) -> None:
    """``nightly-all`` is additive — the single-policy ``nightly``
    subcommand still exists and routes through ``run_nightly`` as
    before (spec_029 §3 "ccd nightly は不変").

    Both subcommands are registered on the parser; each accepts
    ``--repo``; the ``args.command`` field differentiates them so the
    main dispatch picks the right handler."""

    parser = cli.build_parser()
    args_all = parser.parse_args(["nightly-all", "--repo", str(tmp_path)])
    assert args_all.command == "nightly-all"

    parser = cli.build_parser()
    args_one = parser.parse_args(["nightly", "--repo", str(tmp_path)])
    assert args_one.command == "nightly"

    # And the subcommand surface count is now 12 (was 11 before spec_029).
    parser = cli.build_parser()
    sub_actions = [
        a for a in parser._actions  # noqa: SLF001 — argparse public-but-untyped
        if isinstance(a, type(parser._subparsers._group_actions[0]))  # type: ignore[union-attr,attr-defined]
    ]
    assert len(sub_actions) >= 1
    choices = sub_actions[0].choices  # type: ignore[attr-defined]
    assert "nightly" in choices
    assert "nightly-all" in choices
    assert len(choices) == 12


# --------------------------------------------------------------------------- #
# Privacy: propose / off policies write nothing to the target repo
# --------------------------------------------------------------------------- #


def test_sweep_does_not_write_to_target_repo_for_propose_off(
    tmp_path: Path,
) -> None:
    """When a policy is ``fix_mode="propose"`` or ``"off"``, the sweep
    must not write to its target repo (spec §2-3 privacy invariant).

    We exercise this structurally: the per-policy ``discover_dir`` /
    ``brief_dir`` / ``proposal_dir`` passed to ``run_nightly`` MUST all
    sit under the CCD repo, NEVER under the target repo. This is the
    structural guarantee — provided ``run_nightly`` honors those
    overrides (which spec_028's seam wiring + the spec_029 forwarding
    in this commit ensures), the target repo never receives a write.
    """

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    target = tmp_path / "client_repo"
    target.mkdir()
    _write_policy(
        profiles_dir, "client", repo=str(target), fix_mode="propose"
    )

    runner = _RecordingNightlyRunner()
    run_nightly_all(
        repo=tmp_path,
        today=date(2026, 5, 25),
        nightly_runner=runner,
    )

    call = runner.calls[0]
    ccd_workspace = (tmp_path / "_ai_workspace").resolve()
    # Every output path sits under CCD's own _ai_workspace.
    for key in ("discover_dir", "brief_dir", "proposal_dir"):
        path = Path(call[key]).resolve()
        # ``path.is_relative_to`` is the cleanest "starts with" — 3.9+.
        assert path.is_relative_to(ccd_workspace), (
            f"{key}={path} must sit under CCD's _ai_workspace ({ccd_workspace})"
        )
        # Crucially, NOT under the target repo.
        assert not path.is_relative_to(target.resolve()), (
            f"{key}={path} must NOT sit under target repo {target}"
        )


# --------------------------------------------------------------------------- #
# spec_030 — adversarial channel routing in sweep mode
# --------------------------------------------------------------------------- #


def test_sweep_skips_adversarial_when_unconfigured(tmp_path: Path) -> None:
    """spec_030 §2-3 — a policy that lists ``"adversarial"`` in
    ``discovery.channels`` but does NOT supply
    ``[discovery.adversarial.parsers]`` is **skipped**, not silently
    routed to CCD's hard-coded parsers. The skip is surfaced to the
    nightly orchestrator via ``channel_skips`` so the morning brief's
    §D shows an honest line."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(
        profiles_dir,
        "axis",
        repo=str(tmp_path / "axis_repo"),
        extra=(
            '[discovery]\n'
            'channels = ["mutation", "adversarial"]\n'
            'mutation_paths = ["src"]\n'
        ),
    )
    (tmp_path / "axis_repo").mkdir()

    fake = _RecordingNightlyRunner()
    run_nightly_all(repo=tmp_path, nightly_runner=fake, today=date(2026, 5, 27))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    # No adversarial parsers were injected because the profile has no
    # [discovery.adversarial.parsers].
    assert "adversarial_parsers" not in call or call["adversarial_parsers"] is None
    # The skip reason was passed instead.
    skips = call.get("channel_skips") or {}
    assert "adversarial" in skips
    assert "[discovery.adversarial.parsers]" in skips["adversarial"]


def test_sweep_routes_adversarial_parsers_from_profile(tmp_path: Path) -> None:
    """spec_030 §2-3 — when ``[discovery.adversarial.parsers]`` is set,
    the sweep resolves the targets and forwards them via
    ``adversarial_parsers``. ``channel_skips`` is not populated."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(
        profiles_dir,
        "ccd",
        repo=str(tmp_path),
        extra=(
            '[discovery]\n'
            'channels = ["mutation", "adversarial"]\n'
            'mutation_paths = ["ccd"]\n'
            '\n'
            '[[discovery.adversarial.parsers]]\n'
            'import = "ccd.protocol.parse_spec"\n'
            'input_kind = "path"\n'
            '\n'
            '[[discovery.adversarial.parsers]]\n'
            'import = "ccd.protocol.parse_result"\n'
            'input_kind = "path"\n'
        ),
    )

    fake = _RecordingNightlyRunner()
    run_nightly_all(repo=tmp_path, nightly_runner=fake, today=date(2026, 5, 27))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    parsers = call.get("adversarial_parsers")
    assert parsers is not None
    assert tuple(p.name for p in parsers) == (
        "ccd.protocol.parse_spec",
        "ccd.protocol.parse_result",
    )
    assert not call.get("channel_skips")


def test_sweep_surfaces_bad_adversarial_import_as_skip(tmp_path: Path) -> None:
    """spec_030 §2-3 — a target whose dotted path cannot resolve (e.g.
    module missing in the runtime env) must NOT silently fall back to
    CCD parsers. Resolution errors are turned into a skip reason for
    §D so the operator fixes the profile."""

    profiles_dir = tmp_path / "_ai_workspace" / "profiles"
    _write_policy(
        profiles_dir,
        "axis",
        repo=str(tmp_path / "axis_repo"),
        extra=(
            '[discovery]\n'
            'channels = ["adversarial"]\n'
            '\n'
            '[[discovery.adversarial.parsers]]\n'
            'import = "axis_does_not_exist.parser"\n'
            'input_kind = "path"\n'
        ),
    )
    (tmp_path / "axis_repo").mkdir()

    fake = _RecordingNightlyRunner()
    run_nightly_all(repo=tmp_path, nightly_runner=fake, today=date(2026, 5, 27))

    call = fake.calls[0]
    assert call.get("adversarial_parsers") is None
    skips = call.get("channel_skips") or {}
    assert "adversarial" in skips
    assert "cannot resolve" in skips["adversarial"]


def test_sweep_does_not_skip_adversarial_in_fallback_mode(tmp_path: Path) -> None:
    """spec_030 §2-3 — single-profile fallback (no ``profiles/`` dir)
    must preserve spec_015 behavior bit-for-bit: adversarial keeps
    using ``default_parsers()``. The sweep does NOT inject skip
    reasons in fallback mode."""

    # Legacy single profile with adversarial in channels but no
    # [discovery.adversarial] block — spec_015 behavior must hold.
    legacy = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        '[discovery]\n'
        'channels = ["mutation", "adversarial"]\n'
        'mutation_paths = ["ccd"]\n',
        encoding="utf-8",
    )

    fake = _RecordingNightlyRunner()
    run_nightly_all(repo=tmp_path, nightly_runner=fake, today=date(2026, 5, 27))

    call = fake.calls[0]
    # No skip and no injected parsers — falls through to run_channel
    # which then defaults to default_parsers().
    assert not call.get("channel_skips")
    assert call.get("adversarial_parsers") is None
