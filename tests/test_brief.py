"""Tests for ``ccd/brief.py:run_brief`` and the ``ccd brief`` CLI.

spec_017 ships the morning-report renderer for v2 Phase 1. The brief
reads completed ``discover_NNN.json`` (one latest per channel) and
writes a 6-section markdown report. The renderer does not execute any
discovery channel; tests assert this by handing it pre-baked JSON
inputs and verifying nothing else fires.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ccd import cli
from ccd.brief import (
    CHANNEL_ADVERSARIAL,
    CHANNEL_AI,
    CHANNEL_MUTATION,
    BriefResult,
    BriefSummary,
    run_brief,
)

# --------------------------------------------------------------------------- #
# Fixture-writing helpers — build representative discover_NNN.json payloads.
# --------------------------------------------------------------------------- #


def _write_mutation(
    discover_dir: Path,
    *,
    seq: int,
    actionable: tuple[tuple[str, int, str], ...] = (
        ("ccd/dispatch.py", 125, "check=False, → check=True,"),
        ("ccd/dispatch.py", 145, "return 0 → return 1"),
    ),
) -> Path:
    """Write a mutation-channel discover_NNN.json (and a tiny md)."""

    discover_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": {
            "tool": "mutmut",
            "target_paths": ["ccd"],
            "mutants_total": 4,
            "status_breakdown": {"survived": len(actionable), "killed": 2},
            "survived_total": len(actionable),
            "survived_by_file": {"ccd/dispatch.py": len(actionable)},
            "blocklisted_total": 0,
            "actionable_total": len(actionable),
        },
        "actionable": [
            {
                "file": f,
                "line": ln,
                "mutation": m,
                "status": "survived",
                "signature": f"{f}:{ln}:{m}",
            }
            for f, ln, m in actionable
        ],
        "blocklisted": [],
    }
    json_path = discover_dir / f"discover_{seq:03d}.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (discover_dir / f"discover_{seq:03d}.md").write_text(
        f"# discover_{seq:03d} — mutation\n", encoding="utf-8"
    )
    return json_path


def _write_adversarial(
    discover_dir: Path,
    *,
    seq: int,
    findings: tuple[tuple[str, str, str, str], ...] = (
        (
            "ccd.protocol.parse_spec",
            "05_invalid_utf8_bytes",
            "UnicodeDecodeError",
            "'utf-8' codec can't decode byte 0xff in position 19",
        ),
    ),
) -> Path:
    """Write an adversarial-channel discover_NNN.json."""

    discover_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel": "adversarial",
        "summary": {
            "parsers": ["ccd.protocol.parse_spec"],
            "cases_total": 18,
            "evaluations_total": 72,
            "graceful_total": 71,
            "ungraceful_total": len(findings),
            "graceful_by_parser": {"ccd.protocol.parse_spec": 17},
            "ungraceful_by_parser": {"ccd.protocol.parse_spec": len(findings)},
            "ungraceful_by_exception_type": {"UnicodeDecodeError": len(findings)},
        },
        "findings": [
            {
                "parser": parser,
                "case": case,
                "exception_type": exc_type,
                "exception_message": exc_msg,
            }
            for parser, case, exc_type, exc_msg in findings
        ],
        "cases": [],
    }
    json_path = discover_dir / f"discover_{seq:03d}.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (discover_dir / f"discover_{seq:03d}.md").write_text(
        f"# discover_{seq:03d} — adversarial\n", encoding="utf-8"
    )
    return json_path


def _write_ai(
    discover_dir: Path,
    *,
    seq: int,
    findings: tuple[tuple[str, str, str, str], ...] = (
        (
            "dispatch-unchecked-cwd",
            "ccd/dispatch.py:42",
            "The cwd argument is taken on trust from the caller",
            "If a caller passes a relative cwd the subprocess sees the wrong dir.",
        ),
    ),
) -> Path:
    """Write an AI-channel discover_NNN.json."""

    discover_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel": "ai",
        "report_only": True,
        "non_deterministic": True,
        "summary": {
            "target_package": "ccd",
            "files_reviewed": ["ccd/dispatch.py", "ccd/cli.py"],
            "files_total": 2,
            "findings_total": len(findings),
        },
        "findings": [
            {
                "slug": slug,
                "location": location,
                "concern": concern,
                "why_risky": why,
                "source_file": f"_ai_workspace/discover/ai_review/findings_{seq:03d}/{slug}.md",
            }
            for slug, location, concern, why in findings
        ],
    }
    json_path = discover_dir / f"discover_{seq:03d}.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (discover_dir / f"discover_{seq:03d}.md").write_text(
        f"# discover_{seq:03d} — ai\n", encoding="utf-8"
    )
    return json_path


@pytest.fixture
def repo_with_all_three(tmp_path: Path) -> Path:
    """A repo whose _ai_workspace/discover/ has one report per channel."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    _write_adversarial(discover, seq=2)
    _write_ai(discover, seq=3)
    return tmp_path


