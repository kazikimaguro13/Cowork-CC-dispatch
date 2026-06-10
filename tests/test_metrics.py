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


# --------------------------------------------------------------------------- #
# spec_009: PARTIAL counted independently (not done, not failure)             #
# --------------------------------------------------------------------------- #


def test_partial_records_are_counted_independently() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.DONE, duration_s=60.0),
        _rec("spec_002", status=DispatchStatus.DONE, duration_s=60.0),
        _rec("spec_003", status=DispatchStatus.PARTIAL, duration_s=60.0),
        _rec("spec_004", status=DispatchStatus.PARTIAL, duration_s=60.0),
        _rec(
            "spec_005",
            status=DispatchStatus.FAILED,
            duration_s=10.0,
            failure_category=FailureCategory.SMOKE_FAILED,
        ),
    ]

    report = aggregate(records)

    assert report.total_specs == 5
    assert report.done == 2
    assert report.partial == 2
    assert report.failures == 1
    # done numerator excludes PARTIAL; PARTIAL is NOT added to the success rate.
    assert report.dispatch_success_rate.numerator == 2
    assert report.dispatch_success_rate.denominator == 5
    # autonomous completion likewise excludes PARTIAL (same numerator).
    assert report.autonomous_completion_rate.numerator == 2
    # PARTIAL is not in the failure taxonomy; only the 1 real failure counts.
    assert sum(b.count for b in report.failure_taxonomy) == 1
    # And safe-halt rate denominator is failures only (1), not failures + partials.
    assert report.safe_halt_rate.denominator == 1


def test_partial_is_not_treated_as_failure_for_safe_halt() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.PARTIAL, duration_s=60.0),
        _rec("spec_002", status=DispatchStatus.PARTIAL, duration_s=60.0),
    ]

    report = aggregate(records)

    assert report.partial == 2
    assert report.failures == 0
    # PARTIAL must not bleed into the failure taxonomy at all.
    assert report.failure_taxonomy == ()
    # And safe_halt_rate has no denominator (no failures).
    assert report.safe_halt_rate.denominator == 0


def test_aggregate_default_partial_zero_when_no_partial_records() -> None:
    records = [_rec("spec_001", status=DispatchStatus.DONE, duration_s=60.0)]

    report = aggregate(records)

    assert report.partial == 0


def test_render_report_surfaces_partial_count() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.DONE),
        _rec("spec_002", status=DispatchStatus.PARTIAL),
    ]

    text = render_report(aggregate(records))

    assert "Partial: 1" in text
    assert "Done: 1" in text


# --------------------------------------------------------------------------- #
# spec_010: RUNNING is not a failure (and HALTED+INTERRUPTED is)              #
# --------------------------------------------------------------------------- #


def test_running_records_are_counted_independently() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.DONE),
        _rec("spec_002", status=DispatchStatus.RUNNING, duration_s=None),
        _rec("spec_003", status=DispatchStatus.RUNNING, duration_s=None),
    ]

    report = aggregate(records)

    assert report.total_specs == 3
    assert report.done == 1
    assert report.running == 2
    # RUNNING must not bleed into failures — "still in progress" is not "failed".
    assert report.failures == 0
    assert report.failure_taxonomy == ()
    # Safe-halt rate denominator must not include RUNNING.
    assert report.safe_halt_rate.denominator == 0


def test_running_excluded_from_success_rate_denominator_is_still_total() -> None:
    """RUNNING is counted in total_specs (it is a record) but not as success."""

    records = [
        _rec("spec_001", status=DispatchStatus.DONE),
        _rec("spec_002", status=DispatchStatus.RUNNING, duration_s=None),
    ]

    report = aggregate(records)

    # Per spec: RUNNING is reported under `running` and not bucketed elsewhere.
    # Success rate stays honest: 1 done out of 2 records.
    assert report.dispatch_success_rate.numerator == 1
    assert report.dispatch_success_rate.denominator == 2


