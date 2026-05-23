from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccd import cli
from ccd.dashboard import (
    build_trend,
    load_runs,
    main,
    pool_records,
    render_dashboard,
    render_to,
)
from ccd.models import DispatchRecord, DispatchStatus, FailureCategory, RunFile

FIXTURES = Path(__file__).parent / "fixtures" / "dashboard"
ALL_FIXTURES = sorted(FIXTURES.glob("*.json"))


def _stage_runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    for src in ALL_FIXTURES:
        shutil.copy2(src, runs / src.name)
    return runs


# --------------------------------------------------------------------------- #
# Loading + pooling                                                           #
# --------------------------------------------------------------------------- #


def test_load_runs_reads_every_json(tmp_path: Path) -> None:
    runs_dir = _stage_runs(tmp_path)
    runs = load_runs(runs_dir)
    assert len(runs) == 2
    labels = sorted(r.project or "" for r in runs)
    assert labels == ["bash-prototype", "ccd-native"]


def test_load_runs_missing_dir_returns_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        runs = load_runs(tmp_path / "does-not-exist")
    assert runs == []
    assert any("does not exist" in m for m in caplog.messages)


def test_load_runs_skips_unparseable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    runs_dir = _stage_runs(tmp_path)
    (runs_dir / "garbage.json").write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        runs = load_runs(runs_dir)
    assert len(runs) == 2  # two valid fixtures, garbage skipped
    assert any("garbage.json" in m for m in caplog.messages)


def test_pool_records_concatenates_in_order() -> None:
    runs = load_runs(FIXTURES)
    pooled = pool_records(runs)
    # 4 records from bash-prototype + 3 from ccd-native = 7
    assert len(pooled) == 7


# --------------------------------------------------------------------------- #
# Trend                                                                       #
# --------------------------------------------------------------------------- #


def test_build_trend_orders_by_timestamp_and_emits_cumulative() -> None:
    runs = load_runs(FIXTURES)
    pooled = pool_records(runs)
    trend = build_trend(pooled)

    assert len(trend) == 7
    assert [p.index for p in trend] == [1, 2, 3, 4, 5, 6, 7]
    # Timestamps must be monotonically non-decreasing.
    for prev, nxt in zip(trend, trend[1:], strict=False):
        assert prev.timestamp <= nxt.timestamp

    # Cumulative dispatch_success_rate ends at done_count / total_count.
    done = sum(1 for r in pooled if r.status is DispatchStatus.DONE)
    assert trend[-1].dispatch_success_rate == pytest.approx(done / len(pooled))


def test_build_trend_empty() -> None:
    assert build_trend([]) == []


def test_build_trend_single_record() -> None:
    rec = DispatchRecord(
        spec_id="spec_001",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=DispatchStatus.DONE,
        attempts=1,
    )
    trend = build_trend([rec])
    assert len(trend) == 1
    assert trend[0].dispatch_success_rate == 1.0


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #


def test_render_dashboard_contains_four_panels() -> None:
    runs = load_runs(FIXTURES)
    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))

    # Hero band
    assert "自律完走率" in html
    assert "dispatch 成功率" in html
    assert "一発合格率" in html
    assert "リトライ回復率" in html
    assert "安全停止率" in html
    assert "総 dispatch 数" in html
    assert "所要時間 平均" in html

    # 失敗カテゴリ panel
    assert "失敗カテゴリ" in html

    # Trend panel
    assert "推移" in html

    # Run table
    assert "run 一覧" in html
    assert "bash-prototype" in html
    assert "ccd-native" in html

    # Per-spec detail uses <details>/<summary> (no JS).
    assert "<details>" in html
    assert "<summary>" in html


def test_render_dashboard_uses_inline_svg_no_scripts_no_external_refs() -> None:
    runs = load_runs(FIXTURES)
    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))

    # At least one <svg> per chart panel (taxonomy + trend legend + trend body).
    assert html.count("<svg") >= 3

    # No script, no inline event handlers, no external resource refs.
    assert "<script" not in html
    assert "onclick" not in html
    assert "onload" not in html
    assert "<iframe" not in html
    assert "<link" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "<img" not in html
    # No CDN-style imports inside <style>.
    assert "@import" not in html
    assert "url(" not in html


def test_render_dashboard_shows_correct_aggregate_numbers() -> None:
    runs = load_runs(FIXTURES)
    pooled = pool_records(runs)
    total = len(pooled)
    done = sum(1 for r in pooled if r.status is DispatchStatus.DONE)
    failures = total - done

    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))

    # 総 dispatch 数 surfaced as the hero "総 dispatch 数" cell.
    assert f">{total}<" in html
    # dispatch 成功率 "<done>/<total>" appears verbatim.
    assert f"{done}/{total}" in html
    # 安全停止率 denominator must equal the failure count.
    assert re.search(rf"安全停止率.*?\d+/{failures}", html, flags=re.DOTALL)


