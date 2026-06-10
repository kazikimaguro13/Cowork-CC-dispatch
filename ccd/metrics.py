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

spec_042 — v3 nightly metrics
-----------------------------
``aggregate_v3()`` consumes a sequence of :class:`NightSnapshot` and
returns a :class:`V3MetricsReport` describing whether v3's convergence
loop (spec_039) + WorkerPool parallelism (spec_041) are actually
producing value. The metrics are designed to *say no when there is no
yield*: ``marginal_parallel_yield`` returns ``unknown`` for nights with
missing per-worker timestamps rather than inventing a number,
``dispatch_minutes_per_merged_fix`` returns the total minutes alongside
"merges=0" when no merges happened (no 0-division hand-wave), and every
metric carries an explicit ``population_note`` so the reader sees the
denominator the rate is computed against.

Backfill tolerance follows spec_009's流儀: :class:`NightSnapshot` has
defaults for every v3 field so pre-spec_041 nightly records (which
lacked ``worker_started_at`` / ``parallelism`` / ``drop_reasons``) round
through the aggregator without crashing — they show up as observations
with ``parallelism=1`` and no worker intervals, which is structurally
the correct thing for old single-candidate nights.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

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
    partial: int = Field(ge=0)
    running: int = Field(ge=0)
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
    """Aggregate a `ChainResult` (or `DispatchRecord` sequence) into a `MetricsReport`.

    ``PARTIAL`` records are counted on their own axis: they are neither
    successes (excluded from the success numerator) nor failures (excluded
    from the failure denominator and the failure taxonomy). Hiding them in
    either bucket would either inflate the success rate or fake a failure
    category — both are wrong for a status that means "shipped with caveats".

    ``RUNNING`` records (in-flight markers, present transiently between the
    pre-runner write and the post-runner write, and any orphan that escaped
    reconcile) are likewise independent: counting them as failures would
    misclassify "still in progress" as "failed". They appear in
    ``MetricsReport.running`` and nowhere else.
    """

    records = _records_of(source)
    total = len(records)
    done_records = [r for r in records if r.status is DispatchStatus.DONE]
    partial_records = [r for r in records if r.status is DispatchStatus.PARTIAL]
    running_records = [r for r in records if r.status is DispatchStatus.RUNNING]
    fail_records = [
        r
        for r in records
        if r.status is not DispatchStatus.DONE
        and r.status is not DispatchStatus.PARTIAL
        and r.status is not DispatchStatus.RUNNING
    ]

    done = len(done_records)
    partial = len(partial_records)
    running = len(running_records)
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
        partial=partial,
        running=running,
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
        f"- Partial: {report.partial}",
        f"- Running: {report.running}",
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


# --------------------------------------------------------------------------- #
# spec_042 — v3 nightly metrics
# --------------------------------------------------------------------------- #


class WorkerInterval(BaseModel):
    """One worker's start/finish ISO-8601 timestamps + its merge verdict.

    Backfill tolerance: ``started_at`` / ``finished_at`` may be the empty
    string when an old record predates spec_041's per-worker timestamps.
    Such intervals are treated as unobservable (excluded from the
    marginal_parallel_yield numerator/denominator instead of contributing
    a guessed value — spec_042 §2-1 「計算不能な夜は不明と報告する」).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    merged: bool = False

    @property
    def observable(self) -> bool:
        return bool(self.started_at) and bool(self.finished_at)


class NightSnapshot(BaseModel):
    """Per-night subset of :class:`ccd.nightly.NightlyResult` for v3 metrics.

    Every field has a default so that pre-spec_041 record JSON (with no
    parallelism / worker timestamps / drop_reasons) round-trips without
    crashing. ``extra="ignore"`` lets future fields land here without
    forcing migration. ``fix_loop_starts`` is the population for
    ``convergence_rate`` — candidates that actually entered the FixLoop
    (excludes ``skipped=True`` outcomes); ``merges`` is the merge count
    surfaced to the morning brief.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    night_id: str = Field(min_length=1)
    fix_loop_starts: int = Field(default=0, ge=0)
    converged: int = Field(default=0, ge=0)
    iterations_to_green: tuple[int, ...] = ()
    merges: int = Field(default=0, ge=0)
    parallelism: int = Field(default=1, ge=1)
    achieved_max_concurrency: int = Field(default=1, ge=1)
    drop_reasons: tuple[str, ...] = ()
    worker_intervals: tuple[WorkerInterval, ...] = ()
    # Optional — when the night didn't record any worker timestamps the
    # per-worker dispatch seconds can't be derived, but a night may still
    # supply a total directly (e.g. backfilled from launcher logs). This
    # is summed across nights for ``dispatch_minutes_per_merged_fix``.
    total_dispatch_seconds: float | None = None