# --------------------------------------------------------------------------- #
# Core: run_brief renders the 6 sections from all three channels.
# --------------------------------------------------------------------------- #


def test_run_brief_returns_result_with_report_path(repo_with_all_three: Path) -> None:
    result = run_brief(
        repo=repo_with_all_three,
        today=date(2026, 5, 24),
    )

    assert isinstance(result, BriefResult)
    assert result.success is True
    assert result.report_path is not None
    assert result.report_path.name == "report_2026-05-24.md"
    assert result.report_path.parent.name == "nightly"


def test_report_contains_all_six_sections(repo_with_all_three: Path) -> None:
    """spec_017 §2-2 — sections A〜F must all be present."""

    result = run_brief(
        repo=repo_with_all_three,
        today=date(2026, 5, 24),
    )

    md = result.report_path.read_text(encoding="utf-8")
    assert "## A. 一行判定" in md
    assert "## B. 機械的チャンネルの発見" in md
    assert "## C. AI推論の所見" in md
    # D may or may not appear depending on content; here all channels
    # present + no halt_reason → D should be absent. Section E is always
    # present, section F is always present.
    assert "## E. バックログ・推移" in md
    assert "## F. 起きなかったこと" in md


def test_report_mechanical_findings_are_listed_with_location(
    repo_with_all_three: Path,
) -> None:
    """spec_017 §2-2 §B — actionable mutation lines and adversarial
    ungraceful pairs must surface with file:line / parser × case."""

    result = run_brief(
        repo=repo_with_all_three,
        today=date(2026, 5, 24),
    )

    md = result.report_path.read_text(encoding="utf-8")
    # Mutation actionable: file:line — mutation description.
    assert "ccd/dispatch.py:125" in md
    assert "check=False, → check=True," in md
    # Adversarial ungraceful: parser × case — exception type + message.
    assert "ccd.protocol.parse_spec" in md
    assert "05_invalid_utf8_bytes" in md
    assert "UnicodeDecodeError" in md


def test_report_ai_section_marks_report_only_and_distinguishes_from_b(
    repo_with_all_three: Path,
) -> None:
    """spec_017 §2-2 — §C must visually distinguish itself from §B and
    explicitly say "報告専用" / "主張" / "事実ではない"."""

    result = run_brief(
        repo=repo_with_all_three,
        today=date(2026, 5, 24),
    )

    md = result.report_path.read_text(encoding="utf-8")
    # Split by section headings; check the AI section.
    pre_c, _, rest = md.partition("## C. AI推論の所見")
    assert "報告専用" in rest.split("## ", 1)[0]
    assert "主張" in rest.split("## ", 1)[0]
    # The AI finding itself shows up in C.
    assert "dispatch-unchecked-cwd" in rest
    # Mechanical findings sit in B (before C).
    assert "ccd/dispatch.py:125" in pre_c
    # AI section explicitly says "人間判断" and "自律修正の引き金にはしない".
    assert "人間判断" in rest.split("## ", 1)[0]
    assert "自律修正の引き金にはしない" in rest.split("## ", 1)[0]


def test_report_section_f_states_phase_1_does_not_auto_fix(
    repo_with_all_three: Path,
) -> None:
    """spec_017 §2-2 §F — Phase 1 honesty: no autonomous fixes ever ran."""

    result = run_brief(
        repo=repo_with_all_three,
        today=date(2026, 5, 24),
    )

    md = result.report_path.read_text(encoding="utf-8")
    _, _, after_f = md.partition("## F. 起きなかったこと")
    assert "Phase 1 は自律修正していない" in after_f


# --------------------------------------------------------------------------- #
# Determinism + summary.
# --------------------------------------------------------------------------- #


