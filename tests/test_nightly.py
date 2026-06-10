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
from ccd.guard import GuardResult
from ccd.nightly import (
    AutoFixOutcome,
    ChannelOutcome,
    FixDispatchOutcome,
    NightlyResult,
    SuiteOutcome,
    run_nightly,
)
from ccd.profile import (
    DiscoveryConfig,
    Profile,
    SafetyConfig,
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


# --------------------------------------------------------------------------- #
# spec_023 — autonomous-fix loop fakes
# --------------------------------------------------------------------------- #


@dataclass
class _FakeGitOps:
    """Records every git operation the loop performs; never touches git.

    spec_026 §2-2 added ``discard_local_changes`` / ``delete_branch`` so
    HALT-path tests can assert the working-tree restore actually fires.
    The two new lists (``discards``, ``deletes``) record those calls.
    """

    branches_created: list[str] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    checkouts: list[str] = field(default_factory=list)
    diffs_requested: list[tuple[str, str]] = field(default_factory=list)
    discards: list[Path] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    canned_diff: str = ""
    create_should_raise: Exception | None = None
    merge_should_raise: Exception | None = None

    def create_and_checkout_branch(self, *, repo: Path, branch: str) -> None:
        if self.create_should_raise is not None:
            raise self.create_should_raise
        self.branches_created.append(branch)
        self.checkouts.append(branch)

    def diff(self, *, repo: Path, base: str, head: str) -> str:
        self.diffs_requested.append((base, head))
        return self.canned_diff

    def merge_branch_into_main(self, *, repo: Path, branch: str) -> None:
        if self.merge_should_raise is not None:
            raise self.merge_should_raise
        self.merges.append(branch)

    def checkout(self, *, repo: Path, ref: str) -> None:
        self.checkouts.append(ref)

    def discard_local_changes(self, *, repo: Path) -> None:
        self.discards.append(Path(repo))

    def delete_branch(self, *, repo: Path, branch: str) -> None:
        self.deletes.append(branch)


@dataclass
class _FakeFixDispatcher:
    """Records spec_auto dispatches; returns a canned FixDispatchOutcome."""

    outcome: FixDispatchOutcome = field(
        default_factory=lambda: FixDispatchOutcome(status="done", commits_made=1)
    )
    calls: list[tuple[Path, Path, str]] = field(default_factory=list)
    side_effect: Callable[[Path, Path], None] | None = None

    def __call__(
        self,
        *,
        spec_path: Path,
        repo: Path,
        branch: str,
    ) -> FixDispatchOutcome:
        self.calls.append((Path(spec_path), Path(repo), branch))
        if self.side_effect is not None:
            self.side_effect(Path(spec_path), Path(repo))
        return self.outcome


@dataclass
class _FakeSuiteRunner:
    outcome: SuiteOutcome = field(
        default_factory=lambda: SuiteOutcome(passed=True, output="ok")
    )
    calls: list[Path] = field(default_factory=list)

    def __call__(self, *, repo: Path) -> SuiteOutcome:
        self.calls.append(Path(repo))
        return self.outcome


@dataclass
class _FakeMutationRechecker:
    status: str = "killed"
    calls: list[tuple[str, int, str]] = field(default_factory=list)

    def __call__(
        self,
        *,
        repo: Path,
        file: str,
        line: int,
        mutation: str,
        signature: str,
    ) -> str:
        self.calls.append((file, line, signature))
        return self.status


@dataclass
class _FakeGuardInspector:
    result: GuardResult = field(
        default_factory=lambda: GuardResult(
            passed=True, halt_reasons=(), files_touched=(), template="A"
        )
    )
    calls: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)

    def __call__(
        self,
        *,
        diff: str,
        allowed_files: list[str],
        template: str,
    ) -> GuardResult:
        self.calls.append((diff, tuple(allowed_files), template))
        return self.result


def _write_mutation_discover_json(
    *,
    repo: Path,
    actionable: list[dict[str, Any]] | None = None,
) -> Path:
    """Drop a synthetic ``discover_NNN.json`` so the loop has a candidate
    to chew on. Default actionable: one survivor in ``ccd/protocol.py``."""

    discover_dir = repo / "_ai_workspace" / "discover"
    discover_dir.mkdir(parents=True, exist_ok=True)
    if actionable is None:
        actionable = [
            {
                "file": "ccd/protocol.py",
                "line": 46,
                "mutation": "x == y → x != y",
                "status": "survived",
                "signature": "ccd/protocol.py:46:x == y → x != y",
            }
        ]
    payload = {
        "summary": {
            "tool": "mutmut",
            "target_paths": ["ccd"],
            "mutants_total": len(actionable) + 1,
            "status_breakdown": {"survived": len(actionable), "killed": 1},
            "survived_total": len(actionable),
            "survived_by_file": {},
            "blocklisted_total": 0,
            "actionable_total": len(actionable),
        },
        "actionable": actionable,
        "blocklisted": [],
    }
    json_path = discover_dir / "discover_001.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path = discover_dir / "discover_001.md"
    md_path.write_text("# fake discover report\n", encoding="utf-8")
    return json_path


def _autofix_profile(
    *,
    autonomous: bool,
    channels: list[str] | None = None,
) -> Profile:
    return Profile(
        discovery=DiscoveryConfig(
            channels=channels if channels is not None else ["mutation"]
        ),
        schedule=ScheduleConfig(),
        safety=SafetyConfig(fix_mode="auto" if autonomous else "off"),
    )


def _propose_profile(
    *,
    channels: list[str] | None = None,
    fix_templates: list[str] | None = None,
) -> Profile:
    """spec_028 — propose-mode profile helper."""

    return Profile(
        discovery=DiscoveryConfig(
            channels=channels if channels is not None else ["mutation"]
        ),
        schedule=ScheduleConfig(),
        safety=SafetyConfig(
            fix_mode="propose",
            fix_templates=fix_templates if fix_templates is not None else ["A"],
        ),
    )


# --------------------------------------------------------------------------- #
# spec_023 — gate OFF: behavior unchanged from spec_020
# --------------------------------------------------------------------------- #


def test_autonomous_fix_off_means_no_loop_runs(tmp_path: Path) -> None:
    """Gate OFF → ``auto_fix`` is ``None`` and no loop seam is touched."""

    _write_mutation_discover_json(repo=tmp_path)
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=False),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    # spec_020 behavior preserved bit-for-bit when the gate is off.
    assert result.auto_fix is None
    assert dispatcher.calls == []
    assert suite.calls == []
    assert recheck.calls == []
    assert guard.calls == []
    assert gops.branches_created == []
    assert gops.merges == []


def test_autonomous_fix_off_default_profile_no_loop(tmp_path: Path) -> None:
    """A freshly built ``Profile()`` (default) keeps the gate off — Phase 1
    behavior survives intact."""

    _write_mutation_discover_json(repo=tmp_path)
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=Profile(discovery=DiscoveryConfig(channels=["mutation"])),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert result.auto_fix is None
    assert result.success is True


# --------------------------------------------------------------------------- #
# spec_023 — gate ON happy path: full loop merges
# --------------------------------------------------------------------------- #


def test_autonomous_fix_happy_path_merges_locally(tmp_path: Path) -> None:
    """Gate ON + candidate + dispatch ok + R5 killed + R4 green + guard
    pass → loop merges into ``main`` (local, no push)."""

    _write_mutation_discover_json(repo=tmp_path)
    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True, halt_reasons=(), files_touched=("tests/x.py",), template="A"
        )
    )
    gops = _FakeGitOps(canned_diff="diff --git a/tests/x.py b/tests/x.py\n")

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        today=date(2026, 5, 25),
    )

    af = result.auto_fix
    assert isinstance(af, AutoFixOutcome)
    assert af.skipped is False
    assert af.spec_auto_id.startswith("spec_auto_")
    assert af.spec_auto_path is not None
    assert af.spec_auto_path.exists()
    assert af.finding_signature == "ccd/protocol.py:46:x == y → x != y"
    assert af.template == "A"
    assert af.branch == f"auto/{af.spec_auto_id}"
    assert af.dispatched is True
    assert af.dispatch_status == "done"
    assert af.r5_killed is True
    assert af.r4_suite_passed is True
    assert af.guard_passed is True
    assert af.merged is True
    assert af.halt_reason == ""

    # All four loop side-effects fired exactly once.
    assert dispatcher.calls and dispatcher.calls[0][0] == af.spec_auto_path
    assert suite.calls == [tmp_path.resolve()]
    assert recheck.calls == [
        ("ccd/protocol.py", 46, af.finding_signature),
    ]
    assert guard.calls == [
        (gops.canned_diff, ("tests/",), "A"),
    ]
    # Branch was created and (after merging) the loop did NOT call
    # checkout("main") again — only merge_branch_into_main, which the
    # subprocess impl handles internally. Fakes don't auto-checkout.
    assert gops.branches_created == [af.branch]
    assert gops.merges == [af.branch]


def test_autonomous_fix_does_not_push(tmp_path: Path) -> None:
    """Safety boundary level 2 — the loop never invokes a push primitive.

    The fake git_ops has no ``push`` method; any attempt by the loop
    code to call it would AttributeError. We assert structurally by
    enumerating every method the loop touched and pinning it to the
    four we documented."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    # Loop only ever used: create_and_checkout_branch, diff,
    # merge_branch_into_main. (Not push, not force-anything.)
    assert not hasattr(gops, "push")
    # Sanity: the fake recorded the operations as expected.
    assert gops.branches_created and gops.merges


# --------------------------------------------------------------------------- #
# spec_023 — gate ON but no candidate / halt branches
# --------------------------------------------------------------------------- #


def test_autonomous_fix_skipped_when_no_candidate(tmp_path: Path) -> None:
    """Gate ON, no actionable findings → ``skipped=True`` and no dispatch."""

    # No discover JSON dropped → no candidate.
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is True
    assert "no template-A candidate" in af.skip_reason
    assert dispatcher.calls == []
    assert gops.branches_created == []


def test_autonomous_fix_halts_when_guard_halts(tmp_path: Path) -> None:
    """Guard HALT → loop does NOT merge; ``halt_reason`` is recorded."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=False,
            halt_reasons=("R2: tests/x.py removed lines",),
            files_touched=("tests/x.py", "ccd/sneaky.py"),
            template="A",
        )
    )
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is False
    assert af.dispatched is True
    assert af.guard_passed is False
    assert "R2: tests/x.py removed lines" in af.guard_halt_reasons[0]
    assert af.merged is False
    assert "guard halted the fix" in af.halt_reason
    assert "R2:" in af.halt_reason
    # No merge happened — explicitly.
    assert gops.merges == []


