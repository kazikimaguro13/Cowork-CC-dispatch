"""spec_047 — auto ループの運用衛生.

Pins the four operational-hygiene behaviours spec_047 added on top of the
2026-06-11 production observation that a ``smoke_failed`` HALT lost all its
diagnostics with the disposable clone:

- §2-1 HALT artifact persistence (carried out of the clone before rmtree),
  including the machine classification and "readable after the clone is
  gone" guarantee (acceptance §3 #1 / #2).
- §2-2 inbox 退場: archive-on-merge (#3) and supersede-on-re-translate (#4).
- §2-3 same-night staleness recheck for K≥2 (#5).

The heavy worker/Integrator integration uses the same fakes as
``test_nightly`` (imported, not duplicated)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from ccd.loop import FixLoopOutcome
from ccd.nightly import (
    FixDispatchOutcome,
    _classify_halt,
    _persist_halt_artifacts,
    run_nightly,
)
from ccd.translate import (
    DEFAULT_ARCHIVE_DIR_REL,
    Finding,
    translate_finding,
)
from tests.test_nightly import (
    _auto_fix_test_seams,
    _autofix_profile,
    _autofix_profile_pool,
    _FakeGitOps,
    _FakeGuardInspector,
    _FakeMutationRechecker,
    _FakeSuiteRunner,
    _make_fake_brief_runner,
    _multi_actionable,
    _RecordingChannelRunner,
    _write_mutation_discover_json,
)

_NIGHT = date(2026, 6, 11)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_halting_clone(tmp_path: Path) -> Path:
    """A real git clone with a committed "CC diff" on a candidate branch,
    plus the feedback + result files CC would have left under the clone's
    ``_ai_workspace`` — i.e. exactly what a HALT leaves behind before the
    clone is rmtree-d."""

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "checkout", "-q", "-b", "main")
    _git(clone, "config", "user.email", "t@t.t")
    _git(clone, "config", "user.name", "t")
    (clone / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "base")
    _git(clone, "checkout", "-q", "-b", "auto/spec_auto_004")
    (clone / "mod.py").write_text("x = 1\nMARKER_CC_DIFF = 2\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "CC fix attempt")

    logs = clone / "_ai_workspace" / "logs"
    logs.mkdir(parents=True)
    (logs / "fix_feedback_spec_auto_004.md").write_text(
        "FEEDBACK_MARKER: smoke failed here\n", encoding="utf-8"
    )
    outbox = clone / "_ai_workspace" / "bridge" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "result_auto_004.md").write_text(
        "RESULT_MARKER: what CC wrote\n", encoding="utf-8"
    )
    return clone


# --------------------------------------------------------------------------- #
# §2-1 — HALT classification
# --------------------------------------------------------------------------- #


def test_brief_section_d_links_halt_artifacts() -> None:
    """spec_047 §2-1 — §D renders a relative link to a HALT's artifacts."""

    from ccd.brief import _halt_artifacts_link
    from ccd.nightly import AutoFixOutcome

    o = AutoFixOutcome(
        skipped=False,
        halt_artifacts_dir=Path("/x/_ai_workspace/nightly/halts/2026-06-11_spec_auto_004"),
    )
    link = _halt_artifacts_link(o)
    assert link == "[halts/2026-06-11_spec_auto_004/](halts/2026-06-11_spec_auto_004/)"
    assert _halt_artifacts_link(AutoFixOutcome(skipped=False)) == ""


def test_classify_halt_buckets() -> None:
    assert _classify_halt("dispatch failed: smoke_failed") == "smoke_failed"
    assert _classify_halt("dispatch timed out after 2400s") == "timeout"
    assert _classify_halt("guard halted the fix: R2") == "guard_halt"
    assert _classify_halt("R5 failed: target mutation not killed") == "r5_failed"
    assert _classify_halt("integrator: local merge failed") == "integrator"
    assert _classify_halt("something unexpected") == "other"


# --------------------------------------------------------------------------- #
# §2-1 / acceptance #1 — smoke_failed artifacts survive the clone
# --------------------------------------------------------------------------- #


def test_smoke_failed_halt_artifacts_persist_and_outlive_clone(
    tmp_path: Path,
) -> None:
    clone = _make_halting_clone(tmp_path)
    halts = tmp_path / "_ai_workspace" / "nightly" / "halts"

    dest = _persist_halt_artifacts(
        halts_dir=halts,
        night_id=_NIGHT.isoformat(),
        spec_id="spec_auto_004",
        clone_path=clone,
        branch="auto/spec_auto_004",
        halt_reason="dispatch failed: smoke_failed",
        phase="dispatch",
        fl=None,
    )

    assert dest is not None
    assert dest == halts / "2026-06-11_spec_auto_004"

    # Now DELETE the clone — the diagnostics must remain readable.
    shutil.rmtree(clone)

    halt_md = (dest / "halt.md").read_text(encoding="utf-8")
    assert "`smoke_failed`" in halt_md  # machine classification
    assert "`dispatch`" in halt_md  # phase
    assert "dispatch failed: smoke_failed" in halt_md  # verbatim reason

    diff_text = (dest / "diff.patch").read_text(encoding="utf-8")
    assert "MARKER_CC_DIFF" in diff_text  # CC's committed change captured

    feedback = (dest / "feedback" / "fix_feedback_spec_auto_004.md").read_text(
        encoding="utf-8"
    )
    assert "FEEDBACK_MARKER" in feedback
    result = (dest / "outbox" / "result_auto_004.md").read_text(encoding="utf-8")
    assert "RESULT_MARKER" in result