def test_summary_is_deterministic(repo_with_all_three: Path) -> None:
    """Same inputs → same BriefSummary numbers (spec_017 §2-4 spirit)."""

    r1 = run_brief(repo=repo_with_all_three, today=date(2026, 5, 24))
    r2 = run_brief(repo=repo_with_all_three, today=date(2026, 5, 24))

    assert isinstance(r1.summary, BriefSummary)
    assert r1.summary == r2.summary
    assert r1.summary.mutation_actionable == 2
    assert r1.summary.adversarial_ungraceful == 1
    assert r1.summary.ai_findings == 1
    assert r1.summary.mechanical_findings_total == 3
    assert r1.summary.channels_picked == (
        CHANNEL_MUTATION,
        CHANNEL_ADVERSARIAL,
        CHANNEL_AI,
    )
    assert r1.summary.channels_missing == ()


def test_report_filename_uses_today_argument(tmp_path: Path) -> None:
    """The `today` injection makes the filename deterministic."""

    repo = tmp_path
    (repo / "_ai_workspace" / "discover").mkdir(parents=True)
    result = run_brief(repo=repo, today=date(2027, 1, 1))
    assert result.report_path.name == "report_2027-01-01.md"


# --------------------------------------------------------------------------- #
# Graceful: missing channels, zero findings.
# --------------------------------------------------------------------------- #


def test_missing_channels_are_graceful(tmp_path: Path) -> None:
    """spec_017 §2-1 — a channel with no report is recorded as 未実行,
    not a crash."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    # No adversarial or ai report — only mutation present.

    result = run_brief(repo=tmp_path, today=date(2026, 5, 24))

    assert result.success is True
    assert set(result.summary.channels_picked) == {CHANNEL_MUTATION}
    assert set(result.summary.channels_missing) == {
        CHANNEL_ADVERSARIAL,
        CHANNEL_AI,
    }
    md = result.report_path.read_text(encoding="utf-8")
    # Missing channels should surface in §D (halt / skip) AND in §F honesty.
    assert "## D. halt・スキップ項目" in md
    assert "未実行" in md
    # The honesty section still anchors the Phase-1 invariant.
    assert "Phase 1 は自律修正していない" in md


def test_all_channels_missing_is_graceful(tmp_path: Path) -> None:
    """Even with zero discover_NNN files, the brief renders something
    honest rather than crashing."""

    (tmp_path / "_ai_workspace" / "discover").mkdir(parents=True)

    result = run_brief(repo=tmp_path, today=date(2026, 5, 24))

    assert result.success is True
    assert result.summary.channels_picked == ()
    assert result.summary.channels_missing == (
        CHANNEL_MUTATION,
        CHANNEL_ADVERSARIAL,
        CHANNEL_AI,
    )
    md = result.report_path.read_text(encoding="utf-8")
    # Section A says "発見なし" or equivalent.
    assert "発見なし" in md or "未実行" in md
    # F still anchors the Phase-1 invariant.
    assert "Phase 1 は自律修正していない" in md


def test_zero_findings_per_channel_is_concise(tmp_path: Path) -> None:
    """All channels ran but each surfaced zero findings — report stays
    concise and admits the empty result honestly (spec_017 §2-4)."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1, actionable=())
    _write_adversarial(discover, seq=2, findings=())
    _write_ai(discover, seq=3, findings=())

    result = run_brief(repo=tmp_path, today=date(2026, 5, 24))

    assert result.summary.mechanical_findings_total == 0
    assert result.summary.ai_findings == 0
    md = result.report_path.read_text(encoding="utf-8")
    # Three "ゼロ件 / 発見なし" markers — one per channel — appear in B/C.
    assert md.count("発見なし") + md.count("ゼロ件") >= 2


# --------------------------------------------------------------------------- #
# Channel attribution (mutation has no `channel` key — detect by shape).
# --------------------------------------------------------------------------- #


def test_mutation_channel_detected_without_channel_field(tmp_path: Path) -> None:
    """spec_013's discover_NNN.json predates the explicit `channel` field;
    the brief still attributes it correctly by shape."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)

    result = run_brief(repo=tmp_path, today=date(2026, 5, 24))

    assert result.summary.channels_picked == (CHANNEL_MUTATION,)


def test_latest_per_channel_is_picked(tmp_path: Path) -> None:
    """When multiple reports exist for the same channel, only the
    highest-seq one is consumed (spec_017 §2-1)."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(
        discover,
        seq=1,
        actionable=(("ccd/old.py", 10, "old → mutation"),),
    )
    _write_mutation(
        discover,
        seq=4,
        actionable=(("ccd/new.py", 99, "new → mutation"),),
    )

    result = run_brief(repo=tmp_path, today=date(2026, 5, 24))

    md = result.report_path.read_text(encoding="utf-8")
    assert "ccd/new.py:99" in md
    assert "ccd/old.py:10" not in md
    # Only the latest is in the picked channel list, with seq=4.
    picked_seqs = [c.seq for c in result.channels]
    assert picked_seqs == [4]