def test_autonomous_fix_halts_when_r5_fails(tmp_path: Path) -> None:
    """Target mutation still surviving → loop HALTs, no merge."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="survived")  # R5 fails
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.r5_killed is False
    assert af.merged is False
    assert "R5 failed" in af.halt_reason
    assert gops.merges == []


def test_autonomous_fix_halts_when_r4_fails(tmp_path: Path) -> None:
    """Suite red → loop HALTs even when R5 + guard would pass."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=False))  # R4 fails
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.r4_suite_passed is False
    assert af.merged is False
    assert "R4 failed" in af.halt_reason
    assert gops.merges == []


def test_autonomous_fix_halts_when_dispatch_fails(tmp_path: Path) -> None:
    """Dispatch returns ``failed`` → loop skips R4/R5/guard and HALTs."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(
            status="failed",
            halt_reason="agent_misread",
        )
    )
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.dispatched is True
    assert af.dispatch_status == "failed"
    assert af.merged is False
    assert "dispatch failed" in af.halt_reason
    # R4/R5/guard not invoked when dispatch failed.
    assert suite.calls == []
    assert recheck.calls == []
    assert guard.calls == []
    assert gops.merges == []


# --------------------------------------------------------------------------- #
# spec_023 — 1 candidate per night
# --------------------------------------------------------------------------- #


def test_autonomous_fix_processes_exactly_one_candidate(tmp_path: Path) -> None:
    """Discover JSON has multiple actionable findings → exactly one is
    picked (论点3 "1晩1候補"). The other survivors stay in the JSON for
    the morning brief to surface."""

    _write_mutation_discover_json(
        repo=tmp_path,
        actionable=[
            {
                "file": "ccd/foo.py",
                "line": 10,
                "mutation": "a → b",
                "status": "survived",
                "signature": "ccd/foo.py:10:a → b",
            },
            {
                "file": "ccd/bar.py",
                "line": 20,
                "mutation": "c → d",
                "status": "survived",
                "signature": "ccd/bar.py:20:c → d",
            },
            {
                "file": "ccd/baz.py",
                "line": 30,
                "mutation": "e → f",
                "status": "survived",
                "signature": "ccd/baz.py:30:e → f",
            },
        ],
    )
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    # Dispatcher called exactly once (the first candidate).
    assert len(dispatcher.calls) == 1
    assert af.finding_signature == "ccd/foo.py:10:a → b"
    assert af.candidate_count == 3
    # And only one branch + one merge fired.
    assert len(gops.branches_created) == 1
    assert len(gops.merges) == 1


# --------------------------------------------------------------------------- #
# spec_023 — finding selection sources
# --------------------------------------------------------------------------- #


def test_autonomous_fix_reads_from_channel_outcome_when_available(
    tmp_path: Path,
) -> None:
    """When the mutation channel surfaces its own JSON path, the loop
    prefers it over scanning ``_ai_workspace/discover/`` blindly."""

    # The "live" discover dir holds an older report we should NOT pick.
    _write_mutation_discover_json(
        repo=tmp_path,
        actionable=[
            {
                "file": "ccd/old.py",
                "line": 1,
                "mutation": "old → ancient",
                "status": "survived",
                "signature": "ccd/old.py:1:old → ancient",
            }
        ],
    )
    # The channel outcome points to a fresher JSON in a different dir.
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_json = fresh_dir / "discover_999.json"
    fresh_json.write_text(
        json.dumps(
            {
                "summary": {},
                "actionable": [
                    {
                        "file": "ccd/new.py",
                        "line": 5,
                        "mutation": "fresh → stale",
                        "status": "survived",
                        "signature": "ccd/new.py:5:fresh → stale",
                    }
                ],
                "blocklisted": [],
            }
        ),
        encoding="utf-8",
    )

    channel_runner = _RecordingChannelRunner(
        per_channel={
            "mutation": _FakeChannelResult(
                success=True,
                report_md_path=fresh_json.with_suffix(".md"),
                report_json_path=fresh_json,
            )
        }
    )
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.finding_signature == "ccd/new.py:5:fresh → stale"


def test_autonomous_fix_downgrade_when_translate_rejects(
    tmp_path: Path,
) -> None:
    """When the discover JSON only has channel-incompatible findings,
    the pre-filter rejects them and ``skipped`` carries the reason."""

    # Only a "killed" finding (not survived) → fails the pre-filter.
    _write_mutation_discover_json(
        repo=tmp_path,
        actionable=[
            {
                "file": "ccd/foo.py",
                "line": 1,
                "mutation": "x → y",
                "status": "killed",  # not survived → pre-filter rejects
                "signature": "ccd/foo.py:1:x → y",
            }
        ],
    )
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is True
    assert "no template-A candidate" in af.skip_reason
    assert dispatcher.calls == []


# --------------------------------------------------------------------------- #
# spec_023 — CLI surfaces the auto-fix outcome
# --------------------------------------------------------------------------- #


def test_cli_nightly_prints_auto_fix_merged_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the loop merges, ``ccd nightly`` stdout shows an auto-fix line."""

    _write_mutation_discover_json(repo=tmp_path)
    # Write a profile that flips the gate on.
    profile_path = tmp_path / "ccd_profile.toml"
    profile_path.write_text(
        "[discovery]\nchannels = [\"mutation\"]\n\n"
        "[safety]\nfix_mode = \"auto\"\n",
        encoding="utf-8",
    )

    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    rc = cli.main(
        [
            "nightly",
            "--repo",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ],
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-fix: merged" in out
    assert "branch=auto/spec_auto_" in out
    assert "signature=ccd/protocol.py:46:" in out


def test_cli_nightly_prints_auto_fix_halt_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guard HALT → ``auto-fix: HALT ...`` appears in stdout with the reason."""

    _write_mutation_discover_json(repo=tmp_path)
    profile_path = tmp_path / "ccd_profile.toml"
    profile_path.write_text(
        "[discovery]\nchannels = [\"mutation\"]\n\n"
        "[safety]\nfix_mode = \"auto\"\n",
        encoding="utf-8",
    )

    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=False,
            halt_reasons=("R1: tests/sneaky.py is not allowed",),
            files_touched=(),
            template="A",
        )
    )
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    rc = cli.main(
        [
            "nightly",
            "--repo",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ],
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    assert rc == 0  # nightly itself is success; the loop halted, not the brief
    out = capsys.readouterr().out
    assert "auto-fix: HALT" in out
    assert "guard halted the fix" in out
    assert gops.merges == []


def test_cli_nightly_prints_auto_fix_skipped_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No candidate → ``auto-fix: skipped (...)`` line surfaces."""

    profile_path = tmp_path / "ccd_profile.toml"
    profile_path.write_text(
        "[discovery]\nchannels = [\"mutation\"]\n\n"
        "[safety]\nfix_mode = \"auto\"\n",
        encoding="utf-8",
    )

    brief_runner, _ = _make_fake_brief_runner()

    rc = cli.main(
        [
            "nightly",
            "--repo",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ],
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-fix: skipped" in out
    assert "no template-A candidate" in out


def test_cli_nightly_off_profile_no_auto_fix_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gate off → CLI does not print an auto-fix line at all (preserves
    the spec_020 stdout shape bit-for-bit when the gate is off)."""

    rc = cli.main(
        ["nightly", "--repo", str(tmp_path)],
        channel_runner=_RecordingChannelRunner(),
        brief_runner=_make_fake_brief_runner()[0],
        windows_mirror=lambda _p: None,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-fix" not in out


# --------------------------------------------------------------------------- #
# spec_024 — template B fakes / helpers
# --------------------------------------------------------------------------- #


@dataclass
class _FakeAdversarialRechecker:
    status: str = "graceful_error"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(
        self,
        *,
        repo: Path,
        parser: str,
        case_name: str,
    ) -> str:
        self.calls.append((parser, case_name))
        return self.status


def _write_adversarial_discover_json(
    *,
    repo: Path,
    seq: int = 1,
    findings: list[dict[str, Any]] | None = None,
) -> Path:
    """Drop a synthetic adversarial ``discover_NNN.json`` so the loop has
    a template-B candidate to chew on. Default: one UnicodeDecodeError
    leak from parse_spec (mimicking the spec_024 §1 实弾 example)."""

    discover_dir = repo / "_ai_workspace" / "discover"
    discover_dir.mkdir(parents=True, exist_ok=True)
    if findings is None:
        findings = [
            {
                "parser": "ccd.protocol.parse_spec",
                "case": "05_invalid_utf8_bytes",
                "exception_type": "UnicodeDecodeError",
                "exception_message": "'utf-8' codec can't decode byte 0xff",
            }
        ]
    payload = {
        "channel": "adversarial",
        "summary": {
            "parsers": ["ccd.protocol.parse_spec"],
            "cases_total": 18,
            "evaluations_total": 72,
            "graceful_total": 64,
            "ungraceful_total": len(findings),
            "graceful_by_parser": {},
            "ungraceful_by_parser": {},
            "ungraceful_by_exception_type": {},
        },
        "findings": findings,
        "cases": [],
    }
    json_path = discover_dir / f"discover_{seq:03d}.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path = discover_dir / f"discover_{seq:03d}.md"
    md_path.write_text(
        f"# discover_{seq:03d} — fake adversarial report\n",
        encoding="utf-8",
    )
    return json_path


def _autofix_profile_b(*, autonomous: bool = True) -> Profile:
    """Profile with template B enabled (``fix_templates=["A", "B"]``)."""

    return Profile(
        discovery=DiscoveryConfig(
            channels=["mutation", "adversarial"],
        ),
        schedule=ScheduleConfig(),
        safety=SafetyConfig(
            fix_mode="auto" if autonomous else "off",
            fix_templates=["A", "B"],
        ),
    )


# --------------------------------------------------------------------------- #
# spec_024 — template B is gated by safety.fix_templates
# --------------------------------------------------------------------------- #


def test_template_b_finding_ignored_when_only_a_enabled(tmp_path: Path) -> None:
    """``fix_templates=["A"]`` (default) — even with adversarial findings
    on disk, the loop never picks them up. The morning brief still
    surfaces them via the report-only path."""

    _write_adversarial_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    adv_recheck = _FakeAdversarialRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),  # fix_templates=["A"]
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is True
    assert "no template-A candidate" in af.skip_reason
    # Adversarial rechecker NEVER called — template B path not exercised.
    assert adv_recheck.calls == []
    assert dispatcher.calls == []


def test_template_b_happy_path_merges_locally(tmp_path: Path) -> None:
    """Gate ON + ``fix_templates=["A", "B"]`` + only adversarial finding
    + dispatch ok + R5=graceful_error + R4 green + guard pass → loop
    merges the production fix into local main."""

    _write_adversarial_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker()  # A path — not called
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True,
            halt_reasons=(),
            files_touched=("ccd/protocol.py", "tests/test_protocol.py"),
            template="B",
        )
    )
    gops = _FakeGitOps(
        canned_diff=(
            "diff --git a/ccd/protocol.py b/ccd/protocol.py\n"
            "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        )
    )
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
        today=date(2026, 5, 25),
    )

    af = result.auto_fix
    assert isinstance(af, AutoFixOutcome)
    assert af.skipped is False
    assert af.template == "B"
    assert af.spec_auto_id.startswith("spec_auto_")
    assert af.spec_auto_path is not None
    assert af.spec_auto_path.exists()
    assert af.finding_signature == (
        "ccd.protocol.parse_spec:05_invalid_utf8_bytes:UnicodeDecodeError"
    )
    assert af.branch == f"auto/{af.spec_auto_id}"
    assert af.dispatched is True
    assert af.dispatch_status == "done"
    assert af.r5_killed is True  # treated as "R5 passed" — name is historical
    assert af.r4_suite_passed is True
    assert af.guard_passed is True
    assert af.merged is True
    assert af.halt_reason == ""

    # The adversarial rechecker received the parser + case from the finding.
    assert adv_recheck.calls == [
        ("ccd.protocol.parse_spec", "05_invalid_utf8_bytes"),
    ]
    # The mutation rechecker was NOT called — template B uses adv path only.
    assert recheck.calls == []
    # Guard received template B + the named production file + tests/.
    assert guard.calls == [
        (gops.canned_diff, ("ccd/protocol.py", "tests/"), "B"),
    ]
    assert gops.branches_created == [af.branch]
    assert gops.merges == [af.branch]


def test_template_b_halts_when_parser_silently_accepts(tmp_path: Path) -> None:
    """spec_024 §3: 'fix must error gracefully, not succeed'. When the
    rechecker reports ``graceful_success`` (the parser silently accepted
    the broken input), R5 must fail with a distinct halt reason that
    pinpoints the silent-acceptance failure mode."""

    _write_adversarial_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    adv_recheck = _FakeAdversarialRechecker(status="graceful_success")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True, halt_reasons=(), files_touched=(), template="B"
        )
    )
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.template == "B"
    assert af.r5_killed is False
    assert af.merged is False
    assert "silently accepted" in af.halt_reason
    assert "spec_024 §3" in af.halt_reason
    assert gops.merges == []


def test_template_b_halts_when_parser_still_ungraceful(tmp_path: Path) -> None:
    """Rechecker reports ``ungraceful`` (the fix didn't take, the crash
    still leaks) → R5 fails with the "did not become a graceful error"
    reason."""

    _write_adversarial_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    adv_recheck = _FakeAdversarialRechecker(status="ungraceful")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.r5_killed is False
    assert af.merged is False
    assert "did not become a graceful error" in af.halt_reason
    assert "ungraceful" in af.halt_reason
    assert gops.merges == []


def test_template_b_guard_halt_via_r3_diff_size(tmp_path: Path) -> None:
    """spec_024 §2-2: 'R3 (本番 diff サイズ上限) が有効'. The loop must
    pass ``template="B"`` to ``guard_inspector`` so R3 is enforced. We
    pin this by having the fake guard return an R3 halt and verifying the
    loop honored it (didn't merge) AND that the guard call used
    template="B" with the production file in allowed_files."""

    _write_adversarial_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=False,
            halt_reasons=(
                "R3: production diff is 123 +/- lines across 1 file(s) "
                "(limit 60); narrow-scope fixes should not produce large "
                "diffs — likely scope creep",
            ),
            files_touched=("ccd/protocol.py", "tests/test_protocol.py"),
            template="B",
        )
    )
    gops = _FakeGitOps(canned_diff="diff --git a/ccd/protocol.py b/ccd/protocol.py\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.template == "B"
    assert af.guard_passed is False
    assert "R3" in af.guard_halt_reasons[0]
    assert af.merged is False
    assert "guard halted the fix" in af.halt_reason
    # Crucial: the guard call passed template="B" (so R3 is enforced)
    # and the production file + tests/ as allowed_files.
    assert guard.calls == [
        (gops.canned_diff, ("ccd/protocol.py", "tests/"), "B"),
    ]
    assert gops.merges == []


def test_template_b_guard_blocks_production_file_outside_allowed(
    tmp_path: Path,
) -> None:
    """When the guard halts on R1 (a file outside the per-finding allowed
    set), the loop must surface the halt — proving the allowed_files
    contains only the *named* file + tests/, not "any ccd/*.py"."""

    _write_adversarial_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=False,
            halt_reasons=(
                "R1: ccd/sneaky.py is not in the allowed file set "
                "(allowed=['ccd/protocol.py', 'tests/'])",
            ),
            files_touched=(),
            template="B",
        )
    )
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.guard_passed is False
    assert "R1" in af.guard_halt_reasons[0]
    assert af.merged is False


def test_template_a_priority_over_b_when_both_present(tmp_path: Path) -> None:
    """spec_024 priority: 'A before B' (test-only is structurally safer).
    Even when both templates are enabled AND both have candidates, the
    loop picks A first. B's candidate stays in the discover JSON for the
    morning brief / a future night."""

    _write_mutation_discover_json(repo=tmp_path)  # discover_001.json (mutation)
    _write_adversarial_discover_json(repo=tmp_path, seq=2)  # discover_002.json
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.template == "A"  # NOT "B"
    assert af.finding_signature == "ccd/protocol.py:46:x == y → x != y"
    # Mutation rechecker called, adversarial NOT.
    assert recheck.calls
    assert adv_recheck.calls == []


def test_template_b_falls_through_when_a_has_no_candidate(
    tmp_path: Path,
) -> None:
    """With ``fix_templates=["A", "B"]``: if A has no candidate (no
    mutation discover JSON, or only killed/blocklisted findings), the
    loop falls through to template B."""

    # Only an adversarial JSON exists — no mutation JSON.
    _write_adversarial_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True, halt_reasons=(), files_touched=(), template="B"
        )
    )
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.template == "B"
    assert af.merged is True
    assert adv_recheck.calls  # adv path exercised
    assert recheck.calls == []  # mutation path not


def test_template_b_no_candidate_when_only_b_enabled_but_no_adversarial_findings(
    tmp_path: Path,
) -> None:
    """``fix_templates=["B"]`` + no adversarial findings on disk → loop
    skips with a B-specific no-candidate reason (the test pins that the
    message names template-B explicitly so the morning brief can
    distinguish 'no A' from 'no B')."""

    dispatcher = _FakeFixDispatcher()
    adv_recheck = _FakeAdversarialRechecker()
    brief_runner, _ = _make_fake_brief_runner()

    profile = Profile(
        discovery=DiscoveryConfig(channels=["adversarial"]),
        schedule=ScheduleConfig(),
        safety=SafetyConfig(fix_mode="auto", fix_templates=["B"]),
    )

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        adversarial_rechecker=adv_recheck,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is True
    assert "template-B" in af.skip_reason
    assert dispatcher.calls == []


def test_template_b_default_adversarial_rechecker_distinguishes_graceful(
    tmp_path: Path,  # noqa: ARG001 — kept for fixture symmetry
) -> None:
    """The production default rechecker classifies the four real outcomes:

    - ``ccd.protocol.parse_spec`` × ``05_invalid_utf8_bytes`` →
      ``"ungraceful"`` (the current bug we're trying to surface — leaks
      UnicodeDecodeError).
    - ``ccd.protocol.parse_spec`` × ``08_spec_missing_title_heading`` →
      ``"graceful_error"`` (ValueError, clean rejection).
    - unknown parser → ``"unknown"`` (loop halts conservatively).
    - unknown case → ``"unknown"``.

    This pins the rechecker contract without re-running mutmut/claude/
    pytest — it's a pure in-process function call against fixtures from
    ``ccd.adversarial``."""

    from ccd.nightly import _default_adversarial_rechecker

    assert (
        _default_adversarial_rechecker(
            repo=Path("/tmp"),  # noqa: S108 — irrelevant; not used by impl
            parser="ccd.protocol.parse_spec",
            case_name="05_invalid_utf8_bytes",
        )
        == "ungraceful"
    )
    assert (
        _default_adversarial_rechecker(
            repo=Path("/tmp"),
            parser="ccd.protocol.parse_spec",
            case_name="08_spec_missing_title_heading",
        )
        == "graceful_error"
    )
    assert (
        _default_adversarial_rechecker(
            repo=Path("/tmp"),
            parser="ccd.protocol.no_such_parser",
            case_name="05_invalid_utf8_bytes",
        )
        == "unknown"
    )
    assert (
        _default_adversarial_rechecker(
            repo=Path("/tmp"),
            parser="ccd.protocol.parse_spec",
            case_name="999_no_such_case",
        )
        == "unknown"
    )


# --------------------------------------------------------------------------- #
# spec_024 — discover JSON channel routing
# --------------------------------------------------------------------------- #


def test_disk_fallback_distinguishes_mutation_vs_adversarial_json(
    tmp_path: Path,
) -> None:
    """When both a mutation JSON (discover_001.json) and an adversarial
    JSON (discover_002.json) exist on disk and no channel outcome
    surfaces either, the loop must read them via channel — not just
    'latest by number'. Otherwise template A's selector would pick up
    the adversarial JSON's empty 'actionable' list and skip silently
    while a real mutation finding sits unhandled."""

    _write_mutation_discover_json(repo=tmp_path)  # discover_001.json
    _write_adversarial_discover_json(repo=tmp_path, seq=2)  # discover_002.json
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    # Profile only has template A — the loop must find the mutation JSON
    # despite the adversarial JSON having a higher sequence number.
    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.template == "A"
    assert af.finding_signature == "ccd/protocol.py:46:x == y → x != y"
    # Adversarial path stayed dormant.
    assert adv_recheck.calls == []


def test_template_b_one_candidate_per_night(tmp_path: Path) -> None:
    """Multiple adversarial findings → exactly one is picked (论点3,
    same single-candidate guarantee as template A)."""

    _write_adversarial_discover_json(
        repo=tmp_path,
        findings=[
            {
                "parser": "ccd.protocol.parse_spec",
                "case": "05_invalid_utf8_bytes",
                "exception_type": "UnicodeDecodeError",
                "exception_message": "msg-1",
            },
            {
                "parser": "ccd.protocol.parse_result",
                "case": "05_invalid_utf8_bytes",
                "exception_type": "UnicodeDecodeError",
                "exception_message": "msg-2",
            },
            {
                "parser": "ccd.run_writer.load_records",
                "case": "05_invalid_utf8_bytes",
                "exception_type": "UnicodeDecodeError",
                "exception_message": "msg-3",
            },
        ],
    )
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True, halt_reasons=(), files_touched=(), template="B"
        )
    )
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_b(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    # Dispatcher called exactly once.
    assert len(dispatcher.calls) == 1
    assert af.candidate_count == 3
    # The first finding (parse_spec) won.
    assert af.finding_signature == (
        "ccd.protocol.parse_spec:05_invalid_utf8_bytes:UnicodeDecodeError"
    )
    assert len(gops.branches_created) == 1
    assert len(gops.merges) == 1


# --------------------------------------------------------------------------- #
# spec_025 — cost / halt boundaries
# --------------------------------------------------------------------------- #


def test_pause_file_short_circuits_entire_nightly(tmp_path: Path) -> None:
    """spec_025 §2-1(c) — PAUSE file present → nothing runs.

    With the operator-installed kill switch in place, ``run_nightly``
    returns ``paused=True`` and ``success=True`` (it's an intentional
    pause, not an error), and none of the channel runner / fix
    dispatcher / brief runner / mirror is invoked.
    """

    pause_file = tmp_path / "_ai_workspace" / "PAUSE"
    pause_file.parent.mkdir(parents=True)
    pause_file.write_text("manual brake\n", encoding="utf-8")

    _write_mutation_discover_json(repo=tmp_path)
    channel_runner = _RecordingChannelRunner()
    brief_runner, brief_calls = _make_fake_brief_runner()
    mirror, mirror_calls = _make_recording_mirror()
    dispatcher = _FakeFixDispatcher()
    gops = _FakeGitOps()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
        git_ops=gops,
    )

    assert result.paused is True
    assert result.success is True
    assert "PAUSE" in result.halt_reason
    # Nothing ran.
    assert channel_runner.calls == []
    assert brief_calls == []
    assert mirror_calls == []
    assert dispatcher.calls == []
    assert result.auto_fix is None
    assert result.brief_report_wsl is None


def test_pause_file_absent_runs_normally(tmp_path: Path) -> None:
    """Sanity: no PAUSE → normal nightly run (paused=False)."""

    channel_runner = _RecordingChannelRunner()
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=Profile(discovery=DiscoveryConfig(channels=["mutation"])),
        channel_runner=channel_runner,
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert result.paused is False
    assert result.success is True


def test_cli_nightly_prints_paused_line_when_pause_file_present(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI surfaces the pause to operator stdout and exits 0."""

    pause_file = tmp_path / "_ai_workspace" / "PAUSE"
    pause_file.parent.mkdir(parents=True)
    pause_file.write_text("", encoding="utf-8")

    rc = cli.main(
        ["nightly", "--repo", str(tmp_path)],
        channel_runner=_RecordingChannelRunner(),
        brief_runner=_make_fake_brief_runner()[0],
        windows_mirror=lambda _p: None,
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "paused" in out
    assert "PAUSE" in out
    # Normal nightly stdout lines must NOT appear.
    assert "channels executed:" not in out


def test_unpushed_backlog_at_limit_blocks_new_dispatch(tmp_path: Path) -> None:
    """spec_025 §2-1(b) — 3+ un-pushed auto-merges → loop skips with a
    promote-please reason; dispatcher / git seam never touched."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    # Counter says: 3 un-pushed auto-fix commits already on local main.
    def _counter(_repo: Path) -> int:
        return 3

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        unpushed_counter=_counter,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is True
    assert "un-pushed autonomous-fix commits" in af.skip_reason
    assert "3 un-pushed" in af.skip_reason
    assert "limit 3" in af.skip_reason
    assert "git push" in af.skip_reason
    # No dispatch, no git side effects.
    assert dispatcher.calls == []
    assert gops.branches_created == []
    assert gops.merges == []


def test_unpushed_backlog_below_limit_does_not_block(tmp_path: Path) -> None:
    """2 un-pushed < limit of 3 → loop proceeds normally."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    def _counter(_repo: Path) -> int:
        return 2

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        unpushed_counter=_counter,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is False
    assert af.merged is True
    assert dispatcher.calls  # the loop did run


def test_unpushed_backlog_custom_limit_is_honored(tmp_path: Path) -> None:
    """``unpushed_backlog_limit`` overrides the module default."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        unpushed_counter=lambda _repo: 1,
        unpushed_backlog_limit=1,  # 1 un-pushed already trips it
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is True
    assert "un-pushed autonomous-fix commits" in af.skip_reason
    assert dispatcher.calls == []


def test_unpushed_counter_exception_does_not_block(tmp_path: Path) -> None:
    """If the counter raises (git missing / weird state), loop proceeds —
    we don't want a counter failure to silently disable the loop."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    def _boom(_repo: Path) -> int:
        raise RuntimeError("git not found")

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        unpushed_counter=_boom,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is False
    assert af.merged is True


def test_dispatch_timeout_marks_candidate_failed(tmp_path: Path) -> None:
    """spec_025 §2-1(a) — when dispatch exceeds the timeout, the loop
    marks the candidate as failed (not merged) and the halt_reason
    surfaces "timed out" so the morning brief can pinpoint why."""

    import time

    _write_mutation_discover_json(repo=tmp_path)

    def _slow_dispatcher(
        *,
        spec_path: Path,  # noqa: ARG001
        repo: Path,  # noqa: ARG001
        branch: str,  # noqa: ARG001
    ) -> FixDispatchOutcome:
        # Sleep well past the test's tiny timeout.
        time.sleep(2.0)
        return FixDispatchOutcome(status="done", commits_made=1)

    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=_slow_dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        dispatch_timeout_s=0.2,
    )

    af = result.auto_fix
    assert af is not None
    assert af.skipped is False
    assert af.dispatched is True
    assert af.dispatch_status == "failed"
    assert af.merged is False
    assert "timed out" in af.halt_reason
    assert "spec_025 §2-1(a)" in af.halt_reason
    # R4/R5/guard not invoked when dispatch failed.
    assert suite.calls == []
    assert recheck.calls == []
    assert guard.calls == []
    assert gops.merges == []


def test_dispatch_timeout_default_is_40_minutes() -> None:
    """The module's default dispatch timeout is 40 minutes as spec_025
    §2-1(a) prescribes. We test the constant directly so a careless
    edit doesn't silently loosen the safety boundary."""

    from ccd.nightly import _AUTO_FIX_DISPATCH_TIMEOUT_S

    assert _AUTO_FIX_DISPATCH_TIMEOUT_S == 40 * 60


def test_unpushed_backlog_default_limit_is_three() -> None:
    """The module's default un-pushed backlog cap is 3 as spec_025
    §2-1(b) prescribes."""

    from ccd.nightly import _AUTO_FIX_UNPUSHED_BACKLOG_LIMIT

    assert _AUTO_FIX_UNPUSHED_BACKLOG_LIMIT == 3


def test_zero_findings_normal_exit_is_success(tmp_path: Path) -> None:
    """spec_025 §2-1(d) — a night with no actionable findings exits
    success=True, the auto-fix loop reports skipped, and the brief
    renders (no error)."""

    dispatcher = _FakeFixDispatcher()
    brief_runner, brief_calls = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
    )

    assert result.success is True
    af = result.auto_fix
    assert af is not None
    assert af.skipped is True
    assert "no template-A candidate" in af.skip_reason
    assert dispatcher.calls == []
    # Brief still rendered.
    assert len(brief_calls) == 1
    assert result.brief_report_wsl is not None


def test_merge_diff_captured_on_successful_fix(tmp_path: Path) -> None:
    """spec_025 — when the loop merges, the diff text is preserved on
    ``AutoFixOutcome.merge_diff`` so the brief can embed it."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    diff_text = (
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        "+++ added a reproducer test\n"
    )
    gops = _FakeGitOps(canned_diff=diff_text)
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is True
    assert af.merge_diff == diff_text


def test_merge_diff_empty_when_not_merged(tmp_path: Path) -> None:
    """A halted fix's in-progress diff is NOT surfaced via
    ``merge_diff`` — the brief should never embed an un-merged diff."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="survived")  # R5 fails → no merge
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert af.merge_diff == ""


def test_brief_phase2_section_b_when_loop_merged(tmp_path: Path) -> None:
    """End-to-end: when the loop merges, the morning brief's §B is the
    Phase 2 version — embedded diff, R-result evidence, and push
    command. We use the REAL brief runner so the rendering path is
    exercised; the channel runner is a no-op (no discover JSON written
    in the test), so the brief picks up the auto-fix story only."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    diff_text = (
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        "@@ +reproducer test added\n"
    )
    gops = _FakeGitOps(canned_diff=diff_text)
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        # brief_runner left as default — exercises real ccd.brief.run_brief
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        today=date(2026, 5, 25),
    )

    assert result.success is True
    assert result.brief_report_wsl is not None
    md = result.brief_report_wsl.read_text(encoding="utf-8")

    # Phase 2 header + §B title.
    assert "Phase 2" in md
    assert "## B. 昨夜の自律修正" in md
    # Diff embedded.
    assert "```diff" in md
    assert "tests/test_protocol.py" in md
    # Verification evidence.
    assert "R5" in md and "pass" in md
    assert "R4" in md
    assert "ガード" in md
    # Push command appears.
    assert "git" in md and "push origin main" in md
    # Phase 1 §B headline must NOT have replaced §B.
    assert "## B. 機械的チャンネルの発見" not in md


def test_brief_phase1_section_b_when_loop_did_not_merge(
    tmp_path: Path,
) -> None:
    """When the loop ran but did NOT merge (HALT), §B stays Phase 1
    (mechanical-channel discoveries) — Phase 2 §B is gated on
    ``auto_fix.merged is True``."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="survived")  # forces HALT
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        today=date(2026, 5, 25),
    )

    assert result.brief_report_wsl is not None
    md = result.brief_report_wsl.read_text(encoding="utf-8")

    # Phase 1 §B stays.
    assert "## B. 機械的チャンネルの発見" in md
    # Phase 2 §B does NOT appear.
    assert "## B. 昨夜の自律修正" not in md
    # The HALT surfaces in §D (loop ran but didn't merge).
    assert "自律修正 HALT" in md


def test_brief_phase1_section_b_when_no_autofix_at_all(tmp_path: Path) -> None:
    """Gate OFF → ``auto_fix=None`` → §B is Phase 1 (existing behavior)."""

    # No discover JSON, no auto-fix attempted.
    brief_runner, _ = _make_fake_brief_runner()
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=Profile(discovery=DiscoveryConfig(channels=["mutation"])),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=mirror,
    )

    assert result.success is True
    assert result.auto_fix is None


