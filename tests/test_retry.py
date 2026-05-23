"""Tests for `ccd/retry.py:dispatch_with_retry`."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ccd.agent import AgentOutcome, FakeAgentRunner
from ccd.dispatch import dispatch_one
from ccd.models import DispatchStatus, FailureCategory, Result, Spec
from ccd.protocol import write_result
from ccd.retry import dispatch_with_retry


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


def _make_spec(repo: Path, n: str = "100") -> Spec:
    path = repo / "_ai_workspace" / "bridge" / "inbox" / f"spec_{n}.md"
    path.write_text(f"# spec_{n}: test\n\nbody\n", encoding="utf-8")
    return Spec(id=f"spec_{n}", title="test", body="body", path=path)


def _commit_and_write_done(spec: Spec, workdir: Path, unique: str) -> None:
    """Make a unique commit + write a DONE result. ``unique`` differentiates
    retries so back-to-back attempts each produce a real commit (otherwise
    `git commit` would refuse the second attempt with "nothing to commit")."""

    fname = f"{spec.id}_{unique}.py"
    (workdir / fname).write_text("code\n", encoding="utf-8")
    _git("add", fname, cwd=workdir)
    _git("commit", "-q", "-m", f"impl {spec.id} {unique}", cwd=workdir)

    suffix = spec.id[len("spec_") :] if spec.id.startswith("spec_") else spec.id
    out = workdir / "_ai_workspace" / "bridge" / "outbox" / f"result_{suffix}.md"
    write_result(
        Result(spec_id=spec.id, status=DispatchStatus.DONE, body=f"ok ({unique})"),
        out,
    )


@dataclass
class _ScriptedRunner:
    """Runner that executes a per-attempt side effect, indexed by call count."""

    behaviours: list[Callable[[Spec, Path], None]]
    outcomes: list[AgentOutcome] = field(default_factory=list)
    calls: list[tuple[str, Path, Path | None]] = field(default_factory=list)

    def run(
        self,
        spec: Spec,
        *,
        workdir: Path,
        feedback: Path | None = None,
    ) -> AgentOutcome:
        idx = len(self.calls)
        self.calls.append((spec.id, workdir, feedback))
        if idx < len(self.behaviours):
            self.behaviours[idx](spec, workdir)
        if idx < len(self.outcomes):
            return self.outcomes[idx]
        return AgentOutcome(exit_code=0)


# --------------------------------------------------------------------------- #
# Core retry behavior
# --------------------------------------------------------------------------- #


def test_smoke_failure_then_success_returns_attempts_2_done(repo: Path) -> None:
    spec = _make_spec(repo)

    runner = _ScriptedRunner(
        behaviours=[
            lambda s, w: _commit_and_write_done(s, w, "attempt1"),
            lambda s, w: _commit_and_write_done(s, w, "attempt2"),
        ]
    )

    # First smoke probe fails, second probe succeeds — simulate a smoke
    # regression that the agent fixes on the second attempt.
    smoke_calls = {"n": 0}

    def fake_smoke_cmd(_: Path, __: object) -> object:
        smoke_calls["n"] += 1
        return None  # unused — we use a real smoke command below

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[
            # First attempt: `false` fails. Second attempt: feedback file
            # exists → `test -f .../spec_100.feedback.md` succeeds.
            [
                "sh",
                "-c",
                f"test -f {repo}/_ai_workspace/logs/spec_100.feedback.md",
            ],
        ],
    )

    assert record.status is DispatchStatus.DONE
    assert record.attempts == 2
    assert record.intervention is False
    # Runner called twice: once without feedback, once with feedback.
    assert len(runner.calls) == 2
    assert runner.calls[0][2] is None
    assert runner.calls[1][2] is not None
    assert runner.calls[1][2].name == "spec_100.feedback.md"


def test_retry_exhaustion_returns_final_failed_smoke(repo: Path) -> None:
    spec = _make_spec(repo)

    runner = _ScriptedRunner(
        behaviours=[
            (lambda i: lambda s, w: _commit_and_write_done(s, w, f"attempt{i}"))(
                n
            )
            for n in range(1, 4)
        ]
    )

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[["false"]],  # smoke always fails
    )

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.SMOKE_FAILED
    assert record.attempts == 3
    assert record.intervention is False
    assert len(runner.calls) == 3


def test_blocked_status_halts_immediately(repo: Path) -> None:
    spec = _make_spec(repo)

    def write_blocked(s: Spec, w: Path) -> None:
        out = w / "_ai_workspace" / "bridge" / "outbox" / "result_100.md"
        write_result(
            Result(
                spec_id=s.id,
                status=DispatchStatus.BLOCKED,
                body="spec unclear",
                failure_category=FailureCategory.SPEC_UNCLEAR,
            ),
            out,
        )

    runner = _ScriptedRunner(behaviours=[write_blocked] * 3)

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[["true"]],
    )

    assert record.status is DispatchStatus.BLOCKED
    assert record.failure_category is FailureCategory.SPEC_UNCLEAR
    assert record.attempts == 1  # halted immediately, no retry
    assert len(runner.calls) == 1


def test_environment_failure_halts_immediately(repo: Path) -> None:
    spec = _make_spec(repo)

    runner = FakeAgentRunner(outcome=AgentOutcome(exit_code=127))

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[["true"]],
    )

    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.ENVIRONMENT
    assert record.attempts == 1
    assert len(runner.calls) == 1


def test_agent_misread_is_retryable(repo: Path) -> None:
    spec = _make_spec(repo)

    # Behaviour 1: do nothing → no result file, exit_code=0 → AGENT_MISREAD
    # Behaviour 2: commit + write DONE result
    runner = _ScriptedRunner(
        behaviours=[
            lambda s, w: None,
            lambda s, w: _commit_and_write_done(s, w, "attempt2"),
        ]
    )

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[["true"]],
    )

    assert record.status is DispatchStatus.DONE
    assert record.attempts == 2


def test_unclassified_failure_is_retryable(repo: Path) -> None:
    """A FAILED record with failure_category=None should still be retried."""

    spec = _make_spec(repo)

    def opaque_failure(s: Spec, w: Path) -> None:
        out = w / "_ai_workspace" / "bridge" / "outbox" / "result_100.md"
        write_result(
            Result(
                spec_id=s.id,
                status=DispatchStatus.FAILED,
                body="opaque",
                failure_category=None,
            ),
            out,
        )

    runner = _ScriptedRunner(
        behaviours=[
            opaque_failure,
            lambda s, w: _commit_and_write_done(s, w, "attempt2"),
        ]
    )

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[["true"]],
    )

    assert record.status is DispatchStatus.DONE
    assert record.attempts == 2


def test_timeout_is_retryable_and_loops_until_exhausted(repo: Path) -> None:
    spec = _make_spec(repo)

    @dataclass
    class _TimeoutRunner:
        calls: list[tuple[str, Path, Path | None]] = field(default_factory=list)

        def run(
            self,
            spec: Spec,
            *,
            workdir: Path,
            feedback: Path | None = None,
        ) -> AgentOutcome:
            self.calls.append((spec.id, workdir, feedback))
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0)

    runner = _TimeoutRunner()

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[["true"]],
    )

    assert record.status is DispatchStatus.HALTED
    assert record.failure_category is FailureCategory.INTERRUPTED
    assert record.attempts == 3
    assert len(runner.calls) == 3


def test_non_timeout_exception_propagates(repo: Path) -> None:
    spec = _make_spec(repo)

    @dataclass
    class _BoomRunner:
        def run(
            self,
            spec: Spec,
            *,
            workdir: Path,
            feedback: Path | None = None,
        ) -> AgentOutcome:
            raise RuntimeError("boom")

    runner = _BoomRunner()

    with pytest.raises(RuntimeError, match="boom"):
        dispatch_with_retry(
            spec,
            runner,
            repo=repo,
            max_attempts=3,
            smoke_commands=[["true"]],
        )


def test_default_max_attempts_is_one_for_library_callers(repo: Path) -> None:
    """Library default (1) = no retry; existing callers see unchanged behavior."""

    spec = _make_spec(repo)

    runner = _ScriptedRunner(
        behaviours=[
            lambda s, w: _commit_and_write_done(s, w, "attempt1"),
            lambda s, w: _commit_and_write_done(s, w, "attempt2"),
        ]
    )

    # Smoke fails → would retry if max_attempts > 1, but library default is 1.
    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        smoke_commands=[["false"]],
    )

    assert record.attempts == 1
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------- #
# Feedback file content
# --------------------------------------------------------------------------- #


def test_feedback_file_contains_failure_category_and_smoke_output(
    repo: Path,
) -> None:
    spec = _make_spec(repo)

    runner = _ScriptedRunner(
        behaviours=[
            lambda s, w: _commit_and_write_done(s, w, "attempt1"),
            lambda s, w: _commit_and_write_done(s, w, "attempt2"),
        ]
    )

    dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=2,
        smoke_commands=[
            ["sh", "-c", "echo FAKE_RUFF_ERROR >&2; exit 7"],
        ],
    )

    feedback = repo / "_ai_workspace" / "logs" / "spec_100.feedback.md"
    assert feedback.exists()
    text = feedback.read_text(encoding="utf-8")
    assert "smoke_failed" in text
    assert "FAKE_RUFF_ERROR" in text  # smoke stderr is excerpted
    assert "再実装" in text  # the "rebuild from feature branch" instruction


def test_feedback_path_is_propagated_to_runner_on_retry(repo: Path) -> None:
    spec = _make_spec(repo)

    runner = _ScriptedRunner(
        behaviours=[
            lambda s, w: _commit_and_write_done(s, w, "attempt1"),
            lambda s, w: _commit_and_write_done(s, w, "attempt2"),
        ]
    )

    dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=2,
        smoke_commands=[["sh", "-c", "exit 1"]],
    )

    assert len(runner.calls) == 2
    # First attempt: no feedback.
    assert runner.calls[0][2] is None
    # Second attempt: feedback path points at the just-written feedback file.
    fb = runner.calls[1][2]
    assert fb is not None
    assert fb.exists()
    assert fb.name == "spec_100.feedback.md"


def test_feedback_file_mentions_timeout_when_runner_timed_out(repo: Path) -> None:
    spec = _make_spec(repo)

    @dataclass
    class _ToggleTimeoutRunner:
        calls: list[tuple[str, Path, Path | None]] = field(default_factory=list)

        def run(
            self,
            spec: Spec,
            *,
            workdir: Path,
            feedback: Path | None = None,
        ) -> AgentOutcome:
            self.calls.append((spec.id, workdir, feedback))
            if len(self.calls) == 1:
                raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0)
            _commit_and_write_done(spec, workdir, f"attempt{len(self.calls)}")
            return AgentOutcome(exit_code=0)

    runner = _ToggleTimeoutRunner()
    dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=2,
        smoke_commands=[["true"]],
    )

    feedback = repo / "_ai_workspace" / "logs" / "spec_100.feedback.md"
    assert feedback.exists()
    text = feedback.read_text(encoding="utf-8")
    assert "TimeoutExpired" in text or "タイムアウト" in text or "timed out" in text


# --------------------------------------------------------------------------- #
# Back-compat for existing callers (spec §2-6, §2-7)
# --------------------------------------------------------------------------- #


def test_dispatch_one_back_compat_without_feedback_kwarg(repo: Path) -> None:
    """Existing call `dispatch_one(spec, runner, repo=repo)` still works."""

    spec = _make_spec(repo)
    runner = FakeAgentRunner(
        side_effect=lambda s, w: _commit_and_write_done(s, w, "attempt1")
    )
    record = dispatch_one(spec, runner, repo=repo)
    assert record.status is DispatchStatus.DONE
    assert record.attempts == 1
    # The runner.calls entry includes feedback=None for the first attempt.
    assert runner.calls == [("spec_100", repo, None)]


def test_dispatch_one_forwards_feedback_to_runner(repo: Path) -> None:
    spec = _make_spec(repo)
    runner = FakeAgentRunner(
        side_effect=lambda s, w: _commit_and_write_done(s, w, "attempt1")
    )
    fb = repo / "_ai_workspace" / "logs" / "spec_100.feedback.md"
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text("dummy feedback\n", encoding="utf-8")

    dispatch_one(spec, runner, repo=repo, feedback=fb)
    assert runner.calls[0][2] == fb


# --------------------------------------------------------------------------- #
# End-to-end: real `attempts>1 and DONE` record flows into `retry_recovery_rate`
# --------------------------------------------------------------------------- #


def test_retry_recovery_rate_is_populated_by_real_retry_records(repo: Path) -> None:
    """An attempts>1 DONE record must drive `retry_recovery_rate` off 0/0."""

    from ccd.metrics import aggregate

    spec = _make_spec(repo)

    runner = _ScriptedRunner(
        behaviours=[
            lambda s, w: _commit_and_write_done(s, w, "attempt1"),
            lambda s, w: _commit_and_write_done(s, w, "attempt2"),
        ]
    )

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=2,
        # First smoke probe fails; second smoke probe succeeds because the
        # retry's feedback file now exists.
        smoke_commands=[
            [
                "sh",
                "-c",
                f"test -f {repo}/_ai_workspace/logs/spec_100.feedback.md",
            ]
        ],
    )

    assert record.attempts == 2
    assert record.status is DispatchStatus.DONE

    report = aggregate([record])
    # retry_recovery_rate: 1 done after retry / 1 retried = 100%
    assert report.retry_recovery_rate.numerator == 1
    assert report.retry_recovery_rate.denominator == 1
    assert report.retry_recovery_rate.value == 1.0
    # first_pass_rate: 0 done on first try / 1 total = 0%
    assert report.first_pass_rate.numerator == 0
    assert report.first_pass_rate.denominator == 1


def test_first_pass_rate_counts_first_try_dones_correctly(repo: Path) -> None:
    """attempts==1 + DONE should land in first_pass_rate, not retry_recovery."""

    from ccd.metrics import aggregate

    spec = _make_spec(repo)
    runner = FakeAgentRunner(
        side_effect=lambda s, w: _commit_and_write_done(s, w, "attempt1")
    )

    record = dispatch_with_retry(
        spec,
        runner,
        repo=repo,
        max_attempts=3,
        smoke_commands=[["true"]],
    )

    assert record.attempts == 1
    assert record.status is DispatchStatus.DONE

    report = aggregate([record])
    assert report.first_pass_rate.numerator == 1
    # retry_recovery_rate: 0 retries → 0/0
    assert report.retry_recovery_rate.denominator == 0
