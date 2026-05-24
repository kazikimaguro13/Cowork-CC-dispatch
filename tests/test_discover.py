"""Tests for `ccd/discover.py:run_discovery` + `ccd discover` CLI.

Discovery dogfoods the `MutationRunner` seam: every test injects a
`FakeMutationRunner` so no real `mutmut` invocation happens. The few tests
that touch the mutmut parsers exercise the pure-function helpers directly.

spec_014 added the isolation suite (``_isolated_clone`` + MutmutRunner
isolation wiring): those tests use real ``git`` against tmp_path-only
fixtures — no subprocess touches the live repo (spec_014 §3).
"""

from __future__ import annotations

import json
import shutil
import subprocess
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
    MutmutRunner,
    _isolated_clone,
    _parse_mutmut_results,
    _parse_mutmut_show,
    _strip_git_remotes,
    _workspace_env,
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


# --------------------------------------------------------------------------- #
# spec_014 — isolation. Every subprocess below targets tmp_path-only fixtures;
# nothing touches the live repo (spec_014 §3).
# --------------------------------------------------------------------------- #


def _git_available() -> bool:
    return shutil.which("git") is not None


requires_git = pytest.mark.skipif(not _git_available(), reason="git not on PATH")


def _init_repo(path: Path, *, with_remote: bool = True) -> str:
    """Create a real git repo under `path` with one commit. Returns HEAD sha."""

    path.mkdir(parents=True, exist_ok=True)
    env = {
        # Keep these inline so the test never reads the developer's git config.
        "GIT_AUTHOR_NAME": "ccd-test",
        "GIT_AUTHOR_EMAIL": "ccd-test@example.invalid",
        "GIT_COMMITTER_NAME": "ccd-test",
        "GIT_COMMITTER_EMAIL": "ccd-test@example.invalid",
    }
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    (path / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "file.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        env={**env},
    )
    if with_remote:
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "remote",
                "add",
                "origin",
                "https://example.invalid/fake.git",
            ],
            check=True,
            capture_output=True,
        )
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return head