# --------------------------------------------------------------------------- #
# spec_026 §2-2 — HALT-path working-tree restoration
# --------------------------------------------------------------------------- #
#
# spec_026 documented the bug: every HALT exit of ``_run_auto_fix_loop``
# left the auto/spec_auto_NNN branch AND uncommitted edits on the
# working tree, so the next night's pre-flight saw a dirty repo. The
# fix is a ``_restore_repo_after_halt`` helper that runs on every HALT
# path: discard uncommitted edits → checkout main → delete auto branch.
# These tests pin each HALT path independently and assert the three
# operations fired on the injected fake GitOps.


def _assert_halt_restore_fired(
    *, gops: _FakeGitOps, repo: Path, branch: str
) -> None:
    """Shared assertion: spec_026 §2-2 restoration ran on a fake GitOps.

    The HALT path must have invoked all three restore primitives in
    order — discard_local_changes, checkout("main"), delete_branch.
    """

    assert gops.discards, "discard_local_changes was not called on HALT"
    assert Path(repo).resolve() in [p.resolve() for p in gops.discards] or (
        gops.discards[0] == Path(repo)
    ), "discard_local_changes received an unexpected repo path"
    assert "main" in gops.checkouts, "checkout('main') was not called on HALT"
    assert branch in gops.deletes, (
        f"delete_branch was not called for {branch!r} on HALT"
    )