class V3Rate(BaseModel):
    """A v3 rate with an explicit population note.

    The note mirrors the existing scoreboard's "rate over observed
    population" honesty: every v3 metric tells the reader what the
    denominator counts, so a 100% rate from 1/1 doesn't read the same as
    a 100% from 50/50.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float = Field(ge=0.0, le=1.0)
    population_note: str


class IterationsHistogram(BaseModel):
    """spec_042 §2-1 — iterations_to_green distribution.

    Buckets are intentionally coarse (1 / 2 / 3+) because the spec is
    looking for a *shape* signal: if "almost everything converges at
    iteration 1" then FixLoop is insurance and the operator can decide
    whether to keep paying for it; if 2-3 dominates then the loop is
    earning its keep.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    one: int = Field(ge=0)
    two: int = Field(ge=0)
    three_or_more: int = Field(ge=0)
    samples: int = Field(ge=0)
    population_note: str


class DropReasonBreakdown(BaseModel):
    """One bucket of the Integration drop-reason breakdown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    count: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0)


class V3MetricsReport(BaseModel):
    """Aggregate v3 metrics across a sequence of :class:`NightSnapshot`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nights: int = Field(ge=0)
    fix_loop_starts: int = Field(ge=0)
    total_merges: int = Field(ge=0)
    convergence_rate: V3Rate
    iterations_to_green: IterationsHistogram
    # spec_042 §2-1 — `None` means "unknown" — at least one night lacked
    # the per-worker timestamps needed to compute it. The note carries
    # *why* unknown when that's the case.
    marginal_parallel_yield: V3Rate | None
    marginal_parallel_yield_note: str
    conflict_drop_rate: V3Rate
    drop_reasons: tuple[DropReasonBreakdown, ...]
    # spec_042 §2-1 — when merges=0 the rate doesn't exist; surface the
    # total minutes anyway so the reader sees "we burned N minutes and
    # got 0 merges" rather than a hidden 0/0.
    dispatch_minutes_per_merged_fix: float | None
    total_dispatch_minutes: float | None
    dispatch_minutes_note: str


# Anchors for grouping :attr:`NightlyResult.drop_reasons` strings into the
# four buckets spec_042 §2-1 calls out: 衝突 / cap / PAUSE / 窓. The
# anchors are substring matches against the lowercased drop reason; this
# mirrors how the morning brief surfaces these reasons verbatim — the
# strings are anchors in :mod:`ccd.nightly`, not free text, so substring
# matching is stable.
_DROP_BUCKET_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("conflict", ("apply failed", "live re-verification", "merge conflict")),
    (
        "cap",
        (
            "max merges per night cap reached",
            "un-pushed backlog cap",
            "backlog cap",
        ),
    ),
    ("pause", ("pause file", "paused")),
    ("window", ("night wall-clock", "wall-clock budget", "wall clock")),
)
_DROP_BUCKET_OTHER = "other"
_KNOWN_DROP_BUCKETS: tuple[str, ...] = ("conflict", "cap", "pause", "window", "other")


def aggregate_v3(snapshots: Sequence[NightSnapshot]) -> V3MetricsReport:
    """Aggregate :class:`NightSnapshot`s into a :class:`V3MetricsReport`.

    Backfill tolerance: snapshots with missing v3 fields use defaults
    that make the night a structural zero (no fix_loop, no merges, P=1).
    Missing per-worker timestamps make that night's parallel-yield
    contribution unknown — the global metric becomes ``None`` if ANY
    contributing night was unknown (spec_042 §2-1 「でっち上げ禁止」).
    """

    nights = list(snapshots)
    n_nights = len(nights)
    fix_loop_starts = sum(s.fix_loop_starts for s in nights)
    converged = sum(s.converged for s in nights)
    total_merges = sum(s.merges for s in nights)

    convergence_rate = _make_v3_rate(
        numerator=converged,
        denominator=fix_loop_starts,
        zero_note=(
            "母集団=FixLoop が起動した候補数 (skipped を除く)。0 件のため率は未定義。"
        ),
        active_note=(
            f"母集団=FixLoop が起動した候補数 {fix_loop_starts} 件。"
            "skipped 候補は分母に含めない (生存バイアス対策)。"
        ),
    )

    iter_hist = _build_iterations_histogram(nights)
    parallel_yield, parallel_note = _build_marginal_parallel_yield(nights)
    conflict_rate, drop_breakdown = _build_conflict_drop_rate(nights)
    minutes_per_merge, total_minutes, minutes_note = _build_dispatch_minutes(
        nights, total_merges
    )

    return V3MetricsReport(
        nights=n_nights,
        fix_loop_starts=fix_loop_starts,
        total_merges=total_merges,
        convergence_rate=convergence_rate,
        iterations_to_green=iter_hist,
        marginal_parallel_yield=parallel_yield,
        marginal_parallel_yield_note=parallel_note,
        conflict_drop_rate=conflict_rate,
        drop_reasons=drop_breakdown,
        dispatch_minutes_per_merged_fix=minutes_per_merge,
        total_dispatch_minutes=total_minutes,
        dispatch_minutes_note=minutes_note,
    )


