"""Metrics aggregation: scoreboard + improvement-loop indicators over dispatch records.

`aggregate()` reads the `DispatchRecord`s in a `ChainResult` (or a raw sequence)
and produces a `MetricsReport` with the seven v1 metrics:

    Scoreboard
      1. dispatch success rate           (`status == DONE`)
      2. autonomous completion rate      (`status == DONE and not intervention`)
      3. safe halt rate                  (failures with a classified
                                          `failure_category` / all failures)
      4. duration per spec               (mean + median of
                                          `finished_at - started_at`)

    Improvement loop
      5. failure taxonomy                (`FailureCategory` breakdown over
                                          non-DONE specs)
      6. first-pass rate                 (`attempts == 1 and status == DONE`)
      7. retry recovery rate             (`attempts > 1 and status == DONE`
                                          / `attempts > 1`)

`render_report()` formats the same data as a Markdown string.

Rates whose denominator is 0 report `value=0.0` alongside the raw 0/0
counts — keeps `MetricsReport` JSON-stable and lets readers see the empty
denominator instead of a bare "100%" or "n/a".
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .chain import ChainResult
from .models import DispatchRecord, DispatchStatus, FailureCategory


class Rate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float = Field(ge=0.0, le=1.0)


class DurationStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    samples: int = Field(ge=0)
    mean_seconds: float = Field(ge=0.0)
    median_seconds: float = Field(ge=0.0)


class FailureBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: FailureCategory | None
    count: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0)


class MetricsReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_specs: int = Field(ge=0)
    done: int = Field(ge=0)
    failures: int = Field(ge=0)

    dispatch_success_rate: Rate
    autonomous_completion_rate: Rate
    safe_halt_rate: Rate
    duration: DurationStats

    first_pass_rate: Rate
    retry_recovery_rate: Rate
    failure_taxonomy: tuple[FailureBreakdown, ...]


MetricsSource = ChainResult | Sequence[DispatchRecord]


def aggregate(source: MetricsSource) -> MetricsReport:
    """Aggregate a `ChainResult` (or `DispatchRecord` sequence) into a `MetricsReport`."""

    records = _records_of(source)
    total = len(records)
    done_records = [r for r in records if r.status is DispatchStatus.DONE]
    fail_records = [r for r in records if r.status is not DispatchStatus.DONE]

    done = len(done_records)
    failures = len(fail_records)

    dispatch_success_rate = _rate(done, total)
    autonomous_completion_rate = _rate(
        sum(1 for r in done_records if not r.intervention),
        total,
    )
    safe_halt_rate = _rate(
        sum(1 for r in fail_records if r.failure_category is not None),
        failures,
    )

    duration = _duration_stats(records)

    first_pass_rate = _rate(
        sum(1 for r in done_records if r.attempts == 1),
        total,
    )
    retried = [r for r in records if r.attempts > 1]
    retry_recovery_rate = _rate(
        sum(1 for r in retried if r.status is DispatchStatus.DONE),
        len(retried),
    )

    failure_taxonomy = _failure_taxonomy(fail_records)

    return MetricsReport(
        total_specs=total,
        done=done,
        failures=failures,
        dispatch_success_rate=dispatch_success_rate,
        autonomous_completion_rate=autonomous_completion_rate,
        safe_halt_rate=safe_halt_rate,
        duration=duration,
        first_pass_rate=first_pass_rate,
        retry_recovery_rate=retry_recovery_rate,
        failure_taxonomy=failure_taxonomy,
    )


def render_report(report: MetricsReport) -> str:
    """Render a `MetricsReport` as a human-readable Markdown string."""

    def fmt(r: Rate) -> str:
        return f"{r.numerator}/{r.denominator} ({r.value:.1%})"

    lines: list[str] = [
        "# Metrics report",
        "",
        f"- Total specs: {report.total_specs}",
        f"- Done: {report.done}",
        f"- Failures: {report.failures}",
        "",
        "## Scoreboard",
        "",
        f"- Dispatch success rate: {fmt(report.dispatch_success_rate)}",
        f"- Autonomous completion rate: {fmt(report.autonomous_completion_rate)}",
        f"- Safe halt rate: {fmt(report.safe_halt_rate)}",
        (
            f"- Duration: mean {report.duration.mean_seconds:.2f}s, "
            f"median {report.duration.median_seconds:.2f}s "
            f"(n={report.duration.samples})"
        ),
        "",
        "## Improvement loop",
        "",
        f"- First-pass rate: {fmt(report.first_pass_rate)}",
        f"- Retry recovery rate: {fmt(report.retry_recovery_rate)}",
        "",
        "## Failure taxonomy",
        "",
    ]
    if not report.failure_taxonomy:
        lines.append("- (no failures)")
    else:
        for item in report.failure_taxonomy:
            name = item.category.value if item.category is not None else "unknown"
            lines.append(f"- {name}: {item.count} ({item.share:.1%})")
    lines.append("")
    return "\n".join(lines)


def _records_of(source: MetricsSource) -> list[DispatchRecord]:
    if isinstance(source, ChainResult):
        return [step.dispatch for step in source.steps]
    return list(source)


def _rate(numerator: int, denominator: int) -> Rate:
    if denominator == 0:
        return Rate(numerator=0, denominator=0, value=0.0)
    return Rate(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
    )


def _duration_stats(records: Iterable[DispatchRecord]) -> DurationStats:
    durations: list[float] = []
    for r in records:
        if r.finished_at is None:
            continue
        seconds = (r.finished_at - r.started_at).total_seconds()
        if seconds < 0:
            continue
        durations.append(seconds)
    if not durations:
        return DurationStats(samples=0, mean_seconds=0.0, median_seconds=0.0)
    return DurationStats(
        samples=len(durations),
        mean_seconds=statistics.fmean(durations),
        median_seconds=statistics.median(durations),
    )


def _failure_taxonomy(
    fail_records: Sequence[DispatchRecord],
) -> tuple[FailureBreakdown, ...]:
    if not fail_records:
        return ()
    counts: dict[FailureCategory | None, int] = {}
    for r in fail_records:
        counts[r.failure_category] = counts.get(r.failure_category, 0) + 1
    total = len(fail_records)
    items: list[FailureBreakdown] = []
    # Stable order: enum declaration order, then "unknown" (None) last.
    for cat in FailureCategory:
        if cat in counts:
            items.append(
                FailureBreakdown(category=cat, count=counts[cat], share=counts[cat] / total)
            )
    if None in counts:
        items.append(
            FailureBreakdown(category=None, count=counts[None], share=counts[None] / total)
        )
    return tuple(items)