def test_halt_restore_fires_on_dispatch_failure(tmp_path: Path) -> None:
    """spec_026 §2-2 — dispatch returning 'failed' triggers the restore."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(
            status="failed",
            halt_reason="agent_misread",
        )
    )
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert "dispatch failed" in af.halt_reason
    _assert_halt_restore_fired(gops=gops, repo=tmp_path, branch=af.branch)
    # Sanity: the loop must NOT have merged when restoration fires.
    assert gops.merges == []


def test_halt_restore_fires_on_dispatch_exception(tmp_path: Path) -> None:
    """spec_026 §2-2 — a dispatcher that raises also triggers restore."""

    _write_mutation_discover_json(repo=tmp_path)

    def _raising_dispatcher(
        *, spec_path: Path, repo: Path, branch: str
    ) -> FixDispatchOutcome:
        raise RuntimeError("simulated subprocess crash")

    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=_raising_dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert af.dispatched is False
    assert "RuntimeError" in af.halt_reason
    _assert_halt_restore_fired(gops=gops, repo=tmp_path, branch=af.branch)


def test_halt_restore_fires_on_r5_failure(tmp_path: Path) -> None:
    """spec_026 §2-2 — R5 fail (mutation still surviving) triggers restore."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="survived")  # R5 fails
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert "R5 failed" in af.halt_reason
    _assert_halt_restore_fired(gops=gops, repo=tmp_path, branch=af.branch)


