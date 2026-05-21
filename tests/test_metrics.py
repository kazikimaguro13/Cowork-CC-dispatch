from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ccd.chain import ChainResult, ChainStep
from ccd.integrate import IntegrateResult
from ccd.metrics import (
    DurationStats,
    FailureBreakdown,
    MetricsReport,
    Rate,
    aggregate,
    render_report,
)
from ccd.models import DispatchRecord, DispatchStatus, FailureCategory

_T0 = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)


def _rec(
    spec_id: str,
    *,
    status: DispatchStatus,
    duration_s: float | None = 60.0,
    attempts: int = 1,
    failure_category: FailureCategory | None = None,
    intervention: bool = False,
) -> DispatchRecord:
    finished_at = None if duration_s is None else _T0 + timedelta(seconds=duration_s)
    return DispatchRecord(
        spec_id=spec_id,
        started_at=_T0,
        finished_at=finished_at,
        status=status,
        attempts=attempts,
        failure_category=failure_category,
        intervention=intervention,
    )


def test_aggregate_on_empty_sequence_returns_zero_rates() -> None:
    report = aggregate([])

    assert isinstance(report, MetricsReport)
    assert report.total_specs == 0
    assert report.done == 0
    assert report.failures == 0
    # All denominators are 0 → values are 0.0, never NaN.
    for rate in (
        report.dispatch_success_rate,
        report.autonomous_completion_rate,
        report.safe_halt_rate,
        report.first_pass_rate,
        report.retry_recovery_rate,
    ):
        assert isinstance(rate, Rate)
        assert rate.denominator == 0
        assert rate.numerator == 0
        assert rate.value == 0.0
    assert report.duration == DurationStats(samples=0, mean_seconds=0.0, median_seconds=0.0)
    assert report.failure_taxonomy == ()


def test_aggregate_all_done_no_intervention_is_perfect_score() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.DONE, duration_s=60.0),
        _rec("spec_002", status=DispatchStatus.DONE, duration_s=120.0),
        _rec("spec_003", status=DispatchStatus.DONE, duration_s=180.0),
    ]

    report = aggregate(records)

    assert report.total_specs == 3
    assert report.done == 3
    assert report.failures == 0
    assert report.dispatch_success_rate.value == 1.0
    assert report.autonomous_completion_rate.value == 1.0
    # Safe halt rate is undefined (no failures) → denominator 0, value 0.0
    assert report.safe_halt_rate.denominator == 0
    assert report.safe_halt_rate.value == 0.0
    assert report.first_pass_rate.value == 1.0
    # No retries → denominator 0
    assert report.retry_recovery_rate.denominator == 0
    assert report.duration.samples == 3
    assert report.duration.mean_seconds == 120.0
    assert report.duration.median_seconds == 120.0
    assert report.failure_taxonomy == ()


def test_aggregate_mixed_outcomes_computes_each_metric() -> None:
    records = [
        # 2 successes, no intervention
        _rec("spec_001", status=DispatchStatus.DONE, duration_s=60.0),
        _rec("spec_002", status=DispatchStatus.DONE, duration_s=120.0),
        # 1 success but with human intervention
        _rec(
            "spec_003",
            status=DispatchStatus.DONE,
            duration_s=200.0,
            intervention=True,
        ),
        # 1 success after retry
        _rec(
            "spec_004",
            status=DispatchStatus.DONE,
            duration_s=300.0,
            attempts=2,
        ),
        # 1 classified failure (safe halt)
        _rec(
            "spec_005",
            status=DispatchStatus.FAILED,
            duration_s=10.0,
            failure_category=FailureCategory.SMOKE_FAILED,
        ),
        # 1 unclassified failure (unsafe halt: cause unknown)
        _rec(
            "spec_006",
            status=DispatchStatus.FAILED,
            duration_s=10.0,
            failure_category=None,
        ),
        # 1 blocked with category
        _rec(
            "spec_007",
            status=DispatchStatus.BLOCKED,
            duration_s=5.0,
            failure_category=FailureCategory.SPEC_UNCLEAR,
        ),
        # 1 retry that didn't recover
        _rec(
            "spec_008",
            status=DispatchStatus.FAILED,
            duration_s=10.0,
            attempts=3,
            failure_category=FailureCategory.TRANSIENT,
        ),
    ]

    report = aggregate(records)

    assert report.total_specs == 8
    assert report.done == 4
    assert report.failures == 4

    # 1. dispatch success rate: 4/8 = 50%
    assert report.dispatch_success_rate.numerator == 4
    assert report.dispatch_success_rate.denominator == 8
    assert report.dispatch_success_rate.value == 0.5

    # 2. autonomous completion rate: done & no intervention = 3/8
    assert report.autonomous_completion_rate.numerator == 3
    assert report.autonomous_completion_rate.denominator == 8
    assert report.autonomous_completion_rate.value == 0.375

    # 3. safe halt rate: failures with classified category = 3/4
    assert report.safe_halt_rate.numerator == 3
    assert report.safe_halt_rate.denominator == 4
    assert report.safe_halt_rate.value == 0.75

    # 4. duration stats: 8 samples
    assert report.duration.samples == 8
    # samples: 60, 120, 200, 300, 10, 10, 5, 10
    # mean = 715 / 8 = 89.375
    assert report.duration.mean_seconds == 89.375
    # sorted: 5, 10, 10, 10, 60, 120, 200, 300 → median = (10+60)/2 = 35
    assert report.duration.median_seconds == 35.0

    # 5. failure taxonomy
    by_cat = {b.category: b for b in report.failure_taxonomy}
    assert by_cat[FailureCategory.SMOKE_FAILED].count == 1
    assert by_cat[FailureCategory.SPEC_UNCLEAR].count == 1
    assert by_cat[FailureCategory.TRANSIENT].count == 1
    assert by_cat[None].count == 1
    for breakdown in report.failure_taxonomy:
        assert breakdown.share == 0.25
    # "unknown" (None) is last
    assert report.failure_taxonomy[-1].category is None
    # Counts sum to total failures
    assert sum(b.count for b in report.failure_taxonomy) == 4

    # 6. first-pass rate: attempts==1 AND done = 3/8
    assert report.first_pass_rate.numerator == 3
    assert report.first_pass_rate.denominator == 8
    assert report.first_pass_rate.value == 0.375

    # 7. retry recovery rate: 1 done after retry / 2 retried = 0.5
    assert report.retry_recovery_rate.numerator == 1
    assert report.retry_recovery_rate.denominator == 2
    assert report.retry_recovery_rate.value == 0.5