def test_aggregate_default_running_zero_when_no_running_records() -> None:
    records = [_rec("spec_001", status=DispatchStatus.DONE)]

    report = aggregate(records)

    assert report.running == 0


def test_render_report_surfaces_running_count() -> None:
    records = [
        _rec("spec_001", status=DispatchStatus.DONE),
        _rec("spec_002", status=DispatchStatus.RUNNING, duration_s=None),
    ]

    text = render_report(aggregate(records))

    assert "Running: 1" in text


def test_interrupted_appears_in_failure_taxonomy_and_is_safe_halted() -> None:
    """HALTED + INTERRUPTED is a classified failure: it lands in the taxonomy
    and counts toward the safe_halt_rate numerator (we know the cause)."""

    records = [
        _rec(
            "spec_001",
            status=DispatchStatus.HALTED,
            duration_s=None,
            failure_category=FailureCategory.INTERRUPTED,
        ),
        _rec(
            "spec_002",
            status=DispatchStatus.FAILED,
            failure_category=FailureCategory.SMOKE_FAILED,
        ),
    ]

    report = aggregate(records)

    by_cat = {b.category: b for b in report.failure_taxonomy}
    assert FailureCategory.INTERRUPTED in by_cat
    assert by_cat[FailureCategory.INTERRUPTED].count == 1
    # Both failures are classified → safe halt rate is 2/2.
    assert report.safe_halt_rate.numerator == 2
    assert report.safe_halt_rate.denominator == 2


# --------------------------------------------------------------------------- #
# spec_042 — v3 nightly metrics tests
# --------------------------------------------------------------------------- #


import json  # noqa: E402

from ccd.metrics import (  # noqa: E402
    NightSnapshot,
    V3MetricsReport,
    V3Rate,
    WorkerInterval,
    aggregate_v3,
    load_night_snapshots,
    render_v3_report,
)


def _snap(
    night_id: str,
    *,
    fix_loop_starts: int = 0,
    converged: int = 0,
    iterations_to_green: tuple[int, ...] = (),
    merges: int = 0,
    parallelism: int = 1,
    achieved_max_concurrency: int = 1,
    drop_reasons: tuple[str, ...] = (),
    worker_intervals: tuple[WorkerInterval, ...] = (),
    total_dispatch_seconds: float | None = None,
) -> NightSnapshot:
    return NightSnapshot(
        night_id=night_id,
        fix_loop_starts=fix_loop_starts,
        converged=converged,
        iterations_to_green=iterations_to_green,
        merges=merges,
        parallelism=parallelism,
        achieved_max_concurrency=achieved_max_concurrency,
        drop_reasons=drop_reasons,
        worker_intervals=worker_intervals,
        total_dispatch_seconds=total_dispatch_seconds,
    )


def test_v3_empty_input_produces_zero_rates_with_population_notes() -> None:
    report = aggregate_v3([])

    assert isinstance(report, V3MetricsReport)
    assert report.nights == 0
    assert report.fix_loop_starts == 0
    assert report.total_merges == 0
    # All rates: 0/0 with value 0.0 and a non-empty population note.
    for r in (report.convergence_rate, report.conflict_drop_rate):
        assert isinstance(r, V3Rate)
        assert r.denominator == 0
        assert r.value == 0.0
        assert r.population_note  # non-empty
    assert report.iterations_to_green.samples == 0
    # No nights at all → dispatch-minutes is unknown (None) with a note.
    assert report.dispatch_minutes_per_merged_fix is None
    assert report.total_dispatch_minutes is None
    assert "計算不能" in report.dispatch_minutes_note
    # No workers anywhere → parallel yield is observable (0/0 with note).
    assert report.marginal_parallel_yield is not None
    assert report.marginal_parallel_yield.denominator == 0


