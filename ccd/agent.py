"""Agent runner boundary.

`AgentRunner` is the seam used by `dispatch_one` so that the production
implementation (`ClaudeCodeRunner`, which shells out to the `claude` CLI) can be
swapped for `FakeAgentRunner` in tests. v1 only ships a Claude Code runner —
the Protocol exists to keep that choice from leaking into `dispatch.py`, not
to support multiple agents today.
"""

from __future__ import annotations

import inspect
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import Spec

_STDIO_EXCERPT_BYTES = 64 * 1024


@dataclass(frozen=True)
class AgentOutcome:
    """What the runner observed when the agent finished.

    `stdout` / `stderr` are excerpts (head + tail) — agent runs can produce
    multi-MB transcripts and we don't want to hold them in memory or in the
    DispatchRecord pipeline downstream.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


class AgentRunner(Protocol):
    def run(
        self,
        spec: Spec,
        *,
        workdir: Path,
        feedback: Path | None = None,
    ) -> AgentOutcome: ...


class ClaudeCodeRunner:
    """Invoke the `claude` CLI to execute a spec.

    Mirrors the invocation pattern used by the existing bash bridge
    (`_ai_workspace/run_chain.sh`): one-shot `claude -p "<prompt>"` with
    `--dangerously-skip-permissions`, stdin closed.
    """

    DEFAULT_BINARY = "claude"

    def __init__(self, *, binary: str | None = None, timeout: float | None = None) -> None:
        self._binary = binary or self.DEFAULT_BINARY
        self._timeout = timeout

    def run(
        self,
        spec: Spec,
        *,
        workdir: Path,
        feedback: Path | None = None,
    ) -> AgentOutcome:
        prompt = self._build_prompt(spec, workdir=workdir, feedback=feedback)
        argv = [
            self._binary,
            "--dangerously-skip-permissions",
            "-p",
            prompt,
        ]
        started = time.monotonic()
        completed = subprocess.run(
            argv,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        elapsed = time.monotonic() - started
        return AgentOutcome(
            exit_code=completed.returncode,
            stdout=_excerpt(completed.stdout or "", _STDIO_EXCERPT_BYTES),
            stderr=_excerpt(completed.stderr or "", _STDIO_EXCERPT_BYTES),
            duration_seconds=elapsed,
        )

    @staticmethod
    def _build_prompt(
        spec: Spec, *, workdir: Path, feedback: Path | None = None
    ) -> str:
        try:
            spec_rel = spec.path.relative_to(workdir)
        except ValueError:
            spec_rel = spec.path
        result_rel = f"_ai_workspace/bridge/outbox/result_{_strip_spec_prefix(spec.id)}.md"
        prompt = (
            f"{spec_rel} を読んで、その指示どおりに実装してください。"
            f"完了したら {result_rel} に templates/result 構造で結果を書いてください。"
            "push やブランチ操作はしないこと。"
        )
        if feedback is not None:
            try:
                feedback_rel = feedback.relative_to(workdir)
            except ValueError:
                feedback_rel = feedback
            prompt += (
                f" 前回の試行は失敗しました。{feedback_rel} を読んで原因を直してから"
                "再実装してください。"
            )
        return prompt


@dataclass
class FakeAgentRunner:
    """Test double for `AgentRunner`.

    `side_effect` runs first (so tests can simulate the agent writing a result
    file or making a commit), then `outcome` is returned. `calls` records each
    invocation so tests can assert dispatch invoked the runner exactly once.

    ``calls`` records ``(spec_id, workdir, feedback)`` so tests can assert the
    retry loop propagated the feedback path on the second-and-later attempts.
    """

    outcome: AgentOutcome = field(default_factory=lambda: AgentOutcome(exit_code=0))
    side_effect: Callable[..., None] | None = None
    calls: list[tuple[str, Path, Path | None]] = field(default_factory=list)

    def run(
        self,
        spec: Spec,
        *,
        workdir: Path,
        feedback: Path | None = None,
    ) -> AgentOutcome:
        self.calls.append((spec.id, workdir, feedback))
        if self.side_effect is not None:
            # Pre-spec_011 side_effects take (spec, workdir); spec_011-aware
            # ones can take (spec, workdir, feedback). Detect by arity so
            # existing tests don't have to grow a third parameter.
            try:
                arity = len(inspect.signature(self.side_effect).parameters)
            except (TypeError, ValueError):
                arity = 2
            if arity >= 3:
                self.side_effect(spec, workdir, feedback)
            else:
                self.side_effect(spec, workdir)
        return self.outcome


def _strip_spec_prefix(spec_id: str) -> str:
    if spec_id.startswith("spec_"):
        return spec_id[len("spec_") :]
    return spec_id


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n…[truncated]…\n" + text[-half:]
