"""Tests for `ccd/retrospect.py:run_retrospect` + `ccd retrospect` CLI.

The retrospective dogfoods ccd's `AgentRunner` abstraction: tests inject
`FakeAgentRunner` so no real `claude` invocation happens.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ccd import cli
from ccd.agent import FakeAgentRunner
from ccd.retrospect import (
    DEFAULT_LIMIT,
    Evidence,
    RetrospectResult,
    collect_evidence,
    run_retrospect,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / "_ai_workspace" / "bridge" / "inbox").mkdir(parents=True)
    (tmp_path / "_ai_workspace" / "bridge" / "outbox").mkdir(parents=True)
    (tmp_path / "_ai_workspace" / "runs").mkdir(parents=True)
    (tmp_path / "_ai_workspace" / "logs").mkdir(parents=True)
    (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "initial", cwd=tmp_path)
    return tmp_path


def _seed_run_json(repo: Path, name: str = "Cowork-CC-dispatch.json") -> Path:
    path = repo / "_ai_workspace" / "runs" / name
    payload = {
        "version": 1,
        "saved_at": "2026-05-22T00:00:00Z",
        "project": "Cowork-CC-dispatch",
        "generation": "ccd_native",
        "records": [
            {
                "spec_id": "spec_001",
                "started_at": "2026-05-22T00:00:00Z",
                "finished_at": "2026-05-22T00:01:00Z",
                "status": "done",
                "attempts": 1,
                "failure_category": None,
                "intervention": False,
            },
            {
                "spec_id": "spec_002",
                "started_at": "2026-05-22T00:02:00Z",
                "finished_at": "2026-05-22T00:03:00Z",
                "status": "failed",
                "attempts": 1,
                "failure_category": "agent_misread",
                "intervention": False,
            },
            {
                "spec_id": "spec_003",
                "started_at": "2026-05-22T00:04:00Z",
                "finished_at": "2026-05-22T00:05:00Z",
                "status": "done",
                "attempts": 2,
                "failure_category": None,
                "intervention": False,
            },
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _seed_result(repo: Path, n: str, status: str = "done") -> Path:
    path = repo / "_ai_workspace" / "bridge" / "outbox" / f"result_{n}.md"
    path.write_text(
        f"# result_{n}\n\n- **Spec**: spec_{n}\n- **Status**: {status}\n\nbody\n",
        encoding="utf-8",
    )
    return path


def _agent_writes_retro_and_proposal(
    retro_name: str = "retro_001.md",
    proposal_slugs: tuple[str, ...] = ("split-dispatch",),
) -> Callable[[object, Path], None]:
    """Build a side_effect that simulates the agent producing the retro files."""

    def side_effect(spec: object, workdir: Path) -> None:  # noqa: ARG001
        retro_dir = workdir / "_ai_workspace" / "retro"
        retro_dir.mkdir(parents=True, exist_ok=True)
        (retro_dir / retro_name).write_text(
            "# ccd retrospective 001\n\n"
            "## 評価母数\n\n- runs: 1, records: 3, results: 0, commits: 1\n\n"
            "## 観測した摩擦点\n\n- ...\n",
            encoding="utf-8",
        )
        proposals = retro_dir / "proposals"
        proposals.mkdir(exist_ok=True)
        for slug in proposal_slugs:
            (proposals / f"{slug}.md").write_text(
                f"# Proposal: {slug}\n\n根拠: spec_002 (agent_misread)\n",
                encoding="utf-8",
            )

    return side_effect


# --------------------------------------------------------------------------- #
# Evidence collection + factual summary
# --------------------------------------------------------------------------- #


def test_collect_evidence_gathers_runs_results_and_git_log(repo: Path) -> None:
    _seed_run_json(repo)
    _seed_result(repo, "001")
    _seed_result(repo, "002")

    ev = collect_evidence(repo=repo, limit=20)

    assert isinstance(ev, Evidence)
    assert len(ev.run_files) >= 1
    assert any(p.name == "Cowork-CC-dispatch.json" for p in ev.run_files)
    assert len(ev.result_files) == 2
    assert "## git log --oneline" in ev.git_log
    assert ev.summary.records_total == 3
    assert ev.summary.status_breakdown == {"done": 2, "failed": 1}
    assert ev.summary.failure_category_breakdown == {"agent_misread": 1}
    assert ev.summary.result_files == 2
    assert ev.summary.recent_commits >= 1


def test_collect_evidence_picks_up_legacy_logs_run_json(repo: Path) -> None:
    """Legacy bash-bridge era wrote run JSONs under _ai_workspace/logs/."""

    legacy = repo / "_ai_workspace" / "logs" / "spec_009_run.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "records": [
                    {
                        "spec_id": "spec_009",
                        "started_at": "2026-05-22T00:00:00Z",
                        "finished_at": "2026-05-22T00:01:00Z",
                        "status": "done",
                        "attempts": 1,
                        "failure_category": None,
                        "intervention": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    ev = collect_evidence(repo=repo)
    assert any(p.name == "spec_009_run.json" for p in ev.run_files)
    assert ev.summary.records_total == 1


def test_factual_summary_is_deterministic(repo: Path) -> None:
    """Same input → same numbers, every time."""

    _seed_run_json(repo)
    _seed_result(repo, "001")

    a = collect_evidence(repo=repo, limit=10).summary
    b = collect_evidence(repo=repo, limit=10).summary
    assert a == b
    assert a.records_total == 3
    assert a.result_files == 1


# --------------------------------------------------------------------------- #
# Review-spec generation
# --------------------------------------------------------------------------- #


def test_review_spec_includes_evidence_paths_and_constraints(repo: Path) -> None:
    _seed_run_json(repo)
    _seed_result(repo, "001")
    runner = FakeAgentRunner(side_effect=_agent_writes_retro_and_proposal())

    result = run_retrospect(runner, repo=repo)

    spec_text = result.review_spec_path.read_text(encoding="utf-8")
    # Evidence paths must appear so the agent can read them.
    assert "Cowork-CC-dispatch.json" in spec_text
    assert "result_001.md" in spec_text
    # Factual summary anchors are present.
    assert "records_total" not in spec_text  # not raw field name, but value text
    assert "records`" not in spec_text or "DispatchRecord" in spec_text
    assert "3" in spec_text  # records_total
    # Constraints from §3 of the spec must be in the body.
    assert "証拠アンカー" in spec_text
    assert "捏造しない" in spec_text
    assert "human-in-the-loop" in spec_text
    assert "フル spec を生成しない" in spec_text
    # Output destinations are specified.
    assert "_ai_workspace/retro/retro_001.md" in spec_text
    assert "_ai_workspace/retro/proposals" in spec_text


# --------------------------------------------------------------------------- #
# End-to-end: agent writes outputs, retrospect verifies
# --------------------------------------------------------------------------- #


def test_run_retrospect_succeeds_when_agent_writes_expected_files(
    repo: Path,
) -> None:
    _seed_run_json(repo)
    _seed_result(repo, "001")

    runner = FakeAgentRunner(
        side_effect=_agent_writes_retro_and_proposal(
            proposal_slugs=("split-dispatch", "feedback-clarity")
        )
    )

    result = run_retrospect(runner, repo=repo)

    assert isinstance(result, RetrospectResult)
    assert result.success is True
    assert result.retro_path is not None
    assert result.retro_path.name == "retro_001.md"
    assert {p.name for p in result.proposal_paths} == {
        "split-dispatch.md",
        "feedback-clarity.md",
    }
    # Runner was actually invoked.
    assert len(runner.calls) == 1
    assert runner.calls[0][0].startswith("spec_retro_")
    assert runner.calls[0][1] == repo


def test_proposals_written_in_proposals_subdir(repo: Path) -> None:
    """Each proposal goes to its own file under _ai_workspace/retro/proposals/."""

    _seed_run_json(repo)
    runner = FakeAgentRunner(
        side_effect=_agent_writes_retro_and_proposal(
            proposal_slugs=("a", "b", "c")
        )
    )

    result = run_retrospect(runner, repo=repo)

    assert result.success
    proposals_dir = repo / "_ai_workspace" / "retro" / "proposals"
    assert sorted(p.name for p in proposals_dir.glob("*.md")) == [
        "a.md",
        "b.md",
        "c.md",
    ]
    assert len(result.proposal_paths) == 3


def test_retro_number_increments_when_prior_retros_exist(repo: Path) -> None:
    """A second retrospect should write retro_002.md, not overwrite retro_001."""

    _seed_run_json(repo)
    (repo / "_ai_workspace" / "retro").mkdir(parents=True, exist_ok=True)
    (repo / "_ai_workspace" / "retro" / "retro_001.md").write_text(
        "# existing\n", encoding="utf-8"
    )

    runner = FakeAgentRunner(
        side_effect=_agent_writes_retro_and_proposal(retro_name="retro_002.md")
    )
    result = run_retrospect(runner, repo=repo)

    assert result.success
    assert result.retro_path is not None
    assert result.retro_path.name == "retro_002.md"
    # The pre-existing retro_001.md should not be touched.
    assert (repo / "_ai_workspace" / "retro" / "retro_001.md").read_text(
        encoding="utf-8"
    ) == "# existing\n"


def test_no_history_is_graceful(repo: Path) -> None:
    """Empty evidence → success=False with halt_reason; runner not invoked."""

    runner = FakeAgentRunner(
        side_effect=_agent_writes_retro_and_proposal()  # should not be called
    )

    result = run_retrospect(runner, repo=repo)

    assert result.success is False
    assert result.halt_reason  # non-empty
    assert "evidence" in result.halt_reason.lower()
    assert result.runner_invoked is False
    assert runner.calls == []  # the agent was never asked to do work
    # The review spec was still written — so a human can inspect "what would
    # we have asked".
    assert result.review_spec_path.exists()


def test_failure_when_agent_writes_no_retro_file(repo: Path) -> None:
    _seed_run_json(repo)

    # side_effect intentionally writes nothing.
    runner = FakeAgentRunner(side_effect=lambda s, w: None)

    result = run_retrospect(runner, repo=repo)

    assert result.success is False
    assert "did not write" in result.halt_reason
    assert result.runner_invoked is True


def test_failure_when_agent_writes_retro_but_no_proposals(repo: Path) -> None:
    _seed_run_json(repo)

    def only_retro(spec, workdir: Path) -> None:  # noqa: ARG001
        retro_dir = workdir / "_ai_workspace" / "retro"
        retro_dir.mkdir(parents=True, exist_ok=True)
        (retro_dir / "retro_001.md").write_text(
            "# retro without proposals\n", encoding="utf-8"
        )

    runner = FakeAgentRunner(side_effect=only_retro)

    result = run_retrospect(runner, repo=repo)

    assert result.success is False
    assert "no proposals" in result.halt_reason
    assert result.retro_path is not None  # we did observe the body
    assert result.proposal_paths == []


# --------------------------------------------------------------------------- #
# CLI integration
# --------------------------------------------------------------------------- #


def test_cli_retrospect_subcommand_writes_outputs_and_prints_paths(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_run_json(repo)
    _seed_result(repo, "001")

    runner = FakeAgentRunner(side_effect=_agent_writes_retro_and_proposal())

    rc = cli.main(["retrospect", "--repo", str(repo)], runner=runner)

    assert rc == 0
    out = capsys.readouterr().out
    assert "review spec" in out
    assert "retrospective" in out
    assert "retro_001.md" in out
    assert "proposal" in out
    assert "split-dispatch.md" in out


def test_cli_retrospect_returns_nonzero_when_evidence_missing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeAgentRunner()  # no side effect — should not even be called

    rc = cli.main(["retrospect", "--repo", str(repo)], runner=runner)

    assert rc == 1
    err = capsys.readouterr().err
    assert "halted" in err
    assert runner.calls == []


def test_cli_retrospect_accepts_runs_dir_and_limit_flags(repo: Path) -> None:
    """--runs-dir and --limit are wired through."""

    alt_runs = repo / "alt_runs"
    alt_runs.mkdir()
    (alt_runs / "alt.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": [
                    {
                        "spec_id": "spec_001",
                        "started_at": "2026-05-22T00:00:00Z",
                        "finished_at": "2026-05-22T00:01:00Z",
                        "status": "done",
                        "attempts": 1,
                        "failure_category": None,
                        "intervention": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    runner = FakeAgentRunner(side_effect=_agent_writes_retro_and_proposal())

    rc = cli.main(
        [
            "retrospect",
            "--repo",
            str(repo),
            "--runs-dir",
            str(alt_runs),
            "--limit",
            "5",
        ],
        runner=runner,
    )

    assert rc == 0


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


def test_default_limit_matches_module_constant() -> None:
    # Pinning the default so the CLI help and module agree.
    assert DEFAULT_LIMIT == 50
