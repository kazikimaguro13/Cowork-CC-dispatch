from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ccd.agent import FakeAgentRunner
from ccd.chain import ChainResult, ChainStep, run_chain
from ccd.models import DispatchStatus, FailureCategory, Result, Spec
from ccd.protocol import write_result


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


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
    (tmp_path / "_ai_workspace" / "bridge" / "inbox").mkdir(parents=True)
    (tmp_path / "_ai_workspace" / "bridge" / "outbox").mkdir(parents=True)
    (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "initial", cwd=tmp_path)
    return tmp_path


def _make_spec(repo: Path, n: str) -> Spec:
    path = repo / "_ai_workspace" / "bridge" / "inbox" / f"spec_{n}.md"
    path.write_text(f"# spec_{n}: test\n\nbody\n", encoding="utf-8")
    return Spec(id=f"spec_{n}", title="test", body="body", path=path)


def _good_agent(spec: Spec, workdir: Path) -> None:
    """Simulate a successful agent run: makes a commit, writes a DONE result."""

    fname = f"{spec.id}.py"
    (workdir / fname).write_text("code\n", encoding="utf-8")
    _git("add", fname, cwd=workdir)
    _git("commit", "-q", "-m", f"impl {spec.id}", cwd=workdir)

    suffix = spec.id[len("spec_") :] if spec.id.startswith("spec_") else spec.id
    out = workdir / "_ai_workspace" / "bridge" / "outbox" / f"result_{suffix}.md"
    write_result(
        Result(spec_id=spec.id, status=DispatchStatus.DONE, body="ok"),
        out,
    )


def test_chain_all_success_merges_every_spec_to_main(repo: Path) -> None:
    s1 = _make_spec(repo, "100")
    s2 = _make_spec(repo, "101")
    runner = FakeAgentRunner(side_effect=_good_agent)

    result = run_chain([s1, s2], runner, repo=repo, smoke_commands=[["true"]])

    assert isinstance(result, ChainResult)
    assert result.success is True
    assert result.halted_at is None
    assert len(result.steps) == 2
    for step in result.steps:
        assert isinstance(step, ChainStep)
        assert step.integrate.success is True
        assert step.integrate.merged is True
        assert step.dispatch.status is DispatchStatus.DONE

    assert _file_on_main(repo, "spec_100.py")
    assert _file_on_main(repo, "spec_101.py")


def test_chain_halts_on_failure_and_leaves_main_clean(repo: Path) -> None:
    s1 = _make_spec(repo, "100")
    s2 = _make_spec(repo, "101")
    s3 = _make_spec(repo, "102")

    def agent(spec: Spec, workdir: Path) -> None:
        if spec.id == "spec_101":
            # Agent does nothing — no commit, no result file → AGENT_MISREAD
            return
        _good_agent(spec, workdir)

    runner = FakeAgentRunner(side_effect=agent)

    result = run_chain([s1, s2, s3], runner, repo=repo, smoke_commands=[["true"]])

    assert result.success is False
    assert result.halted_at == "spec_101"
    assert len(result.steps) == 2

    # spec_100 succeeded and merged
    assert result.steps[0].integrate.success is True
    assert result.steps[0].integrate.merged is True
    # spec_101 dispatched but failed integration (dispatch said FAILED)
    assert result.steps[1].dispatch.status is DispatchStatus.FAILED
    assert result.steps[1].dispatch.failure_category is FailureCategory.AGENT_MISREAD
    assert result.steps[1].integrate.success is False
    assert result.steps[1].integrate.merged is False

    # spec_102 was never attempted — runner was only called for spec_100 and spec_101
    assert [call[0] for call in runner.calls] == ["spec_100", "spec_101"]

    # main is not broken: spec_100 was merged, spec_101/102 work isn't there
    assert _file_on_main(repo, "spec_100.py")
    assert not _file_on_main(repo, "spec_101.py")
    assert not _file_on_main(repo, "spec_102.py")


def test_chain_halts_when_smoke_fails_without_merging(repo: Path) -> None:
    s1 = _make_spec(repo, "100")
    s2 = _make_spec(repo, "101")
    runner = FakeAgentRunner(side_effect=_good_agent)

    result = run_chain([s1, s2], runner, repo=repo, smoke_commands=[["false"]])

    assert result.success is False
    assert result.halted_at == "spec_100"
    assert len(result.steps) == 1
    assert result.steps[0].integrate.failure_category is FailureCategory.SMOKE_FAILED
    assert result.steps[0].integrate.merged is False
    # Smoke failed on spec_100 so the agent was never invoked for spec_101
    assert [call[0] for call in runner.calls] == ["spec_100"]
    assert not _file_on_main(repo, "spec_100.py")
    assert not _file_on_main(repo, "spec_101.py")


def test_chain_with_empty_specs_returns_success(repo: Path) -> None:
    runner = FakeAgentRunner(side_effect=_good_agent)
    result = run_chain([], runner, repo=repo, smoke_commands=[["true"]])

    assert result.success is True
    assert result.halted_at is None
    assert result.steps == ()
    assert runner.calls == []


def test_chain_uses_branch_for_to_name_branches(repo: Path) -> None:
    s1 = _make_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)

    result = run_chain(
        [s1],
        runner,
        repo=repo,
        smoke_commands=[["true"]],
        branch_for=lambda s: f"work/{s.id}",
    )

    assert result.success is True
    assert result.steps[0].branch == "work/spec_100"
    # The branch exists
    branches = subprocess.run(
        ["git", "branch", "--list", "work/spec_100"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert "work/spec_100" in branches