def render_v3_report(report: V3MetricsReport) -> str:
    """Render :class:`V3MetricsReport` as a human-readable Markdown string.

    Mirrors ``render_report()``'s shape so ``ccd report`` can concatenate
    the two without layout drift. Each metric leads with the number, then
    the population note in a sub-line — same流儀 as the scoreboard's
    duration + sample size on the same line.
    """

    lines: list[str] = [
        "# v3 nightly metrics",
        "",
        f"- Nights observed: {report.nights}",
        f"- FixLoop starts: {report.fix_loop_starts}",
        f"- Merges: {report.total_merges}",
        "",
        "## Convergence",
        "",
        f"- Convergence rate: {_fmt_v3_rate(report.convergence_rate)}",
        f"  - {report.convergence_rate.population_note}",
        (
            "- Iterations to green: "
            f"1={report.iterations_to_green.one} / "
            f"2={report.iterations_to_green.two} / "
            f"3+={report.iterations_to_green.three_or_more} "
            f"(n={report.iterations_to_green.samples})"
        ),
        f"  - {report.iterations_to_green.population_note}",
        "",
        "## Parallelism",
        "",
    ]
    if report.marginal_parallel_yield is None:
        lines.append("- Marginal parallel yield: 不明")
    else:
        lines.append(
            f"- Marginal parallel yield: {_fmt_v3_rate(report.marginal_parallel_yield)}"
        )
    lines.append(f"  - {report.marginal_parallel_yield_note}")
    lines.append("")
    lines.append("## Integration drops")
    lines.append("")
    lines.append(f"- Conflict / drop rate: {_fmt_v3_rate(report.conflict_drop_rate)}")
    lines.append(f"  - {report.conflict_drop_rate.population_note}")
    if not report.drop_reasons:
        lines.append("- Drop reasons: (none)")
    else:
        for item in report.drop_reasons:
            lines.append(
                f"- {item.category}: {item.count} ({item.share:.1%})"
            )
    lines.append("")
    lines.append("## Cost")
    lines.append("")
    if report.dispatch_minutes_per_merged_fix is None:
        if report.total_dispatch_minutes is None:
            lines.append("- Dispatch minutes per merged fix: 不明")
        else:
            lines.append(
                "- Dispatch minutes per merged fix: "
                f"総 {report.total_dispatch_minutes:.1f} 分 / merge=0"
            )
    else:
        lines.append(
            "- Dispatch minutes per merged fix: "
            f"{report.dispatch_minutes_per_merged_fix:.1f} 分 "
            f"(総 {report.total_dispatch_minutes:.1f} 分 / merge={report.total_merges})"
        )
    lines.append(f"  - {report.dispatch_minutes_note}")
    lines.append("")
    return "\n".join(lines)


def load_night_snapshots(path: Path) -> list[NightSnapshot]:
    """Load v3 :class:`NightSnapshot` records from a directory or a single file.

    Designed to be tolerant: malformed JSON files are skipped (the v3
    section should never block on one bad snapshot, mirroring the
    dashboard's run-file loader). Returns snapshots sorted by
    ``night_id`` for deterministic downstream rendering.
    """

    path = Path(path)
    if not path.exists():
        return []
    files: list[Path]
    if path.is_dir():
        files = sorted(path.glob("night_*.json"))
    elif path.is_file():
        files = [path]
    else:
        return []
    out: list[NightSnapshot] = []
    for fp in files:
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
            out.append(NightSnapshot.model_validate(payload))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda s: s.night_id)
    return out


def _make_v3_rate(
    *,
    numerator: int,
    denominator: int,
    zero_note: str,
    active_note: str,
) -> V3Rate:
    if denominator == 0:
        return V3Rate(numerator=0, denominator=0, value=0.0, population_note=zero_note)
    return V3Rate(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
        population_note=active_note,
    )


def _fmt_v3_rate(rate: V3Rate) -> str:
    return f"{rate.numerator}/{rate.denominator} ({rate.value:.1%})"