def test_halt_restore_fires_on_r4_failure(tmp_path: Path) -> None:
    """spec_026 §2-2 — R4 fail (suite red) triggers restore."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=False))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert "R4 failed" in af.halt_reason
    _assert_halt_restore_fired(gops=gops, repo=tmp_path, branch=af.branch)


def test_halt_restore_fires_on_guard_halt(tmp_path: Path) -> None:
    """spec_026 §2-2 — guard HALT triggers restore."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=False,
            halt_reasons=("R2: tests/x.py removed lines",),
            files_touched=("tests/x.py", "ccd/sneaky.py"),
            template="A",
        )
    )
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert "guard halted the fix" in af.halt_reason
    _assert_halt_restore_fired(gops=gops, repo=tmp_path, branch=af.branch)


def test_halt_restore_fires_on_branch_creation_failure(tmp_path: Path) -> None:
    """spec_026 §2-2 — branch creation failure also calls the restore.

    Even though no work happened, the partial branch state must be
    swept up so the next pre-flight starts clean.
    """

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker()
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(create_should_raise=RuntimeError("checkout -b boom"))
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert "branch creation failed" in af.halt_reason
    # The fake records discard / checkout / delete_branch calls even
    # when create_and_checkout_branch raised — that is the entire point
    # of the restore-on-HALT contract (spec_026 §2-2).
    assert gops.discards, "discard_local_changes was not called"
    assert "main" in gops.checkouts
    assert af.branch in gops.deletes


def test_success_merge_deletes_feature_branch_but_keeps_main(
    tmp_path: Path,
) -> None:
    """spec_026 §2-2 — success path also deletes the auto branch, but the
    merge commit stays on main (the existing behavior we must not break).

    The success path doesn't need to discard local changes (the merge
    already moved us to main with a clean tree); only the branch delete
    fires."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True, halt_reasons=(), files_touched=("tests/x.py",), template="A"
        )
    )
    gops = _FakeGitOps(canned_diff="diff --git a/tests/x.py b/tests/x.py\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is True
    # Merge commit is recorded — existing behavior preserved.
    assert gops.merges == [af.branch]
    # Branch was deleted post-merge.
    assert af.branch in gops.deletes
    # Success path does NOT discard / checkout — the merge already
    # put us on main with a clean tree.
    assert gops.discards == []
    # The only checkout was the one create_and_checkout_branch did
    # during setup (FakeGitOps appends the branch name to checkouts);
    # the loop does NOT add a redundant checkout("main") on success.
    assert "main" not in gops.checkouts


def test_halt_restore_swallows_exceptions_per_step(tmp_path: Path) -> None:
    """spec_026 §2-2 — restoration is best-effort. If any single step
    raises (e.g. ``git checkout main`` fails because main was just
    deleted), the remaining steps still run. The morning brief still
    renders with the original halt_reason."""

    _write_mutation_discover_json(repo=tmp_path)

    @dataclass
    class _PartiallyFailingGitOps(_FakeGitOps):
        # All three restore primitives raise to prove the loop's
        # try/except wrapping catches each independently.
        def discard_local_changes(self, *, repo: Path) -> None:
            self.discards.append(Path(repo))
            raise RuntimeError("git reset failed")

        def checkout(self, *, repo: Path, ref: str) -> None:
            self.checkouts.append(ref)
            raise RuntimeError("git checkout failed")

        def delete_branch(self, *, repo: Path, branch: str) -> None:
            self.deletes.append(branch)
            raise RuntimeError("git branch -D failed")

    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="survived")  # forces HALT
    guard = _FakeGuardInspector()
    gops = _PartiallyFailingGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    # The whole run must not raise — every restore step is wrapped in
    # try/except, and the brief renders the halt_reason from the
    # original failure rather than the cleanup-step exception.
    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.merged is False
    assert "R5 failed" in af.halt_reason
    # All three primitives fired (each appended before raising).
    assert gops.discards
    assert "main" in gops.checkouts
    assert af.branch in gops.deletes


def test_gitops_protocol_has_spec_026_methods() -> None:
    """spec_026 §2-2 — the GitOps Protocol must expose
    ``discard_local_changes`` and ``delete_branch`` so tests + the
    SubprocessGitOps default can implement them. A future deletion of
    either method would silently disable the HALT-restore contract."""

    from ccd.nightly import GitOps, SubprocessGitOps

    # Protocol methods (read off attributes; Protocol allows them at
    # runtime as ellipsis stubs).
    assert hasattr(GitOps, "discard_local_changes")
    assert hasattr(GitOps, "delete_branch")
    # SubprocessGitOps must implement them as real callables.
    impl = SubprocessGitOps()
    assert callable(impl.discard_local_changes)
    assert callable(impl.delete_branch)


# --------------------------------------------------------------------------- #
# spec_028 — propose mode
# --------------------------------------------------------------------------- #


def _fake_isolated_workspace_factory(
    clone_root: Path,
) -> Callable[[Path], Any]:
    """Build an ``isolated_workspace`` seam that yields ``clone_root``.

    Records each invocation's ``live_repo`` arg on
    ``factory.invocations`` so tests can verify the propose loop calls
    the factory with the live repo path (and *only* the live repo,
    never the clone).
    """

    from contextlib import contextmanager

    invocations: list[Path] = []

    @contextmanager
    def factory(live_repo: Path) -> Any:
        invocations.append(Path(live_repo))
        clone_root.mkdir(parents=True, exist_ok=True)
        yield clone_root

    factory.invocations = invocations  # type: ignore[attr-defined]
    return factory


def _snapshot_live_repo(repo: Path) -> dict[str, Any]:
    """Capture the live repo's branch + tree state for before/after pins."""

    refs: list[str] = []
    refs_dir = repo / ".git" / "refs" / "heads"
    if refs_dir.is_dir():
        refs = sorted(p.name for p in refs_dir.iterdir() if p.is_file())
    return {
        "branches": refs,
        # Capture the names of top-level files/dirs so a test can pin
        # "no new files appeared on the live tree".
        "tree": sorted(p.name for p in repo.iterdir() if p.name != ".git"),
    }