# --------------------------------------------------------------------------- #
# Explicit `inputs` argument (test seam).
# --------------------------------------------------------------------------- #


def test_explicit_inputs_override_auto_collection(tmp_path: Path) -> None:
    """``inputs=`` lets tests pass pre-baked JSON paths bypassing the
    discover/ scan."""

    # Build two discover JSONs in a sibling directory the auto-collector
    # would never see (no _ai_workspace/discover/ at all).
    sandbox = tmp_path / "elsewhere"
    sandbox.mkdir()
    mut = _write_mutation(sandbox, seq=7)
    ai = _write_ai(sandbox, seq=9)

    # Empty real discover dir — auto-collection would pick nothing up.
    (tmp_path / "_ai_workspace" / "discover").mkdir(parents=True)

    result = run_brief(
        repo=tmp_path,
        inputs=[mut, ai],
        today=date(2026, 5, 24),
    )

    assert set(c.channel for c in result.channels) == {
        CHANNEL_MUTATION,
        CHANNEL_AI,
    }
    assert CHANNEL_ADVERSARIAL in result.summary.channels_missing


# --------------------------------------------------------------------------- #
# CLI end-to-end.
# --------------------------------------------------------------------------- #


def test_cli_brief_end_to_end(
    repo_with_all_three: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ccd brief --repo <repo>` writes the morning report and prints the
    factual summary."""

    rc = cli.main(["brief", "--repo", str(repo_with_all_three)])

    assert rc == 0
    captured = capsys.readouterr()
    assert "morning report:" in captured.out
    assert "factual summary:" in captured.out
    assert "mechanical=3" in captured.out
    assert "mutation=2" in captured.out
    assert "adversarial=1" in captured.out
    assert "ai=1" in captured.out
    # The report file was actually written.
    report = repo_with_all_three / "_ai_workspace" / "nightly"
    assert any(p.name.startswith("report_") for p in report.iterdir())


def test_cli_brief_with_missing_channels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)

    rc = cli.main(["brief", "--repo", str(tmp_path)])

    assert rc == 0
    captured = capsys.readouterr()
    assert "channels not yet executed:" in captured.out
    assert "adversarial" in captured.out
    assert "ai" in captured.out


def test_cli_brief_inputs_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ccd brief --inputs <json>...` accepts explicit JSON paths."""

    sandbox = tmp_path / "elsewhere"
    sandbox.mkdir()
    mut = _write_mutation(sandbox, seq=1)
    adv = _write_adversarial(sandbox, seq=2)

    rc = cli.main(
        [
            "brief",
            "--repo",
            str(tmp_path),
            "--inputs",
            str(mut),
            str(adv),
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "morning report:" in captured.out
    # ai missing because we didn't pass it.
    assert "channels not yet executed:" in captured.out
    assert "ai" in captured.out


# --------------------------------------------------------------------------- #
# spec_025 — §B Phase 2 upgrade (autonomous-fix narrative)
# --------------------------------------------------------------------------- #


def _make_merged_auto_fix(
    *,
    template: str = "A",
    merge_diff: str = (
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        "+++ added reproducer\n"
    ),
):
    """Build an :class:`AutoFixOutcome` representing a merged fix."""

    from ccd.nightly import AutoFixOutcome

    return AutoFixOutcome(
        skipped=False,
        spec_auto_id="spec_auto_001",
        spec_auto_path=Path("/tmp/spec_auto_001.md"),  # noqa: S108
        finding_signature="ccd/protocol.py:46:x == y → x != y",
        candidate_count=1,
        template=template,
        branch="auto/spec_auto_001",
        dispatched=True,
        dispatch_status="done",
        r5_killed=True,
        r4_suite_passed=True,
        guard_passed=True,
        merged=True,
        merge_diff=merge_diff,
    )


def test_section_b_phase2_rendered_when_auto_fix_merged(tmp_path: Path) -> None:
    """spec_025 §2-2 — when ``auto_fix.merged is True``, the brief
    replaces §B with the Phase 2 narrative (finding + diff + R-evidence
    + push command)."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    af = _make_merged_auto_fix(template="A")

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    # Header announces Phase 2.
    assert "Phase 2" in md
    # New §B header.
    assert "## B. 昨夜の自律修正" in md
    # The Phase 1 §B title is replaced (not merely appended).
    assert "## B. 機械的チャンネルの発見" not in md
    # Finding + spec_auto + branch surface.
    assert "ccd/protocol.py:46:x == y → x != y" in md
    assert "spec_auto_001" in md
    assert "auto/spec_auto_001" in md
    # R-evidence.
    assert "R5" in md and "pass" in md
    assert "R4" in md
    assert "ガード" in md
    # Diff embed.
    assert "```diff" in md
    assert "tests/test_protocol.py" in md
    # Push command — must be copy-pasteable.
    assert "git " in md
    assert "push origin main" in md


def test_section_b_phase2_push_command_includes_repo_path(
    tmp_path: Path,
) -> None:
    """The push command embeds the absolute repo path so the operator
    can paste it from any shell."""

    af = _make_merged_auto_fix()
    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    # The push command embeds the repo via -C <abs_path>.
    assert f"git -C {tmp_path.resolve()} push origin main" in md


def test_section_b_stays_phase1_when_auto_fix_is_none(tmp_path: Path) -> None:
    """No auto_fix → Phase 1 §B (existing behavior preserved)."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)

    result = run_brief(repo=tmp_path, today=date(2026, 5, 25))
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    assert "## B. 機械的チャンネルの発見" in md
    assert "## B. 昨夜の自律修正" not in md
    assert "Phase 1" in md
    # No push command appears anywhere in the brief.
    assert "push origin main" not in md


def test_section_b_stays_phase1_when_auto_fix_skipped(tmp_path: Path) -> None:
    """Skipped fix (no candidate) → Phase 1 §B; §D surfaces the skip."""

    from ccd.nightly import AutoFixOutcome

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    af = AutoFixOutcome(
        skipped=True,
        skip_reason="no template-A candidate available",
    )

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    assert "## B. 機械的チャンネルの発見" in md
    assert "## B. 昨夜の自律修正" not in md
    # Skip surfaces in §D.
    assert "自律修正 skipped" in md
    assert "no template-A candidate available" in md


def test_section_b_stays_phase1_when_auto_fix_halted(tmp_path: Path) -> None:
    """Loop ran but did NOT merge (HALT) → Phase 1 §B; §D surfaces HALT."""

    from ccd.nightly import AutoFixOutcome

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    af = AutoFixOutcome(
        skipped=False,
        spec_auto_id="spec_auto_001",
        finding_signature="ccd/x.py:1:a → b",
        template="A",
        branch="auto/spec_auto_001",
        dispatched=True,
        dispatch_status="done",
        merged=False,
        halt_reason="R5 failed: target mutation not killed",
    )

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    assert "## B. 機械的チャンネルの発見" in md
    assert "## B. 昨夜の自律修正" not in md
    assert "自律修正 HALT" in md
    assert "R5 failed" in md


def test_section_d_warns_on_added_slow_marker(tmp_path: Path) -> None:
    """spec_048 §2-3 / §3-4 — a merged fix whose diff purely ADDS
    @pytest.mark.slow surfaces a one-line warning in §D (the test it tagged
    drops from the `-m "not slow"` mutation subset). It is safe-side, so the
    fix still MERGES — the §B happy-path narrative is unchanged and no HALT
    occurs; §D merely adds the observation."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    slow_diff = (
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        "--- a/tests/test_protocol.py\n"
        "+++ b/tests/test_protocol.py\n"
        "@@ -10,3 +10,6 @@ def test_existing():\n"
        "     assert parse('X') == 'x'\n"
        "+\n"
        "+@pytest.mark.slow\n"
        "+def test_new_heavy_case():\n"
    )
    af = _make_merged_auto_fix(merge_diff=slow_diff)
    result = run_brief(repo=tmp_path, today=date(2026, 5, 25), auto_fix=af)
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    # Still a merged happy-path (no HALT), §B is the Phase 2 narrative.
    assert "## B. 昨夜の自律修正" in md
    assert "自律修正 HALT" not in md
    # §D carries the non-halting observation.
    assert "mutation サブセット縮小" in md
    assert "@pytest.mark.slow" in md


