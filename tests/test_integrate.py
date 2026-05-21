from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccd.integrate import (
    CommandResult,
    IntegrateResult,
    SmokeResult,
    integrate,
)
from ccd.models import DispatchRecord, DispatchStatus, FailureCategory


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _current_branch(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _file_on_main(repo: Path, name: str) -> bool:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"main:{name}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "initial", cwd=tmp_path)
    return tmp_path


def _make_branch_with_commit(repo: Path, branch: str, filename: str) -> None:
    _git("checkout", "-b", branch, cwd=repo)
    (repo / filename).write_text("x\n", encoding="utf-8")
    _git("add", filename, cwd=repo)
    _git("commit", "-q", "-m", f"add {filename}", cwd=repo)


def _record(
    status: DispatchStatus = DispatchStatus.DONE,
    failure_category: FailureCategory | None = None,
) -> DispatchRecord:
    now = datetime.now(UTC)
    return DispatchRecord(
        spec_id="spec_010",
        started_at=now,
        finished_at=now,
        status=status,
        attempts=1,
        failure_category=failure_category,
        intervention=False,
    )


def test_integrate_smoke_green_merges_branch_to_main(repo: Path) -> None:
    _make_branch_with_commit(repo, "feat/spec_010", "feature.py")

    result = integrate(
        _record(),
        repo=repo,
        branch="feat/spec_010",
        smoke_commands=[["true"]],
    )

    assert isinstance(result, IntegrateResult)
    assert result.success is True
    assert result.merged is True
    assert result.failure_category is None
    assert result.smoke is not None
    assert result.smoke.passed is True
    assert _current_branch(repo) == "main"
    assert _file_on_main(repo, "feature.py")


def test_integrate_skips_smoke_and_merge_when_dispatch_failed(repo: Path) -> None:
    _make_branch_with_commit(repo, "feat/spec_010", "feature.py")

    record = _record(
        status=DispatchStatus.FAILED,
        failure_category=FailureCategory.AGENT_MISREAD,
    )
    result = integrate(
        record,
        repo=repo,
        branch="feat/spec_010",
        smoke_commands=[["false"]],  # would fail if it ran
    )

    assert result.success is False
    assert result.merged is False
    assert result.smoke is None
    assert result.failure_category is FailureCategory.AGENT_MISREAD
    assert not _file_on_main(repo, "feature.py")


def test_integrate_smoke_failure_halts_without_merge(repo: Path) -> None:
    _make_branch_with_commit(repo, "feat/spec_010", "feature.py")

    result = integrate(
        _record(),
        repo=repo,
        branch="feat/spec_010",
        smoke_commands=[["false"]],
    )

    assert result.success is False
    assert result.merged is False
    assert result.smoke is not None
    assert result.smoke.passed is False
    assert result.failure_category is FailureCategory.SMOKE_FAILED
    assert not _file_on_main(repo, "feature.py")


def test_integrate_smoke_stops_at_first_failure(repo: Path) -> None:
    _make_branch_with_commit(repo, "feat/spec_010", "feature.py")

    result = integrate(
        _record(),
        repo=repo,
        branch="feat/spec_010",
        smoke_commands=[["false"], ["true"]],
    )

    assert result.success is False
    assert result.smoke is not None
    assert result.smoke.passed is False
    assert len(result.smoke.commands) == 1
    assert isinstance(result.smoke.commands[0], CommandResult)
    assert result.smoke.commands[0].argv == ("false",)
    assert result.smoke.commands[0].returncode != 0


def test_integrate_smoke_runs_all_when_all_pass(repo: Path) -> None:
    _make_branch_with_commit(repo, "feat/spec_010", "feature.py")

    result = integrate(
        _record(),
        repo=repo,
        branch="feat/spec_010",
        smoke_commands=[["true"], ["true"]],
    )

    assert result.success is True
    assert result.smoke is not None
    assert result.smoke.passed is True
    assert len(result.smoke.commands) == 2


def test_integrate_uses_no_ff_merge_so_a_merge_commit_appears(repo: Path) -> None:
    _make_branch_with_commit(repo, "feat/spec_010", "feature.py")

    result = integrate(
        _record(),
        repo=repo,
        branch="feat/spec_010",
        smoke_commands=[["true"]],
    )
    assert result.success is True

    log = subprocess.run(
        ["git", "log", "--pretty=%s", "-1", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert log == "merge: feat/spec_010"


def test_integrate_does_not_push_when_no_remote_configured(repo: Path) -> None:
    """If integrate tried to `git push`, this would fail (no remote configured).

    The success of this test on a repo with zero remotes is the evidence that
    v1 stays local-only — the operator pushes manually.
    """
    _make_branch_with_commit(repo, "feat/spec_010", "feature.py")
    remotes = subprocess.run(
        ["git", "remote"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remotes == ""

    result = integrate(
        _record(),
        repo=repo,
        branch="feat/spec_010",
        smoke_commands=[["true"]],
    )
    assert result.success is True
    assert result.merged is True


def test_smoke_result_default_state() -> None:
    smoke = SmokeResult(passed=True)
    assert smoke.commands == ()
