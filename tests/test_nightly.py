"""Tests for ``ccd/nightly.py:run_nightly`` and the ``ccd nightly`` CLI.

spec_020 wires the Phase-1 scheduler skeleton: load profile, light
pre-flight, run the profile's enabled discovery channels in order,
render the morning brief, mirror the brief to a Windows-visible path.

These tests never invoke real ``mutmut`` / ``claude`` / Windows file
operations. Instead they pass fake ``channel_runner`` / ``brief_runner``
/ ``windows_mirror`` callbacks so the orchestration is exercised
end-to-end without any subprocess.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ccd import cli
from ccd.brief import BriefResult, BriefSummary
from ccd.nightly import (
    ChannelOutcome,
    NightlyResult,
    run_nightly,
)
from ccd.profile import (
    DiscoveryConfig,
    Profile,
    ScheduleConfig,
)

# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #


@dataclass
class _FakeChannelResult:
    """Minimal duck-typed stand-in for DiscoveryResult / AdversarialResult /
    AIReviewResult. The orchestrator only reads four fields, so this is all
    the fakery the tests need."""

    success: bool = True
    report_md_path: Path | None = None
    report_json_path: Path | None = None
    halt_reason: str = ""


@dataclass
class _RecordingChannelRunner:
    """Fake ``channel_runner`` that records calls and returns canned results.

    ``per_channel`` maps a channel name to a ``_FakeChannelResult`` (or a
    callable that gets ``(channel, repo, paths)`` and returns one). Channels
    not present default to a successful result with no report paths.
    """

    per_channel: dict[
        str,
        _FakeChannelResult | Callable[..., _FakeChannelResult],
    ] = field(default_factory=dict)
    calls: list[tuple[str, Path, tuple[str, ...] | None]] = field(
        default_factory=list
    )

    def __call__(
        self,
        channel: str,
        *,
        repo: Path,
        paths: list[str] | None = None,
        **_ignored: Any,
    ) -> _FakeChannelResult:
        self.calls.append(
            (channel, Path(repo), tuple(paths) if paths is not None else None)
        )
        canned = self.per_channel.get(channel, _FakeChannelResult())
        if callable(canned):
            return canned(channel=channel, repo=repo, paths=paths)
        return canned


def _make_fake_brief_runner(
    *,
    success: bool = True,
    halt_reason: str = "",
) -> tuple[Callable[..., BriefResult], list[Any]]:
    """Return (fake_brief_runner, calls_list). The fake writes a tiny
    morning report so the Windows mirror has something to copy."""

    calls: list[Any] = []

    def _fake(*, repo: Path, today: date | None = None, **_ignored: Any) -> BriefResult:
        calls.append({"repo": Path(repo), "today": today})
        nightly_dir = Path(repo) / "_ai_workspace" / "nightly"
        nightly_dir.mkdir(parents=True, exist_ok=True)
        day = today or date(2026, 5, 25)
        report_path = nightly_dir / f"report_{day.isoformat()}.md"
        if success:
            report_path.write_text(
                "# fake morning report\n",
                encoding="utf-8",
            )
        summary = BriefSummary(
            channels_picked=(),
            channels_missing=(),
            mutation_actionable=0,
            adversarial_ungraceful=0,
            ai_findings=0,
            mechanical_findings_total=0,
        )
        return BriefResult(
            success=success,
            report_path=report_path if success else None,
            summary=summary,
            halt_reason=halt_reason,
        )

    return _fake, calls


def _make_recording_mirror(
    *, dest_root: Path | None = None
) -> tuple[Callable[[Path], Path | None], list[Path]]:
    """Return (fake_mirror, calls_list). Copies into ``dest_root`` when
    given, otherwise just records the call and returns a synthetic Windows
    path under ``/tmp/win-mirror/``."""

    calls: list[Path] = []

    def _fake(report_md_path: Path) -> Path | None:
        calls.append(report_md_path)
        if dest_root is None:
            return Path("/tmp/win-mirror") / report_md_path.name
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / report_md_path.name
        dest.write_bytes(report_md_path.read_bytes())
        return dest

    return _fake, calls


# --------------------------------------------------------------------------- #
# Tests: enabled channels are the ones the profile says
# --------------------------------------------------------------------------- #


def test_runs_only_profile_enabled_channels(tmp_path: Path) -> None:
    """``run_nightly`` invokes channel_runner exactly once per channel in
    ``profile.discovery.channels``, in order, and skips disabled ones."""

    profile = Profile(
        repo=str(tmp_path),
        discovery=DiscoveryConfig(
            channels=["mutation", "ai"],
            mutation_paths=["ccd"],
        ),
        schedule=ScheduleConfig(),
    )

    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert isinstance(result, NightlyResult)
    assert result.success is True
    invoked_channels = [c for c, _r, _p in channel_runner.calls]
    assert invoked_channels == ["mutation", "ai"]
    # adversarial is NOT in the profile → never invoked.
    assert "adversarial" not in invoked_channels


def test_mutation_channel_receives_profile_paths(tmp_path: Path) -> None:
    """``profile.discovery.mutation_paths`` is forwarded to the mutation
    channel only — the other channels receive ``paths=None`` (parity with
    :func:`ccd.discover.run_channel`)."""

    profile = Profile(
        repo=str(tmp_path),
        discovery=DiscoveryConfig(
            channels=["mutation", "adversarial", "ai"],
            mutation_paths=["ccd", "tests"],
        ),
        schedule=ScheduleConfig(),
    )

    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    by_channel = {c: (r, p) for c, r, p in channel_runner.calls}
    assert by_channel["mutation"][1] == ("ccd", "tests")
    assert by_channel["adversarial"][1] is None
    assert by_channel["ai"][1] is None


def test_all_three_channels_default_when_profile_absent(tmp_path: Path) -> None:
    """No profile file + no injected profile → the all-defaults profile is
    used (three channels enabled)."""

    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    invoked = [c for c, _r, _p in channel_runner.calls]
    assert invoked == ["mutation", "adversarial", "ai"]
    assert result.profile.discovery.channels == [
        "mutation",
        "adversarial",
        "ai",
    ]


# --------------------------------------------------------------------------- #
# Tests: pre-flight halt
# --------------------------------------------------------------------------- #


def test_pre_flight_halts_when_repo_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    channel_runner = _RecordingChannelRunner()
    brief_runner, brief_calls = _make_fake_brief_runner()
    mirror, mirror_calls = _make_recording_mirror()

    result = run_nightly(
        repo=missing,
        profile=Profile(),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert result.success is False
    assert "pre-flight failed" in result.halt_reason
    # No channel runner / brief / mirror should have fired.
    assert channel_runner.calls == []
    assert brief_calls == []
    assert mirror_calls == []


def test_pre_flight_halt_does_not_run_any_channel(tmp_path: Path) -> None:
    """Even when a profile lists channels, a pre-flight halt skips them all."""

    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("oops", encoding="utf-8")
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=not_a_dir,
        profile=Profile(
            discovery=DiscoveryConfig(channels=["mutation"]),
        ),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert result.success is False
    assert "pre-flight failed" in result.halt_reason
    assert channel_runner.calls == []
    assert result.channels_run == ()


# --------------------------------------------------------------------------- #
# Tests: brief render + Windows mirror
# --------------------------------------------------------------------------- #


def test_morning_report_rendered_and_mirrored(tmp_path: Path) -> None:
    """End-to-end: a fake brief writes a markdown report, the mirror
    callback copies it to a destination, and ``NightlyResult`` carries
    both paths."""

    profile = Profile(
        repo=str(tmp_path),
        discovery=DiscoveryConfig(channels=["mutation"]),
    )
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    win_dest = tmp_path / "win-mirror"
    mirror, mirror_calls = _make_recording_mirror(dest_root=win_dest)

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
        today=date(2026, 5, 25),
    )

    # WSL-side morning report written by the brief.
    assert result.brief_report_wsl is not None
    assert result.brief_report_wsl.name == "report_2026-05-25.md"
    assert result.brief_report_wsl.exists()

    # Windows-side mirror landed where the mirror callback was told to copy.
    assert mirror_calls == [result.brief_report_wsl]
    assert result.brief_report_windows is not None
    assert result.brief_report_windows.parent == win_dest
    assert result.brief_report_windows.read_text(encoding="utf-8") == (
        "# fake morning report\n"
    )


def test_mirror_returning_none_keeps_success_true(tmp_path: Path) -> None:
    """A declining mirror (no /mnt/c on CI hosts) is a soft fail — the
    operator still gets the WSL copy and the overall run reports success."""

    profile = Profile(repo=str(tmp_path))
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()

    def _declining_mirror(_report: Path) -> Path | None:
        return None

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=_declining_mirror,
    )

    assert result.success is True
    assert result.brief_report_wsl is not None
    assert result.brief_report_windows is None


def test_mirror_oserror_is_swallowed(tmp_path: Path) -> None:
    """A mirror that raises OSError (read-only mount, ENOSPC) must not
    crash the orchestrator — Phase 1 wants the brief no matter what."""

    profile = Profile(repo=str(tmp_path))
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()

    def _broken_mirror(_report: Path) -> Path | None:
        raise OSError("disk full")

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=_broken_mirror,
    )

    assert result.success is True
    assert result.brief_report_windows is None


# --------------------------------------------------------------------------- #
# Tests: per-channel halt does not stop the loop
# --------------------------------------------------------------------------- #


def test_channel_halt_records_halt_reason_but_continues(tmp_path: Path) -> None:
    """If one channel halts (e.g. mutation canary halt from spec_019), the
    other channels still run and the brief is still rendered. The operator
    needs to see what the other channels found."""

    profile = Profile(
        repo=str(tmp_path),
        discovery=DiscoveryConfig(
            channels=["mutation", "adversarial", "ai"],
        ),
    )

    channel_runner = _RecordingChannelRunner(
        per_channel={
            "mutation": _FakeChannelResult(
                success=False,
                halt_reason="mutation setup is broken: ...",
            ),
        }
    )
    brief_runner, brief_calls = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    # Overall run is still a success — the brief ran.
    assert result.success is True
    # All three channels were invoked (the mutation halt didn't break the loop).
    assert [c for c, _r, _p in channel_runner.calls] == [
        "mutation",
        "adversarial",
        "ai",
    ]
    # The mutation outcome carries the halt reason.
    by_channel = {co.channel: co for co in result.channels_run}
    assert by_channel["mutation"].success is False
    assert "mutation setup is broken" in by_channel["mutation"].halt_reason
    assert by_channel["adversarial"].success is True
    assert by_channel["ai"].success is True
    # The brief still rendered.
    assert len(brief_calls) == 1
    assert result.brief_report_wsl is not None


def test_channel_exception_caught_and_continues(tmp_path: Path) -> None:
    """A channel raising mid-run is converted to a halt outcome — the
    orchestrator never lets a single broken channel crash the night."""

    def _exploding(channel: str, **_ignored: Any) -> _FakeChannelResult:
        raise RuntimeError("boom from " + channel)

    profile = Profile(
        repo=str(tmp_path),
        discovery=DiscoveryConfig(channels=["mutation", "ai"]),
    )
    channel_runner = _RecordingChannelRunner(
        per_channel={"mutation": _exploding}
    )
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    by_channel = {co.channel: co for co in result.channels_run}
    assert by_channel["mutation"].success is False
    assert "RuntimeError" in by_channel["mutation"].halt_reason
    assert by_channel["ai"].success is True
    assert result.success is True


# --------------------------------------------------------------------------- #
# Tests: brief halt → nightly halt
# --------------------------------------------------------------------------- #


def test_brief_halt_flips_overall_success(tmp_path: Path) -> None:
    """If the brief itself halts (no inputs at all, render error), the
    nightly run reports success=False with the brief's halt_reason."""

    profile = Profile(repo=str(tmp_path))
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner(
        success=False, halt_reason="brief render failed"
    )
    mirror, mirror_calls = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert result.success is False
    assert result.halt_reason == "brief render failed"
    # Mirror was not asked to copy a missing report.
    assert mirror_calls == []