# --------------------------------------------------------------------------- #
# §2-1 / acceptance #2 — timeout halt is captured the same way
# --------------------------------------------------------------------------- #


def test_timeout_halt_artifacts_persist(tmp_path: Path) -> None:
    clone = _make_halting_clone(tmp_path)
    halts = tmp_path / "halts"
    fl = FixLoopOutcome(
        iterations=1,
        converged=False,
        halt_reason="fix-loop: budget exhausted",
        final_verification=None,
        final_dispatch_status="failed",
        final_dispatch_halt_reason="dispatch timed out after 2400s",
        final_dispatched=True,
    )

    dest = _persist_halt_artifacts(
        halts_dir=halts,
        night_id=_NIGHT.isoformat(),
        spec_id="spec_auto_009",
        clone_path=clone,
        branch="auto/spec_auto_009",
        halt_reason="dispatch failed: dispatch timed out after 2400s",
        phase="dispatch",
        fl=fl,
    )

    assert dest is not None
    shutil.rmtree(clone)
    assert "`timeout`" in (dest / "halt.md").read_text(encoding="utf-8")
    assert (dest / "diff.patch").read_text(encoding="utf-8")


def test_persist_halt_artifacts_caps_size(tmp_path: Path) -> None:
    clone = _make_halting_clone(tmp_path)
    # Blow up a feedback file far past the cap.
    big = clone / "_ai_workspace" / "logs" / "huge.log"
    big.write_text("Z" * 5_000, encoding="utf-8")

    dest = _persist_halt_artifacts(
        halts_dir=tmp_path / "halts",
        night_id=_NIGHT.isoformat(),
        spec_id="spec_auto_001",
        clone_path=clone,
        branch="auto/spec_auto_001",
        halt_reason="dispatch failed: smoke_failed",
        phase="dispatch",
        fl=None,
        max_bytes=1_000,
    )

    assert dest is not None
    capped = (dest / "feedback" / "huge.log").read_text(encoding="utf-8")
    assert len(capped.encode("utf-8")) <= 1_000 + 80  # tail + marker line
    assert "truncated" in capped


def test_persist_halt_artifacts_disabled_is_noop(tmp_path: Path) -> None:
    # halts_dir None ⇒ feature off ⇒ no directory, no raise.
    assert (
        _persist_halt_artifacts(
            halts_dir=None,
            night_id=_NIGHT.isoformat(),
            spec_id="spec_auto_001",
            clone_path=tmp_path,
            branch="b",
            halt_reason="x",
            phase="dispatch",
        )
        is None
    )


# --------------------------------------------------------------------------- #
# §2-1 integration — a smoke_failed dispatch through run_nightly persists
# --------------------------------------------------------------------------- #


def test_run_nightly_smoke_failed_writes_halt_dir(tmp_path: Path) -> None:
    _write_mutation_discover_json(repo=tmp_path)
    brief_runner, _ = _make_fake_brief_runner()

    class _SmokeFailedDispatcher:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def __call__(
            self, *, spec_path: Path, repo: Path, branch: str, **_: Any
        ) -> FixDispatchOutcome:
            self.calls.append(branch)
            return FixDispatchOutcome(
                status="failed", halt_reason="smoke_failed", commits_made=0
            )

    result = run_nightly(
        repo=tmp_path,
        today=_NIGHT,
        profile=_autofix_profile(autonomous=True),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=_SmokeFailedDispatcher(),
        suite_runner=_FakeSuiteRunner(),
        mutation_rechecker=_FakeMutationRechecker(status="killed"),
        guard_inspector=_FakeGuardInspector(),
        git_ops=_FakeGitOps(),
        **_auto_fix_test_seams(tmp_path),
    )

    af = result.auto_fix
    assert af is not None and af.merged is False
    assert af.halt_artifacts_dir is not None
    halt_md = (af.halt_artifacts_dir / "halt.md").read_text(encoding="utf-8")
    assert "`smoke_failed`" in halt_md
    # Under the default (single-policy) layout the halts live below
    # _ai_workspace/nightly/halts/.
    assert af.halt_artifacts_dir.parent == (
        tmp_path / "_ai_workspace" / "nightly" / "halts"
    )


# --------------------------------------------------------------------------- #
# §2-2 / acceptance #4 — re-translate of the same signature supersedes
# --------------------------------------------------------------------------- #


def _mutation_finding(sig: str = "ccd/protocol.py:46:x == y → x != y") -> Finding:
    file, line, mutation = sig.split(":", 2)
    return Finding(
        channel="mutation",
        file=file,
        line=int(line),
        mutation=mutation,
        status="survived",
        signature=sig,
    )


