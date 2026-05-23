from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ccd.models import (
    DispatchRecord,
    DispatchStatus,
    FailureCategory,
    Result,
    RunFile,
    Spec,
)


def test_dispatch_status_values() -> None:
    assert {s.value for s in DispatchStatus} == {
        "pending",
        "running",
        "done",
        "failed",
        "blocked",
        "halted",
        "partial",
    }


def test_failure_category_values() -> None:
    assert {c.value for c in FailureCategory} == {
        "spec_unclear",
        "agent_misread",
        "smoke_failed",
        "merge_conflict",
        "environment",
        "transient",
        "interrupted",
    }


def test_spec_holds_required_fields(tmp_path: Path) -> None:
    p = tmp_path / "spec_010.md"
    spec = Spec(id="spec_010", title="example", body="hello", path=p)
    assert spec.id == "spec_010"
    assert spec.title == "example"
    assert spec.body == "hello"
    assert spec.path == p


def test_spec_is_frozen(tmp_path: Path) -> None:
    spec = Spec(id="spec_010", title="x", body="", path=tmp_path / "s.md")
    with pytest.raises(ValidationError):
        spec.title = "y"  # type: ignore[misc]


def test_spec_rejects_empty_id_or_title(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Spec(id="", title="t", body="b", path=tmp_path / "s.md")
    with pytest.raises(ValidationError):
        Spec(id="spec_001", title="", body="b", path=tmp_path / "s.md")


def test_spec_forbids_extra_fields(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Spec(  # type: ignore[call-arg]
            id="spec_010",
            title="t",
            body="",
            path=tmp_path / "s.md",
            unknown="x",
        )


def test_result_defaults() -> None:
    r = Result(spec_id="spec_001", status=DispatchStatus.DONE, body="ok")
    assert r.commits == []
    assert r.failure_category is None


def test_result_failure_category_round_trip() -> None:
    r = Result(
        spec_id="spec_001",
        status=DispatchStatus.FAILED,
        body="boom",
        commits=["abc1234"],
        failure_category=FailureCategory.SMOKE_FAILED,
    )
    assert r.failure_category is FailureCategory.SMOKE_FAILED
    assert r.commits == ["abc1234"]


def test_result_status_accepts_string() -> None:
    r = Result(spec_id="spec_001", status="done", body="")  # type: ignore[arg-type]
    assert r.status is DispatchStatus.DONE


def test_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        Result(spec_id="spec_001", status="weird", body="")  # type: ignore[arg-type]


def test_dispatch_record_minimum_fields() -> None:
    started = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
    record = DispatchRecord(
        spec_id="spec_001",
        started_at=started,
        status=DispatchStatus.RUNNING,
        attempts=1,
    )
    assert record.finished_at is None
    assert record.failure_category is None
    assert record.intervention is False


def test_dispatch_record_full_fields() -> None:
    started = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
    finished = datetime(2026, 5, 22, 10, 30, tzinfo=UTC)
    record = DispatchRecord(
        spec_id="spec_001",
        started_at=started,
        finished_at=finished,
        status=DispatchStatus.FAILED,
        attempts=2,
        failure_category=FailureCategory.TRANSIENT,
        intervention=True,
    )
    assert record.finished_at == finished
    assert record.failure_category is FailureCategory.TRANSIENT
    assert record.intervention is True


def test_dispatch_record_rejects_negative_attempts() -> None:
    with pytest.raises(ValidationError):
        DispatchRecord(
            spec_id="spec_001",
            started_at=datetime(2026, 5, 22, tzinfo=UTC),
            status=DispatchStatus.RUNNING,
            attempts=-1,
        )


def test_run_file_defaults() -> None:
    run = RunFile()
    assert run.version == 1
    assert run.saved_at is None
    assert run.project is None
    assert run.generation is None
    assert run.records == []


def test_run_file_accepts_extra_keys() -> None:
    run = RunFile.model_validate(
        {
            "version": 1,
            "saved_at": "2026-05-22T00:00:00+00:00",
            "records": [],
            "chain": {"success": True, "branches": []},
        }
    )
    dumped = run.model_dump(mode="json")
    assert dumped["chain"] == {"success": True, "branches": []}