def test_propose_mode_happy_path_writes_patch_without_touching_live(
    tmp_path: Path,
) -> None:
    """spec_028 §2-2 happy path — propose mode dispatches inside the
    clone, R5/R4/guard all pass against the clone, the diff is captured
    as a patch file under ``_ai_workspace/nightly/proposals/``, and the
    live repo tree / branches are unchanged."""

    _write_mutation_discover_json(repo=tmp_path)
    pre_snapshot = _snapshot_live_repo(tmp_path)

    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True,
            halt_reasons=(),
            files_touched=("tests/x.py",),
            template="A",
        )
    )
    diff_text = (
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        "+++ added reproducer\n"
    )
    gops = _FakeGitOps(canned_diff=diff_text)
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
        today=date(2026, 5, 25),
    )

    af = result.auto_fix
    assert isinstance(af, AutoFixOutcome)
    assert af.mode == "propose"
    assert af.skipped is False
    assert af.proposed is True
    assert af.template == "A"
    assert af.r5_killed is True
    assert af.r4_suite_passed is True
    assert af.guard_passed is True
    # Propose mode never merges — the loop must not touch the merge seam.
    assert af.merged is False
    assert gops.merges == []
    # The verified diff is what we ought to embed in the brief.
    assert af.proposal_diff == diff_text
    # The patch file exists under the live repo's proposals dir.
    assert af.proposal_patch_path is not None
    assert af.proposal_patch_path.exists()
    assert af.proposal_patch_path.parent == (
        tmp_path / "_ai_workspace" / "nightly" / "proposals"
    )
    assert af.proposal_patch_path.read_text(encoding="utf-8") == (
        diff_text if diff_text.endswith("\n") else diff_text + "\n"
    )

    # The isolated workspace factory was called once, with the LIVE repo.
    assert factory.invocations == [tmp_path.resolve()]  # type: ignore[attr-defined]
    # Every loop seam was pointed at the clone, not the live repo.
    assert all(call[1] == clone_dir for call in dispatcher.calls)
    assert suite.calls == [clone_dir]
    # The mutation rechecker doesn't surface its repo arg via _FakeMutationRechecker,
    # but the guard inspector records the diff — and the diff came from
    # gops.diff against the clone. Pin the gops call shape:
    assert gops.diffs_requested == [("main", af.branch)]
    assert gops.branches_created == [af.branch]
    assert af.branch.startswith("propose/")

    # CRITICAL invariant: the live repo's branches and top-level tree
    # are unchanged (the propose clone dir was created by the test
    # factory itself, so it appears in the post snapshot — strip it).
    post_snapshot = _snapshot_live_repo(tmp_path)
    post_tree = [
        n for n in post_snapshot["tree"]
        if n not in (
            "_ai_workspace",
            "_propose_clone",
        )
    ]
    pre_tree = [n for n in pre_snapshot["tree"] if n != "_ai_workspace"]
    assert post_tree == pre_tree
    assert post_snapshot["branches"] == pre_snapshot["branches"]


def test_propose_mode_skipped_when_no_candidate(tmp_path: Path) -> None:
    """spec_028 — no candidate ⇒ skipped, mode="propose", no patch written."""

    # No discover JSON written.
    factory = _fake_isolated_workspace_factory(tmp_path / "_clone")
    dispatcher = _FakeFixDispatcher()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        isolated_workspace=factory,
    )

    af = result.auto_fix
    assert af is not None
    assert af.mode == "propose"
    assert af.skipped is True
    assert "no template-A candidate" in af.skip_reason
    assert af.proposed is False
    assert af.proposal_patch_path is None
    # Factory was never invoked — propose skipped before reaching the clone.
    assert factory.invocations == []  # type: ignore[attr-defined]
    assert dispatcher.calls == []


def test_propose_mode_guard_halt_drops_proposal_and_writes_no_patch(
    tmp_path: Path,
) -> None:
    """spec_028 §2-3 — when the guard halts inside the clone, the
    proposal is dropped: ``proposed=False``, no patch file is written,
    ``halt_reason`` carries the guard-halt prefix so §D can render
    a one-liner."""

    _write_mutation_discover_json(repo=tmp_path)
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=False,
            halt_reasons=("R1: tests/sneaky.py is not allowed",),
            files_touched=("tests/sneaky.py",),
            template="A",
        )
    )
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
    )

    af = result.auto_fix
    assert af is not None
    assert af.mode == "propose"
    assert af.skipped is False
    assert af.proposed is False
    assert af.proposal_patch_path is None
    assert af.merged is False
    assert "proposal guard halted" in af.halt_reason
    assert "R1: tests/sneaky.py" in af.halt_reason
    # Merge was NEVER called.
    assert gops.merges == []
    # No patch file landed in proposals/.
    proposals_dir = tmp_path / "_ai_workspace" / "nightly" / "proposals"
    if proposals_dir.exists():
        assert list(proposals_dir.iterdir()) == []


def test_propose_mode_r5_fail_drops_proposal(tmp_path: Path) -> None:
    """spec_028 — R5 failure (mutation still survives in clone) → drop
    proposal, no patch, §D-class halt_reason."""

    _write_mutation_discover_json(repo=tmp_path)
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="survived")  # R5 fails
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
    )

    af = result.auto_fix
    assert af is not None
    assert af.mode == "propose"
    assert af.proposed is False
    assert af.r5_killed is False
    assert "proposal R5 failed" in af.halt_reason
    assert af.proposal_patch_path is None


def test_propose_mode_r4_fail_drops_proposal(tmp_path: Path) -> None:
    """spec_028 — R4 suite failure inside clone → drop proposal."""

    _write_mutation_discover_json(repo=tmp_path)
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=False))  # R4 fails
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
    )

    af = result.auto_fix
    assert af is not None
    assert af.mode == "propose"
    assert af.proposed is False
    assert af.r4_suite_passed is False
    assert "proposal R4 failed" in af.halt_reason
    assert af.proposal_patch_path is None


def test_propose_mode_dispatch_failed_drops_proposal(tmp_path: Path) -> None:
    """spec_028 — dispatch failed inside clone → drop proposal."""

    _write_mutation_discover_json(repo=tmp_path)
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(
            status="failed", halt_reason="claude_error"
        )
    )
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=_FakeSuiteRunner(),
        mutation_rechecker=_FakeMutationRechecker(),
        guard_inspector=_FakeGuardInspector(),
        git_ops=_FakeGitOps(),
        isolated_workspace=factory,
    )

    af = result.auto_fix
    assert af is not None
    assert af.mode == "propose"
    assert af.proposed is False
    assert "proposal dispatch failed" in af.halt_reason
    assert "claude_error" in af.halt_reason
    assert af.proposal_patch_path is None


def test_propose_mode_never_calls_merge_or_unpushed_counter(
    tmp_path: Path,
) -> None:
    """spec §2-2 / §3 — propose mode never merges. At K=1 (default) the
    un-pushed backlog counter is also never consulted, since the spec_038
    candidate-間 re-check only fires for i ≥ 1 (a multi-candidate
    propose run does re-evaluate the cap — see
    ``test_propose_k3_backlog_cap_between_candidates_skips_remainder``)."""

    _write_mutation_discover_json(repo=tmp_path)
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n")
    brief_runner, _ = _make_fake_brief_runner()

    counter_calls: list[Path] = []

    def boom_counter(repo: Path) -> int:
        counter_calls.append(repo)
        # If propose mode somehow consulted this, the limit (0) would
        # trip the skip; but it must NOT be consulted at all.
        raise RuntimeError("propose mode must not consult un-pushed counter")

    run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
        unpushed_counter=boom_counter,
        unpushed_backlog_limit=0,
    )

    assert counter_calls == []
    assert gops.merges == []


def test_propose_mode_off_fix_mode_no_loop_runs(tmp_path: Path) -> None:
    """spec_028 — ``fix_mode="off"`` should never invoke the propose loop
    (or the auto loop). ``auto_fix`` is ``None`` and no seam is touched."""

    _write_mutation_discover_json(repo=tmp_path)
    factory = _fake_isolated_workspace_factory(tmp_path / "_clone")
    dispatcher = _FakeFixDispatcher()
    brief_runner, _ = _make_fake_brief_runner()

    profile = Profile(
        discovery=DiscoveryConfig(channels=["mutation"]),
        safety=SafetyConfig(fix_mode="off"),
    )

    result = run_nightly(
        repo=tmp_path,
        profile=profile,
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        isolated_workspace=factory,
    )

    assert result.auto_fix is None
    assert factory.invocations == []  # type: ignore[attr-defined]
    assert dispatcher.calls == []


def test_propose_mode_template_b_happy_path(tmp_path: Path) -> None:
    """spec_028 — template B in propose mode: adversarial finding,
    dispatch + R5 (graceful_error) + R4 + guard all pass in clone →
    proposal patch written."""

    _write_adversarial_discover_json(repo=tmp_path)
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    adv_recheck = _FakeAdversarialRechecker(status="graceful_error")
    guard = _FakeGuardInspector(
        result=GuardResult(
            passed=True,
            halt_reasons=(),
            files_touched=("ccd/protocol.py", "tests/test_protocol.py"),
            template="B",
        )
    )
    diff_text = (
        "diff --git a/ccd/protocol.py b/ccd/protocol.py\n"
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
    )
    gops = _FakeGitOps(canned_diff=diff_text)
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile(
            channels=["adversarial"], fix_templates=["A", "B"]
        ),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        adversarial_rechecker=adv_recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
        today=date(2026, 5, 25),
    )

    af = result.auto_fix
    assert af is not None
    assert af.mode == "propose"
    assert af.template == "B"
    assert af.proposed is True
    assert af.r5_killed is True  # template-B "graceful_error" → R5 pass
    assert af.proposal_patch_path is not None
    assert af.proposal_patch_path.exists()


