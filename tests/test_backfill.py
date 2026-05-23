from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccd.backfill import (
    BackfillSource,
    backfill_project,
    load_sources_config,
    main,
    parse_result_file,
    write_run_file,
)
from ccd.models import DispatchStatus, FailureCategory, RunFile

FIXTURES = Path(__file__).parent / "fixtures" / "backfill"
ALL_FIXTURES = sorted(FIXTURES.glob("result_*.md"))


def _stage_outbox(tmp_path: Path, fixture_names: list[str] | None = None) -> Path:
    """Copy fixture results into a fresh fake-project outbox under tmp_path."""

    outbox = tmp_path / "_ai_workspace" / "bridge" / "outbox"
    outbox.mkdir(parents=True)
    sources = (
        [FIXTURES / name for name in fixture_names]
        if fixture_names is not None
        else ALL_FIXTURES
    )
    for src in sources:
        shutil.copy2(src, outbox / src.name)
    return tmp_path


# --------------------------------------------------------------------------- #
# Single-file parsing                                                          #
# --------------------------------------------------------------------------- #


def test_parse_bash_generation_extracts_started_finished_status() -> None:
    record = parse_result_file(FIXTURES / "result_001.md")
    assert record is not None
    assert record.spec_id == "spec_001"
    assert record.status is DispatchStatus.DONE
    assert record.started_at == datetime(2025, 12, 1, tzinfo=UTC)
    assert record.finished_at == datetime(2025, 12, 1, tzinfo=UTC)
    assert record.failure_category is None
    assert record.attempts == 1
    assert record.intervention is False


def test_parse_bash_generation_with_failure_category() -> None:
    record = parse_result_file(FIXTURES / "result_002.md")
    assert record is not None
    assert record.status is DispatchStatus.FAILED
    assert record.failure_category is FailureCategory.SMOKE_FAILED


def test_parse_ccd_native_generation_uses_completed_field() -> None:
    record = parse_result_file(FIXTURES / "result_003.md")
    assert record is not None
    assert record.spec_id == "spec_003"
    assert record.status is DispatchStatus.DONE
    # No "Started" — falls back to mirroring Completed.
    assert record.started_at == datetime(2026, 1, 15, tzinfo=UTC)
    assert record.finished_at == datetime(2026, 1, 15, tzinfo=UTC)


def test_parse_client_named_spec_id_returned_verbatim_before_anonymize() -> None:
    # parse_result_file is below the anonymization layer — it returns the raw id.
    record = parse_result_file(FIXTURES / "result_004.md")
    assert record is not None
    assert record.spec_id == "spec_acmeproject_007"
    assert record.status is DispatchStatus.BLOCKED


