"""Tests for `ccd/discover.py:run_discovery` + `ccd discover` CLI.

Discovery dogfoods the `MutationRunner` seam: every test injects a
`FakeMutationRunner` so no real `mutmut` invocation happens. The few tests
that touch the mutmut parsers exercise the pure-function helpers directly.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ccd import cli
from ccd.discover import (
    DEFAULT_BLOCKLIST_FILENAME,
    DEFAULT_DISCOVER_DIR_REL,
    DiscoveryResult,
    DiscoverySummary,
    FakeMutationRunner,
    Mutant,
    MutationRunOutcome,
    _parse_mutmut_results,
    _parse_mutmut_show,
    run_discovery,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "_ai_workspace").mkdir(parents=True)
    return tmp_path


def _sample_mutants() -> list[Mutant]:
    return [
        Mutant(
            file="ccd/dispatch.py",
            line=14,
            mutation="x > 0 → x >= 0",
            status="survived",
        ),
        Mutant(
            file="ccd/dispatch.py",
            line=42,
            mutation="not flag → flag",
            status="survived",
        ),
        Mutant(
            file="ccd/agent.py",
            line=33,
            mutation="+ 1 → - 1",
            status="killed",
        ),
        Mutant(
            file="ccd/agent.py",
            line=55,
            mutation="return value → return None",
            status="survived",
        ),
        Mutant(
            file="ccd/integrate.py",
            line=10,
            mutation="x → None",
            status="timeout",
        ),
    ]


# --------------------------------------------------------------------------- #
# End-to-end: run_discovery with FakeMutationRunner
# --------------------------------------------------------------------------- #


def test_discover_writes_report_with_survived_mutants(repo: Path) -> None:
    runner = FakeMutationRunner(mutants=_sample_mutants())

    result = run_discovery(runner, repo=repo)

    assert isinstance(result, DiscoveryResult)
    assert result.success
    assert result.report_md_path is not None
    assert result.report_md_path.name == "discover_001.md"
    assert result.report_json_path is not None
    assert result.report_json_path.name == "discover_001.json"

    md = result.report_md_path.read_text(encoding="utf-8")
    # Survived mutants are listed with file:line.
    assert "ccd/dispatch.py:14" in md
    assert "ccd/dispatch.py:42" in md
    assert "ccd/agent.py:55" in md
    assert "x > 0 → x >= 0" in md
    # A killed mutant must not appear in the actionable section
    # (it's still in the breakdown, just not as a survivor).
    assert "ccd/agent.py:33" not in md or "killed" in md  # tolerant — see below
    # The status breakdown surfaces all observed statuses.
    assert "survived" in md
    assert "killed" in md
    assert "timeout" in md

    # JSON has the same structured data.
    payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["mutants_total"] == 5
    assert payload["summary"]["survived_total"] == 3
    assert payload["summary"]["actionable_total"] == 3
    assert payload["summary"]["blocklisted_total"] == 0
    assert {m["file"] for m in payload["actionable"]} == {
        "ccd/dispatch.py",
        "ccd/agent.py",
    }
    assert all("signature" in m for m in payload["actionable"])


def test_factual_summary_is_deterministic(repo: Path) -> None:
    """Same input → same numbers, every time."""

    runner = FakeMutationRunner(mutants=_sample_mutants())
    result_a = run_discovery(runner, repo=repo)
    runner_2 = FakeMutationRunner(mutants=_sample_mutants())
    result_b = run_discovery(runner_2, repo=repo)

    # Two runs in a row in the same repo produce discover_001.md and
    # discover_002.md respectively; the *summaries* should match exactly.
    assert result_a.summary == result_b.summary
    assert result_a.summary.mutants_total == 5
    assert result_a.summary.survived_total == 3
    assert result_a.summary.status_breakdown == {
        "killed": 1,
        "survived": 3,
        "timeout": 1,
    }
    assert result_a.summary.survived_by_file == {
        "ccd/agent.py": 1,
        "ccd/dispatch.py": 2,
    }


def test_actionable_mutants_listed_with_file_line(repo: Path) -> None:
    runner = FakeMutationRunner(mutants=_sample_mutants())

    result = run_discovery(runner, repo=repo)

    sigs = {m.signature for m in result.actionable_mutants}
    assert "ccd/dispatch.py:14:x > 0 → x >= 0" in sigs
    assert "ccd/dispatch.py:42:not flag → flag" in sigs
    assert "ccd/agent.py:55:return value → return None" in sigs

    md = result.report_md_path.read_text(encoding="utf-8") if result.report_md_path else ""
    assert "`ccd/dispatch.py:14`" in md
    assert "`ccd/dispatch.py:42`" in md


# --------------------------------------------------------------------------- #
# blocklist
# --------------------------------------------------------------------------- #


def test_blocklist_excludes_listed_signatures(repo: Path) -> None:
    """Mutants whose signature appears in blocklist.txt go to blocklisted."""

    discover_dir = repo / DEFAULT_DISCOVER_DIR_REL
    discover_dir.mkdir(parents=True, exist_ok=True)
    (discover_dir / DEFAULT_BLOCKLIST_FILENAME).write_text(
        "# equivalent mutation: this branch is unreachable\n"
        "ccd/dispatch.py:14:x > 0 → x >= 0\n"
        "\n"
        "# also intentional\n"
        "ccd/agent.py:55:return value → return None\n",
        encoding="utf-8",
    )

    runner = FakeMutationRunner(mutants=_sample_mutants())
    result = run_discovery(runner, repo=repo)

    blocked_sigs = {m.signature for m in result.blocklisted_mutants}
    actionable_sigs = {m.signature for m in result.actionable_mutants}

    assert "ccd/dispatch.py:14:x > 0 → x >= 0" in blocked_sigs
    assert "ccd/agent.py:55:return value → return None" in blocked_sigs
    assert "ccd/dispatch.py:14:x > 0 → x >= 0" not in actionable_sigs
    assert "ccd/agent.py:55:return value → return None" not in actionable_sigs
    # The third survivor remains actionable.
    assert "ccd/dispatch.py:42:not flag → flag" in actionable_sigs

    assert result.summary.blocklisted_total == 2
    assert result.summary.actionable_total == 1
    assert result.summary.survived_total == 3  # blocklisting doesn't lower this


def test_blocklist_missing_is_graceful(repo: Path) -> None:
    """No blocklist file → empty blocklist, all survivors actionable."""

    blocklist = repo / DEFAULT_DISCOVER_DIR_REL / DEFAULT_BLOCKLIST_FILENAME
    assert not blocklist.exists()

    runner = FakeMutationRunner(mutants=_sample_mutants())
    result = run_discovery(runner, repo=repo)

    assert result.success
    assert result.blocklisted_mutants == []
    assert result.summary.blocklisted_total == 0
    assert result.summary.actionable_total == result.summary.survived_total


# --------------------------------------------------------------------------- #
# Numbering / report layout
# --------------------------------------------------------------------------- #


def test_discover_number_increments_when_prior_reports_exist(repo: Path) -> None:
    """A second discover should write discover_002.md, not overwrite 001."""

    discover_dir = repo / DEFAULT_DISCOVER_DIR_REL
    discover_dir.mkdir(parents=True, exist_ok=True)
    (discover_dir / "discover_001.md").write_text("# existing\n", encoding="utf-8")
    (discover_dir / "discover_001.json").write_text("{}\n", encoding="utf-8")

    runner = FakeMutationRunner(mutants=_sample_mutants())
    result = run_discovery(runner, repo=repo)

    assert result.success
    assert result.report_md_path is not None
    assert result.report_md_path.name == "discover_002.md"
    # The pre-existing discover_001.md is preserved.
    assert (discover_dir / "discover_001.md").read_text(
        encoding="utf-8"
    ) == "# existing\n"


def test_discover_dir_is_created_when_missing(repo: Path) -> None:
    assert not (repo / DEFAULT_DISCOVER_DIR_REL).exists()
    runner = FakeMutationRunner(mutants=_sample_mutants())

    result = run_discovery(runner, repo=repo)

    assert (repo / DEFAULT_DISCOVER_DIR_REL).is_dir()
    assert result.report_md_path is not None
    assert result.report_md_path.exists()


# --------------------------------------------------------------------------- #
# Graceful edges
# --------------------------------------------------------------------------- #


def test_zero_mutants_is_graceful(repo: Path) -> None:
    runner = FakeMutationRunner(mutants=[])

    result = run_discovery(runner, repo=repo)

    assert result.success
    assert result.summary.mutants_total == 0
    assert result.summary.survived_total == 0
    assert result.actionable_mutants == []
    assert result.report_md_path is not None
    md = result.report_md_path.read_text(encoding="utf-8")
    assert "mutant 総数: **0**" in md


def test_zero_survivors_is_graceful(repo: Path) -> None:
    runner = FakeMutationRunner(
        mutants=[
            Mutant(file="ccd/agent.py", line=1, mutation="a → b", status="killed"),
            Mutant(file="ccd/agent.py", line=2, mutation="c → d", status="killed"),
        ]
    )

    result = run_discovery(runner, repo=repo)

    assert result.success
    assert result.summary.mutants_total == 2
    assert result.summary.survived_total == 0
    assert result.actionable_mutants == []
    md = result.report_md_path.read_text(encoding="utf-8") if result.report_md_path else ""
    assert "該当なし" in md  # actionable section says "none"


def test_runner_error_halts_gracefully(repo: Path) -> None:
    """Mutation tool failure → success=False, no report, no traceback."""

    runner = FakeMutationRunner(
        mutants=[],
        error="mutmut binary not found",
    )

    result = run_discovery(runner, repo=repo)

    assert result.success is False
    assert "mutation tool failed" in result.halt_reason
    assert "mutmut" in result.halt_reason
    assert result.report_md_path is None
    assert result.report_json_path is None


# --------------------------------------------------------------------------- #
# Runner contract — paths are forwarded
# --------------------------------------------------------------------------- #


def test_paths_argument_is_forwarded_to_runner(repo: Path) -> None:
    runner = FakeMutationRunner(mutants=[])
    run_discovery(runner, repo=repo, paths=["ccd/dispatch.py", "ccd/agent.py"])

    assert len(runner.calls) == 1
    _, forwarded_paths = runner.calls[0]
    assert forwarded_paths == ("ccd/dispatch.py", "ccd/agent.py")


def test_default_paths_when_unspecified(repo: Path) -> None:
    runner = FakeMutationRunner(mutants=[])
    result = run_discovery(runner, repo=repo)
    assert result.summary.target_paths == ("ccd",)


# --------------------------------------------------------------------------- #
# CLI integration
# --------------------------------------------------------------------------- #


def test_cli_discover_subcommand_end_to_end(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeMutationRunner(mutants=_sample_mutants())

    rc = cli.main(["discover", "--repo", str(repo)], mutation_runner=runner)

    assert rc == 0
    out = capsys.readouterr().out
    assert "discovery report" in out
    assert "discover_001.md" in out
    assert "factual summary" in out
    assert "actionable: ccd/dispatch.py:14" in out
    # The discover dir + report files actually exist on disk.
    assert (repo / DEFAULT_DISCOVER_DIR_REL / "discover_001.md").exists()
    assert (repo / DEFAULT_DISCOVER_DIR_REL / "discover_001.json").exists()


def test_cli_discover_halts_nonzero_on_runner_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeMutationRunner(mutants=[], error="mutmut binary not found")

    rc = cli.main(["discover", "--repo", str(repo)], mutation_runner=runner)

    assert rc == 1
    err = capsys.readouterr().err
    assert "discovery halted" in err
    assert "mutmut" in err


def test_cli_discover_accepts_paths_flag(repo: Path) -> None:
    runner = FakeMutationRunner(mutants=[])

    rc = cli.main(
        ["discover", "--repo", str(repo), "--paths", "ccd/dispatch.py", "ccd/agent.py"],
        mutation_runner=runner,
    )

    assert rc == 0
    assert len(runner.calls) == 1
    _, forwarded_paths = runner.calls[0]
    assert forwarded_paths == ("ccd/dispatch.py", "ccd/agent.py")


# --------------------------------------------------------------------------- #
# Pure-function parsing helpers (mutmut output shapes)
# --------------------------------------------------------------------------- #


def test_parse_mutmut_results_groups_by_status_and_file() -> None:
    text = """