def _build_iterations_histogram(
    nights: Sequence[NightSnapshot],
) -> IterationsHistogram:
    bucket_one = 0
    bucket_two = 0
    bucket_three_plus = 0
    samples = 0
    for snap in nights:
        for it in snap.iterations_to_green:
            if it <= 0:
                continue
            samples += 1
            if it == 1:
                bucket_one += 1
            elif it == 2:
                bucket_two += 1
            else:
                bucket_three_plus += 1
    if samples == 0:
        note = (
            "母集団=収束した候補の iteration 数。0 件のため形状未確定。"
        )
    else:
        note = (
            f"母集団=収束 (converged=True) した候補 {samples} 件。"
            "1 が支配的ならループは保険、2-3 が多いならループが価値を生んでいるシグナル。"
        )
    return IterationsHistogram(
        one=bucket_one,
        two=bucket_two,
        three_or_more=bucket_three_plus,
        samples=samples,
        population_note=note,
    )


def _build_marginal_parallel_yield(
    nights: Sequence[NightSnapshot],
) -> tuple[V3Rate | None, str]:
    """Compute marginal parallel yield with honest "不明" semantics.

    A merged worker counts toward the numerator iff its lifespan
    overlapped with at least one other worker during the same night.
    The denominator is total merges across observable nights. If ANY
    night had ≥ 2 workers but missing timestamps, the global metric is
    ``None`` ── spec_042 §2-1「計算不能な夜は不明と報告する (でっち上げ
    禁止)」.
    """

    unobservable_nights: list[str] = []
    overlapping_merges = 0
    observable_merges = 0
    observed_nights = 0
    for snap in nights:
        intervals = list(snap.worker_intervals)
        if len(intervals) <= 1:
            # K=1 (or zero workers) — no parallel by construction. Such
            # nights contribute their merges to the denominator with 0
            # overlap; they are observable because there is nothing to
            # measure.
            observable_merges += sum(1 for w in intervals if w.merged)
            observed_nights += 1
            continue
        if not all(w.observable for w in intervals):
            unobservable_nights.append(snap.night_id)
            continue
        parsed = [
            (
                _parse_iso8601(w.started_at),
                _parse_iso8601(w.finished_at),
                w.merged,
            )
            for w in intervals
        ]
        if any(s is None or f is None for s, f, _ in parsed):
            unobservable_nights.append(snap.night_id)
            continue
        for i, (si, fi, merged_i) in enumerate(parsed):
            if not merged_i:
                continue
            observable_merges += 1
            overlaps = any(
                _intervals_overlap(si, fi, sj, fj)
                for j, (sj, fj, _) in enumerate(parsed)
                if j != i
            )
            if overlaps:
                overlapping_merges += 1
        observed_nights += 1

    if unobservable_nights:
        note = (
            "計算不能 — per-worker timestamp が欠損した夜があるため (でっち上げ禁止、"
            "spec_042 §2-1)。不明な夜: "
            + ", ".join(unobservable_nights)
            + f"。観測できた夜のみで集計すると overlap merge = {overlapping_merges} / "
            f"merge = {observable_merges} になる (参考値)。"
        )
        return None, note

    note = (
        f"母集団=同時実行数2以上の時間帯に worker lifespan が重なった merge を分子、"
        f"観測夜の全 merge を分母 (観測夜 {observed_nights})。"
    )
    rate = _make_v3_rate(
        numerator=overlapping_merges,
        denominator=observable_merges,
        zero_note=(
            "母集団=観測夜の全 merge。merge=0 のため率は未定義。"
        ),
        active_note=note,
    )
    return rate, note


def _build_conflict_drop_rate(
    nights: Sequence[NightSnapshot],
) -> tuple[V3Rate, tuple[DropReasonBreakdown, ...]]:
    bucket_counts: dict[str, int] = {b: 0 for b in _KNOWN_DROP_BUCKETS}
    total_drops = 0
    total_processed = 0
    for snap in nights:
        # "Processed" approximates the population the Integrator looked at:
        # merges + drops (each drop reason is one trip). FixLoop starts is
        # an upper bound but includes candidates that never reached the
        # Integrator (e.g. translate fail), so we use merges + drops to
        # stay structurally honest.
        drops_this_night = len(snap.drop_reasons)
        total_drops += drops_this_night
        total_processed += snap.merges + drops_this_night
        for reason in snap.drop_reasons:
            bucket = _classify_drop_reason(reason)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    rate = _make_v3_rate(
        numerator=total_drops,
        denominator=total_processed,
        zero_note=(
            "母集団=Integrator が処理した候補数 (merge + drop)。0 件のため率は未定義。"
        ),
        active_note=(
            f"母集団=Integrator が処理した候補数 {total_processed} 件 "
            f"(merge {total_processed - total_drops} + drop {total_drops})。"
            "理由別内訳は下行参照。"
        ),
    )

    if total_drops == 0:
        return rate, ()
    breakdown: list[DropReasonBreakdown] = []
    for bucket in _KNOWN_DROP_BUCKETS:
        count = bucket_counts.get(bucket, 0)
        if count == 0:
            continue
        breakdown.append(
            DropReasonBreakdown(
                category=bucket,
                count=count,
                share=count / total_drops,
            )
        )
    return rate, tuple(breakdown)


