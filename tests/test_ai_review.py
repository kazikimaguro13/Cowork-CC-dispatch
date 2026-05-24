"""Tests for `ccd/ai_review.py:run_ai_review` + `ccd discover --channel ai`.

The AI-inference channel is report-only and non-deterministic by design.
Tests inject :class:`FakeAgentRunner` so no real ``claude`` invocation
happens; the fake's ``side_effect`` writes the per-finding markdown files
just as a real agent would, and the channel collects + renders them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ccd import cli
from ccd.agent import FakeAgentRunner
from ccd.ai_review import (
    AI_REVIEW_SUBDIR,
    TARGET_PACKAGE,
    AIReviewFinding,
    AIReviewResult,
    AIReviewSummary,
    run_ai_review,
)
from ccd.discover import (
    CHANNEL_ADVERSARIAL,
    CHANNEL_AI,
    CHANNEL_MUTATION,
    DEFAULT_DISCOVER_DIR_REL,
    SUPPORTED_CHANNELS,
    FakeMutationRunner,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repo with a synthetic ``ccd/`` so reviews have targets."""

    pkg = tmp_path / TARGET_PACKAGE
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '0.0.0'\n", encoding="utf-8")
    (pkg / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (pkg / "beta.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    (tmp_path / "_ai_workspace").mkdir()
    return tmp_path


def _agent_writes_findings(
    *,
    findings: tuple[tuple[str, str, str, str], ...] = (
        (
            "dispatch-unchecked-cwd",
            "ccd/alpha.py:1",
            "alpha() returns a magic number without context",
            "The function name does not hint at the return value; callers may "
            "misinterpret. Consider documenting or renaming.",
        ),
        (
            "beta-name-mismatch",
            "ccd/beta.py:1",
            "beta() returns 2 but the name implies a state, not a constant",
            "The implementation drift between name and value is a typical "
            "source of confusion when callers read the signature only.",
        ),
    ),
) -> Callable[[object, Path], None]:
    """Build a side_effect that simulates the agent writing finding files.

    The agent is responsible for figuring out *where* the findings dir
    lives — in production that's parsed out of the review-task spec, but
    in tests we cheat: the spec_NNN body is deterministic, so the
    side_effect can locate the latest ``findings_NNN`` directory under
    ``_ai_workspace/discover/ai_review/`` and write into it.
    """

    def side_effect(spec: object, workdir: Path) -> None:  # noqa: ARG001
        ai_root = workdir / "_ai_workspace" / "discover" / AI_REVIEW_SUBDIR
        # The channel pre-created the latest findings_NNN before invoking
        # the agent — pick the highest-numbered one.
        finding_dirs = sorted(ai_root.glob("findings_*"))
        assert finding_dirs, (
            "channel must create findings dir before running the agent"
        )
        target = finding_dirs[-1]
        for slug, location, concern, why in findings:
            (target / f"{slug}.md").write_text(
                "# finding: "
                f"{slug}\n\n"
                f"- **Location**: `{location}`\n"
                f"- **Concern**: {concern}\n"
                f"- **Why risky**: {why}\n",
                encoding="utf-8",
            )

    return side_effect


# --------------------------------------------------------------------------- #
# Review-spec generation
# --------------------------------------------------------------------------- #


def test_review_spec_includes_constraints_and_target(repo: Path) -> None:
    """spec_016 §2-3: spec must encode evidence-anchor / no-fabrication /
    report-only constraints + the ccd/ target."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())

    result = run_ai_review(runner, repo=repo)

    assert result.review_spec_path is not None
    spec_text = result.review_spec_path.read_text(encoding="utf-8")
    # spec_016 §2-3 constraints — exact phrases the spec calls out.
    assert "証拠アンカー" in spec_text
    assert "捏造しない" in spec_text
    assert "報告のみ" in spec_text
    # Target is ccd/.
    assert "ccd/" in spec_text or "`ccd`" in spec_text
    # Output destination for findings is hard-coded into the prompt.
    assert "findings_001" in spec_text
    # Honesty about non-determinism is implied via the explicit
    # "zero-findings-is-OK" clause.
    assert "ゼロ件" in spec_text or "0 件" in spec_text


def test_review_spec_files_reviewed_list_appears_in_body(repo: Path) -> None:
    """The deterministic factual anchor (file list) is embedded so the
    agent must quote the same numbers."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings(findings=()))

    result = run_ai_review(runner, repo=repo)

    spec_text = result.review_spec_path.read_text(encoding="utf-8")
    # Every enumerated file appears verbatim.
    for f in result.summary.files_reviewed:
        assert f in spec_text


# --------------------------------------------------------------------------- #
# End-to-end: agent writes findings, channel collects them
# --------------------------------------------------------------------------- #


def test_run_ai_review_collects_findings_and_writes_report(repo: Path) -> None:
    runner = FakeAgentRunner(side_effect=_agent_writes_findings())

    result = run_ai_review(runner, repo=repo)

    assert isinstance(result, AIReviewResult)
    assert result.success is True
    assert result.runner_invoked is True
    assert result.report_md_path is not None
    assert result.report_json_path is not None
    assert result.report_md_path.name == "discover_001.md"
    assert result.report_json_path.name == "discover_001.json"
    assert len(result.findings) == 2

    # The agent's findings are surfaced.
    slugs = {f.slug for f in result.findings}
    assert slugs == {"dispatch-unchecked-cwd", "beta-name-mismatch"}


def test_report_md_marks_channel_as_report_only_and_non_deterministic(
    repo: Path,
) -> None:
    """spec_016 §2-2 / §2-4 — the report must be visually distinct from
    the mutation / adversarial reports and admit non-determinism."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())

    result = run_ai_review(runner, repo=repo)

    md = result.report_md_path.read_text(encoding="utf-8")
    # Report-only banner.
    assert "報告専用" in md
    # Subjective-claim disclaimer.
    assert "主張" in md and "事実" in md
    # Non-determinism disclaimer.
    assert "非決定的" in md
    # Channel name surfaced.
    assert "ai" in md.lower()


def test_report_json_carries_channel_and_findings(repo: Path) -> None:
    runner = FakeAgentRunner(side_effect=_agent_writes_findings())

    result = run_ai_review(runner, repo=repo)

    payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
    assert payload["channel"] == "ai"
    assert payload["report_only"] is True
    assert payload["non_deterministic"] is True
    assert payload["summary"]["target_package"] == TARGET_PACKAGE
    assert payload["summary"]["findings_total"] == 2
    assert len(payload["findings"]) == 2
    for f in payload["findings"]:
        assert {"slug", "location", "concern", "why_risky", "source_file"} <= set(
            f.keys()
        )


def test_findings_distinguished_from_mutation_and_adversarial_visually(
    repo: Path,
) -> None:
    """spec_016 §2-2 — the AI report must be visually distinguishable
    from the mutation / adversarial reports."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())
    result = run_ai_review(runner, repo=repo)
    md = result.report_md_path.read_text(encoding="utf-8")

    # The header explicitly contrasts with the other channels.
    assert "ミューテーション" in md
    assert "敵対的入力" in md
    # The contrast section calls out "report-only" vs "auto-fix".
    assert "自律修正" in md


# --------------------------------------------------------------------------- #
# Graceful zero-findings handling
# --------------------------------------------------------------------------- #


def test_zero_findings_is_graceful(repo: Path) -> None:
    """Agent producing no findings is a legitimate outcome — success."""

    runner = FakeAgentRunner(
        side_effect=_agent_writes_findings(findings=())
    )

    result = run_ai_review(runner, repo=repo)

    assert result.success is True
    assert result.runner_invoked is True
    assert result.findings == []
    assert result.report_md_path is not None
    md = result.report_md_path.read_text(encoding="utf-8")
    # Zero-findings is surfaced honestly (not papered over).
    assert "ゼロ件" in md or "0 件" in md or "該当なし" in md


def test_agent_writes_nothing_at_all_is_graceful(repo: Path) -> None:
    """Even an agent that produces no output at all yields a clean report
    (zero findings) — the channel does not require the agent to write."""

    # side_effect that intentionally does nothing.
    runner = FakeAgentRunner(side_effect=lambda spec, workdir: None)

    result = run_ai_review(runner, repo=repo)

    assert result.success is True
    assert result.runner_invoked is True
    assert result.findings == []


def test_no_target_files_halts_gracefully(tmp_path: Path) -> None:
    """A repo without a ``ccd/`` package halts without invoking the agent."""

    # No ccd/ directory at all.
    (tmp_path / "_ai_workspace").mkdir()

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())

    result = run_ai_review(runner, repo=tmp_path)

    assert result.success is False
    assert result.runner_invoked is False
    assert "no review target" in result.halt_reason
    # The review-task spec was still written so a human can see "what
    # would we have asked the agent" (same idea as retrospect's empty
    # evidence path).
    assert result.review_spec_path is not None
    assert result.review_spec_path.exists()
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# Per-finding markdown parsing
# --------------------------------------------------------------------------- #


def test_finding_without_location_is_surfaced_not_dropped(repo: Path) -> None:
    """Spec requires an evidence anchor — but if the agent disobeys we
    surface the broken finding so a human can see the leak."""

    def side_effect(spec: object, workdir: Path) -> None:  # noqa: ARG001
        ai_root = workdir / "_ai_workspace" / "discover" / AI_REVIEW_SUBDIR
        target = sorted(ai_root.glob("findings_*"))[-1]
        (target / "no-anchor.md").write_text(
            "# finding: no-anchor\n\n"
            "- **Concern**: a generic concern with no specific location\n"
            "- **Why risky**: vague hand-waving without code reference\n",
            encoding="utf-8",
        )

    runner = FakeAgentRunner(side_effect=side_effect)

    result = run_ai_review(runner, repo=repo)

    assert len(result.findings) == 1
    assert result.findings[0].location == "(unspecified)"
    # The §4 "判断できなかったこと" section flags the bad anchor count.
    md = result.report_md_path.read_text(encoding="utf-8")
    assert "アンカー欠落" in md or "(unspecified)" in md


def test_finding_with_multiline_why_risky_is_preserved(repo: Path) -> None:
    def side_effect(spec: object, workdir: Path) -> None:  # noqa: ARG001
        ai_root = workdir / "_ai_workspace" / "discover" / AI_REVIEW_SUBDIR
        target = sorted(ai_root.glob("findings_*"))[-1]
        (target / "multi.md").write_text(
            "# finding: multi\n\n"
            "- **Location**: `ccd/alpha.py:1`\n"
            "- **Concern**: complex thing\n"
            "- **Why risky**: first line of reasoning\n"
            "  second line continues the thought\n"
            "  third line wraps up\n",
            encoding="utf-8",
        )

    runner = FakeAgentRunner(side_effect=side_effect)

    result = run_ai_review(runner, repo=repo)

    assert len(result.findings) == 1
    why = result.findings[0].why_risky
    assert "first line of reasoning" in why
    assert "second line continues" in why
    assert "third line wraps up" in why


def test_findings_sorted_deterministically(repo: Path) -> None:
    """Findings are sorted by (location, slug) so the report is stable
    against agent output ordering."""

    runner = FakeAgentRunner(
        side_effect=_agent_writes_findings(
            findings=(
                (
                    "z-last",
                    "ccd/zeta.py:10",
                    "z concern",
                    "z why",
                ),
                (
                    "a-first",
                    "ccd/alpha.py:5",
                    "a concern",
                    "a why",
                ),
                (
                    "m-middle",
                    "ccd/mid.py:1",
                    "m concern",
                    "m why",
                ),
            )
        )
    )

    result = run_ai_review(runner, repo=repo)

    locations = [f.location for f in result.findings]
    assert locations == sorted(locations)


# --------------------------------------------------------------------------- #
# Discover-NNN numbering shared across all three channels
# --------------------------------------------------------------------------- #


def test_discover_numbering_shared_with_other_channels(repo: Path) -> None:
    """Mutation / adversarial / ai share the discover_NNN counter so a
    human reading ``_ai_workspace/discover/`` sees one chronological stream.
    """

    discover_dir = repo / DEFAULT_DISCOVER_DIR_REL
    discover_dir.mkdir(parents=True, exist_ok=True)
    (discover_dir / "discover_001.md").write_text("# preexisting\n", encoding="utf-8")
    (discover_dir / "discover_001.json").write_text("{}\n", encoding="utf-8")
    (discover_dir / "discover_002.md").write_text("# preexisting2\n", encoding="utf-8")

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())

    result = run_ai_review(runner, repo=repo)

    assert result.report_md_path is not None
    assert result.report_md_path.name == "discover_003.md"