# --------------------------------------------------------------------------- #
# Tests: determinism
# --------------------------------------------------------------------------- #


def test_nightly_result_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    """Same profile + same fake runners → same channels_executed tuple."""

    profile = Profile(
        repo=str(tmp_path),
        discovery=DiscoveryConfig(channels=["mutation", "adversarial", "ai"]),
    )

    def _build() -> NightlyResult:
        channel_runner = _RecordingChannelRunner()
        brief_runner, _ = _make_fake_brief_runner()
        mirror, _ = _make_recording_mirror()
        return run_nightly(
            repo=tmp_path,
            profile=profile,
            channel_runner=channel_runner,
            brief_runner=brief_runner,
            windows_mirror=mirror,
        )

    r1 = _build()
    r2 = _build()
    assert r1.channels_executed == r2.channels_executed
    assert r1.channels_executed == ("mutation", "adversarial", "ai")


def test_profile_path_loaded_from_disk(tmp_path: Path) -> None:
    """Passing ``profile_path`` triggers ``load_profile`` — the loaded
    profile's channels drive the run."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        '[discovery]\nchannels = ["adversarial"]\n',
        encoding="utf-8",
    )
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile_path=profile_path,
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert result.profile.discovery.channels == ["adversarial"]
    assert [c for c, _r, _p in channel_runner.calls] == ["adversarial"]


# --------------------------------------------------------------------------- #
# CLI end-to-end (with injection)
# --------------------------------------------------------------------------- #


def test_cli_nightly_runs_and_prints_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror_dest = tmp_path / "win-mirror"
    mirror, _ = _make_recording_mirror(dest_root=mirror_dest)

    rc = cli.main(
        ["nightly", "--repo", str(tmp_path)],
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "channels executed: mutation, adversarial, ai" in captured.out
    assert "morning report (wsl):" in captured.out
    assert "morning report (windows):" in captured.out
    # The mirror destination shows up in stdout.
    assert "win-mirror" in captured.out


def test_cli_nightly_halts_with_nonzero_on_pre_flight_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "no-such-repo"
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    rc = cli.main(
        ["nightly", "--repo", str(missing)],
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "nightly halted" in captured.err
    assert "pre-flight failed" in captured.err
    assert channel_runner.calls == []


def test_cli_nightly_honors_profile_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--profile <path>`` is honored by the CLI: the profile loaded from
    disk drives which channels run."""

    profile_path = tmp_path / "custom_profile.toml"
    profile_path.write_text(
        '[discovery]\nchannels = ["ai"]\n',
        encoding="utf-8",
    )
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    rc = cli.main(
        [
            "nightly",
            "--repo",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ],
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert [c for c, _r, _p in channel_runner.calls] == ["ai"]
    assert "channels executed: ai" in captured.out


def test_cli_nightly_renders_brief_inputs_from_disk(tmp_path: Path) -> None:
    """When the *real* brief runner is left as default, ``run_nightly``
    should still produce a brief from the discover JSON the channels
    (in this test, fakes that write a file) drop on disk.

    This wires up the real :func:`ccd.brief.run_brief` against a fake
    channel runner that writes a synthetic discover_NNN.json — proving
    the orchestrator's brief step is genuinely reading what the channels
    produced rather than relying on the brief stub."""

    discover_dir = tmp_path / "_ai_workspace" / "discover"

    def _fake_channel(
        channel: str,
        *,
        repo: Path,
        paths: list[str] | None = None,
        **_ignored: Any,
    ) -> _FakeChannelResult:
        # Write a minimal mutation-channel discover_NNN.json the brief
        # will pick up. Only the mutation channel produces one here.
        if channel == "mutation":
            discover_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "summary": {
                    "tool": "fake",
                    "target_paths": ["ccd"],
                    "mutants_total": 2,
                    "status_breakdown": {"killed": 2},
                    "survived_total": 0,
                    "survived_by_file": {},
                    "blocklisted_total": 0,
                    "actionable_total": 0,
                },
                "actionable": [],
                "blocklisted": [],
            }
            json_path = discover_dir / "discover_001.json"
            json_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            return _FakeChannelResult(
                success=True,
                report_md_path=json_path.with_suffix(".md"),
                report_json_path=json_path,
            )
        return _FakeChannelResult(success=True)

    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=Profile(
            discovery=DiscoveryConfig(channels=["mutation"]),
        ),
        channel_runner=_fake_channel,
        # brief_runner left as default — real run_brief reads disk.
        windows_mirror=mirror,
        today=date(2026, 5, 25),
    )

    assert result.success is True
    assert result.brief_report_wsl is not None
    md = result.brief_report_wsl.read_text(encoding="utf-8")
    # The real brief renderer wrote its 6-section header.
    assert "## A. 一行判定" in md
    assert "## F. 起きなかったこと" in md


# --------------------------------------------------------------------------- #
# ChannelOutcome shape sanity (the dataclass other code reads off of)
# --------------------------------------------------------------------------- #


def test_channel_outcome_fields_are_preserved(tmp_path: Path) -> None:
    md = tmp_path / "_ai_workspace" / "discover" / "discover_001.md"
    js = md.with_suffix(".json")
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("x", encoding="utf-8")
    js.write_text("{}", encoding="utf-8")

    channel_runner = _RecordingChannelRunner(
        per_channel={
            "mutation": _FakeChannelResult(
                success=True,
                report_md_path=md,
                report_json_path=js,
            ),
        }
    )
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=Profile(discovery=DiscoveryConfig(channels=["mutation"])),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    co = result.channels_run[0]
    assert isinstance(co, ChannelOutcome)
    assert co.channel == "mutation"
    assert co.success is True
    assert co.report_md_path == md
    assert co.report_json_path == js
    assert co.halt_reason == ""