def test_section_d_no_slow_warning_without_marker(tmp_path: Path) -> None:
    """A merged fix with an ordinary diff (no slow marker) gets no §D
    warning — the observation only fires on a real純追加."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    af = _make_merged_auto_fix()  # default diff has no slow marker
    result = run_brief(repo=tmp_path, today=date(2026, 5, 25), auto_fix=af)
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    assert "mutation サブセット縮小" not in md


def test_section_b_phase2_diff_truncation(tmp_path: Path) -> None:
    """A pathologically large diff is truncated with an explanatory
    footer so the morning report doesn't balloon."""

    from ccd.brief import _PHASE2_DIFF_CAP

    huge_diff = "diff --git a/x.py b/x.py\n" + ("+x = 1\n" * 20000)
    assert len(huge_diff) > _PHASE2_DIFF_CAP

    af = _make_merged_auto_fix(merge_diff=huge_diff)
    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    assert "切り詰めました" in md
    assert "## B. 昨夜の自律修正" in md


def test_section_b_phase2_template_b_label(tmp_path: Path) -> None:
    """Template B's Phase 2 §B describes a production-fix, not test-only."""

    af = _make_merged_auto_fix(template="B")
    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    assert "テンプレ B" in md
    assert "本番修正" in md
    # The R5 label is the template-B variant ("graceful error").
    assert "graceful error" in md