def test_retranslate_same_signature_supersedes_old_inbox_spec(
    tmp_path: Path,
) -> None:
    finding = _mutation_finding()

    first = translate_finding(finding, repo=tmp_path, today=_NIGHT)
    assert first.success and first.spec_auto_id == "spec_auto_001"
    assert first.superseded_ids == ()

    second = translate_finding(finding, repo=tmp_path, today=_NIGHT)
    assert second.success and second.spec_auto_id == "spec_auto_002"
    # The older spec was retired, not left to pile up.
    assert second.superseded_ids == ("spec_auto_001",)

    inbox = tmp_path / "_ai_workspace" / "bridge" / "inbox"
    live_specs = sorted(p.name for p in inbox.glob("spec_auto_*.md"))
    assert live_specs == ["spec_auto_002.md"]  # only one live spec per sig
    archive = tmp_path / DEFAULT_ARCHIVE_DIR_REL
    assert (archive / "spec_auto_001.md").exists()


def test_retranslate_distinct_signature_does_not_supersede(
    tmp_path: Path,
) -> None:
    a = translate_finding(_mutation_finding("ccd/a.py:1:p → q"), repo=tmp_path)
    b = translate_finding(_mutation_finding("ccd/b.py:2:r → s"), repo=tmp_path)
    assert a.success and b.success
    assert b.superseded_ids == ()
    inbox = tmp_path / "_ai_workspace" / "bridge" / "inbox"
    assert len(list(inbox.glob("spec_auto_*.md"))) == 2


# --------------------------------------------------------------------------- #
# §2-3 / acceptance #5 — same-night staleness skip (K=2)
# --------------------------------------------------------------------------- #


class _StaleRechecker:
    """Fake staleness rechecker: reports the configured status for every
    signature and records each query (only the 2nd+ candidate should be
    checked, after the 1st merge)."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.calls: list[str] = []

    def __call__(
        self, *, repo: Path, file: str, line: int, mutation: str, signature: str
    ) -> str:
        self.calls.append(signature)
        return self.status


def test_k2_second_candidate_skipped_when_first_merge_kills_it(
    tmp_path: Path,
) -> None:
    _write_mutation_discover_json(repo=tmp_path, actionable=_multi_actionable(2))
    brief_runner, _ = _make_fake_brief_runner()

    class _RecordingDispatcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(
            self, *, spec_path: Path, repo: Path, branch: str, **_: Any
        ) -> FixDispatchOutcome:
            self.calls.append(branch)
            return FixDispatchOutcome(status="done", commits_made=1)

    dispatcher = _RecordingDispatcher()
    gops = _FakeGitOps()
    stale = _StaleRechecker(status="killed")

    result = run_nightly(
        repo=tmp_path,
        today=_NIGHT,
        # K=2, P=1, merge cap 2 ⇒ staleness recheck is armed.
        profile=_autofix_profile_pool(k=2, p=1, max_merges=2),
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=dispatcher,
        suite_runner=_FakeSuiteRunner(),
        mutation_rechecker=_FakeMutationRechecker(status="killed"),
        guard_inspector=_FakeGuardInspector(),
        git_ops=gops,
        staleness_rechecker=stale,
        **_auto_fix_test_seams(tmp_path),
    )

    # 1st candidate merged; 2nd was re-checked once and SKIPPED before any
    # dispatch — so exactly one dispatch and one merge happened, and the
    # merge cap (2) was NOT consumed by the skip.
    assert len(dispatcher.calls) == 1
    assert len(gops.merges) == 1
    assert len(stale.calls) == 1

    outcomes = [result.auto_fix, *result.auto_fix_extras]
    stale_skips = [
        o
        for o in outcomes
        if o is not None
        and o.skipped
        and o.skip_reason.startswith("stale candidate skipped")
    ]
    assert len(stale_skips) == 1


def test_k1_default_never_runs_staleness_recheck(tmp_path: Path) -> None:
    """K=1 (current operation) ⇒ the staleness recheck never fires: there is
    no 2nd candidate queued after a merge, so behaviour is unchanged even
    when a rechecker is wired (spec_047 §2-3)."""

    from tests.test_nightly import _FakeFixDispatcher

    _write_mutation_discover_json(repo=tmp_path)
    brief_runner, _ = _make_fake_brief_runner()
    stale = _StaleRechecker(status="killed")

    result = run_nightly(
        repo=tmp_path,
        today=_NIGHT,
        profile=_autofix_profile(autonomous=True),  # K=1, P=1
        channel_runner=_RecordingChannelRunner(),
        brief_runner=brief_runner,
        windows_mirror=lambda _p: None,
        fix_dispatcher=_FakeFixDispatcher(),
        suite_runner=_FakeSuiteRunner(),
        mutation_rechecker=_FakeMutationRechecker(status="killed"),
        guard_inspector=_FakeGuardInspector(),
        git_ops=_FakeGitOps(),
        staleness_rechecker=stale,
        **_auto_fix_test_seams(tmp_path),
    )

    assert result.auto_fix is not None and result.auto_fix.merged is True
    # Only one candidate ⇒ no queued sibling after the merge ⇒ never called.
    assert stale.calls == []