def test_propose_mode_cli_stdout_surfaces_propose_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """spec_028 — ``ccd nightly`` stdout surfaces a ``propose: proposed``
    line when the proposal lands; helps the operator see the headline
    before opening the brief."""

    _write_mutation_discover_json(repo=tmp_path)
    profile_path = tmp_path / "ccd_profile.toml"
    profile_path.write_text(
        "[discovery]\nchannels = [\"mutation\"]\n\n"
        "[safety]\nfix_mode = \"propose\"\n",
        encoding="utf-8",
    )
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n")
    brief_runner, _ = _make_fake_brief_runner()

    rc = cli.main(
        [
            "nightly",
            "--repo",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ],
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "propose: proposed" in out
    assert "patch=" in out


def test_propose_mode_passes_finding_to_brief(tmp_path: Path) -> None:
    """spec_028 — the AutoFixOutcome handed to ``run_brief`` carries the
    propose-mode bits so the brief can render the propose §B."""

    _write_mutation_discover_json(repo=tmp_path)
    clone_dir = tmp_path / "_propose_clone"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n")

    brief_invocations: list[dict[str, Any]] = []

    def inspecting_brief_runner(
        *, repo: Path, today: date | None = None, **kwargs: Any
    ) -> BriefResult:
        brief_invocations.append({"repo": repo, "today": today, **kwargs})
        nightly_dir = Path(repo) / "_ai_workspace" / "nightly"
        nightly_dir.mkdir(parents=True, exist_ok=True)
        day = today or date(2026, 5, 25)
        report_path = nightly_dir / f"report_{day.isoformat()}.md"
        report_path.write_text("# fake\n", encoding="utf-8")
        return BriefResult(
            success=True,
            report_path=report_path,
            summary=BriefSummary(
                channels_picked=(),
                channels_missing=(),
                mutation_actionable=0,
                adversarial_ungraceful=0,
                ai_findings=0,
                mechanical_findings_total=0,
            ),
        )

    run_nightly(
        repo=tmp_path,
        profile=_propose_profile(),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=inspecting_brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
    )

    assert brief_invocations
    auto_fix = brief_invocations[0].get("auto_fix")
    assert auto_fix is not None
    assert auto_fix.mode == "propose"
    assert auto_fix.proposed is True


def test_propose_default_isolated_workspace_is_disposable_clone(
    tmp_path: Path,
) -> None:
    """spec_028 — when no ``isolated_workspace`` seam is injected, the
    propose loop falls back to ``_default_isolated_workspace``, which
    wraps ``_isolated_clone`` (the spec_014 disposable, remoteless
    clone). We assert the default is exported and is a context manager
    that yields a directory != the live repo."""

    from ccd.nightly import _default_isolated_workspace

    # Build a tiny fake repo so the clone has *something* to copy.
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")

    with _default_isolated_workspace(tmp_path) as workspace:
        assert isinstance(workspace, Path)
        assert workspace != tmp_path.resolve()
        assert workspace.exists()
        # README copied in.
        assert (workspace / "README.md").exists()
        # _ai_workspace is excluded — pin it by writing one to the live
        # repo before the with-block? Already past that, so just confirm
        # the workspace path is a fresh tmp dir.
        assert str(workspace).startswith("/tmp/") or "/tmp/" in str(workspace)
    # After exit, the workspace was rmtree-d.
    assert not workspace.exists()


# --------------------------------------------------------------------------- #
# spec_038 — top-K candidate selection (multi-candidate per night, 直列)
# --------------------------------------------------------------------------- #


def _multi_actionable(n: int) -> list[dict[str, Any]]:
    """Build n synthetic mutation actionable entries with distinct
    signatures so the loop can tell them apart in the dispatch trail."""

    return [
        {
            "file": f"ccd/file_{i}.py",
            "line": 10 + i,
            "mutation": f"a{i} → b{i}",
            "status": "survived",
            "signature": f"ccd/file_{i}.py:{10 + i}:a{i} → b{i}",
        }
        for i in range(n)
    ]


def _autofix_profile_k(*, k: int) -> Profile:
    """K-candidate auto-mode profile (spec_038)."""
    return Profile(
        discovery=DiscoveryConfig(channels=["mutation"]),
        schedule=ScheduleConfig(),
        safety=SafetyConfig(fix_mode="auto", max_candidates_per_night=k),
    )


def test_default_k1_dispatch_count_unchanged_vs_v2(tmp_path: Path) -> None:
    """spec_038 §3-1: with ``max_candidates_per_night`` unspecified (K=1
    default), the夜間処理 外形 (dispatch count, NightlyResult shape) is
    identical to spec_023〜026 — 1 dispatch, 1 merge, no extras."""

    _write_mutation_discover_json(repo=tmp_path, actionable=_multi_actionable(4))
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),  # default K=1
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    assert len(dispatcher.calls) == 1
    assert result.auto_fix is not None
    assert result.auto_fix.merged is True
    assert result.auto_fix_extras == ()


def test_k3_with_four_candidates_processes_only_top_three(
    tmp_path: Path,
) -> None:
    """spec_038 §3-2: K=3 and 4 candidates in the source JSON →
    exactly the top 3 are dispatched in priority (source-JSON) order.
    The 4th candidate stays unprocessed."""

    _write_mutation_discover_json(repo=tmp_path, actionable=_multi_actionable(4))
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_k(k=3),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    # 3 dispatches, 3 merges (all R5/R4/guard pass in this fake setup).
    assert len(dispatcher.calls) == 3
    assert len(gops.merges) == 3
    # Primary + 2 extras = 3 total outcomes recorded.
    assert result.auto_fix is not None
    assert len(result.auto_fix_extras) == 2
    all_outs = (result.auto_fix, *result.auto_fix_extras)
    sigs = [o.finding_signature for o in all_outs]
    assert sigs == [
        "ccd/file_0.py:10:a0 → b0",
        "ccd/file_1.py:11:a1 → b1",
        "ccd/file_2.py:12:a2 → b2",
    ]


def test_k3_backlog_cap_between_candidates_skips_remainder(
    tmp_path: Path,
) -> None:
    """spec_038 §3-3: with K=3, if the un-pushed backlog cap is hit
    BEFORE candidate 2 starts (e.g. candidate 1 merged → cap reached),
    the remaining candidates are skipped and the reason is recorded."""

    _write_mutation_discover_json(repo=tmp_path, actionable=_multi_actionable(3))
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    # Counter returns 0 first (loop entry), then 3 (after candidate 1
    # merged, simulating a now-tripped backlog).
    call_counts: list[int] = []

    def _counter(_repo: Path) -> int:
        call_counts.append(1)
        return 0 if len(call_counts) == 1 else 3

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_k(k=3),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        unpushed_counter=_counter,
    )

    # Only candidate 1 dispatched + merged; remainder bailed.
    assert len(dispatcher.calls) == 1
    assert len(gops.merges) == 1
    assert result.auto_fix is not None
    assert result.auto_fix.merged is True
    # The rollup skip outcome shows up as the single extra.
    assert len(result.auto_fix_extras) == 1
    skip = result.auto_fix_extras[0]
    assert skip.skipped is True
    assert "remaining candidate(s) skipped" in skip.skip_reason
    assert "un-pushed autonomous-fix commits" in skip.skip_reason
    assert "2 件" in skip.skip_reason


def test_k3_first_candidate_halt_does_not_stop_second(
    tmp_path: Path,
) -> None:
    """spec_038 §3-4: a candidate-1 HALT (guard rejects, R5 fails, etc.)
    does NOT stop the loop — candidate 2 still gets its full per-candidate
    attempt. spec_038 §2-3: "1候補の失敗は残候補の処理を止めない"."""

    _write_mutation_discover_json(repo=tmp_path, actionable=_multi_actionable(2))
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    # Recheck returns "survived" → R5 fails → candidate 1 halts.
    # But the rechecker is shared across candidates; switch its status
    # mid-run via a stateful counter.
    state = {"calls": 0}

    def _stateful_recheck(
        *,
        repo: Path,
        file: str,
        line: int,
        mutation: str,
        signature: str,
    ) -> str:
        state["calls"] += 1
        # First candidate: fail R5. Second candidate: pass R5.
        return "survived" if state["calls"] == 1 else "killed"

    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_k(k=3),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=_stateful_recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    # 2 dispatches (1 halt + 1 merge).
    assert len(dispatcher.calls) == 2
    assert len(gops.merges) == 1
    assert result.auto_fix is not None
    assert result.auto_fix.merged is False
    assert result.auto_fix.r5_killed is False
    assert "R5 failed" in result.auto_fix.halt_reason
    assert len(result.auto_fix_extras) == 1
    extra = result.auto_fix_extras[0]
    assert extra.merged is True
    assert extra.r5_killed is True


def test_k1_default_brief_layout_identical(tmp_path: Path) -> None:
    """spec_038 §3-1: at K=1, the morning report contains no
    multi-candidate §B markers — the v2 layout is preserved unchanged."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()

    # Use the real brief (not the fake) so we can assert on the rendered
    # markdown. Patch the windows mirror to a no-op.
    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),  # default K=1
        channel_runner=_RecordingChannelRunner(),
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    assert result.success
    assert result.brief_report_wsl is not None
    md = result.brief_report_wsl.read_text(encoding="utf-8")
    # K=1 keeps the v2 §B Phase 2 layout.
    assert "Phase 2" in md
    # Multi-candidate marker MUST NOT appear at K=1.
    assert "候補ごとの小節" not in md
    assert "件直列処理" not in md
    assert "候補 1/" not in md


def test_k3_brief_renders_multi_candidate_section_b(tmp_path: Path) -> None:
    """spec_038 §2-4: at K>1, §B switches to per-candidate subsections
    with one ``### 候補 i/N`` entry per outcome (primary + extras)."""

    _write_mutation_discover_json(repo=tmp_path, actionable=_multi_actionable(3))
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_k(k=3),
        channel_runner=_RecordingChannelRunner(),
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    assert result.success
    assert result.brief_report_wsl is not None
    md = result.brief_report_wsl.read_text(encoding="utf-8")
    assert "3 件直列処理" in md
    assert "### 候補 1/3" in md
    assert "### 候補 2/3" in md
    assert "### 候補 3/3" in md


def _propose_profile_k(*, k: int) -> Profile:
    """K-candidate propose-mode profile (spec_038)."""
    return Profile(
        discovery=DiscoveryConfig(channels=["mutation"]),
        schedule=ScheduleConfig(),
        safety=SafetyConfig(
            fix_mode="propose",
            fix_templates=["A"],
            max_candidates_per_night=k,
        ),
    )