def test_v3_single_night_all_converged_one_iteration_each() -> None:
    snap = _snap(
        "2026-06-01",
        fix_loop_starts=3,
        converged=3,
        iterations_to_green=(1, 1, 1),
        merges=3,
        parallelism=1,
        achieved_max_concurrency=1,
    )

    report = aggregate_v3([snap])

    assert report.convergence_rate.numerator == 3
    assert report.convergence_rate.denominator == 3
    assert report.convergence_rate.value == 1.0
    # Iteration histogram: all 1s → "loop is insurance" signal.
    assert report.iterations_to_green.one == 3
    assert report.iterations_to_green.two == 0
    assert report.iterations_to_green.three_or_more == 0
    assert report.iterations_to_green.samples == 3
    # No drops → drop rate 0/3 (merge=3, drop=0).
    assert report.conflict_drop_rate.numerator == 0
    assert report.conflict_drop_rate.denominator == 3
    assert report.drop_reasons == ()
    # No worker intervals → minutes-per-merge is 0.0 / merge=3 = 0.0
    # (total = 0 because no workers contributed time, but observable).
    assert report.dispatch_minutes_per_merged_fix == 0.0
    assert report.total_dispatch_minutes == 0.0


def test_v3_iterations_histogram_three_plus_bucket() -> None:
    snap = _snap(
        "2026-06-02",
        fix_loop_starts=5,
        converged=4,
        iterations_to_green=(1, 2, 3, 5),
        merges=4,
    )

    report = aggregate_v3([snap])

    # 1 → bucket "one"; 2 → bucket "two"; 3 and 5 → bucket "three_or_more".
    assert report.iterations_to_green.one == 1
    assert report.iterations_to_green.two == 1
    assert report.iterations_to_green.three_or_more == 2
    assert report.iterations_to_green.samples == 4
    # convergence: 4 of 5 starts.
    assert report.convergence_rate.numerator == 4
    assert report.convergence_rate.denominator == 5


def test_v3_convergence_rate_excludes_skipped_from_denominator() -> None:
    # 5 candidates entered the loop, only 2 converged, plus snapshot
    # implicitly excludes any skip outcomes (build_night_snapshot doesn't
    # count them toward fix_loop_starts).
    snap = _snap(
        "2026-06-03",
        fix_loop_starts=5,
        converged=2,
        iterations_to_green=(1, 2),
        merges=2,
    )

    report = aggregate_v3([snap])

    assert report.convergence_rate.numerator == 2
    assert report.convergence_rate.denominator == 5
    assert "skipped" in report.convergence_rate.population_note


def test_v3_drop_reasons_bucketed_into_known_categories() -> None:
    snap = _snap(
        "2026-06-04",
        fix_loop_starts=4,
        converged=4,
        iterations_to_green=(1, 1, 1, 1),
        merges=1,
        drop_reasons=(
            "max merges per night cap reached (1 merges, limit 1)",
            "apply failed in live re-verification",
            "PAUSE file detected before integration",
        ),
    )

    report = aggregate_v3([snap])

    # 1 merge + 3 drops = 4 processed.
    assert report.conflict_drop_rate.numerator == 3
    assert report.conflict_drop_rate.denominator == 4
    bucket_names = {b.category for b in report.drop_reasons}
    assert "cap" in bucket_names
    assert "conflict" in bucket_names
    assert "pause" in bucket_names


def test_v3_marginal_parallel_yield_overlap_counts() -> None:
    # Two workers that overlap → both their merges count toward overlap.
    snap = _snap(
        "2026-06-05",
        fix_loop_starts=2,
        converged=2,
        iterations_to_green=(1, 1),
        merges=2,
        parallelism=2,
        achieved_max_concurrency=2,
        worker_intervals=(
            WorkerInterval(
                worker_id="w1",
                started_at="2026-06-05T02:00:00+00:00",
                finished_at="2026-06-05T02:10:00+00:00",
                merged=True,
            ),
            WorkerInterval(
                worker_id="w2",
                started_at="2026-06-05T02:05:00+00:00",
                finished_at="2026-06-05T02:15:00+00:00",
                merged=True,
            ),
        ),
    )

    report = aggregate_v3([snap])

    assert report.marginal_parallel_yield is not None
    assert report.marginal_parallel_yield.numerator == 2
    assert report.marginal_parallel_yield.denominator == 2
    assert report.marginal_parallel_yield.value == 1.0