# --------------------------------------------------------------------------- #
# Isolation — only the report + findings dir land in the live repo
# --------------------------------------------------------------------------- #


def test_findings_dir_created_under_discover_subtree(repo: Path) -> None:
    """The per-run findings dir lives under ``_ai_workspace/discover/ai_review/``."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())
    result = run_ai_review(runner, repo=repo)

    assert result.findings_dir is not None
    assert result.findings_dir.parent.name == AI_REVIEW_SUBDIR
    assert result.findings_dir.parent.parent == (repo / DEFAULT_DISCOVER_DIR_REL).resolve()
    assert result.findings_dir.exists()
    assert result.findings_dir.name == "findings_001"


# --------------------------------------------------------------------------- #
# CLI integration — ``ccd discover --channel ai``
# --------------------------------------------------------------------------- #


def test_ai_channel_in_supported_channels() -> None:
    assert CHANNEL_AI == "ai"
    assert CHANNEL_AI in SUPPORTED_CHANNELS


def test_cli_discover_channel_ai_end_to_end(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ccd discover --channel ai`` runs the AI channel via FakeAgentRunner."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())

    rc = cli.main(
        ["discover", "--repo", str(repo), "--channel", CHANNEL_AI],
        runner=runner,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "discovery report" in out
    assert "discover_001.md" in out
    assert "report-only" in out
    assert "factual summary" in out
    # AI summary uses ai-channel-specific keys.
    assert "files=" in out
    assert "findings=" in out
    assert "non-deterministic" in out

    md_path = repo / DEFAULT_DISCOVER_DIR_REL / "discover_001.md"
    json_path = repo / DEFAULT_DISCOVER_DIR_REL / "discover_001.json"
    assert md_path.exists()
    assert json_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["channel"] == "ai"


def test_cli_discover_default_channel_remains_mutation(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """spec_013 挙動 不変 — ``--channel`` omitted still means mutation."""

    runner = FakeMutationRunner(mutants=[])
    rc = cli.main(["discover", "--repo", str(repo)], mutation_runner=runner)
    assert rc == 0
    assert len(runner.calls) == 1
    out = capsys.readouterr().out
    assert "mutants=" in out


def test_cli_discover_channel_adversarial_still_works(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """spec_015 挙動 不変 — adversarial channel unaffected by AI addition."""

    rc = cli.main(
        ["discover", "--repo", str(repo), "--channel", CHANNEL_ADVERSARIAL]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "parsers=" in out
    assert "cases=" in out


def test_cli_discover_channel_mutation_explicit_still_works(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeMutationRunner(mutants=[])
    rc = cli.main(
        ["discover", "--repo", str(repo), "--channel", CHANNEL_MUTATION],
        mutation_runner=runner,
    )
    assert rc == 0
    assert len(runner.calls) == 1


def test_cli_discover_rejects_unknown_channel_after_ai_added(
    repo: Path,
) -> None:
    """argparse choices still rejects garbage even after `ai` was added."""

    with pytest.raises(SystemExit):
        cli.main(["discover", "--repo", str(repo), "--channel", "bogus"])


def test_cli_discover_ai_does_not_invoke_real_claude(repo: Path) -> None:
    """End-to-end via cli.main with a FakeAgentRunner — no real claude call."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())
    rc = cli.main(
        ["discover", "--repo", str(repo), "--channel", CHANNEL_AI],
        runner=runner,
    )
    assert rc == 0
    # The fake recorded one invocation.
    assert len(runner.calls) == 1
    spec_id, workdir, _feedback = runner.calls[0]
    assert spec_id.startswith("spec_ai_review_")
    assert workdir == repo


def test_cli_discover_paths_flag_ignored_for_ai(repo: Path) -> None:
    """``--paths`` is mutation-only; the AI channel ignores it."""

    runner = FakeAgentRunner(side_effect=_agent_writes_findings())
    rc = cli.main(
        [
            "discover",
            "--repo",
            str(repo),
            "--channel",
            CHANNEL_AI,
            "--paths",
            "ccd/alpha.py",
        ],
        runner=runner,
    )
    assert rc == 0


# --------------------------------------------------------------------------- #
# Dataclass shape
# --------------------------------------------------------------------------- #


def test_finding_dataclass_is_frozen() -> None:
    f = AIReviewFinding(
        slug="x",
        location="ccd/y.py:1",
        concern="z",
        why_risky="w",
        source_file="a/b.md",
    )
    with pytest.raises(FrozenInstanceError):
        f.slug = "other"  # type: ignore[misc]


def test_summary_dataclass_is_frozen() -> None:
    s = AIReviewSummary(
        target_package="ccd",
        files_reviewed=("ccd/x.py",),
        files_total=1,
        findings_total=0,
    )
    with pytest.raises(FrozenInstanceError):
        s.files_total = 99  # type: ignore[misc]
