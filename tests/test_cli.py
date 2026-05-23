from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ccd import __version__, cli
from ccd.agent import AgentOutcome, FakeAgentRunner
from ccd.models import DispatchStatus, Result, Spec
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


def _write_spec(repo: Path, n: str) -> Path:
    path = repo / "_ai_workspace" / "bridge" / "inbox" / f"spec_{n}.md"
    path.write_text(f"# spec_{n}: test\n\nbody\n", encoding="utf-8")
    return path


def _good_agent(spec: Spec, workdir: Path) -> None:
    fname = f"{spec.id}.py"
    (workdir / fname).write_text("code\n", encoding="utf-8")
    _git("add", fname, cwd=workdir)
    _git("commit", "-q", "-m", f"impl {spec.id}", cwd=workdir)

    suffix = spec.id[len("spec_") :] if spec.id.startswith("spec_") else spec.id
    out = workdir / "_ai_workspace" / "bridge" / "outbox" / f"result_{suffix}.md"
    write_result(Result(spec_id=spec.id, status=DispatchStatus.DONE, body="ok"), out)


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ccd {__version__}"


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main([])
    assert rc == 0
    assert "dispatch" in capsys.readouterr().out


def test_dispatch_subcommand_runs_and_saves_record(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = _write_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)

    rc = cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)

    assert rc == 0
    out = capsys.readouterr().out
    assert "spec_100" in out
    assert "done" in out

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    assert saved.exists()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert "chain" not in payload
    assert len(payload["records"]) == 1
    assert payload["records"][0]["spec_id"] == "spec_100"
    assert payload["records"][0]["status"] == "done"


def test_dispatch_subcommand_returns_nonzero_when_dispatch_fails(repo: Path) -> None:
    spec_path = _write_spec(repo, "100")
    runner = FakeAgentRunner(outcome=AgentOutcome(exit_code=127))

    rc = cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)

    assert rc == 1


