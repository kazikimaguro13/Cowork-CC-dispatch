from __future__ import annotations

import json
import subprocess
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