def test_parse_skips_file_missing_status(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        record = parse_result_file(FIXTURES / "result_005.md")
    assert record is None
    assert any("status" in msg for msg in caplog.messages)


def test_parse_skips_file_with_unknown_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        record = parse_result_file(FIXTURES / "result_006.md")
    assert record is None
    assert any("status" in msg for msg in caplog.messages)


def test_parse_skips_file_missing_spec_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    f = tmp_path / "result_orphan.md"
    f.write_text(
        "# (no id colon — title alone)\n\n- **Status**: done\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        record = parse_result_file(f)
    assert record is None
    assert any("spec_id" in msg for msg in caplog.messages)


def test_parse_handles_iso_timestamp_with_T_separator(tmp_path: Path) -> None:
    f = tmp_path / "result_iso.md"
    f.write_text(
        "# result_iso: x\n\n"
        "- **Spec**: spec_042\n"
        "- **Started-At**: 2026-03-10T08:15:00Z\n"
        "- **Finished-At**: 2026-03-10T09:00:00Z\n"
        "- **Status**: done\n",
        encoding="utf-8",
    )
    record = parse_result_file(f)
    assert record is not None
    assert record.started_at == datetime(2026, 3, 10, tzinfo=UTC)
    assert record.finished_at == datetime(2026, 3, 10, tzinfo=UTC)


def test_parse_falls_back_to_mtime_when_no_dates(tmp_path: Path) -> None:
    f = tmp_path / "result_nodates.md"
    f.write_text(
        "# result_nodates: no dates here\n\n"
        "- **Spec**: spec_999\n"
        "- **Status**: done\n",
        encoding="utf-8",
    )
    # Force a known mtime so the test is deterministic.
    target = datetime(2024, 6, 1, 13, 47, 0, tzinfo=UTC)
    import os

    os.utime(f, (target.timestamp(), target.timestamp()))

    record = parse_result_file(f)
    assert record is not None
    # Day rounded, midnight UTC.
    assert record.started_at == datetime(2024, 6, 1, tzinfo=UTC)
    assert record.started_at.hour == 0
    assert record.started_at.tzinfo == UTC
    assert record.finished_at is None


def test_parse_ignores_inline_kv_in_body(tmp_path: Path) -> None:
    f = tmp_path / "result_bodyfake.md"
    f.write_text(
        "# result_bodyfake: x\n\n"
        "- **Spec**: spec_077\n"
        "- **Status**: done\n"
        "- **Completed**: 2026-04-01\n\n"
        "## Notes\n\n"
        "- **Spec**: spec_999_FAKE\n"
        "- **Status**: failed\n",
        encoding="utf-8",
    )
    record = parse_result_file(f)
    assert record is not None
    assert record.spec_id == "spec_077"
    assert record.status is DispatchStatus.DONE


# --------------------------------------------------------------------------- #
# Project-level ingest + anonymization                                         #
# --------------------------------------------------------------------------- #


def test_backfill_project_parses_valid_skips_invalid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = _stage_outbox(tmp_path)
    with caplog.at_level(logging.WARNING):
        run = backfill_project(
            project, label="fixture-project", generation="bash_prototype"
        )
    # 4 valid (001-004) out of 6 fixtures; result_005 and 006 are skipped.
    assert len(run.records) == 4
    assert run.project == "fixture-project"
    assert run.generation == "bash_prototype"
    assert run.version == 1
    assert run.saved_at is not None
    # Both malformed fixtures generated a warning.
    skip_msgs = [m for m in caplog.messages if "skipping" in m]
    assert len(skip_msgs) >= 2


def test_backfill_project_renumbers_non_standard_spec_ids(tmp_path: Path) -> None:
    project = _stage_outbox(tmp_path)
    run = backfill_project(
        project, label="fixture-project", generation="bash_prototype"
    )
    ids = [r.spec_id for r in run.records]
    # spec_001/002/003 keep their ids; spec_acmeproject_007 (from result_004) is renumbered.
    assert "spec_001" in ids
    assert "spec_002" in ids
    assert "spec_003" in ids
    assert all(rid != "spec_acmeproject_007" for rid in ids)
    # The renumber must be ``spec_<digits>``.
    import re

    assert all(re.match(r"^spec_\d+$", rid) for rid in ids)
    # The renumbered id starts past the highest existing standard id (003).
    renumbered = [
        rid for rid in ids if rid not in {"spec_001", "spec_002", "spec_003"}
    ]
    assert len(renumbered) == 1
    n = int(renumbered[0].removeprefix("spec_"))
    assert n >= 4


def test_backfill_project_dates_are_midnight_utc(tmp_path: Path) -> None:
    project = _stage_outbox(tmp_path)
    run = backfill_project(project, label="x", generation="bash_prototype")
    for r in run.records:
        assert r.started_at.hour == 0
        assert r.started_at.minute == 0
        assert r.started_at.second == 0
        assert r.started_at.tzinfo is not None
        assert r.started_at.utcoffset() == UTC.utcoffset(None)
        if r.finished_at is not None:
            assert r.finished_at.hour == 0
            assert r.finished_at.minute == 0
            assert r.finished_at.second == 0


def test_backfill_project_missing_results_dir_returns_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        run = backfill_project(
            tmp_path, label="empty", generation="bash_prototype"
        )
    assert run.records == []
    assert any("does not exist" in m for m in caplog.messages)


# --------------------------------------------------------------------------- #
# Output / round-trip                                                          #
# --------------------------------------------------------------------------- #


def test_write_run_file_excludes_body_and_commits(tmp_path: Path) -> None:
    project = _stage_outbox(tmp_path)
    run = backfill_project(project, label="x", generation="bash_prototype")

    out_dir = tmp_path / "runs"
    written = write_run_file(run, out_dir)
    text = written.read_text(encoding="utf-8")

    # No prose body should appear in the JSON.
    for marker in (
        "SECRET_BODY_MARKER_001",
        "SECRET_BODY_MARKER_002",
        "SECRET_BODY_MARKER_003",
        "SECRET_BODY_MARKER_004",
    ):
        assert marker not in text
    # No commit hashes either.
    assert "0123456789abcdef" not in text
    assert "deadbeefcafe" not in text
    # No "Branch:" / "Executor:" / "Author:" labels survive — those headers
    # were never captured as DispatchRecord fields.
    assert "Executor" not in text
    assert "Branch" not in text
    # And the client-named spec_id was anonymized.
    assert "acmeproject" not in text


def test_write_run_file_slugifies_label(tmp_path: Path) -> None:
    run = RunFile(
        version=1,
        saved_at=datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        project="Spaces and / slashes",
        generation="bash_prototype",
        records=[],
    )
    out = write_run_file(run, tmp_path)
    assert out.name == "Spaces_and___slashes.json"


def test_run_file_round_trip(tmp_path: Path) -> None:
    project = _stage_outbox(tmp_path)
    run = backfill_project(project, label="x", generation="ccd_native")
    out = write_run_file(run, tmp_path / "runs")
    payload = json.loads(out.read_text(encoding="utf-8"))
    re_run = RunFile.model_validate(payload)
    assert re_run.project == "x"
    assert re_run.generation == "ccd_native"
    assert len(re_run.records) == len(run.records)
    assert re_run.records[0].spec_id == run.records[0].spec_id


def test_run_file_reads_legacy_envelope_without_project(tmp_path: Path) -> None:
    """The v1 ``cli.py:_save_run`` writes no project/generation. We must still parse it."""

    payload = {
        "version": 1,
        "saved_at": "2026-05-22T10:00:00+00:00",
        "records": [
            {
                "spec_id": "spec_001",
                "started_at": "2026-05-22T10:00:00+00:00",
                "finished_at": "2026-05-22T10:05:00+00:00",
                "status": "done",
                "attempts": 1,
                "failure_category": None,
                "intervention": False,
            }
        ],
        "chain": {
            "success": True,
            "halted_at": None,
            "halt_reason": "",
            "branches": ["feat/spec_001"],
        },
    }
    run = RunFile.model_validate(payload)
    assert run.project is None
    assert run.generation is None
    assert len(run.records) == 1
    # extra="allow" → chain block is preserved through model_dump.
    dumped = run.model_dump(mode="json")
    assert "chain" in dumped


# --------------------------------------------------------------------------- #
# Sources config / CLI                                                         #
# --------------------------------------------------------------------------- #


def test_load_sources_config(tmp_path: Path) -> None:
    cfg = tmp_path / "sources.json"
    cfg.write_text(
        json.dumps(
            [
                {
                    "path": str(tmp_path / "p1"),
                    "label": "axis-knowledge-rag",
                    "generation": "bash_prototype",
                },
                {
                    "path": str(tmp_path / "p2"),
                    "label": "実務案件A",
                    "generation": "ccd_native",
                    "results_dir": str(tmp_path / "p2" / "alt_outbox"),
                },
            ]
        ),
        encoding="utf-8",
    )
    sources = load_sources_config(cfg)
    assert len(sources) == 2
    assert sources[0].label == "axis-knowledge-rag"
    assert sources[1].label == "実務案件A"
    assert sources[1].results_dir == tmp_path / "p2" / "alt_outbox"


def test_load_sources_config_rejects_non_list(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.json"
    cfg.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_sources_config(cfg)


def test_load_sources_config_requires_path_and_label(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.json"
    cfg.write_text(
        json.dumps([{"path": "/x"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_sources_config(cfg)


def test_cli_main_with_source_flag_writes_run_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _stage_outbox(tmp_path)
    out_dir = tmp_path / "runs"
    rc = main(
        [
            "--source",
            str(project),
            "fixture",
            "bash_prototype",
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    files = list(out_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == "fixture.json"
    captured = capsys.readouterr()
    assert "fixture" in captured.out


def test_cli_main_with_no_sources_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # ensure no default config picked up
    rc = main(["--output-dir", str(tmp_path / "runs")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no sources" in err


def test_cli_main_with_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _stage_outbox(tmp_path)
    cfg = tmp_path / "sources.json"
    cfg.write_text(
        json.dumps(
            [
                {
                    "path": str(project),
                    "label": "labelA",
                    "generation": "bash_prototype",
                }
            ]
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "runs"
    rc = main(["--config", str(cfg), "--output-dir", str(out_dir)])
    assert rc == 0
    files = list(out_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == "labelA.json"


def test_backfill_source_dataclass_is_hashable_default_results_dir() -> None:
    s = BackfillSource(path=Path("/x"), label="a", generation="bash_prototype")
    assert s.results_dir is None