def test_chain_subcommand_runs_and_saves_records(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s1 = _write_spec(repo, "100")
    s2 = _write_spec(repo, "101")
    runner = FakeAgentRunner(side_effect=_good_agent)

    rc = cli.main(
        ["chain", str(s1), str(s2), "--repo", str(repo)],
        runner=runner,
        smoke_commands=[["true"]],
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "spec_100" in out
    assert "spec_101" in out

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["chain"]["success"] is True
    assert payload["chain"]["halted_at"] is None
    assert len(payload["records"]) == 2
    assert [r["spec_id"] for r in payload["records"]] == ["spec_100", "spec_101"]


def test_chain_subcommand_returns_nonzero_on_halt(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s1 = _write_spec(repo, "100")
    s2 = _write_spec(repo, "101")

    def agent(spec: Spec, workdir: Path) -> None:
        if spec.id == "spec_101":
            return
        _good_agent(spec, workdir)

    runner = FakeAgentRunner(side_effect=agent)

    rc = cli.main(
        ["chain", str(s1), str(s2), "--repo", str(repo)],
        runner=runner,
        smoke_commands=[["true"]],
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "spec_101" in err

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["chain"]["success"] is False
    assert payload["chain"]["halted_at"] == "spec_101"


def test_report_subcommand_renders_from_saved_run(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = _write_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)
    cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)
    capsys.readouterr()

    rc = cli.main(["report", "--repo", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Metrics report" in out
    assert "Dispatch success rate" in out
    assert "1/1" in out


def test_report_subcommand_errors_when_no_record(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["report", "--repo", str(repo)])

    assert rc == 2
    assert "no run record" in capsys.readouterr().err


def test_report_subcommand_uses_explicit_from_path(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = _write_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)
    custom = tmp_path / "custom_run.json"
    cli.main(
        ["dispatch", str(spec_path), "--repo", str(repo), "--save", str(custom)],
        runner=runner,
    )
    capsys.readouterr()

    assert custom.exists()
    rc = cli.main(["report", "--repo", str(repo), "--from", str(custom)])

    assert rc == 0
    assert "Metrics report" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# spec_010: exception safety, in-flight markers, timeout, reconcile, carry    #
# --------------------------------------------------------------------------- #


@dataclass
class _ExplodingRunner:
    """Runner whose ``run`` always raises (TimeoutExpired or anything else)."""

    exc: BaseException
    calls: list[tuple[str, Path]] = field(default_factory=list)

    def run(self, spec: Spec, *, workdir: Path) -> AgentOutcome:  # pragma: no cover
        self.calls.append((spec.id, workdir))
        raise self.exc


def test_dispatch_runner_exception_records_halted_interrupted(repo: Path) -> None:
    spec_path = _write_spec(repo, "100")
    runner = _ExplodingRunner(RuntimeError("boom"))

    rc = cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)
    assert rc == 1

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    assert saved.exists()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert len(payload["records"]) == 1
    rec = payload["records"][0]
    assert rec["spec_id"] == "spec_100"
    assert rec["status"] == "halted"
    assert rec["failure_category"] == "interrupted"
    # finished_at is NOT invented — duration is unknown.
    assert rec["finished_at"] is None


def test_dispatch_timeoutexpired_records_halted_interrupted(repo: Path) -> None:
    spec_path = _write_spec(repo, "100")
    runner = _ExplodingRunner(
        subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0)
    )

    rc = cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)
    assert rc == 1

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["records"][0]["status"] == "halted"
    assert payload["records"][0]["failure_category"] == "interrupted"


def test_dispatch_writes_inflight_running_marker_before_runner_call(
    repo: Path,
) -> None:
    """While the runner is mid-call, the run JSON must show RUNNING for the spec."""

    spec_path = _write_spec(repo, "100")
    save_path = repo / cli.DEFAULT_LAST_RUN_PATH
    observed: dict[str, object] = {}

    def side_effect(spec: Spec, workdir: Path) -> None:
        # Read the run JSON from inside the runner — the RUNNING marker must be there.
        payload = json.loads(save_path.read_text(encoding="utf-8"))
        observed["records"] = payload["records"]
        _good_agent(spec, workdir)

    runner = FakeAgentRunner(side_effect=side_effect)
    cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)

    records = observed["records"]
    assert isinstance(records, list)
    assert len(records) == 1
    assert records[0]["spec_id"] == "spec_100"
    assert records[0]["status"] == "running"
    assert records[0]["finished_at"] is None


def test_chain_runner_exception_persists_partial_progress(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 1-2 complete, spec 3 raises, spec 4 is never attempted.

    The on-disk run JSON must contain spec 1 + 2 as DONE and spec 3 as
    HALTED+INTERRUPTED.
    """

    s1 = _write_spec(repo, "100")
    s2 = _write_spec(repo, "101")
    s3 = _write_spec(repo, "102")
    s4 = _write_spec(repo, "103")

    def behaviour(spec: Spec, workdir: Path) -> AgentOutcome:
        if spec.id == "spec_102":
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0)
        _good_agent(spec, workdir)
        return AgentOutcome(exit_code=0)

    @dataclass
    class _MixedRunner:
        calls: list[tuple[str, Path]] = field(default_factory=list)

        def run(self, spec: Spec, *, workdir: Path) -> AgentOutcome:
            self.calls.append((spec.id, workdir))
            return behaviour(spec, workdir)

    runner = _MixedRunner()
    rc = cli.main(
        ["chain", str(s1), str(s2), str(s3), str(s4), "--repo", str(repo)],
        runner=runner,
        smoke_commands=[["true"]],
    )
    assert rc == 1

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    payload = json.loads(saved.read_text(encoding="utf-8"))
    statuses = [(r["spec_id"], r["status"]) for r in payload["records"]]
    assert ("spec_100", "done") in statuses
    assert ("spec_101", "done") in statuses
    # spec_102: HALTED + INTERRUPTED
    spec102 = next(r for r in payload["records"] if r["spec_id"] == "spec_102")
    assert spec102["status"] == "halted"
    assert spec102["failure_category"] == "interrupted"
    # spec_103 was never attempted.
    assert all(r["spec_id"] != "spec_103" for r in payload["records"])
    # Runner was only called for the first 3.
    assert [c[0] for c in runner.calls] == ["spec_100", "spec_101", "spec_102"]
    assert payload["chain"]["success"] is False
    assert payload["chain"]["halted_at"] == "spec_102"


def test_reconcile_subcommand_handles_file_and_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Single-file form.
    f = tmp_path / "run.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-23T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_001",
                        "started_at": "2026-05-23T10:00:00+00:00",
                        "finished_at": None,
                        "status": "running",
                        "attempts": 1,
                        "failure_category": None,
                        "intervention": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(["reconcile", str(f)])
    assert rc == 0
    payload = json.loads(f.read_text(encoding="utf-8"))
    assert payload["records"][0]["status"] == "halted"
    assert payload["records"][0]["failure_category"] == "interrupted"
    out = capsys.readouterr().out
    assert "reconciled 1" in out

    # Directory form.
    d = tmp_path / "runs"
    d.mkdir()
    for i in range(2):
        (d / f"r_{i}.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "saved_at": "2026-05-23T00:00:00+00:00",
                    "records": [
                        {
                            "spec_id": f"spec_{i}",
                            "started_at": "2026-05-23T10:00:00+00:00",
                            "finished_at": None,
                            "status": "running",
                            "attempts": 1,
                            "failure_category": None,
                            "intervention": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    rc = cli.main(["reconcile", str(d)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "reconciled 2" in out
    assert "2 file(s)" in out


def test_reconcile_subcommand_missing_target_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["reconcile", str(tmp_path / "nope")])
    assert rc == 2
    assert "no such file" in capsys.readouterr().err


def test_dispatch_auto_carry_forward_from_orphan_running(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If --save points at a file with an orphan RUNNING, the new dispatch
    auto-reconciles and keeps the salvaged record alongside the new one."""

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-22T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_009",
                        "started_at": "2026-05-22T10:00:00+00:00",
                        "finished_at": None,
                        "status": "running",
                        "attempts": 1,
                        "failure_category": None,
                        "intervention": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    spec_path = _write_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)
    rc = cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)
    assert rc == 0

    err = capsys.readouterr().err
    assert "salvaged 1" in err

    payload = json.loads(saved.read_text(encoding="utf-8"))
    spec_ids = [r["spec_id"] for r in payload["records"]]
    assert spec_ids == ["spec_009", "spec_100"]
    # Salvaged record converted.
    spec009 = payload["records"][0]
    assert spec009["status"] == "halted"
    assert spec009["failure_category"] == "interrupted"
    assert spec009["finished_at"] is None
    # New record DONE.
    spec100 = payload["records"][1]
    assert spec100["status"] == "done"


