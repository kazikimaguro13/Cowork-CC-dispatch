"""Static HTML dashboard renderer for v1.5 (`ccd dashboard`).

Reads the `RunFile` envelopes under `_ai_workspace/runs/` (or any directory
of run JSON), pools every `DispatchRecord` together for the hero numbers,
and emits a single self-contained HTML file. No external resources are
referenced — every chart is inline SVG, no <script>, no <link>, no JS.

The dashboard has four panels:

  1. Hero band — pool-aggregate metrics (autonomous completion rate large,
     scoreboard + improvement-loop indicators alongside).
  2. Failure taxonomy — horizontal SVG bars of `FailureCategory` shares
     over the pooled failures.
  3. Run trend — cumulative dispatch_success / autonomous_completion /
     first_pass rate as SVG polylines over the time-ordered dispatch
     sequence.
  4. Run table — one row per RunFile (project + generation tag), with a
     `<details>`/`<summary>` per-spec expansion (no JS).

Data-quality candor: the generation tag is always shown next to its run,
and a banner notes that backfilled `bash_prototype` runs default
`attempts=1` / `intervention=False`, which makes first-pass / retry /
autonomous-completion rates an upper-bound estimate.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .metrics import MetricsReport, Rate, aggregate
from .models import DispatchRecord, DispatchStatus, RunFile

logger = logging.getLogger(__name__)

_DEFAULT_RUNS_REL = Path("_ai_workspace") / "runs"
_DEFAULT_OUTPUT_REL = Path("docs") / "index.html"

# Generation tags whose runs may have synthetic defaults from backfill
# (attempts=1, intervention=False). Used to drive the data-quality banner.
_BACKFILLED_GENERATIONS = frozenset({"bash_prototype"})


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #


def load_runs(runs_dir: Path) -> list[RunFile]:
    """Load every ``*.json`` under ``runs_dir`` as a `RunFile`.

    Files that fail to parse are skipped with a warning — a single bad run
    must not block the whole dashboard.
    """

    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        logger.warning("dashboard: runs directory %s does not exist", runs_dir)
        return []

    runs: list[RunFile] = []
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            runs.append(RunFile.model_validate(payload))
        except (OSError, ValueError) as exc:
            logger.warning("dashboard: skipping %s (%s)", path, exc)
            continue
    return runs


def pool_records(runs: Iterable[RunFile]) -> list[DispatchRecord]:
    """Concatenate every `DispatchRecord` across runs (input order preserved)."""

    out: list[DispatchRecord] = []
    for run in runs:
        out.extend(run.records)
    return out


# --------------------------------------------------------------------------- #
# Trend                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrendPoint:
    """One step on the cumulative-rate trend (after `index` dispatches)."""

    index: int
    timestamp: datetime
    dispatch_success_rate: float
    autonomous_completion_rate: float
    first_pass_rate: float


def build_trend(records: Sequence[DispatchRecord]) -> list[TrendPoint]:
    """Order records by ``finished_at or started_at`` and emit cumulative rates."""

    ordered = sorted(records, key=_record_sort_key)
    points: list[TrendPoint] = []
    total = 0
    done = 0
    auto = 0
    first = 0
    for r in ordered:
        total += 1
        if r.status is DispatchStatus.DONE:
            done += 1
            if not r.intervention:
                auto += 1
            if r.attempts == 1:
                first += 1
        points.append(
            TrendPoint(
                index=total,
                timestamp=r.finished_at or r.started_at,
                dispatch_success_rate=done / total,
                autonomous_completion_rate=auto / total,
                first_pass_rate=first / total,
            )
        )
    return points


def _record_sort_key(record: DispatchRecord) -> datetime:
    return record.finished_at or record.started_at


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #


def render_dashboard(runs: Sequence[RunFile], *, generated_at: datetime | None = None) -> str:
    """Render the full dashboard HTML for the given runs."""

    records = pool_records(runs)
    report = aggregate(records)
    trend = build_trend(records)
    generated = generated_at or datetime.now(UTC)

    body = "\n".join(
        [
            _render_header(generated, total_runs=len(runs), total_records=len(records)),
            _render_quality_note(runs),
            _render_hero(report, project_count=len(runs)),
            _render_failure_taxonomy(report),
            _render_trend(trend),
            _render_runs_table(runs),
        ]
    )

    return _HTML_DOCUMENT.format(
        title="ccd dashboard",
        style=_CSS,
        body=body,
    )


# --- header & quality note ------------------------------------------------- #


def _render_header(generated_at: datetime, *, total_runs: int, total_records: int) -> str:
    return (
        '<header class="page-header">'
        "<h1>ccd dashboard</h1>"
        f'<p class="subtitle">run {total_runs} 件 · dispatch {total_records} 件 · '
        f"生成 {html.escape(generated_at.isoformat(timespec='seconds'))}</p>"
        "</header>"
    )


def _render_quality_note(runs: Sequence[RunFile]) -> str:
    generations = sorted({(r.generation or "(未指定)") for r in runs})
    has_backfill = any(
        (r.generation or "") in _BACKFILLED_GENERATIONS for r in runs
    )
    chips = "".join(
        f'<span class="chip chip-{_chip_kind(g)}">{html.escape(g)}</span>'
        for g in generations
    )
    notes: list[str] = []
    # v1.6 (spec_010): ccd now records orchestrator-side interruptions
    # (TimeoutExpired, git errors, runner crashes, process death) as
    # HALTED + INTERRUPTED instead of silently dropping the run. The
    # remaining structural blind spots are explicitly enumerated below so
    # the dashboard never pretends to be a complete sample.
    notes.append(
        '<p class="quality-note"><strong>カバレッジ注記</strong> (v1.6): '
        "<code>ccd</code> は中断された dispatch (オーケストレータの死・未処理例外・"
        "<code>--timeout</code> 超過) を <code>HALTED</code> / "
        "<code>INTERRUPTED</code> として記録するようになりました。"
        "残る構造的な死角は (a) <code>ccd</code> が dispatch を開始する前にプロセスが"
        "落ちたケース と (b) bash bridge 時代の履歴データ のみです。"
        "観測の死角は v1.5 より縮みましたが、母集団全体の上限の目安として"
        "読んでください。</p>"
    )
    if has_backfill:
        notes.append(
            '<p class="quality-note">バックフィルした run は <code>attempts=1</code> '
            "/ <code>intervention=false</code> を既定値としています。一発合格率・"
            "リトライ回復率・自律完走率はこの欠損の影響を受けるため、上限寄りの概算値です。"
            "元の <code>result_*.md</code> がこれらのフィールドを持たないためです。"
            "安全停止率は下限、一発合格率・リトライ・自律完走の各指標は"
            "上限の目安として読んでください。</p>"
        )
    return (
        '<section class="quality" aria-label="データ品質">'
        f'<div class="chip-row" aria-label="世代">{chips}</div>'
        + "".join(notes)
        + "</section>"
    )


def _chip_kind(generation: str) -> str:
    if generation in _BACKFILLED_GENERATIONS:
        return "backfill"
    if generation == "ccd_native":
        return "native"
    return "other"


# --- hero ------------------------------------------------------------------ #


def _render_hero(report: MetricsReport, *, project_count: int) -> str:
    auto_pct = _percent(report.autonomous_completion_rate)
    duration = report.duration
    breakdown = _outcome_breakdown(report)
    return (
        '<section class="panel hero" aria-label="主要指標">'
        '<div class="hero-primary">'
        '<div class="hero-label">自律完走率</div>'
        f'<div class="hero-value">{auto_pct}</div>'
        f'<div class="hero-sub">{_fmt_rate(report.autonomous_completion_rate)}</div>'
        f'<div class="hero-breakdown">{breakdown}</div>'
        "</div>"
        '<div class="hero-grid">'
        + _hero_cell("dispatch 成功率", _fmt_rate(report.dispatch_success_rate))
        + _hero_cell("一発合格率", _fmt_rate(report.first_pass_rate))
        + _hero_cell("リトライ回復率", _fmt_rate(report.retry_recovery_rate))
        + _hero_cell("安全停止率", _fmt_rate(report.safe_halt_rate))
        + _hero_cell("総 dispatch 数", str(report.total_specs))
        + _hero_cell("プロジェクト数", str(project_count))
        + _hero_cell(
            "done / partial",
            f"{report.done} done / {report.partial} partial / {report.failures} failed",
        )
        + _hero_cell(
            "所要時間",
            f"平均 {duration.mean_seconds:.1f}s · 中央値 {duration.median_seconds:.1f}s "
            f"(n={duration.samples})",
        )
        + "</div>"
        "</section>"
    )


def _outcome_breakdown(report: MetricsReport) -> str:
    """Render `done` / `partial` / `failed` counts as a tiny inline pill row.

    Surfaced under the hero number so the dashboard never reads as "100% done"
    when there are partials silently in the pool.
    """

    items = [
        ("done", report.done, "outcome-done"),
        ("partial", report.partial, "outcome-partial"),
        ("failed", report.failures, "outcome-failed"),
    ]
    pills = []
    for label, count, cls in items:
        if count == 0:
            continue
        pills.append(
            f'<span class="outcome-pill {cls}">'
            f'<span class="outcome-count">{count}</span> '
            f'<span class="outcome-label">{html.escape(label)}</span>'
            "</span>"
        )
    if not pills:
        return ""
    return "".join(pills)


def _hero_cell(label: str, value: str) -> str:
    return (
        '<div class="hero-cell">'
        f'<div class="hero-cell-label">{html.escape(label)}</div>'
        f'<div class="hero-cell-value">{html.escape(value)}</div>'
        "</div>"
    )


# --- failure taxonomy ----------------------------------------------------- #


def _render_failure_taxonomy(report: MetricsReport) -> str:
    items = report.failure_taxonomy
    if not items:
        return (
            '<section class="panel" aria-label="失敗カテゴリ">'
            "<h2>失敗カテゴリ</h2>"
            '<p class="empty">失敗の記録はありません。</p>'
            "</section>"
        )

    # Horizontal bar chart in inline SVG. Geometry is fixed and deterministic
    # so tests can grep specific coordinates if needed.
    bar_h = 22
    gap = 10
    pad_top = 8
    pad_left = 180
    pad_right = 80
    chart_w = 520
    height = pad_top + len(items) * (bar_h + gap)
    width = pad_left + chart_w + pad_right
    max_share = max(item.share for item in items) or 1.0

    bars: list[str] = []
    for i, item in enumerate(items):
        y = pad_top + i * (bar_h + gap)
        bar_w = (item.share / max_share) * chart_w
        label = item.category.value if item.category is not None else "不明"
        pct = f"{item.share * 100:.1f}%"
        count = f"{item.count} ({pct})"
        bars.append(
            f'<text x="{pad_left - 8}" y="{y + bar_h * 0.7:.1f}" '
            'class="bar-label" text-anchor="end">'
            f"{html.escape(label)}</text>"
            f'<rect x="{pad_left}" y="{y}" width="{bar_w:.1f}" height="{bar_h}" '
            'class="bar-fill" rx="3" ry="3"/>'
            f'<text x="{pad_left + bar_w + 6:.1f}" y="{y + bar_h * 0.7:.1f}" '
            'class="bar-count">'
            f"{html.escape(count)}</text>"
        )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" '
        f'width="100%" preserveAspectRatio="xMinYMin meet" '
        'role="img" aria-label="失敗カテゴリの割合">'
        + "".join(bars)
        + "</svg>"
    )
    return (
        '<section class="panel" aria-label="失敗カテゴリ">'
        "<h2>失敗カテゴリ</h2>"
        f"{svg}"
        "</section>"
    )


# --- trend ---------------------------------------------------------------- #


def _render_trend(points: Sequence[TrendPoint]) -> str:
    if not points:
        return (
            '<section class="panel" aria-label="推移">'
            "<h2>推移</h2>"
            '<p class="empty">dispatch の記録がまだありません。'
            "run が蓄積されると表示されます。</p>"
            "</section>"
        )

    width = 760
    height = 240
    pad_l = 48
    pad_r = 16
    pad_t = 16
    pad_b = 36
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b

    n = len(points)
    # x-axis: dispatch sequence (1..n). y-axis: rate 0..1.
    def x_of(idx: int) -> float:
        if n == 1:
            return pad_l + inner_w / 2
        return pad_l + (idx - 1) * inner_w / (n - 1)

    def y_of(rate: float) -> float:
        return pad_t + (1.0 - max(0.0, min(1.0, rate))) * inner_h

    def polyline(extractor) -> str:
        coords = " ".join(
            f"{x_of(p.index):.1f},{y_of(extractor(p)):.1f}" for p in points
        )
        return coords

    # Y-axis gridlines + labels at 0, 25, 50, 75, 100%.
    grid_lines: list[str] = []
    for pct in (0, 25, 50, 75, 100):
        y = y_of(pct / 100)
        grid_lines.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + inner_w}" '
            f'y2="{y:.1f}" class="grid"/>'
            f'<text x="{pad_l - 6}" y="{y + 4:.1f}" class="axis-label" '
            f'text-anchor="end">{pct}%</text>'
        )

    # X-axis ticks at first / middle / last.
    tick_indices = sorted({1, (n + 1) // 2, n})
    x_ticks: list[str] = []
    for idx in tick_indices:
        x = x_of(idx)
        label = points[idx - 1].timestamp.date().isoformat()
        x_ticks.append(
            f'<line x1="{x:.1f}" y1="{pad_t + inner_h}" x2="{x:.1f}" '
            f'y2="{pad_t + inner_h + 4}" class="grid"/>'
            f'<text x="{x:.1f}" y="{pad_t + inner_h + 18}" '
            f'class="axis-label" text-anchor="middle">{html.escape(label)}</text>'
            f'<text x="{x:.1f}" y="{pad_t + inner_h + 30}" '
            f'class="axis-label-small" text-anchor="middle">#{idx}</text>'
        )

    lines = [
        ("dispatch成功率", "trend-success", lambda p: p.dispatch_success_rate),
        ("自律完走率", "trend-auto", lambda p: p.autonomous_completion_rate),
        ("一発合格率", "trend-first", lambda p: p.first_pass_rate),
    ]
    polylines = "".join(
        f'<polyline class="{cls}" fill="none" points="{polyline(extract)}"/>'
        for _, cls, extract in lines
    )

    # Legend.
    legend_items = []
    for i, (name, cls, _extract) in enumerate(lines):
        lx = pad_l + i * 200
        legend_items.append(
            f'<rect x="{lx}" y="4" width="14" height="3" class="{cls}-swatch"/>'
            f'<text x="{lx + 20}" y="9" class="axis-label">'
            f"{html.escape(name)}</text>"
        )
    legend = (
        '<svg viewBox="0 0 760 18" width="100%" preserveAspectRatio="xMinYMin meet" '
        'role="img" aria-label="凡例">'
        + "".join(legend_items)
        + "</svg>"
    )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" '
        'width="100%" preserveAspectRatio="xMinYMin meet" '
        'role="img" aria-label="dispatch 系列の累積率">'
        + "".join(grid_lines)
        + "".join(x_ticks)
        + polylines
        + "</svg>"
    )
    return (
        '<section class="panel" aria-label="推移">'
        "<h2>推移</h2>"
        '<p class="caption">dispatch を時系列順（古い → 新しい）に'
        "並べた累積率です。</p>"
        f"{legend}{svg}"
        "</section>"
    )


# --- run table ------------------------------------------------------------ #


def _render_runs_table(runs: Sequence[RunFile]) -> str:
    if not runs:
        return (
            '<section class="panel" aria-label="run 一覧">'
            "<h2>run 一覧</h2>"
            '<p class="empty">run ファイルがありません。</p>'
            "</section>"
        )

    rows: list[str] = []
    for run in runs:
        rows.append(_render_run_row(run))

    return (
        '<section class="panel" aria-label="run 一覧">'
        "<h2>run 一覧</h2>"
        '<table class="runs">'
        "<thead><tr>"
        "<th>日付</th><th>プロジェクト</th><th>世代</th>"
        "<th class=\"num\">spec数</th><th class=\"num\">完了</th>"
        "<th class=\"num\">partial</th>"
        "<th class=\"num\">失敗</th><th class=\"num\">所要時間 平均</th>"
        "</tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
        "</section>"
    )


def _render_run_row(run: RunFile) -> str:
    records = run.records
    done = sum(1 for r in records if r.status is DispatchStatus.DONE)
    partial = sum(1 for r in records if r.status is DispatchStatus.PARTIAL)
    failed = len(records) - done - partial

    durations = [
        (r.finished_at - r.started_at).total_seconds()
        for r in records
        if r.finished_at is not None
    ]
    duration_str = (
        f"{statistics.fmean(durations):.1f}秒" if durations else "—"
    )

    date_str = _representative_date(run, records)
    project = run.project or "(未指定)"
    generation = run.generation or "(未指定)"

    details = _render_run_details(run)

    summary_row = (
        "<tr>"
        f'<td>{html.escape(date_str)}</td>'
        f'<td>{html.escape(project)}</td>'
        f'<td><span class="chip chip-{_chip_kind(generation)}">'
        f'{html.escape(generation)}</span></td>'
        f'<td class="num">{len(records)}</td>'
        f'<td class="num">{done}</td>'
        f'<td class="num">{partial}</td>'
        f'<td class="num">{failed}</td>'
        f'<td class="num">{html.escape(duration_str)}</td>'
        "</tr>"
    )
    detail_row = (
        '<tr class="detail-row"><td colspan="8">'
        f"{details}"
        "</td></tr>"
    )
    return summary_row + detail_row


def _render_run_details(run: RunFile) -> str:
    if not run.records:
        return (
            "<details><summary>dispatch 記録なし</summary></details>"
        )
    items: list[str] = []
    for r in run.records:
        if r.failure_category is not None:
            cat = r.failure_category.value
        elif r.status is DispatchStatus.DONE or r.status is DispatchStatus.PARTIAL:
            cat = "—"
        else:
            cat = "不明"
        duration = (
            f"{(r.finished_at - r.started_at).total_seconds():.1f}秒"
            if r.finished_at is not None
            else "—"
        )
        items.append(
            "<tr>"
            f'<td>{html.escape(r.spec_id)}</td>'
            f'<td>{html.escape(r.status.value)}</td>'
            f'<td>{html.escape(cat)}</td>'
            f'<td class="num">{html.escape(duration)}</td>'
            "</tr>"
        )
    return (
        "<details><summary>spec 別の明細</summary>"
        '<table class="specs">'
        "<thead><tr>"
        "<th>spec_id</th><th>ステータス</th><th>失敗カテゴリ</th>"
        '<th class="num">所要時間</th>'
        "</tr></thead>"
        "<tbody>"
        + "".join(items)
        + "</tbody></table>"
        "</details>"
    )


def _representative_date(run: RunFile, records: Sequence[DispatchRecord]) -> str:
    """Pick a single date for the run row.

    Prefer the latest dispatch timestamp (gives a "as of when" feel), else
    the envelope's ``saved_at``, else ``—``.
    """

    if records:
        latest = max(_record_sort_key(r) for r in records)
        return latest.date().isoformat()
    if run.saved_at:
        return run.saved_at.split("T", 1)[0]
    return "—"


# --- helpers --------------------------------------------------------------- #


def _fmt_rate(rate: Rate) -> str:
    return f"{rate.numerator}/{rate.denominator} ({rate.value * 100:.1f}%)"


def _percent(rate: Rate) -> str:
    return f"{rate.value * 100:.1f}%"


# --------------------------------------------------------------------------- #
# Static assets                                                                #
# --------------------------------------------------------------------------- #

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #0f172a;
  --panel: #1e293b;
  --text: #e2e8f0;
  --muted: #94a3b8;
  --accent: #38bdf8;
  --success: #4ade80;
  --warn: #facc15;
  --danger: #f87171;
  --chip-backfill: #f59e0b;
  --chip-native: #22c55e;
  --chip-other: #64748b;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
  "Helvetica Neue", Arial, sans-serif; line-height: 1.45; }
main { max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }
.page-header h1 { margin: 0 0 4px; font-size: 2rem; letter-spacing: -0.01em; }
.subtitle { color: var(--muted); margin: 0; font-size: 0.95rem; }
.quality { margin-top: 16px; }
.chip-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
.chip { display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 0.8rem; font-weight: 600; color: #0f172a; }
.chip-backfill { background: var(--chip-backfill); }
.chip-native { background: var(--chip-native); }
.chip-other { background: var(--chip-other); color: var(--text); }
.quality-note { color: var(--muted); font-size: 0.9rem; margin: 6px 0 0; }
.quality-note code { background: rgba(148,163,184,0.15); padding: 1px 4px;
  border-radius: 3px; font-size: 0.85em; }
.panel { background: var(--panel); border-radius: 12px; padding: 20px 24px;
  margin-top: 24px; box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset; }
.panel h2 { margin: 0 0 12px; font-size: 1.1rem; letter-spacing: 0.02em;
  text-transform: uppercase; color: var(--muted); }
.panel .caption { color: var(--muted); margin: 0 0 8px; font-size: 0.85rem; }
.panel .empty { color: var(--muted); margin: 4px 0 0; }
.hero { display: grid; grid-template-columns: minmax(220px, 280px) 1fr;
  gap: 24px; align-items: center; }
.hero-primary { text-align: center; padding: 8px 4px; }
.hero-label { color: var(--muted); font-size: 0.85rem;
  text-transform: uppercase; letter-spacing: 0.06em; }
.hero-value { font-size: 3.2rem; font-weight: 700; color: var(--success);
  line-height: 1.05; margin-top: 4px; }
.hero-sub { color: var(--muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
.hero-breakdown { display: flex; flex-wrap: wrap; justify-content: center;
  gap: 6px; margin-top: 10px; }
.outcome-pill { display: inline-flex; align-items: baseline; gap: 4px;
  padding: 2px 8px; border-radius: 999px; font-size: 0.78rem;
  background: rgba(15,23,42,0.55); }
.outcome-pill .outcome-count { font-weight: 700; font-variant-numeric: tabular-nums; }
.outcome-pill .outcome-label { color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; font-size: 0.7rem; }
.outcome-done .outcome-count { color: var(--success); }
.outcome-partial .outcome-count { color: var(--warn); }
.outcome-failed .outcome-count { color: var(--danger); }
.hero-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; }
.hero-cell { background: rgba(15,23,42,0.55); border-radius: 8px;
  padding: 10px 12px; }
.hero-cell-label { color: var(--muted); font-size: 0.78rem;
  text-transform: uppercase; letter-spacing: 0.04em; }
.hero-cell-value { font-size: 1.15rem; margin-top: 2px;
  font-variant-numeric: tabular-nums; }
.bar-label, .bar-count, .axis-label { fill: var(--text); font-size: 12px;
  font-family: inherit; }
.axis-label-small { fill: var(--muted); font-size: 10px; font-family: inherit; }
.bar-fill { fill: var(--accent); }
.bar-count { fill: var(--muted); }
.grid { stroke: rgba(148,163,184,0.25); stroke-width: 1; }
.trend-success { stroke: var(--success); stroke-width: 2; }
.trend-auto { stroke: var(--accent); stroke-width: 2; }
.trend-first { stroke: var(--warn); stroke-width: 2; }
.trend-success-swatch { fill: var(--success); }
.trend-auto-swatch { fill: var(--accent); }
.trend-first-swatch { fill: var(--warn); }
table.runs, table.specs { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
table.runs th, table.runs td, table.specs th, table.specs td {
  padding: 8px 10px; border-bottom: 1px solid rgba(148,163,184,0.15);
  text-align: left; vertical-align: top; }
table.runs th, table.specs th { color: var(--muted); font-weight: 600;
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
table.runs td.num, table.runs th.num, table.specs td.num, table.specs th.num {
  text-align: right; font-variant-numeric: tabular-nums; }
.detail-row > td { padding: 0 10px 10px; border-bottom: 1px solid
  rgba(148,163,184,0.15); }
details summary { cursor: pointer; color: var(--muted); padding: 4px 0;
  font-size: 0.85rem; }
table.specs { margin-top: 6px; background: rgba(15,23,42,0.4);
  border-radius: 6px; overflow: hidden; }
"""

_HTML_DOCUMENT = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Top-level entry                                                             #
# --------------------------------------------------------------------------- #


def render_to(
    runs_dir: Path,
    output: Path,
    *,
    generated_at: datetime | None = None,
) -> Path:
    """Load runs from ``runs_dir`` and write the dashboard HTML to ``output``."""

    runs = load_runs(runs_dir)
    html_text = render_dashboard(runs, generated_at=generated_at)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_text, encoding="utf-8")
    return output


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        prog="ccd.dashboard",
        description="Render a static HTML dashboard from run JSON envelopes.",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path(_DEFAULT_RUNS_REL),
        help=f"Directory of run JSON files (default: {_DEFAULT_RUNS_REL}/).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(_DEFAULT_OUTPUT_REL),
        help=f"Output HTML path (default: {_DEFAULT_OUTPUT_REL}).",
    )
    args = parser.parse_args(argv)

    written = render_to(args.runs_dir, args.output)
    print(f"wrote {written}", file=sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
