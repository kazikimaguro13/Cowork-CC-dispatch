from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ccd.agent import AgentOutcome, FakeAgentRunner
from ccd.dispatch import dispatch_one
from ccd.models import DispatchStatus, FailureCategory, Result, Spec
from ccd.protocol import write_result


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
    (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "initial", cwd=tmp_path)
    return tmp_path


def _make_spec(repo: Path) -> Spec:
    path = repo / "_ai_workspace" / "bridge" / "inbox" / "spec_010.md"
    path.write_text("# spec_010: example\n\nbody\n", encoding="utf-8")
    return Spec(id="spec_010", title="example", body="body", path=path)


def _make_commit(repo: Path, name: str, content: str = "x") -> None:
    f = repo / name
    f.write_text(content, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", f"add {name}", cwd=repo)


def _write_result_file(
    repo: Path,
    *,
    status: DispatchStatus,
    body: str = "",
    failure_category: FailureCategory | None = None,
    commits: list[str] | None = None,
) -> None:
    out = repo / "_ai_workspace" / "bridge" / "outbox" / "result_010.md"
    result = Result(
        spec_id="spec_010",
        status=status,
        body=body or status.value,
        failure_category=failure_category,
        commits=commits or [],
    )
    write_result(result, out)


def test_dispatch_success(repo: Path) -> None:
    spec = _make_spec(repo)

    def agent(spec: Spec, workdir: Path) -> None:
        _make_commit(workdir, "feature.py", "code")
        _write_result_file(workdir, status=DispatchStatus.DONE, body="ok")

    runner = FakeAgentRunner(side_effect=agent)

    record = dispatch_one(spec, runner, repo=repo)

    assert record.spec_id == "spec_010"
    assert record.status is DispatchStatus.DONE
    assert record.failure_category is None
    assert record.attempts == 1
    assert record.intervention is False
    assert record.finished_at is not None
    assert record.finished_at >= record.started_at
    assert runner.calls == [("spec_010", repo)]


def test_dispatch_blocked_uses_reported_category(repo: Path) -> None:
    spec = _make_spec(repo)

    def agent(spec: Spec, workdir: Path) -> None:
        _make_commit(workdir, "notes.md", "drafted")
        _write_result_file(
            workdir,
            status=DispatchStatus.BLOCKED,
            body="needs clarification",
            failure_category=FailureCategory.SPEC_UNCLEAR,
        )

    record = dispatch_one(spec, FakeAgentRunner(side_effect=agent), repo=repo)

    assert record.status is DispatchStatus.BLOCKED
    assert record.failure_category is FailureCategory.SPEC_UNCLEAR


def test_dispatch_blocked_without_category_defaults_to_spec_unclear(repo: Path) -> None:
    spec = _make_spec(repo)

    def agent(spec: Spec, workdir: Path) -> None:
        _write_result_file(workdir, status=DispatchStatus.BLOCKED, body="stuck")

    record = dispatch_one(spec, FakeAgentRunner(side_effect=agent), repo=repo)

    assert record.status is DispatchStatus.BLOCKED
    assert record.failure_category is FailureCategory.SPEC_UNCLEAR


def test_dispatch_failed_uses_reported_category(repo: Path) -> None:
    spec = _make_spec(repo)

    def agent(spec: Spec, workdir: Path) -> None:
        _write_result_file(
            workdir,
            status=DispatchStatus.FAILED,
            body="smoke failed",
            failure_category=FailureCategory.SMOKE_FAILED,
        )

    record = dispatch_one(spec, FakeAgentRunner(side_effect=agent), repo=repo)

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.SMOKE_FAILED


def test_dispatch_failed_without_category_is_unknown(repo: Path) -> None:
    spec = _make_spec(repo)

    def agent(spec: Spec, workdir: Path) -> None:
        _write_result_file(workdir, status=DispatchStatus.FAILED, body="opaque error")

    record = dispatch_one(spec, FakeAgentRunner(side_effect=agent), repo=repo)

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is None


def test_dispatch_missing_result_with_zero_exit_is_agent_misread(repo: Path) -> None:
    spec = _make_spec(repo)

    runner = FakeAgentRunner(outcome=AgentOutcome(exit_code=0))

    record = dispatch_one(spec, runner, repo=repo)

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.AGENT_MISREAD


def test_dispatch_missing_result_with_nonzero_exit_is_environment(repo: Path) -> None:
    spec = _make_spec(repo)

    runner = FakeAgentRunner(outcome=AgentOutcome(exit_code=127, stderr="command not found"))

    record = dispatch_one(spec, runner, repo=repo)

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.ENVIRONMENT


def test_dispatch_done_without_commits_is_agent_misread(repo: Path) -> None:
    spec = _make_spec(repo)

    def agent(spec: Spec, workdir: Path) -> None:
        _write_result_file(workdir, status=DispatchStatus.DONE, body="ok")

    record = dispatch_one(spec, FakeAgentRunner(side_effect=agent), repo=repo)

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.AGENT_MISREAD


def test_dispatch_unparseable_result_is_agent_misread(repo: Path) -> None:
    spec = _make_spec(repo)

    def agent(spec: Spec, workdir: Path) -> None:
        out = workdir / "_ai_workspace" / "bridge" / "outbox" / "result_010.md"
        out.write_text("garbage without proper structure", encoding="utf-8")

    record = dispatch_one(spec, FakeAgentRunner(side_effect=agent), repo=repo)

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.AGENT_MISREAD


def test_dispatch_records_timestamps_bracket_runner_invocation(repo: Path) -> None:
    spec = _make_spec(repo)
    invocation_seen: list[bool] = []

    def agent(spec: Spec, workdir: Path) -> None:
        invocation_seen.append(True)
        _make_commit(workdir, "f.py", "x")
        _write_result_file(workdir, status=DispatchStatus.DONE)

    record = dispatch_one(spec, FakeAgentRunner(side_effect=agent), repo=repo)

    assert invocation_seen == [True]
    assert record.started_at.tzinfo is not None
    assert record.finished_at is not None
    assert record.finished_at >= record.started_at