def test_aggregate_groups_same_failure_category() -> None:
    records = [
        _rec(
            "spec_001",
            status=DispatchStatus.FAILED,
            failure_category=FailureCategory.AGENT_MISREAD,
        ),
        _rec(
            "spec_002",
            status=DispatchStatus.FAILED,
            failure_category=FailureCategory.AGENT_MISREAD,
        ),
        _rec(
            "spec_003",
            status=DispatchStatus.FAILED,
            failure_category=FailureCategory.SMOKE_FAILED,
        ),
    ]

    report = aggregate(records)

    breakdowns = {b.category: b for b in report.failure_taxonomy}
    assert breakdowns[FailureCategory.AGENT_MISREAD].count == 2
    assert breakdowns[FailureCategory.AGENT_MISREAD].share == 2 / 3
    assert breakdowns[FailureCategory.SMOKE_FAILED].count == 1


def test_aggregate_skips_records_without_finished_at() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.DONE, duration_s=60.0),
        _rec("spec_002", status=DispatchStatus.RUNNING, duration_s=None),
    ]

    report = aggregate(records)

    assert report.duration.samples == 1
    assert report.duration.mean_seconds == 60.0


def test_aggregate_accepts_chain_result() -> None:
    rec = _rec("spec_001", status=DispatchStatus.DONE, duration_s=42.0)
    step = ChainStep(
        spec_id="spec_001",
        branch="feat/spec_001",
        dispatch=rec,
        integrate=IntegrateResult(
            spec_id="spec_001",
            success=True,
            merged=True,
            smoke=None,
        ),
    )
    chain_result = ChainResult(steps=(step,), success=True, halted_at=None)

    report = aggregate(chain_result)

    assert report.total_specs == 1
    assert report.done == 1
    assert report.dispatch_success_rate.value == 1.0


def test_render_report_includes_all_seven_metrics() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.DONE, duration_s=60.0),
        _rec(
            "spec_002",
            status=DispatchStatus.FAILED,
            duration_s=10.0,
            failure_category=FailureCategory.SMOKE_FAILED,
        ),
    ]

    text = render_report(aggregate(records))

    assert isinstance(text, str)
    # Each of the 7 metrics is present somewhere in the report text.
    assert "Dispatch success rate" in text
    assert "Autonomous completion rate" in text
    assert "Safe halt rate" in text
    assert "Duration" in text
    assert "First-pass rate" in text
    assert "Retry recovery rate" in text
    assert "Failure taxonomy" in text
    # Failure category is listed by enum value.
    assert "smoke_failed" in text


def test_render_report_handles_empty_input() -> None:
    text = render_report(aggregate([]))

    assert isinstance(text, str)
    assert "Total specs: 0" in text
    assert "(no failures)" in text


def test_failure_breakdown_is_frozen_pydantic_model() -> None:
    breakdown = FailureBreakdown(
        category=FailureCategory.TRANSIENT,
        count=1,
        share=0.5,
    )
    assert breakdown.category is FailureCategory.TRANSIENT
    assert breakdown.count == 1
