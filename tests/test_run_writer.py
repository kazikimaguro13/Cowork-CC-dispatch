"""Tests for ccd/run_writer.py — atomic/incremental persistence + reconcile."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccd.models import DispatchRecord, DispatchStatus, FailureCategory
from ccd.run_writer import (
    RunWriter,
    halted_interrupted_record,
    reconcile_path,
    reconcile_run_file,
)

_T0 = datetime(2026, 5, 23, 10, 0, tzinfo=UTC)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_start_writes_running_marker(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    writer = RunWriter(path)
    writer.start("spec_001", started_at=_T0)

    assert path.exists()
    payload = _read(path)
    assert len(payload["records"]) == 1
    rec = payload["records"][0]
    assert rec["spec_id"] == "spec_001"
    assert rec["status"] == "running"
    assert rec["finished_at"] is None
    assert rec["attempts"] == 1
    assert rec["failure_category"] is None


def test_finish_replaces_running_with_final_record(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    writer = RunWriter(path)
    writer.start("spec_001", started_at=_T0)

    final = DispatchRecord(
        spec_id="spec_001",
        started_at=_T0,
        finished_at=_T0,
        status=DispatchStatus.DONE,
        attempts=1,
    )
    writer.finish(final)

    payload = _read(path)
    assert len(payload["records"]) == 1
    rec = payload["records"][0]
    assert rec["status"] == "done"
    # No RUNNING marker leftover.
    assert all(r["status"] != "running" for r in payload["records"])


def test_multiple_specs_accumulate_incrementally(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    writer = RunWriter(path)

    writer.start("spec_001", started_at=_T0)
    writer.finish(
        DispatchRecord(
            spec_id="spec_001",
            started_at=_T0,
            finished_at=_T0,
            status=DispatchStatus.DONE,
            attempts=1,
        )
    )
    # Disk state mid-chain: spec_001 done + spec_002 running.
    writer.start("spec_002", started_at=_T0)
    payload_mid = _read(path)
    assert len(payload_mid["records"]) == 2
    assert payload_mid["records"][0]["status"] == "done"
    assert payload_mid["records"][1]["status"] == "running"

    writer.finish(
        DispatchRecord(
            spec_id="spec_002",
            started_at=_T0,
            finished_at=_T0,
            status=DispatchStatus.DONE,
            attempts=1,
        )
    )
    payload_end = _read(path)
    assert len(payload_end["records"]) == 2
    assert all(r["status"] == "done" for r in payload_end["records"])


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    writer = RunWriter(path)
    writer.start("spec_001", started_at=_T0)
    writer.finish(
        DispatchRecord(
            spec_id="spec_001",
            started_at=_T0,
            finished_at=_T0,
            status=DispatchStatus.DONE,
            attempts=1,
        )
    )

    # No stray .tmp from the atomic-replace flow.
    leftover = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftover == []


# --------------------------------------------------------------------------- #
# Reconcile                                                                   #
# --------------------------------------------------------------------------- #


def test_reconcile_run_file_converts_running_to_halted_interrupted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-23T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_001",
                        "started_at": "2026-05-23T10:00:00+00:00",
                        "finished_at": "2026-05-23T10:05:00+00:00",
                        "status": "done",
                        "attempts": 1,
                        "failure_category": None,
                        "intervention": False,
                    },
                    {
                        "spec_id": "spec_002",
                        "started_at": "2026-05-23T10:10:00+00:00",
                        "finished_at": None,
                        "status": "running",
                        "attempts": 1,
                        "failure_category": None,
                        "intervention": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    n = reconcile_run_file(path)
    assert n == 1

    payload = _read(path)
    # First record untouched.
    assert payload["records"][0]["status"] == "done"
    # Second record reconciled.
    rec = payload["records"][1]
    assert rec["status"] == "halted"
    assert rec["failure_category"] == "interrupted"
    # finished_at is NOT invented.
    assert rec["finished_at"] is None


def test_reconcile_run_file_no_running_returns_zero(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-23T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_001",
                        "started_at": "2026-05-23T10:00:00+00:00",
                        "finished_at": "2026-05-23T10:05:00+00:00",
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
    saved_before = _read(path)["saved_at"]
    n = reconcile_run_file(path)
    assert n == 0
    # File untouched (saved_at not rewritten).
    assert _read(path)["saved_at"] == saved_before


def test_reconcile_path_on_directory(tmp_path: Path) -> None:
    for i, status in enumerate(["running", "done", "running"]):
        path = tmp_path / f"run_{i}.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "saved_at": "2026-05-23T00:00:00+00:00",
                    "records": [
                        {
                            "spec_id": f"spec_{i:03d}",
                            "started_at": "2026-05-23T10:00:00+00:00",
                            "finished_at": None
                            if status == "running"
                            else "2026-05-23T10:05:00+00:00",
                            "status": status,
                            "attempts": 1,
                            "failure_category": None,
                            "intervention": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    files, records = reconcile_path(tmp_path)
    assert files == 2
    assert records == 2


def test_reconcile_path_on_missing_target_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        reconcile_path(tmp_path / "nope")


# --------------------------------------------------------------------------- #
# Auto carry-forward                                                          #
# --------------------------------------------------------------------------- #


def test_salvage_orphans_carries_forward_and_reconciles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-22T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_old",
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

    writer = RunWriter(path)
    salvaged = writer.salvage_orphans()
    assert salvaged == 1

    payload = _read(path)
    assert len(payload["records"]) == 1
    rec = payload["records"][0]
    assert rec["spec_id"] == "spec_old"
    assert rec["status"] == "halted"
    assert rec["failure_category"] == "interrupted"

    err = capsys.readouterr().err
    assert "salvaged 1" in err

    # A new dispatch on the same path appends to the carry-forward records.
    writer.start("spec_new", started_at=_T0)
    writer.finish(
        DispatchRecord(
            spec_id="spec_new",
            started_at=_T0,
            finished_at=_T0,
            status=DispatchStatus.DONE,
            attempts=1,
        )
    )
    payload = _read(path)
    assert [r["spec_id"] for r in payload["records"]] == ["spec_old", "spec_new"]
    assert [r["status"] for r in payload["records"]] == ["halted", "done"]


def test_salvage_orphans_noop_when_no_running(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-05-22T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_old",
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

    writer = RunWriter(path)
    assert writer.salvage_orphans() == 0
    # Existing file untouched until a real write happens.
    assert _read(path)["records"][0]["spec_id"] == "spec_old"

    # The previous DONE record is NOT auto-carried (no RUNNING orphan to rescue).
    # When a new spec runs, only the new record appears — matching pre-spec_010
    # behavior for the common "no orphans" path.
    writer.start("spec_new", started_at=_T0)
    payload = _read(path)
    spec_ids = [r["spec_id"] for r in payload["records"]]
    assert "spec_new" in spec_ids
    assert "spec_old" not in spec_ids


def test_salvage_orphans_missing_file_returns_zero(tmp_path: Path) -> None:
    writer = RunWriter(tmp_path / "does-not-exist.json")
    assert writer.salvage_orphans() == 0


# --------------------------------------------------------------------------- #
# halted_interrupted_record helper                                            #
# --------------------------------------------------------------------------- #


def test_halted_interrupted_record_does_not_invent_finished_at() -> None:
    rec = halted_interrupted_record("spec_001", started_at=_T0)
    assert rec.status is DispatchStatus.HALTED
    assert rec.failure_category is FailureCategory.INTERRUPTED
    assert rec.finished_at is None
    assert rec.attempts == 1
    assert rec.intervention is False