def _build_dispatch_minutes(
    nights: Sequence[NightSnapshot],
    total_merges: int,
) -> tuple[float | None, float | None, str]:
    """Compute total dispatch minutes / merged fix.

    Uses per-worker (finished - started) for nights that recorded
    timestamps; falls back to ``total_dispatch_seconds`` when supplied;
    falls back to "unknown" when neither is available. 0-merge nights
    surface the total minutes + "merge=0" rather than collapsing to a
    silent 0/0 (spec_042 §2-1「0 除算をごまかさない」).
    """

    total_seconds: float = 0.0
    any_known = False
    unobservable_nights: list[str] = []
    for snap in nights:
        seconds = _night_dispatch_seconds(snap)
        if seconds is None:
            unobservable_nights.append(snap.night_id)
            continue
        any_known = True
        total_seconds += seconds

    if not any_known:
        note = (
            "計算不能 — どの夜も per-worker timestamp / "
            "total_dispatch_seconds を残しておらず実時間が分からない。"
        )
        return None, None, note

    total_minutes = total_seconds / 60.0
    if total_merges == 0:
        note = (
            f"merge=0 のため率は未定義。総 dispatch 時間は {total_minutes:.1f} 分。"
            "rate でなく「総分数 + merge 0」として読む。"
        )
        if unobservable_nights:
            note += " 一部夜の timestamp 欠損 (" + ", ".join(unobservable_nights) + ")"
        return None, total_minutes, note

    rate_minutes = total_minutes / total_merges
    note = (
        f"母集団=merge 達成した候補 {total_merges} 件。"
        f"総 dispatch 時間 {total_minutes:.1f} 分 ÷ merge {total_merges} = "
        f"{rate_minutes:.1f} 分/merge。"
    )
    if unobservable_nights:
        note += " 一部夜の timestamp 欠損 (" + ", ".join(unobservable_nights) + ")"
    return rate_minutes, total_minutes, note


def _night_dispatch_seconds(snap: NightSnapshot) -> float | None:
    """Per-night dispatch seconds.

    Returns ``None`` only when the night had workers but none of them had
    observable timestamps AND no ``total_dispatch_seconds`` fallback was
    supplied. Nights with zero workers contribute 0 seconds (nothing
    happened) instead of going unknown.
    """

    if snap.total_dispatch_seconds is not None:
        return float(snap.total_dispatch_seconds)
    if not snap.worker_intervals:
        return 0.0
    observable = [w for w in snap.worker_intervals if w.observable]
    if not observable:
        return None
    total = 0.0
    for w in observable:
        s = _parse_iso8601(w.started_at)
        f = _parse_iso8601(w.finished_at)
        if s is None or f is None:
            return None
        delta = (f - s).total_seconds()
        if delta < 0:
            delta = 0.0
        total += delta
    return total


def _classify_drop_reason(reason: str) -> str:
    """Bucket a drop-reason string into one of {conflict, cap, pause, window, other}."""

    needle = reason.lower()
    for bucket, anchors in _DROP_BUCKET_PATTERNS:
        for a in anchors:
            if a in needle:
                return bucket
    return _DROP_BUCKET_OTHER


def _parse_iso8601(stamp: str) -> datetime | None:
    """Best-effort ISO-8601 parser.

    Accepts the ``"…Z"`` suffix (treated as UTC) since spec_041's
    ``_now_iso_utc()`` may produce either ``+00:00`` or ``Z``. Returns
    ``None`` on unparseable input rather than raising — the caller treats
    that as "unobservable" and marks the metric unknown.
    """

    if not stamp:
        return None
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _intervals_overlap(
    a_start: datetime,
    a_finish: datetime,
    b_start: datetime,
    b_finish: datetime,
) -> bool:
    """Two half-open intervals [start, finish) overlap iff start < other.finish."""

    return a_start < b_finish and b_start < a_finish