def test_section_a_surfaces_auto_fix_merged_headline(tmp_path: Path) -> None:
    """§A's one-line judgment includes the auto-fix headline when
    merged (so the operator sees it without scrolling)."""

    af = _make_merged_auto_fix()
    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    # Locate §A — it ends before "## B." starts.
    section_a = md.split("## B.", 1)[0]
    assert "昨夜の自律修正" in section_a
    assert "spec_auto_001" in section_a


def test_section_a_zero_finding_normal_note(tmp_path: Path) -> None:
    """spec_025 §2-1(d) — zero findings should render the friendly
    "今夜は何もなし — エラーではない" note rather than an error."""

    # No discover JSON at all → all three channels missing.
    result = run_brief(repo=tmp_path, today=date(2026, 5, 25))
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    assert "発見なし" in md
    assert "今夜は何もなし" in md


# --------------------------------------------------------------------------- #
# spec_028 — §B propose variant + §D rejected fallback
# --------------------------------------------------------------------------- #


def _make_proposed_auto_fix(
    *,
    template: str = "A",
    proposal_diff: str = (
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        "+++ added reproducer\n"
    ),
    proposal_patch_path: Path | None = None,
):
    """Build an :class:`AutoFixOutcome` representing a propose mode
    success (verified diff, patch file path set)."""

    from ccd.nightly import AutoFixOutcome

    return AutoFixOutcome(
        skipped=False,
        spec_auto_id="spec_auto_001",
        spec_auto_path=Path("/tmp/spec_auto_001.md"),  # noqa: S108
        finding_signature="ccd/protocol.py:46:x == y → x != y",
        candidate_count=1,
        template=template,
        branch="propose/spec_auto_001",
        dispatched=True,
        dispatch_status="done",
        r5_killed=True,
        r4_suite_passed=True,
        guard_passed=True,
        merged=False,
        halt_reason="",
        mode="propose",
        proposed=True,
        proposal_patch_path=proposal_patch_path
        or Path("/tmp/proposal_2026-05-25_spec_auto_001.patch"),  # noqa: S108
        proposal_diff=proposal_diff,
    )


def _make_rejected_proposal(
    *,
    template: str = "A",
    halt_reason: str = (
        "proposal guard halted: R1: tests/sneaky.py is not allowed"
    ),
):
    """Build an :class:`AutoFixOutcome` for a propose loop that ran
    but verification/guard rejected the candidate."""

    from ccd.nightly import AutoFixOutcome

    return AutoFixOutcome(
        skipped=False,
        spec_auto_id="spec_auto_002",
        spec_auto_path=Path("/tmp/spec_auto_002.md"),  # noqa: S108
        finding_signature="ccd/protocol.py:46:x == y → x != y",
        candidate_count=1,
        template=template,
        branch="propose/spec_auto_002",
        dispatched=True,
        dispatch_status="done",
        r5_killed=False,
        r4_suite_passed=True,
        guard_passed=False,
        guard_halt_reasons=("R1: tests/sneaky.py is not allowed",),
        merged=False,
        halt_reason=halt_reason,
        mode="propose",
        proposed=False,
    )


