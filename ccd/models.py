"""Core data types for Cowork-CC-dispatch.

The fields on `DispatchRecord` are the input surface that spec_005's metrics
aggregation will read — keep them in sync if you add a new metric.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DispatchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    HALTED = "halted"


class FailureCategory(StrEnum):
    SPEC_UNCLEAR = "spec_unclear"
    AGENT_MISREAD = "agent_misread"
    SMOKE_FAILED = "smoke_failed"
    MERGE_CONFLICT = "merge_conflict"
    ENVIRONMENT = "environment"
    TRANSIENT = "transient"


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str
    path: Path


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_id: str = Field(min_length=1)
    status: DispatchStatus
    body: str
    commits: list[str] = Field(default_factory=list)
    failure_category: FailureCategory | None = None


class DispatchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_id: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime | None = None
    status: DispatchStatus
    attempts: int = Field(ge=0)
    failure_category: FailureCategory | None = None
    intervention: bool = False
