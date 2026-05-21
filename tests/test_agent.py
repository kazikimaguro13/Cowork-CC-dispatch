from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ccd.agent import AgentOutcome, ClaudeCodeRunner, FakeAgentRunner
from ccd.models import Spec


def _make_spec(tmp_path: Path) -> Spec:
    p = tmp_path / "spec_010.md"
    p.write_text("# spec_010: example\n\nbody\n", encoding="utf-8")
    return Spec(id="spec_010", title="example", body="body", path=p)


def test_agent_outcome_defaults() -> None:
    o = AgentOutcome(exit_code=0)
    assert o.stdout == ""
    assert o.stderr == ""
    assert o.duration_seconds == 0.0


def test_agent_outcome_is_frozen() -> None:
    o = AgentOutcome(exit_code=0)
    with pytest.raises(FrozenInstanceError):
        o.exit_code = 1  # type: ignore[misc]


def test_fake_runner_returns_configured_outcome(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    runner = FakeAgentRunner(outcome=AgentOutcome(exit_code=2, stderr="boom"))

    out = runner.run(spec, workdir=tmp_path)

    assert out.exit_code == 2
    assert out.stderr == "boom"
    assert runner.calls == [("spec_010", tmp_path)]


def test_fake_runner_default_outcome_is_success(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    runner = FakeAgentRunner()

    out = runner.run(spec, workdir=tmp_path)

    assert out.exit_code == 0


def test_fake_runner_invokes_side_effect_before_returning(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    events: list[str] = []

    def side_effect(s: Spec, w: Path) -> None:
        events.append(f"side:{s.id}:{w}")

    runner = FakeAgentRunner(side_effect=side_effect)
    runner.run(spec, workdir=tmp_path)

    assert events == [f"side:spec_010:{tmp_path}"]


def test_claude_runner_prompt_mentions_spec_and_result_paths(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)

    prompt = ClaudeCodeRunner._build_prompt(spec, workdir=tmp_path)

    assert "spec_010.md" in prompt
    assert "result_010.md" in prompt
    assert "push" in prompt


def test_claude_runner_prompt_uses_relative_spec_path_inside_workdir(tmp_path: Path) -> None:
    nested = tmp_path / "inbox"
    nested.mkdir()
    spec_path = nested / "spec_010.md"
    spec_path.write_text("# spec_010: x\n\nb\n", encoding="utf-8")
    spec = Spec(id="spec_010", title="x", body="b", path=spec_path)

    prompt = ClaudeCodeRunner._build_prompt(spec, workdir=tmp_path)

    assert "inbox/spec_010.md" in prompt


def test_claude_runner_subprocess_smoke(tmp_path: Path) -> None:
    """End-to-end subprocess wiring smoke test against /bin/true (not claude)."""

    true_bin = Path("/bin/true")
    if not true_bin.exists():
        pytest.skip("POSIX /bin/true not available")

    spec = _make_spec(tmp_path)
    runner = ClaudeCodeRunner(binary=str(true_bin))

    out = runner.run(spec, workdir=tmp_path)

    assert out.exit_code == 0
    assert out.duration_seconds >= 0.0