def test_section_b_propose_rendered_when_proposed(tmp_path: Path) -> None:
    """spec_028 §2-3 — proposal landed → §B switches to propose
    variant (diff + R-evidence + git apply ワンライナー + patch path)."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    patch_path = tmp_path / "_ai_workspace" / "nightly" / "proposals" / (
        "proposal_2026-05-25_spec_auto_001.patch"
    )
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(
        "diff --git a/tests/test_protocol.py b/tests/test_protocol.py\n"
        "+++ added reproducer\n",
        encoding="utf-8",
    )
    af = _make_proposed_auto_fix(proposal_patch_path=patch_path)

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    # Header announces propose mode.
    assert "提案モード" in md
    # Propose §B header (not the Phase 2 auto §B, not the Phase 1 §B).
    assert "## B. 昨夜の修正案" in md
    assert "## B. 機械的チャンネルの発見" not in md
    assert "## B. 昨夜の自律修正" not in md
    # Diff embedded.
    assert "```diff" in md
    assert "tests/test_protocol.py" in md
    # R-evidence with "in clone" qualifier so the reader knows it's
    # the disposable workspace.
    assert "R5" in md
    assert "R4" in md
    assert "ガード" in md
    # The body announces "git apply" (NOT "git push") — propose mode
    # never merges.
    assert "git apply" in md
    assert "push origin main" not in md
    # Patch path is surfaced.
    assert str(patch_path) in md or patch_path.name in md
    # The §F honesty section pins "merge / commit / push のいずれも実行
    # していない".
    assert "merge" in md and "実行していない" in md


def test_section_b_propose_includes_apply_command_with_repo(
    tmp_path: Path,
) -> None:
    """The git apply ワンライナー embeds the absolute repo path so the
    operator can paste from any shell."""

    patch_path = (
        tmp_path
        / "_ai_workspace"
        / "nightly"
        / "proposals"
        / "proposal_2026-05-25_spec_auto_001.patch"
    )
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
    af = _make_proposed_auto_fix(proposal_patch_path=patch_path)

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    assert f"git -C {tmp_path.resolve()} apply {patch_path}" in md


def test_section_d_includes_rejected_proposal_one_liner(
    tmp_path: Path,
) -> None:
    """spec_028 §2-3 — when propose generated a candidate but
    verification/guard rejected it, §B stays Phase 1 (no unverified
    diff in the body) and §D gets a one-line note."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    af = _make_rejected_proposal()

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    # §B is NOT the propose-§B body (no diff embed in §B).
    assert "## B. 昨夜の修正案" not in md
    # Phase 1 §B is back.
    assert "## B. 機械的チャンネルの発見" in md
    # §D carries the rejected-proposal note.
    assert "## D. halt・スキップ項目" in md
    assert "提案モード rejected" in md
    assert "R1: tests/sneaky.py" in md
    # The unverified diff itself is NEVER in the body (the rejected
    # AutoFixOutcome doesn't even carry one — defend in depth).
    assert "```diff" not in md


def test_section_a_surfaces_propose_headline(tmp_path: Path) -> None:
    """§A's one-line judgment should include the propose-mode headline
    when a proposal landed (so the operator sees it without scrolling)."""

    patch_path = (
        tmp_path
        / "_ai_workspace"
        / "nightly"
        / "proposals"
        / "proposal_2026-05-25_spec_auto_001.patch"
    )
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
    af = _make_proposed_auto_fix(proposal_patch_path=patch_path)

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    section_a = md.split("## B.", 1)[0]
    assert "修正案" in section_a
    assert "spec_auto_001" in section_a


def test_phase2_auto_brief_unchanged_by_spec_028(tmp_path: Path) -> None:
    """spec_028 §2-3 / §4 — the auto-mode Phase 2 §B is unchanged.
    A merged AutoFixOutcome must still render the existing Phase 2 §B
    (with ``git push``), not the propose variant."""

    discover = tmp_path / "_ai_workspace" / "discover"
    _write_mutation(discover, seq=1)
    af = _make_merged_auto_fix()
    # The propose-mode fields should be at their defaults for an
    # auto-mode AutoFixOutcome (mode="auto", proposed=False).
    assert af.mode == "auto"
    assert af.proposed is False

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 25),
        auto_fix=af,
    )
    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")

    # Phase 2 auto §B header (spec_025), NOT propose §B.
    assert "## B. 昨夜の自律修正" in md
    assert "## B. 昨夜の修正案" not in md
    assert "push origin main" in md
    assert "git apply" not in md


def _extract_section(md: str, header: str) -> list[str]:
    """Return the lines of one ``## <header>`` section (until the next
    ``## `` header). Helper for spec_030 §D-only assertions — keeps
    them from accidentally matching lines in §A / §F."""

    lines = md.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith(header):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            out.append(line)
    return out


# --------------------------------------------------------------------------- #
# spec_030 — §D surfaces channel-level HALT / skip via channel_outcomes
# --------------------------------------------------------------------------- #