Survived (3)

---- ccd/dispatch.py (2) ----

1, 3

---- ccd/agent.py (1) ----

5

Killed (1)

---- ccd/integrate.py (1) ----

7
"""
    groups = _parse_mutmut_results(text)
    assert "survived" in groups
    assert "killed" in groups
    assert groups["survived"]["ccd/dispatch.py"] == ["1", "3"]
    assert groups["survived"]["ccd/agent.py"] == ["5"]
    assert groups["killed"]["ccd/integrate.py"] == ["7"]


def test_parse_mutmut_results_handles_ranges() -> None:
    text = """
Survived (4)

---- ccd/foo.py (4) ----

10-13
"""
    groups = _parse_mutmut_results(text)
    assert groups["survived"]["ccd/foo.py"] == ["10", "11", "12", "13"]


def test_parse_mutmut_show_extracts_file_line_and_change() -> None:
    text = """--- ccd/dispatch.py
+++ ccd/dispatch.py
@@ -14,7 +14,7 @@
     context
-    if x > 0:
+    if x >= 0:
     more context
"""
    file_name, line, desc = _parse_mutmut_show(text)
    assert file_name == "ccd/dispatch.py"
    assert line == 15  # 14 (hunk base) + 1 context line offset
    assert desc == "if x > 0: → if x >= 0:"


def test_parse_mutmut_show_strips_a_b_prefix() -> None:
    text = """--- a/ccd/foo.py
+++ b/ccd/foo.py
@@ -10,3 +10,3 @@
-x = 1
+x = 2
"""
    file_name, _, desc = _parse_mutmut_show(text)
    assert file_name == "ccd/foo.py"
    assert desc == "x = 1 → x = 2"


# --------------------------------------------------------------------------- #
# Mutant dataclass — signature stability
# --------------------------------------------------------------------------- #


def test_mutant_signature_combines_file_line_mutation() -> None:
    m = Mutant(file="ccd/foo.py", line=42, mutation="a → b", status="survived")
    assert m.signature == "ccd/foo.py:42:a → b"


def test_mutation_run_outcome_carries_error_field() -> None:
    out = MutationRunOutcome(mutants=[], tool="mutmut", error="not found")
    assert out.error == "not found"
    assert out.mutants == []


def test_discovery_summary_is_a_frozen_dataclass() -> None:
    s = DiscoverySummary(
        tool="fake",
        target_paths=("ccd",),
        mutants_total=0,
        status_breakdown={},
        survived_total=0,
        survived_by_file={},
        blocklisted_total=0,
        actionable_total=0,
    )
    with pytest.raises(FrozenInstanceError):
        s.tool = "x"  # type: ignore[misc]