def test_v3_marginal_parallel_yield_no_overlap_when_serial() -> None:
    # Two workers run strictly back-to-back — no overlap.
    snap = _snap(
        "2026-06-06",
        fix_loop_starts=2,
        converged=2,
        iterations_to_green=(1, 1),
        merges=2,
        parallelism=2,
        achieved_max_concurrency=1,
        worker_intervals=(
            WorkerInterval(
                worker_id="w1",
                started_at="2026-06-06T02:00:00+00:00",
                finished_at="2026-06-06T02:05:00+00:00",
                merged=True,
            ),
            WorkerInterval(
                worker_id="w2",
                started_at="2026-06-06T02:05:00+00:00",
                finished_at="2026-06-06T02:10:00+00:00",
                merged=True,
            ),
        ),
    )

    report = aggregate_v3([snap])

    assert report.marginal_parallel_yield is not None
    assert report.marginal_parallel_yield.numerator == 0
    assert report.marginal_parallel_yield.denominator == 2


def test_v3_marginal_parallel_yield_unknown_when_timestamps_missing() -> None:
    # Two workers with no timestamps → night is unobservable.
    snap = _snap(
        "2026-06-07",
        fix_loop_starts=2,
        converged=2,
        iterations_to_green=(1, 1),
        merges=2,
        parallelism=2,
        achieved_max_concurrency=2,
        worker_intervals=(
            WorkerInterval(worker_id="w1", merged=True),
            WorkerInterval(worker_id="w2", merged=True),
        ),
    )

    report = aggregate_v3([snap])

    assert report.marginal_parallel_yield is None
    assert "不明" in report.marginal_parallel_yield_note
    assert "2026-06-07" in report.marginal_parallel_yield_note


def test_v3_dispatch_minutes_per_merged_fix_uses_intervals() -> None:
    snap = _snap(
        "2026-06-08",
        fix_loop_starts=1,
        converged=1,
        iterations_to_green=(1,),
        merges=1,
        parallelism=1,
        worker_intervals=(
            WorkerInterval(
                worker_id="w1",
                started_at="2026-06-08T02:00:00+00:00",
                finished_at="2026-06-08T02:12:00+00:00",
                merged=True,
            ),
        ),
    )

    report = aggregate_v3([snap])

    # 12 minutes / 1 merge = 12.0.
    assert report.dispatch_minutes_per_merged_fix == 12.0
    assert report.total_dispatch_minutes == 12.0


def test_v3_dispatch_minutes_when_merges_zero_shows_total_with_zero() -> None:
    snap = _snap(
        "2026-06-09",
        fix_loop_starts=1,
        converged=0,
        iterations_to_green=(),
        merges=0,
        parallelism=1,
        worker_intervals=(
            WorkerInterval(
                worker_id="w1",
                started_at="2026-06-09T02:00:00+00:00",
                finished_at="2026-06-09T02:20:00+00:00",
                merged=False,
            ),
        ),
    )

    report = aggregate_v3([snap])

    # merge=0 → rate is None; total minutes is shown, note flags merge=0.
    assert report.dispatch_minutes_per_merged_fix is None
    assert report.total_dispatch_minutes == 20.0
    assert "merge=0" in report.dispatch_minutes_note