def test_render_dashboard_shows_failure_categories() -> None:
    runs = load_runs(FIXTURES)
    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))
    # The taxonomy bars label each present failure category.
    assert FailureCategory.SMOKE_FAILED.value in html
    assert FailureCategory.SPEC_UNCLEAR.value in html
    assert FailureCategory.AGENT_MISREAD.value in html


def test_render_dashboard_shows_generation_chips_and_quality_note() -> None:
    runs = load_runs(FIXTURES)
    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))
    # Both generation labels surfaced as chips.
    assert "bash_prototype" in html
    assert "ccd_native" in html
    # Data-quality candor note about backfill defaults.
    assert "概算値" in html


def test_render_dashboard_handles_empty_runs() -> None:
    html = render_dashboard([], generated_at=datetime(2026, 5, 23, tzinfo=UTC))
    # Should still emit a valid document with empty-state messaging.
    assert "<html" in html
    assert "</html>" in html
    assert "<script" not in html
    assert "ccd dashboard" in html


def test_render_dashboard_html_is_well_formed() -> None:
    runs = load_runs(FIXTURES)
    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))
    # Same number of opening/closing tags for a few critical elements.
    assert html.count("<section") == html.count("</section>")
    assert html.count("<svg") == html.count("</svg>")
    assert html.count("<details>") == html.count("</details>")
    assert html.count("<table") == html.count("</table>")


# --------------------------------------------------------------------------- #
# render_to / CLI                                                             #
# --------------------------------------------------------------------------- #


def test_render_to_writes_file_and_creates_parents(tmp_path: Path) -> None:
    runs_dir = _stage_runs(tmp_path)
    output = tmp_path / "out" / "deep" / "index.html"
    written = render_to(runs_dir, output)
    assert written == output
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "<html" in text
    assert "推移" in text


def test_module_main_writes_to_default_output_under_repo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_dir = _stage_runs(tmp_path)
    output = tmp_path / "docs" / "index.html"
    rc = main(["--runs-dir", str(runs_dir), "--output", str(output)])
    assert rc == 0
    assert output.exists()
    captured = capsys.readouterr()
    assert str(output) in captured.out