def test_dispatch_no_carry_forward_when_no_running(repo: Path) -> None:
    """Pre-existing file without RUNNING ⇒ no salvage notice, prior records dropped.

    This matches pre-spec_010 behavior — only the orphan-RUNNING path keeps
    history on the same --save path.
    """

    saved = repo / cli.DEFAULT_LAST_RUN_PATH
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-22T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_009",
                        "started_at": "2026-05-22T10:00:00+00:00",
                        "finished_at": "2026-05-22T10:05:00+00:00",
                        "status": "done",
                        "attempts": 1,
                        "failure_category": None,
                        "intervention": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    spec_path = _write_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)
    cli.main(["dispatch", str(spec_path), "--repo", str(repo)], runner=runner)

    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert [r["spec_id"] for r in payload["records"]] == ["spec_100"]


def test_dispatch_accepts_timeout_flag(repo: Path) -> None:
    spec_path = _write_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)
    rc = cli.main(
        [
            "dispatch",
            str(spec_path),
            "--repo",
            str(repo),
            "--timeout",
            "10",
        ],
        runner=runner,
    )
    assert rc == 0


def test_chain_accepts_timeout_flag(repo: Path) -> None:
    s1 = _write_spec(repo, "100")
    runner = FakeAgentRunner(side_effect=_good_agent)
    rc = cli.main(
        [
            "chain",
            str(s1),
            "--repo",
            str(repo),
            "--timeout",
            "10",
        ],
        runner=runner,
        smoke_commands=[["true"]],
    )
    assert rc == 0