def test_v3_backfill_old_records_without_v3_fields_do_not_crash() -> None:
    # An "old" record JSON might only have night_id; everything else
    # should fall back to defaults and aggregate without raising.
    payload = {"night_id": "2026-05-01"}
    old_snap = NightSnapshot.model_validate(payload)

    # parallelism + achieved_max_concurrency are validated >= 1 so the
    # defaults of 1 stick. Everything else: zero.
    assert old_snap.parallelism == 1
    assert old_snap.merges == 0
    assert old_snap.worker_intervals == ()

    report = aggregate_v3([old_snap])
    # Doesn't crash; rates are 0/0.
    assert report.convergence_rate.denominator == 0
    assert report.iterations_to_green.samples == 0


def test_v3_backfill_tolerates_unknown_extra_fields_via_extra_ignore() -> None:
    # spec_009 流儀 — old records can carry fields the v3 schema doesn't
    # know about; ``extra="ignore"`` swallows them silently.
    payload = {
        "night_id": "2026-05-02",
        "unknown_future_field": [1, 2, 3],
        "merges": 1,
    }
    snap = NightSnapshot.model_validate(payload)
    assert snap.merges == 1


def test_v3_multiple_nights_aggregate_across_observations() -> None:
    nights = [
        _snap(
            "2026-06-10",
            fix_loop_starts=2,
            converged=2,
            iterations_to_green=(1, 1),
            merges=2,
        ),
        _snap(
            "2026-06-11",
            fix_loop_starts=3,
            converged=1,
            iterations_to_green=(2,),
            merges=1,
            drop_reasons=("backlog cap reached",),
        ),
    ]

    report = aggregate_v3(nights)

    assert report.nights == 2
    assert report.fix_loop_starts == 5
    assert report.total_merges == 3
    assert report.convergence_rate.numerator == 3
    assert report.convergence_rate.denominator == 5
    # One drop across both nights, falls into "cap" bucket.
    by_cat = {b.category: b for b in report.drop_reasons}
    assert by_cat["cap"].count == 1


def test_v3_render_includes_all_metrics_with_population_notes() -> None:
    nights = [
        _snap(
            "2026-06-12",
            fix_loop_starts=2,
            converged=2,
            iterations_to_green=(1, 2),
            merges=2,
            parallelism=2,
            achieved_max_concurrency=2,
            worker_intervals=(
                WorkerInterval(
                    worker_id="w1",
                    started_at="2026-06-12T02:00:00+00:00",
                    finished_at="2026-06-12T02:10:00+00:00",
                    merged=True,
                ),
                WorkerInterval(
                    worker_id="w2",
                    started_at="2026-06-12T02:05:00+00:00",
                    finished_at="2026-06-12T02:15:00+00:00",
                    merged=True,
                ),
            ),
        ),
    ]
    report = aggregate_v3(nights)
    md = render_v3_report(report)

    assert "# v3 nightly metrics" in md
    assert "Convergence rate" in md
    assert "Iterations to green" in md
    assert "Marginal parallel yield" in md
    assert "Conflict / drop rate" in md
    assert "Dispatch minutes per merged fix" in md
    # Population notes are present (Japanese 母集団 anchor).
    assert "母集団" in md


def test_v3_load_night_snapshots_from_directory_skips_malformed(tmp_path) -> None:
    good = tmp_path / "night_2026-06-13.json"
    good.write_text(
        json.dumps({"night_id": "2026-06-13", "merges": 1}),
        encoding="utf-8",
    )
    malformed = tmp_path / "night_2026-06-14.json"
    malformed.write_text("not-json", encoding="utf-8")
    other = tmp_path / "not_a_snapshot.json"  # doesn't match glob
    other.write_text("{}", encoding="utf-8")

    snapshots = load_night_snapshots(tmp_path)
    # The malformed file is silently skipped; the unrelated file is
    # ignored by the glob pattern.
    assert [s.night_id for s in snapshots] == ["2026-06-13"]
    assert snapshots[0].merges == 1


def test_v3_load_night_snapshots_returns_empty_for_missing_dir(tmp_path) -> None:
    snapshots = load_night_snapshots(tmp_path / "does-not-exist")
    assert snapshots == []