def test_propose_k3_backlog_cap_between_candidates_skips_remainder(
    tmp_path: Path,
) -> None:
    """spec_038 §2-3 (literal) — between candidates the propose loop
    re-evaluates BOTH PAUSE and the un-pushed backlog cap (spec says
    "未push バックログ cap と PAUSE を再評価" without a mode restriction).

    Setup: K=3 with 3 candidates in the source JSON. The un-pushed
    counter returns 0 for the first call (before candidate 1) and 3 on
    every subsequent call so the cap trips between candidates. The
    propose loop must therefore process candidate 1 and emit a rollup
    skip outcome for the remaining 2 candidates.
    """

    _write_mutation_discover_json(repo=tmp_path, actionable=_multi_actionable(3))
    clone_dir = tmp_path / "_propose_clone_k3"
    factory = _fake_isolated_workspace_factory(clone_dir)
    dispatcher = _FakeFixDispatcher(
        outcome=FixDispatchOutcome(status="done", commits_made=1)
    )
    suite = _FakeSuiteRunner(outcome=SuiteOutcome(passed=True))
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/x b/x\n+y\n")
    brief_runner, _ = _make_fake_brief_runner()

    # Propose mode has NO pre-loop backlog check (auto's initial gate
    # is auto-only), so the first counter invocation is the
    # inter-candidate re-evaluation that fires before candidate 2.
    # Returning 3 (= default cap) immediately trips the gate.
    counter_calls: list[int] = []

    def _counter(_repo: Path) -> int:
        counter_calls.append(1)
        return 3

    result = run_nightly(
        repo=tmp_path,
        profile=_propose_profile_k(k=3),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        isolated_workspace=factory,
        unpushed_counter=_counter,
    )

    # Candidate 1 dispatched and produced a proposal; candidates 2-3
    # were skipped because the cap tripped between them.
    assert len(dispatcher.calls) == 1
    assert result.auto_fix is not None
    assert result.auto_fix.mode == "propose"
    assert result.auto_fix.proposed is True
    # The rollup skip outcome surfaces in extras with the backlog reason.
    assert len(result.auto_fix_extras) == 1
    skip = result.auto_fix_extras[0]
    assert skip.skipped is True
    assert skip.mode == "propose"
    assert "remaining candidate(s) skipped" in skip.skip_reason
    assert "un-pushed autonomous-fix commits" in skip.skip_reason
    assert "2 件" in skip.skip_reason
    # The counter was consulted at least once between candidates.
    assert len(counter_calls) >= 1


# --------------------------------------------------------------------------- #
# spec_039 — FixLoop integration (nightly side)
# --------------------------------------------------------------------------- #


def _autofix_profile_loop(*, iterations: int) -> Profile:
    """spec_039 — auto-mode profile with ``loop_max_iterations`` set."""

    return Profile(
        discovery=DiscoveryConfig(channels=["mutation"]),
        schedule=ScheduleConfig(),
        safety=SafetyConfig(
            fix_mode="auto",
            loop_max_iterations=iterations,
        ),
    )


@dataclass
class _ScriptedAutoDispatcher:
    """Like ``_FakeFixDispatcher`` but returns canned outcomes in turn AND
    accepts the spec_039 ``feedback=`` kwarg."""

    outcomes: list[FixDispatchOutcome] = field(
        default_factory=lambda: [
            FixDispatchOutcome(status="done", commits_made=1)
        ]
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        spec_path: Path,
        repo: Path,
        branch: str,
        feedback: Path | None = None,
    ) -> FixDispatchOutcome:
        self.calls.append(
            {
                "spec_path": Path(spec_path),
                "repo": Path(repo),
                "branch": branch,
                "feedback": feedback,
            }
        )
        idx = min(len(self.calls), len(self.outcomes)) - 1
        return self.outcomes[idx]


@dataclass
class _StatefulMutationRechecker:
    """Returns the next status string from ``sequence`` each call.

    Used to simulate "1st iter R5 fail → 2nd iter R5 pass" — the loop's
    convergence path drives the rechecker once per iteration; we feed
    different statuses to verify the loop iterated.
    """

    sequence: list[str] = field(default_factory=lambda: ["killed"])
    calls: list[tuple[str, int, str]] = field(default_factory=list)

    def __call__(
        self,
        *,
        repo: Path,  # noqa: ARG002
        file: str,
        line: int,
        mutation: str,  # noqa: ARG002
        signature: str,
    ) -> str:
        self.calls.append((file, line, signature))
        idx = min(len(self.calls), len(self.sequence)) - 1
        return self.sequence[idx]


def test_default_loop_max_iterations_is_one(tmp_path: Path) -> None:
    """spec_039 §3-1 — default profile keeps ``loop_max_iterations=1``,
    so the FixLoop wraps a single dispatch+verify with no behavioral
    change relative to spec_023〜038."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/tests/x.py b/tests/x.py\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        # plain auto profile = default loop_max_iterations
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    # Exactly one dispatch — the v2 single-shot behavior is preserved.
    assert len(dispatcher.calls) == 1
    # spec_039 telemetry: iterations=1, converged=True (verifier was green).
    assert af.iterations == 1
    assert af.converged is True
    # Loop did not need to halt — halt anchor is empty for a converged run.
    assert af.loop_halt_reason == ""


def test_loop_converges_at_iteration_2_when_recheck_flips(
    tmp_path: Path,
) -> None:
    """spec_039 §3-2 — fake runner "iter 1 R5 fail → iter 2 R5 pass"
    converges with ``iterations=2`` and the loop merges."""

    _write_mutation_discover_json(repo=tmp_path)
    # The dispatcher returns "done" both times; the spec_039 verifier
    # judges convergence, NOT the dispatcher self-report.
    dispatcher = _ScriptedAutoDispatcher(
        outcomes=[
            FixDispatchOutcome(status="done", commits_made=1),
            FixDispatchOutcome(status="done", commits_made=1),
        ]
    )
    suite = _FakeSuiteRunner()
    # Iter 1 → R5 fails (mutation still survived), iter 2 → R5 passes.
    recheck = _StatefulMutationRechecker(sequence=["survived", "killed"])
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/tests/x.py b/tests/x.py\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_loop(iterations=3),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert af.iterations == 2
    assert af.converged is True
    assert af.merged is True
    # Dispatcher called twice; iter 2 received a feedback path.
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[0]["feedback"] is None
    assert dispatcher.calls[1]["feedback"] is not None
    assert Path(dispatcher.calls[1]["feedback"]).exists()


def test_loop_no_progress_halts_before_iteration_3(
    tmp_path: Path,
) -> None:
    """spec_039 §3-3 — same failure twice in a row halts the loop
    before iteration 3 starts (max_iterations=5 here)."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _ScriptedAutoDispatcher(
        outcomes=[FixDispatchOutcome(status="done", commits_made=1)]
    )
    suite = _FakeSuiteRunner()
    # Same R5 failure every time → no progress.
    recheck = _StatefulMutationRechecker(
        sequence=["survived", "survived", "killed"]
    )
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/tests/x.py b/tests/x.py\n")
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_loop(iterations=5),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    # Two dispatches, two recheck calls (verifier ran twice); iter 3
    # was NOT started.
    assert len(dispatcher.calls) == 2
    assert len(recheck.calls) == 2
    assert af.iterations == 2
    assert af.converged is False
    assert af.merged is False
    # Halt reason carries the no-progress anchor.
    assert "no-progress" in af.loop_halt_reason


def test_loop_immediate_halt_on_blocked_status_does_not_iterate(
    tmp_path: Path,
) -> None:
    """spec_039 §3-5 — a BLOCKED dispatch halts the loop after one
    iteration even when ``loop_max_iterations`` allows more. No feedback
    file is written; no verifier ran."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _ScriptedAutoDispatcher(
        outcomes=[
            FixDispatchOutcome(
                status="blocked",
                halt_reason="agent declared BLOCKED",
            )
        ]
    )
    suite = _FakeSuiteRunner()
    recheck = _StatefulMutationRechecker(sequence=["killed"])
    guard = _FakeGuardInspector()
    gops = _FakeGitOps()
    brief_runner, _ = _make_fake_brief_runner()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_loop(iterations=5),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
    )

    af = result.auto_fix
    assert af is not None
    assert len(dispatcher.calls) == 1
    # Verifier never ran → recheck untouched.
    assert recheck.calls == []
    assert suite.calls == []
    assert af.iterations == 1
    assert af.converged is False
    assert "immediate-halt" in af.loop_halt_reason


def test_brief_renders_fix_loop_summary_line(tmp_path: Path) -> None:
    """spec_039 §2-3 — when the convergence loop ran more than once the
    morning brief surfaces ``- 収束: N iterations`` or ``- 未収束:`` in §B."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _ScriptedAutoDispatcher(
        outcomes=[
            FixDispatchOutcome(status="done", commits_made=1),
            FixDispatchOutcome(status="done", commits_made=1),
        ]
    )
    suite = _FakeSuiteRunner()
    recheck = _StatefulMutationRechecker(sequence=["survived", "killed"])
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/tests/x.py b/tests/x.py\n")
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile_loop(iterations=3),
        channel_runner=_RecordingChannelRunner(),
        # real brief runner exercises rendering path
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        today=date(2026, 5, 25),
    )

    assert result.brief_report_wsl is not None
    body = result.brief_report_wsl.read_text(encoding="utf-8")
    # Converged after iter 2 → brief shows "収束: 2 iterations".
    assert "収束: 2 iterations" in body


def test_default_k1_iter1_brief_does_not_mention_fix_loop(
    tmp_path: Path,
) -> None:
    """spec_039 §3-1 — the default K=1 / iter=1 brief is bit-for-bit
    identical to spec_038; no "収束" / "未収束" line appears."""

    _write_mutation_discover_json(repo=tmp_path)
    dispatcher = _FakeFixDispatcher()
    suite = _FakeSuiteRunner()
    recheck = _FakeMutationRechecker(status="killed")
    guard = _FakeGuardInspector()
    gops = _FakeGitOps(canned_diff="diff --git a/tests/x.py b/tests/x.py\n")
    mirror, _ = _make_recording_mirror()

    result = run_nightly(
        repo=tmp_path,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        windows_mirror=mirror,
        fix_dispatcher=dispatcher,
        suite_runner=suite,
        mutation_rechecker=recheck,
        guard_inspector=guard,
        git_ops=gops,
        today=date(2026, 5, 25),
    )

    assert result.brief_report_wsl is not None
    body = result.brief_report_wsl.read_text(encoding="utf-8")
    assert "収束:" not in body
    assert "未収束:" not in body