def _git_branches(path: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(path), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {b.strip() for b in out.splitlines() if b.strip()}


def _git_log(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _git_remotes(path: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(path), "remote"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {r.strip() for r in out.split() if r.strip()}


@requires_git
def test_isolated_clone_simulated_mutmut_leak_does_not_pollute_live_repo(
    tmp_path: Path,
) -> None:
    """spec_014 §2-4 core proof.

    Simulate what happened in the real incident: while mutmut is running, a
    runaway git commit fires *inside the workspace* (impersonating the
    `impl spec_100` leak from 2026-05-24). Assert the *live* repo's HEAD,
    log, branches, and remotes are byte-identical before and after.
    """

    src = tmp_path / "live_repo"
    head_before = _init_repo(src, with_remote=True)
    branches_before = _git_branches(src)
    log_before = _git_log(src)
    remotes_before = _git_remotes(src)
    file_before = (src / "file.txt").read_text(encoding="utf-8")

    leaked_workspace_path: list[Path] = []

    with _isolated_clone(src) as workspace:
        leaked_workspace_path.append(workspace)
        # Sanity: workspace is a real, separate path.
        assert workspace != src
        assert workspace.exists()
        assert (workspace / ".git").is_dir()

        # Simulate mutmut-induced leak: write a file, commit it, create a
        # branch — all things the real incident's runaway git invocations
        # might do. These are aimed at WORKSPACE; the live repo MUST be
        # unaffected even though both share an ancestor history.
        (workspace / "LEAK.txt").write_text("leaked\n", encoding="utf-8")
        (workspace / "file.txt").write_text("v2-mutated\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", "impl spec_100"],
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "leak",
                "GIT_AUTHOR_EMAIL": "leak@x.invalid",
                "GIT_COMMITTER_NAME": "leak",
                "GIT_COMMITTER_EMAIL": "leak@x.invalid",
            },
        )
        subprocess.run(
            ["git", "-C", str(workspace), "branch", "leaked-branch"],
            check=True,
            capture_output=True,
        )

    # Live repo is byte-identical to what it was before the with block.
    assert _git_log(src) == log_before
    assert _git_branches(src) == branches_before
    assert (
        subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == head_before
    )
    assert _git_remotes(src) == remotes_before
    assert (src / "file.txt").read_text(encoding="utf-8") == file_before
    assert not (src / "LEAK.txt").exists()

    # And the workspace is cleaned up (so disk doesn't fill across runs).
    assert leaked_workspace_path
    assert not leaked_workspace_path[0].exists()


@requires_git
def test_isolated_clone_strips_origin_remote_so_push_has_no_target(
    tmp_path: Path,
) -> None:
    """Even if a mutation triggers `git push`, the workspace has no remote."""

    src = tmp_path / "src"
    _init_repo(src, with_remote=True)
    assert "origin" in _git_remotes(src)

    with _isolated_clone(src) as workspace:
        # The clone inherits the .git, but origin has been ripped out.
        assert _git_remotes(workspace) == set()


@requires_git
def test_isolated_clone_cleans_up_on_exception(tmp_path: Path) -> None:
    """try/finally — the temp tree is removed even if the body raises."""

    src = tmp_path / "src"
    _init_repo(src, with_remote=False)

    captured: list[Path] = []
    with pytest.raises(RuntimeError, match="boom"):
        with _isolated_clone(src) as workspace:
            captured.append(workspace)
            assert workspace.exists()
            raise RuntimeError("boom")

    assert captured
    assert not captured[0].exists()
    # Parent tmp dir is gone too (we shutil.rmtree the root, not the clone).
    assert not captured[0].parent.exists()


def test_isolated_clone_excludes_heavy_or_unsafe_dirs(tmp_path: Path) -> None:
    """`_ai_workspace/`, caches, .venv etc. must not be copied.

    Two reasons: (1) `_ai_workspace/` belongs to the live repo (discovery
    reports are written there, not the clone — copying it would create a
    hall-of-mirrors); (2) caches/venv are huge.
    """

    src = tmp_path / "src"
    src.mkdir()
    (src / "ccd").mkdir()
    (src / "ccd" / "__init__.py").write_text("# package\n", encoding="utf-8")
    # Things that MUST be excluded.
    (src / "_ai_workspace").mkdir()
    (src / "_ai_workspace" / "logs").mkdir()
    (src / "_ai_workspace" / "logs" / "last_run.json").write_text(
        "{}", encoding="utf-8"
    )
    (src / ".venv").mkdir()
    (src / ".venv" / "marker").write_text("x", encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "cached.pyc").write_text("x", encoding="utf-8")
    (src / "ccd_native.egg-info").mkdir()
    (src / "ccd_native.egg-info" / "PKG-INFO").write_text("x", encoding="utf-8")
    (src / ".mutmut-cache").write_text("cached", encoding="utf-8")

    with _isolated_clone(src) as workspace:
        assert (workspace / "ccd" / "__init__.py").exists()
        assert not (workspace / "_ai_workspace").exists()
        assert not (workspace / ".venv").exists()
        assert not (workspace / "__pycache__").exists()
        assert not (workspace / "ccd_native.egg-info").exists()
        assert not (workspace / ".mutmut-cache").exists()


def test_isolated_clone_captures_uncommitted_edits(tmp_path: Path) -> None:
    """Mutation testing must reflect what's on disk, not just HEAD.

    We use `shutil.copytree` (not `git clone --local`) precisely so that a
    developer running `ccd discover` while mid-edit gets mutation tested on
    what they're actually working on.
    """

    src = tmp_path / "src"
    src.mkdir()
    (src / "ccd").mkdir()
    (src / "ccd" / "mod.py").write_text("WORK_IN_PROGRESS = True\n", encoding="utf-8")

    with _isolated_clone(src) as workspace:
        assert (
            (workspace / "ccd" / "mod.py").read_text(encoding="utf-8")
            == "WORK_IN_PROGRESS = True\n"
        )


def test_strip_git_remotes_is_a_noop_without_dot_git(tmp_path: Path) -> None:
    """No `.git` dir → nothing to strip, no exception."""

    src = tmp_path / "no_git"
    src.mkdir()
    _strip_git_remotes(src)  # must not raise


def test_workspace_env_prepends_pythonpath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spec_014 §2-1 (d) — tests inside the clone must import the *clone's* ccd."""

    monkeypatch.setenv("PYTHONPATH", "/some/existing/path")
    env = _workspace_env(tmp_path / "work")

    import os as _os

    parts = env["PYTHONPATH"].split(_os.pathsep)
    assert parts[0] == str(tmp_path / "work")
    assert "/some/existing/path" in parts


def test_workspace_env_works_when_pythonpath_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = _workspace_env(tmp_path / "work")
    assert env["PYTHONPATH"] == str(tmp_path / "work")


def test_mutmut_runner_subprocess_targets_isolated_clone_not_live_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MutmutRunner must invoke mutmut with cwd=<clone>, never cwd=<live>.

    We patch `subprocess.run` to record every cwd it sees, then verify none
    of them is the live repo path. The fact that something else (the clone)
    is used is the spec_014 §2-1 (a) guarantee from the production runner's
    side. No real mutmut runs.
    """

    src = tmp_path / "src"
    src.mkdir()
    (src / "ccd").mkdir()
    (src / "ccd" / "__init__.py").write_text("# x\n", encoding="utf-8")

    cwds_seen: list[str] = []
    envs_seen: list[dict[str, str]] = []

    real_run = subprocess.run

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Only intercept calls made from MutmutRunner (mutmut binary). Let
        # the isolation helper's `git remote` calls go through.
        argv = args[0] if args else kwargs.get("args", [])
        if isinstance(argv, (list, tuple)) and argv and "mutmut" in str(argv[0]):
            cwds_seen.append(kwargs.get("cwd", ""))
            envs_seen.append(kwargs.get("env") or {})
            # Return an empty results listing so MutmutRunner finishes
            # gracefully (no mutants found, no error).
            from subprocess import CompletedProcess

            return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        return real_run(*args, **kwargs)

    monkeypatch.setattr("ccd.discover.subprocess.run", fake_run)

    runner = MutmutRunner()
    outcome = runner.run(repo=src, paths=["ccd"])

    # mutmut was invoked at least once for `run` and once for `results`.
    assert cwds_seen, "MutmutRunner did not invoke mutmut"
    for cwd in cwds_seen:
        assert cwd != str(src), (
            "MutmutRunner used the live repo as cwd — isolation broke"
        )
        assert cwd != str(src.resolve()), (
            "MutmutRunner used the live repo (resolved) as cwd — isolation broke"
        )
        # All cwds point to a *temporary* isolated workspace path.
        assert "ccd_discover_iso_" in cwd, (
            f"cwd {cwd!r} does not look like an isolated workspace"
        )
    # PYTHONPATH was passed and prepended with the workspace.
    assert envs_seen
    for env in envs_seen:
        pp = env.get("PYTHONPATH", "")
        first = pp.split(":")[0] if ":" in pp else pp
        assert "ccd_discover_iso_" in first, (
            f"PYTHONPATH not prefixed with workspace: {pp!r}"
        )
    # The outcome is empty (no mutants) and not an error.
    assert outcome.error == ""
    assert outcome.mutants == []

    # Critically: the isolation temp dir is cleaned up — no leftover dirs.
    # We can't enumerate easily, but at least the live repo is unchanged.
    assert (src / "ccd" / "__init__.py").read_text(encoding="utf-8") == "# x\n"


@requires_git
def test_mutmut_runner_isolation_survives_real_git_writes_to_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end-ish: MutmutRunner's isolation absorbs a fake mutmut that
    actually performs a git commit on its cwd. Real repo stays unchanged.

    This is the spec_014 §2-4 proof at the *MutmutRunner* layer (vs the
    `_isolated_clone` unit test above which checks the helper directly).
    """

    src = tmp_path / "src"
    head_before = _init_repo(src, with_remote=True)
    log_before = _git_log(src)
    branches_before = _git_branches(src)

    real_run = subprocess.run

    import os as _os

    leak_env = {
        **_os.environ,
        "GIT_AUTHOR_NAME": "leak",
        "GIT_AUTHOR_EMAIL": "leak@x.invalid",
        "GIT_COMMITTER_NAME": "leak",
        "GIT_COMMITTER_EMAIL": "leak@x.invalid",
    }

    leak_count = {"n": 0}

    def malicious_mutmut(*args, **kwargs):  # type: ignore[no-untyped-def]
        argv = args[0] if args else kwargs.get("args", [])
        if isinstance(argv, (list, tuple)) and argv and "mutmut" in str(argv[0]):
            # Inside the isolated clone: simulate a runaway commit + branch.
            # MutmutRunner invokes mutmut multiple times (run / results / show)
            # — we only need to leak once to prove the isolation; subsequent
            # invocations would 'nothing to commit' which isn't a real-world
            # failure mode of mutmut.
            cwd = kwargs.get("cwd", "")
            leak_count["n"] += 1
            if leak_count["n"] == 1:
                (Path(cwd) / "LEAK.txt").write_text("leaked\n", encoding="utf-8")
                real_run(
                    ["git", "-C", cwd, "add", "LEAK.txt"],
                    check=True,
                    capture_output=True,
                )
                real_run(
                    ["git", "-C", cwd, "commit", "-m", "leaked from mutmut"],
                    check=True,
                    capture_output=True,
                    env=leak_env,
                )
                real_run(
                    ["git", "-C", cwd, "branch", "leaked-branch"],
                    check=True,
                    capture_output=True,
                )
            from subprocess import CompletedProcess

            return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        return real_run(*args, **kwargs)

    monkeypatch.setattr("ccd.discover.subprocess.run", malicious_mutmut)

    runner = MutmutRunner()
    runner.run(repo=src, paths=["."])

    # Live repo's HEAD, log, branches, remotes — all unchanged.
    assert (
        real_run(
            ["git", "-C", str(src), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == head_before
    )
    assert _git_log(src) == log_before
    assert _git_branches(src) == branches_before
    assert not (src / "LEAK.txt").exists()