def test_section_d_surfaces_mutation_zero_mutants_halt(tmp_path: Path) -> None:
    """spec_030 §2-4 — when the mutation channel emits a 0-mutants HALT
    (silent-failure detection: 0 mutants for non-empty targets), §D
    must surface the halt reason verbatim instead of the
    indistinguishable "未実行" fallback line."""

    from ccd.nightly import ChannelOutcome

    outcomes = (
        ChannelOutcome(
            channel="mutation",
            success=False,
            halt_reason=(
                "mutation setup likely failed: 0 mutants generated for "
                "non-empty targets ['backend/src/_decay.py']. "
                "Possible causes: iso-venv dependency install error, ..."
            ),
            report_md_path=None,
            report_json_path=None,
        ),
    )
    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 27),
        channel_outcomes=outcomes,
    )

    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    assert "## D. halt・スキップ項目" in md
    assert "0 mutants generated for non-empty targets" in md
    # Within §D specifically: mutation surfaces as a halt line, not as
    # the indistinguishable "未実行" fallback. §A still mentions
    # "一部チャンネル未実行" (some channels not run) — that's a
    # different layer; we only assert §D here.
    section_d = _extract_section(md, "## D.")
    mutation_lines = [line for line in section_d if "ミューテーション" in line]
    assert any("halt" in line for line in mutation_lines)
    assert not any("未実行" in line for line in mutation_lines)


def test_section_d_surfaces_adversarial_skipped(tmp_path: Path) -> None:
    """spec_030 §2-4 — when the sweep skips the adversarial channel
    (profile has ``"adversarial"`` in channels but no
    ``[discovery.adversarial.parsers]``), §D shows the skip reason
    verbatim — the operator sees that the channel was deliberately
    not run, not silently absent."""

    from ccd.nightly import ChannelOutcome

    outcomes = (
        ChannelOutcome(
            channel="adversarial",
            success=False,
            halt_reason=(
                "adversarial channel skipped: profile に "
                "[discovery.adversarial.parsers] が未設定 "
                "(CCD のパーサは走らせない — spec_030 §2-3)"
            ),
            report_md_path=None,
            report_json_path=None,
        ),
    )
    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 27),
        channel_outcomes=outcomes,
    )

    assert result.report_path is not None
    md = result.report_path.read_text(encoding="utf-8")
    assert "## D. halt・スキップ項目" in md
    assert "adversarial channel skipped" in md
    assert "[discovery.adversarial.parsers]" in md
    # Within §D: adversarial surfaces with the explicit halt reason,
    # not the generic "未実行" fallback.
    section_d = _extract_section(md, "## D.")
    adversarial_lines = [line for line in section_d if "敵対的入力" in line]
    assert any("halt" in line for line in adversarial_lines)
    assert not any("未実行" in line for line in adversarial_lines)


def test_section_a_includes_halt_count_when_channels_halted(tmp_path: Path) -> None:
    """spec_030 §2-4 — §A appends a ``HALT N 件`` count when channels
    are halted/skipped, so the operator notices silent failures at a
    glance instead of having to scroll to §D."""

    from ccd.nightly import ChannelOutcome

    outcomes = (
        ChannelOutcome(
            channel="mutation",
            success=False,
            halt_reason="mutation setup likely failed: 0 mutants ...",
            report_md_path=None,
            report_json_path=None,
        ),
        ChannelOutcome(
            channel="adversarial",
            success=False,
            halt_reason="adversarial channel skipped: ...",
            report_md_path=None,
            report_json_path=None,
        ),
    )
    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 27),
        channel_outcomes=outcomes,
    )

    md = result.report_path.read_text(encoding="utf-8")
    # §A header followed by a HALT count line.
    assert "## A. 一行判定" in md
    assert "HALT 2 件" in md


def test_section_a_omits_halt_count_when_no_halts(tmp_path: Path) -> None:
    """spec_030 — when no channel halted/skipped, §A does NOT get the
    HALT line (avoid noise on clean nights)."""

    result = run_brief(
        repo=tmp_path,
        today=date(2026, 5, 27),
        channel_outcomes=(),
    )

    md = result.report_path.read_text(encoding="utf-8")
    assert "HALT" not in md


def test_existing_section_d_path_unchanged_when_no_channel_outcomes(
    tmp_path: Path,
) -> None:
    """spec_030 — backward compatibility. When ``channel_outcomes`` is
    omitted (single-CLI / legacy nightly path), §D behaves bit-for-bit
    as before: missing channels surface as "未実行" lines."""

    result = run_brief(repo=tmp_path, today=date(2026, 5, 27))

    md = result.report_path.read_text(encoding="utf-8")
    assert "## D. halt・スキップ項目" in md
    # All three channels missing — each shows the "未実行" fallback.
    assert md.count("未実行") >= 3