def test_module_main_with_missing_runs_dir_still_succeeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    output = tmp_path / "out.html"
    rc = main(
        [
            "--runs-dir",
            str(tmp_path / "does-not-exist"),
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "run ファイルがありません" in text


# --------------------------------------------------------------------------- #
# CLI integration                                                             #
# --------------------------------------------------------------------------- #


def test_cli_dashboard_subcommand_uses_repo_defaults(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_dir = tmp_path / "_ai_workspace" / "runs"
    runs_dir.mkdir(parents=True)
    for src in ALL_FIXTURES:
        shutil.copy2(src, runs_dir / src.name)

    rc = cli.main(["dashboard", "--repo", str(tmp_path)])
    assert rc == 0
    output = tmp_path / cli.DEFAULT_DASHBOARD_OUTPUT_PATH
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "ccd dashboard" in text
    # Sanity: the project labels from fixtures appear.
    assert "bash-prototype" in text
    assert "ccd-native" in text
    out = capsys.readouterr().out
    assert str(output) in out


def test_cli_dashboard_subcommand_honors_explicit_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_dir = _stage_runs(tmp_path)
    output = tmp_path / "custom_dir" / "report.html"
    rc = cli.main(
        [
            "dashboard",
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.exists()


def test_cli_dashboard_subcommand_does_not_change_existing_subcommands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Adding `dashboard` must not break the existing subcommand surface."""

    # Just hit --version to confirm the parser still builds cleanly.
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- #
# Generation tag presentation                                                 #
# --------------------------------------------------------------------------- #


def test_unspecified_generation_falls_back_to_placeholder(tmp_path: Path) -> None:
    """A legacy RunFile without `generation` should still render without errors."""

    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-04-01T00:00:00+00:00",
                "records": [
                    {
                        "spec_id": "spec_001",
                        "started_at": "2026-04-01T00:00:00+00:00",
                        "finished_at": "2026-04-01T00:00:00+00:00",
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
    runs = load_runs(tmp_path)
    assert len(runs) == 1
    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))
    assert "(未指定)" in html


def test_pooled_metrics_match_aggregate_independently() -> None:
    """The hero band must agree with `metrics.aggregate` over pooled records."""

    from ccd.metrics import aggregate

    runs = load_runs(FIXTURES)
    pooled = pool_records(runs)
    report = aggregate(pooled)

    html = render_dashboard(runs, generated_at=datetime(2026, 5, 23, tzinfo=UTC))
    # Spot-check the autonomous_completion ratio is in the rendered HTML.
    expected = (
        f"{report.autonomous_completion_rate.numerator}/"
        f"{report.autonomous_completion_rate.denominator}"
    )
    assert expected in html


# --------------------------------------------------------------------------- #
# spec_009: survival-bias coverage note + done/partial breakdown              #
# --------------------------------------------------------------------------- #


def _run_with_records(records: list[DispatchRecord], *, generation: str = "ccd_native") -> RunFile:
    return RunFile(
        version=1,
        saved_at="2026-05-23T00:00:00+00:00",
        project="example",
        generation=generation,
        records=records,
    )


def test_dashboard_renders_survival_bias_coverage_note(tmp_path: Path) -> None:
    runs = [
        _run_with_records(
            [
                DispatchRecord(
                    spec_id="spec_001",
                    started_at=datetime(2026, 5, 1, tzinfo=UTC),
                    finished_at=datetime(2026, 5, 1, tzinfo=UTC),
                    status=DispatchStatus.DONE,
                    attempts=1,
                )
            ]
        )
    ]
    html_text = render_dashboard(runs)
    # v1.6: coverage note now reflects that ccd records orchestrator-side
    # interruptions as HALTED + INTERRUPTED. The two remaining structural
    # blind spots (pre-dispatch crashes + bash bridge history) are called
    # out explicitly.
    assert "カバレッジ注記" in html_text
    assert "v1.6" in html_text
    assert "INTERRUPTED" in html_text
    assert "bash bridge" in html_text
    assert "dispatch を開始する前" in html_text
    # The note does not claim "complete coverage" — the dashboard stays honest.
    assert "完全網羅" not in html_text


def test_dashboard_breakdown_shows_done_partial_failed_separately() -> None:
    runs = [
        _run_with_records(
            [
                DispatchRecord(
                    spec_id="spec_001",
                    started_at=datetime(2026, 5, 1, tzinfo=UTC),
                    finished_at=datetime(2026, 5, 1, tzinfo=UTC),
                    status=DispatchStatus.DONE,
                    attempts=1,
                ),
                DispatchRecord(
                    spec_id="spec_002",
                    started_at=datetime(2026, 5, 2, tzinfo=UTC),
                    finished_at=datetime(2026, 5, 2, tzinfo=UTC),
                    status=DispatchStatus.PARTIAL,
                    attempts=1,
                ),
                DispatchRecord(
                    spec_id="spec_003",
                    started_at=datetime(2026, 5, 3, tzinfo=UTC),
                    finished_at=datetime(2026, 5, 3, tzinfo=UTC),
                    status=DispatchStatus.FAILED,
                    attempts=1,
                    failure_category=FailureCategory.SMOKE_FAILED,
                ),
            ]
        )
    ]

    html_text = render_dashboard(runs)
    # done / partial / failed pills are all surfaced — the hero can no longer
    # read as "100% done" when there are partials in the pool.
    assert "outcome-done" in html_text
    assert "outcome-partial" in html_text
    assert "outcome-failed" in html_text
    # And the runs table grew a `partial` column.
    assert "<th class=\"num\">partial</th>" in html_text


def test_dashboard_done_partial_breakdown_omits_zero_categories() -> None:
    runs = [
        _run_with_records(
            [
                DispatchRecord(
                    spec_id="spec_001",
                    started_at=datetime(2026, 5, 1, tzinfo=UTC),
                    finished_at=datetime(2026, 5, 1, tzinfo=UTC),
                    status=DispatchStatus.DONE,
                    attempts=1,
                )
            ]
        )
    ]
    html_text = render_dashboard(runs)
    # The done pill renders; partial/failed pills don't (zero counts → omitted),
    # even though the CSS classes themselves are present in the stylesheet.
    assert 'class="outcome-pill outcome-done"' in html_text
    assert 'class="outcome-pill outcome-partial"' not in html_text
    assert 'class="outcome-pill outcome-failed"' not in html_text


def test_dashboard_hero_not_100_percent_when_partials_present() -> None:
    runs = [
        _run_with_records(
            [
                DispatchRecord(
                    spec_id="spec_001",
                    started_at=datetime(2026, 5, 1, tzinfo=UTC),
                    finished_at=datetime(2026, 5, 1, tzinfo=UTC),
                    status=DispatchStatus.DONE,
                    attempts=1,
                ),
                DispatchRecord(
                    spec_id="spec_002",
                    started_at=datetime(2026, 5, 2, tzinfo=UTC),
                    finished_at=datetime(2026, 5, 2, tzinfo=UTC),
                    status=DispatchStatus.PARTIAL,
                    attempts=1,
                ),
            ]
        )
    ]
    html_text = render_dashboard(runs)
    # The hero now shows autonomous-completion = 1/2 = 50%, not 100%.
    assert ">50.0%<" in html_text
    assert ">100.0%<" not in html_text


def test_run_file_round_trip_through_loader(tmp_path: Path) -> None:
    """A `RunFile` written to disk reloads cleanly via load_runs."""

    run = RunFile(
        version=1,
        saved_at="2026-05-01T00:00:00+00:00",
        project="example",
        generation="ccd_native",
        records=[
            DispatchRecord(
                spec_id="spec_001",
                started_at=datetime(2026, 5, 1, tzinfo=UTC),
                finished_at=datetime(2026, 5, 1, tzinfo=UTC),
                status=DispatchStatus.DONE,
                attempts=1,
            )
        ],
    )
    target = tmp_path / "example.json"
    target.write_text(
        json.dumps(run.model_dump(mode="json")), encoding="utf-8"
    )
    loaded = load_runs(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].project == "example"
    assert loaded[0].records[0].spec_id == "spec_001"
