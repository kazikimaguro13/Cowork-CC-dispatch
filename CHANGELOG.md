# Changelog

本プロジェクトの注目すべき変更を記録する。フォーマットは [Keep a Changelog](https://keepachangelog.com/) に準ずる。

## [0.27.0] — 2026-06-11

### Fixed (performance / 構造)

- spec_046: **mutation 発見 / R5 検証から重い統合テストを除外する ── per-mutant
  コスト爆発の根治**。2026-06-11 の `ccd=auto` / `axis=propose` 手動 `nightly-all`
  初実走で、CCD 自身の mutation 発見 + R5 検証が **1 時間半経っても protocol.py
  1 ファイルを抜けられなかった**。根本原因: mutation testing は **ミュータント
  1 体ごとにテストスイートを丸ごと実行**するが、CCD のスイートには **テスト自身が
  `ccd nightly-all` / mutmut / 隔離 venv プロビジョニングをまるごと起動する統合
  テスト**が含まれ（プロセスツリーに `ccd nightly-all --repo /tmp/pytest-of-…`、
  `…/.ccd-iso-venv` 等を実測）、それが mutmut subprocess の中で**入れ子に再起動**
  して所要時間が非線形に膨らんでいた。
  - **方針A（マーカー除外）を採用**。重い統合テスト（`ccd nightly-all` / iso-venv
    を実起動する `test_launchers.py` の wrapper 実行 5 件 +
    `test_discover.py::test_provision_iso_venv_creates_clone_local_python` + 2s sleep の
    `test_nightly.py::test_dispatch_timeout_marks_candidate_failed`、計 7 件）に
    **`@pytest.mark.slow`（デコレータ形）**を付与。pyproject に `slow` マーカーを
    登録。マーカーは **モジュールレベル `pytestmark =` 代入形ではなくデコレータ形**
    で付ける（代入形は guard R2 (spec_043) が skip ベクタとして弾くため）。
  - CCD profile（`_ai_workspace/profiles/ccd.toml` / `ccd_profile.toml`）を spec_032
    の `[discovery.mutation]` 形に変換し、`extra_args = ["--runner", "python -m
    pytest -x --assert=plain -m 'not slow'"]` を設定。mutmut の既定ランナー
    （`python -m pytest -x --assert=plain`）に `-m "not slow"` を足して**発見/R5
    検証のサブセットから重い統合テストを除外**する。axis profile は現状維持。
  - R5 recheck の既定ランナー（`_build_default_mutation_rechecker`）が **bare
    `MutmutRunner()` にフォールバックしてフルスイートを回していた穴**を塞ぎ、
    profile の `mutation_config`（cwd / tests_dir / extra_args）を
    `_mutmut_runner_from_config` 経由で threading。発見と R5 検証が**同じ軽量
    サブセット**を回す。R5 の判定基準（killed / survived / unknown）は不変。
  - **最終ゲートは従来どおりフル**: R4（`_default_suite_runner`）はベースライン
    （修正前クローン）・修正後・live 再検証のいずれも `pytest -q -p no:cacheprovider`
    で**フルスイート**を回す（`-m` を一切渡さない）。サブセット化は mutmut 専用で、
    R4 のベースライン/修正後は同一ランナーで測られ subset 一致 → 偽陽性 halt しない。
    「発見・検証ループの中だけ軽量サブセット、最終ゲートはフル」を明確に分離。
  - 実測: protocol.py を回す per-suite-run が **33.3s → 7.4s（4.5×）**、かつ
    入れ子の nightly-all/mutmut/iso-venv 再起動を完全に排除（これが 1.5h 爆発の主因）。

## [0.26.0] — 2026-06-11

### Changed (security-hardening)

- spec_043: **guard 動的検証の強化 ── R4 を「green 判定」から「実行テスト数の
  非減少」へ**。2026-06-10 のレッドチーム (Fable 5) が見つけたテスト無効化バイパス
  RT-1 / RT-4 / RT-7 への一本化対策。根本原因は「**diff の文字列を見て skip を
  探している**」こと ── 文字列マッチは追いかけっこで原理的に負ける。CCD の一貫
  原則「**自己申告でなく事実を見る**」に立ち返り、R4 検証を「pytest が green か」
  から「**修正前より実行 (pass) テスト数・収集テスト数が減っていないこと**」に
  格上げした。skip も deselect も collection-hook も import エイリアスも、すべて
  「**実行数が減った**」という 1 つの事実に集約され、文字列を一切見ずに捕まる。
  - `SuiteOutcome` に `collected: int | None` / `passed_count: int | None` を追加。
    `_default_suite_runner` は `pytest -q -p no:cacheprovider` の出力から
    `collected N items` と `N passed` を寛容にパース (読めなければ `None`)。
  - ワーカーの隔離クローン内で、**修正を当てる前に 1 回** suite を実行して
    ベースライン `(collected_base, passed_base)` を測定。修正後の R4 で得た
    `(collected_after, passed_after)` と比較し、`passed_after >= passed_base`
    かつ `collected_after >= collected_base` を R4 合格条件に**加える** (従来の
    「pytest exit 0」条件は維持)。ベースラインが既知件数を持つとき、修正後件数が
    `None` (パース不能) なら保守的に R4 fail (偽陽性は許す・偽陰性は許さない)。
  - guard の `_SKIP_MARKER_PATTERNS` に **保険として** (a) `pytestmark =`、
    (b) `collect_ignore`、(c) `pytest_collection_modifyitems` /
    `pytest_ignore_collect` を追加。これは多層防御の外側で、**主防御ではない**
    ことを docstring に明記。import エイリアス (RT-7) は実行数ベースで捕まるので
    正規表現での追跡はしない。
  - 朝レポート §B の検証証拠に `R4: … — collected N, passed N, baseline N` を表示。
    件数減少で halt した場合は理由に「実行テスト数が baseline を下回った」を明示。

## [0.25.0] — 2026-06-10

### Added

- spec_042: **v3 メトリクス + dashboard 表示 + ドキュメント同期**。spec_038〜041 で
  乗ったレールの上に「**実際に価値を生んでいるか**」を **数字が正直に言う** メトリクス
  層を載せる。v1 からの一貫テーマ「成功率は観測できた母集団の中の率」を踏襲し、
  各指標に **母集団・観測限界の注記** を必ず付ける。0 除算をごまかさない (`merge=0`
  なら「総分数 + merge 0」と書く)、per-worker timestamp が欠損した夜は推定でなく
  **不明** と書く、`marginal_parallel_yield` が観測できない夜は `None` を返す。
- `ccd/metrics.py` ── 新 Pydantic モデル `NightSnapshot` (一晩分の v3 metric
  feedstock、`extra="ignore"` で backfill 寛容) / `WorkerInterval` (per-worker
  start/finish + merged フラグ) / `V3Rate` (population_note 付き) /
  `IterationsHistogram` (1/2/3+ バケット) / `DropReasonBreakdown` /
  `V3MetricsReport`。
- `ccd/metrics.py` ── 新関数 `aggregate_v3(snapshots)` ── 5 指標を集計:
  - `convergence_rate` — 母集団 = FixLoop が起動した候補数。skipped 候補は分母に
    含めない (生存バイアス対策)。
  - `iterations_to_green` ヒストグラム — 1 / 2 / 3+ バケット。1 が支配的ならループ
    は保険、2-3 が多いならループが価値を生んでいるシグナル。
  - `marginal_parallel_yield` — worker lifespan が他の worker と重なった merge を
    分子。per-worker timestamp が欠損した夜は **None** (でっち上げ禁止)。
  - `conflict_drop_rate` — Integrator drop 率。理由別バケット (conflict / cap /
    pause / window / other) は drop_reasons の anchor 文字列で分類。
  - `dispatch_minutes_per_merged_fix` — 総 dispatch 時間 ÷ merge 数。merge=0 の
    夜は「総分数 + merge 0」を表示 (0 除算回避)。
- `ccd/metrics.py` ── 新関数 `render_v3_report(report)` (`ccd report` の v3 節を
  Markdown で render) / `load_night_snapshots(path)` (ディレクトリから snapshot
  JSON を集める、不正ファイルは skip)。
- `ccd/nightly.py` ── 新関数 `build_night_snapshot(result, night_id)` ──
  `NightlyResult` を v3 snapshot dict に投影 (auto_fix + auto_fix_extras から
  `fix_loop_starts` / `converged` / `iterations_to_green` / `merges` /
  `worker_intervals` を計算)。
- `ccd/nightly.py` ── 新関数 `save_night_snapshot(result, night_id, record_dir)` ──
  `<record_dir>/night_<id>.json` に snapshot を JSON dump (sort_keys=True で
  決定的、`ensure_ascii=False` で日本語 anchor が読みやすい)。
- `ccd/nightly.py` ── `run_nightly()` に新 kwarg `record_dir: Path | None`。
  既定 `<repo>/_ai_workspace/nightly/records/`。完了時に snapshot を best-effort
  で save (write 失敗で nightly 全体は止めない)。新定数
  `_NIGHTLY_RECORDS_DIR_REL`。
- `ccd/sweep.py` ── 各 policy ごとに `record_dir` を `<ccd_repo>/_ai_workspace/
  nightly/<policy>/records/` に redirect (client repo に write が漏れない構造を
  proposal_dir と同じ流儀で維持)。fallback mode (legacy 単一 profile) は record_dir
  も `None` で既定経路。
- `ccd/dashboard.py` ── 新 v3 panel renderer `_render_v3_panel(report, night_count)`
  ── 5 指標 + drop 理由内訳をインライン SVG なしの軽量 HTML で表示。`render_dashboard()`
  / `render_to()` が `v3_records_dir` を読んで非空なら panel を追加 (v1 layout は
  snapshot が無い限り bit-for-bit 不変)。
- `ccd/cli.py` ── `ccd report` に `--v3-records DIR` flag 追加。記録があれば v1
  scoreboard の後に v3 節を追記 (snapshot 無し → v1 のみで bit-for-bit 互換)。
  `ccd nightly` / `ccd nightly-all` は `record_dir` kwarg を尊重。
- 新規 pytest:
  - `tests/test_metrics.py` ── v3 集計テスト 13 件 (空 / 単一夜 / 複数夜 / 0 除算 /
    backfill 寛容性 / `marginal_parallel_yield` 不明 / 旧 record 混在 / drop_reasons
    分類 / iteration ヒストグラム / dispatch 分計算 / 順序不変 / render 出力 /
    population_note 文字列)。
  - `tests/test_nightly.py` ── snapshot 永続化テスト 2 件
    (`test_build_night_snapshot_projects_v3_fields` / `test_save_night_snapshot_writes_json`)。
  - `tests/test_dashboard.py` ── v3 panel テスト 2 件 (snapshot 無し / 有り)。

### Changed

- `docs/DESIGN.md` ── v3 シリーズ §10 を追加 (旧 `docs/DESIGN_v3_draft.md` の §10.1〜
  §10.9 をそのまま取り込み、§10.10 に **実装の記録 + 予想と違った点 4 点** を追記)。
  draft ファイルは削除。
- `docs/DESIGN_v3_draft.md` ── **削除** (上記 §10 に取り込み済み)。
- `README.md` ── version 0.24.0 → 0.25.0、テスト数を spec_042 反映後の値に同期、
  運用ステータス節に v3 シリーズ 5/5 (メトリクス + dashboard) を追記、推奨の段階的
  有効化手順 (K=2 → loop_max_iterations=3 → P=2) を 1 段落追加 (spec §2-4 逐語)。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.24.0` →
  `0.25.0` (minor bump — v3 metric layer + snapshot persistence 追加)。

### Notes

- spec_042 §2-4 「有効化はしない」を逐語遵守: `_ai_workspace/ccd_profile.toml` /
  `_ai_workspace/profiles/*.toml` は **変更していない**。K / P / loop_max_iterations の
  既定値は v2 のまま (1 / 1 / 1) で、merge しても挙動は変わらない。operator が朝
  レポートを見ながら段階的に有効化する (README 末尾の推奨手順)。
- spec_042 §3-2 「v3 フィールドが無い古い record が混ざっても集計が落ちない」を
  Pydantic の `extra="ignore"` + 全フィールドにデフォルト値で構造的に満たす
  (`tests/test_metrics.py` の backfill テストが pin)。
- spec_042 §3-3 「report / brief / dashboard で同一夜の数字が一致」── 同じ
  `NightlyResult` の `merged` フィールドを 3 経路すべてが参照するので merge 数は
  必ず一致。`drop_reasons` も `NightlyResult.drop_reasons` 経由で 3 経路一致 (brief
  の §B 「drop=N」は「候補のうち merge しなかった広義の drop」で別概念 — v3 metric
  の `conflict_drop_rate` は「Integration gate-trip の狭義 drop」、note 文字列で
  明示)。

## [0.24.0] — 2026-06-10

### Added

- spec_041: **WorkerPool ── 複数 CC dispatch の並列化と直列 Integration queue**。
  spec_038 の K 候補と spec_039 の FixLoop と spec_040 の隔離 Integrator を
  並列実行できる形にまとめる。並列度 P (1..4) のワーカープールが候補を並列
  処理し、完了した検証済み patch を **直列の Integration queue** に投入。
  Integrator は完了順に 1 件ずつ live へ apply + 再検証 + merge する。各
  integration の前に **PAUSE / 未push バックログ cap / `max_merges_per_night`
  / 夜間窓 wall-clock** を再評価するゲートが入り、いずれかが trip すると
  残りの patch は退避 (`_ai_workspace/nightly/proposals/dropped_*.patch`) +
  朝レポートでの 1 行で surface される。既定 (P=1) で v2 / spec_038〜040 と
  外形完全一致 (テスト固定)。
- `ccd/profile.py` ── `SafetyConfig.parallelism: int = 1` (1..4 バリデータ
  付き) + `SafetyConfig.max_merges_per_night: int = 3` (1..10 バリデータ付き)。
- `ccd/nightly.py` ── 新ヘルパ `_run_worker_phase()` (worker (クローン) 内
  の dispatch + R5/R4/guard 回しを 1 関数に切り出し) + 新ヘルパ
  `_drain_worker_pool()` (ThreadPoolExecutor で K 候補を P 並列に dispatch
  → as_completed 順に直列 Integrator)。worker は **`_WorkerPhaseResult`**
  という新 dataclass を返し、verified diff + worker_verification + fl +
  start/finish ISO timestamps を carry。失敗 (translate fail / 例外 / 未収束)
  は `halt_outcome` フィールドに包む。
- `ccd/nightly.py` ── 新 halt anchor 定数: `_HALT_MAX_MERGES_REACHED_PREFIX`,
  `_HALT_NIGHT_WALL_CLOCK_PREFIX`, `_HALT_WORKER_CRASHED_PREFIX`。
- `ccd/nightly.py` `AutoFixOutcome` に新フィールド `worker_id: str = ""`,
  `worker_started_at: str = ""`, `worker_finished_at: str = ""`。spec_042
  が **実測** で並列効率を集計できるようにする。既定値は空文字列で v2 外形
  互換。
- `ccd/nightly.py` `NightlyResult` に新フィールド `parallelism: int = 1`,
  `achieved_max_concurrency: int = 1`, `drop_reasons: tuple[str, ...] = ()`。
  既定値は spec_023〜040 と bit-for-bit 一致。
- `ccd/nightly.py` ── 新ヘルパ `_save_dropped_patch()` ── gate trip で
  退避された verified patch を `_ai_workspace/nightly/proposals/dropped_*.patch`
  に保存。propose-mode artifact と同じ shape で朝レポート §B が surface
  しやすい。
- `ccd/brief.py` ── §B 複数候補レンダラ (`_render_section_b_multi`) に
  **夜サマリ 1 行** を追加: P>1 のときに「候補 K / 並列 P / 達成同時実行数
  / merge 数 / drop 数 (理由別)」を表示。P=1 では出力されないので v2 layout
  と bit-for-bit 一致。
- `ccd/brief.py` ── §B 候補小節 (`_render_one_candidate_subsection`) に
  worker_id + start/finish timestamp を追加。outcome の `worker_id` が
  空文字列のときは小節に何も足さないので spec_038〜040 と bit-for-bit 一致。
- 新規 pytest 11 件 (test_nightly.py) ──
  - `test_default_p1_run_outcomes_identical_to_spec040` (P=1 外形互換)
  - `test_p2_three_candidates_parallel_workers_serial_integration` (P=2 で
    workers が実際に並列に走り、Integration は直列)
  - `test_max_merges_per_night_drops_remaining_with_patch_save` (cap 到達 → drop)
  - `test_worker_exception_isolated_does_not_stop_sibling` (例外隔離)
  - `test_per_worker_timestamps_recorded_on_outcomes` (per-worker timestamp)
  - `test_p2_brief_renders_night_summary_line` (夜サマリ render)
  - `test_p1_brief_does_not_mention_parallel_summary` (P=1 layout 不変)
  - `test_parallelism_clamped_to_safety_bounds` (1..4)
  - `test_max_merges_clamped_to_safety_bounds` (1..10)
  - `test_clones_cleaned_up_after_night` (clone cleanup)
  - `test_rate_limit_dispatch_failure_is_transient_for_fixloop`
    (spec_041 §2-6 — rate-limit は既存 transient 分類で FixLoop が拾う)

### Changed

- `ccd/nightly.py` ── `_run_auto_fix_loop()` の戻り値を 5-tuple に拡張:
  `(primary, extras, parallelism, achieved_max_concurrency, drop_reasons)`。
  `run_nightly()` は後ろ 3 つを `NightlyResult` に詰める。Public 互換性は
  保たれる (新フィールドはデフォルト値が v2 外形と一致)。
- `ccd/nightly.py` ── `_run_auto_fix_loop()` の本体を WorkerPool model に
  rewrite。候補選択 → **全候補を main thread で直列 translate** (spec_auto
  ID の monotonic 採番が thread-safe でないため) → `_drain_worker_pool()`
  で並列 dispatch + 直列 integrate。spec_038 の K 直列ループは内部実装が
  変わったが外形は同一。
- `ccd/nightly.py` ── 既存の `_process_one_auto_fix_candidate()` は
  そのまま残してあるが (内部の synchronous convenience として)、
  `_run_auto_fix_loop()` からの呼び出しは削除した。後方互換性のためだけ
  に残置。
- `ccd/brief.py` `run_brief()` / `_render_md()` / `_render_section_b_multi()`
  に `parallelism` / `achieved_max_concurrency` / `drop_reasons` を追加。
  既定値は v2 と一致 (P=1, conc=1, reasons=())。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ──
  `0.23.0` → `0.24.0` (minor bump — 新 SafetyConfig フィールド +
  WorkerPool model 導入)。

## [0.23.0] — 2026-06-10

### Added

- spec_040: **隔離の統一 ── auto モードの clone-and-patch 化と Integrator 導入**。
  spec_028 で propose モードが導入したクローン隔離ワークフローを **auto モードにも
  一本化**。すべての fix 作業 (translate → branch → dispatch → R5 + R4 + guard
  → 収束ループ) は使い捨て隔離クローン内で実行され、live repo に触れるのは
  **新設の直列 Integrator** のみ。Integrator は worker が検証済みの patch を
  受け取り、live の feat branch に apply → guard + R4 を **live 上で再検証** →
  main へ local merge (push しない) → feat branch 削除。apply 失敗 / 再検証
  失敗 / merge 失敗のいずれでも **drop + live 復元** (rebase も再 dispatch も
  しない、spec_040 §2-2 逐語)。狙い: (1) spec_041 の並列ワーカーが live を
  奪い合う事故を構造的に排除、(2) auto / propose のコードパスを統一、
  (3) ワーカーの暴走 git 書き込みを clone に閉じ込める (spec_014 の発見隔離
  と同じ思想を修正側に適用)。
- `ccd/nightly.py` ── 新型 `PatchApplier = Callable[..., None]` (Integrator
  patch 適用 seam) + デフォルト実装 `_default_patch_applier()` (subprocess
  `git apply --index --whitespace=nowarn` → `git commit -m "auto-fix: <spec_id>"`)。
- `ccd/nightly.py` ── Integrator 本体 `_integrate_one_candidate()` を新設。
  branch 作成 → patch 適用 → live diff 取得 → guard 再検証 → R4 再検証 →
  merge → branch 削除を 1 関数で表現。失敗時は `_restore_repo_after_halt()`
  (spec_026 §2-2 リストア primitive) を呼ぶ。
- `ccd/nightly.py` ── Integrator 用 halt anchor 定数群:
  `_HALT_INTEGRATOR_APPLY_FAILED` / `_HALT_INTEGRATOR_GUARD_FAILED` /
  `_HALT_INTEGRATOR_R4_FAILED` / `_HALT_INTEGRATOR_BRANCH_FAILED` /
  `_HALT_INTEGRATOR_MERGE_FAILED` / `_HALT_INTEGRATOR_EMPTY_PATCH`。
  朝レポート §D が "live drift / live regression" を判別できる。
- `run_nightly()` / `_run_auto_fix_loop()` / `_process_one_auto_fix_candidate()`
  に `isolated_workspace` + `apply_patch` キーワードを追加 (propose と同じ
  seam を auto でも使う)。`cli.main()` / `_cmd_nightly()` / `_cmd_nightly_all()`
  も `apply_patch` を素通しで forward。
- 新規 pytest 5 件 (test_nightly.py) ──
  `test_auto_mode_worker_phase_does_not_write_to_live` (構造不変条件: worker
  phase で live への書き込みが一切ないことを `repo=` 引数バケットで pin)、
  `test_auto_mode_integrator_drops_when_apply_fails` (apply 衝突 → drop +
  live 復元)、`test_auto_mode_integrator_drops_when_live_r4_fails` (clone
  green / live red の R4 ドリフト → drop)、
  `test_auto_mode_integrator_drops_when_live_guard_fails` (live guard
  ドリフト → drop)、`test_auto_mode_e2e_v2_external_state_preserved`
  (外形 v2 互換: `merged=True` / `merge_diff` / `auto_fix.dispatch_status`
  の shape が spec_023〜038 と bit-for-bit 一致)。

### Changed

- `ccd/nightly.py` ── `_process_one_auto_fix_candidate()` を全面書き換え。
  v2 では live のワーキングツリー上で dispatch → R5/R4/guard → merge を
  順に実行していた (spec_028 で propose のみクローン化)。spec_040 で auto も
  クローン内ワーカー + 後段 Integrator に分離:
  - Worker phase (クローン内): translate → `isolated_clone` → clone に
    spec_auto.md コピー → `gops.create_and_checkout_branch(repo=clone)` →
    `run_fix_loop(repo=clone, ...)` (dispatcher / R5 / R4 / guard すべて
    clone を見る) → 検証済み diff を抽出。
  - Integrator phase (live): `_integrate_one_candidate()` が patch を live
    に適用し guard + R4 を再検証して merge。worker の `proposal_diff` /
    `verif` を入力として受け取り、`AutoFixOutcome.merged=True` / `merge_diff`
    を produce する。
- `ccd/nightly.py` `_copy_spec_auto_into_clone()` ── clone == live の縮退
  (テスト fixture の degenerate factory) を `SameFileError` 回避のため
  no-op として扱う (本番では別 tmpdir を yield するので影響なし)。
- `ccd/cli.py` `cli.main()` / `_cmd_nightly()` / `_cmd_nightly_all()` ──
  `apply_patch: Any | None = None` を追加し `run_nightly` に forward。
- spec_026 §2-2 のリストア primitive (`_restore_repo_after_halt`) の発火
  経路が変わった: worker phase の R5 / R4 / guard / dispatch 失敗では
  **live を一切書き換えないので restore も不要** (clone は workspace
  context-manager に rmtree-d)。Integrator 失敗時にだけ live restore が
  fire。既存のスペック_026 系テスト 6 件を spec_040 後の挙動に rewrite。
- `tests/test_nightly.py` ── 既存 auto テスト 51 件に `_auto_fix_test_seams(tmp_path)`
  (`isolated_workspace` + `apply_patch` の test fake) を inject。
  `_FakeGitOps.canned_diff` のデフォルトを空文字列から非空プレースホルダーに
  変更 (Integrator の `_HALT_INTEGRATOR_EMPTY_PATCH` ガードに偶然 hit しないため)。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ──
  `0.22.0` → `0.23.0` (minor bump — 新 PatchApplier seam / 新 Integrator
  関数 / auto モードのコードパス変更)。

## [0.22.0] — 2026-06-10

### Added

- spec_039: **FixLoop ── 収束ループ (ralph 型外側ループ + 無進捗検知)**。
  夜間 fix の dispatch を「単発 + 検証」から「**検証が green になるまで反復する
  収束ループ**」に昇格。ralph loop の思想（完了条件を満たすまで同じ仕事に戻し
  続ける）だけを借り、完了判定は自己申告ではなく **R5/R4/guard の機械検証**に
  置く。**既定 `loop_max_iterations=1` で v2 (spec_023〜038) と外形完全一致**。
- 新モジュール `ccd/loop.py` ── `run_fix_loop`、`FixLoopOutcome`、
  `IterationVerification` と halt anchor 定数群（`LOOP_HALT_MAX_ITERATIONS` /
  `LOOP_HALT_BUDGET` / `LOOP_HALT_NO_PROGRESS` / `LOOP_HALT_IMMEDIATE`）。
  per-iteration の dispatch + verify を `max_iterations` だけ回し、green / 予算
  超過 / 無進捗 / 即 halt カテゴリのいずれかで停止。
- `ccd/profile.py` `SafetyConfig.loop_max_iterations: int = 1`（範囲 1..5、
  validator `_loop_iterations_in_range` で loud-fail）。`render_profile` も
  新フィールドを TOML-shaped 出力に反映。
- `ccd/nightly.py` `AutoFixOutcome` ── 新フィールド `iterations: int = 0` /
  `converged: bool = False` / `loop_halt_reason: str = ""`。後段（spec_042）が
  夜間 record JSON から集計できる形で永続化。
- `ccd/retry.py` `dispatch_with_retry` ── `initial_feedback: Path | None = None`
  キーワードを追加（FixLoop が convergence loop の feedback 入口として使用）。
  既存呼び出しは default `None` のまま変化なし。
- `ccd/retry.py` `is_failure_immediate_halt()` ── 公開ヘルパ関数。
  retry.py の `_HALT_ON_CATEGORIES` + BLOCKED 判定を loop.py と共有することで、
  chain-side smoke-retry と nightly-side 収束ループの「即 halt」境界を一元化。
- 新規 pytest 21 件（loop 10 + nightly 6 + profile 5）── 1 回失敗→2 回目 green
  で converged=True / iterations=2、毎回同一シグネチャ fail で iter 3 開始前
  に halt、wall-clock 予算超過で `LOOP_HALT_BUDGET`、`blocked` / `environment`
  カテゴリで 1 回 halt、`smoke_failed` は retryable で feedback 経由でループ続行、
  デフォルト K=1 / iter=1 brief に `収束:` / `未収束:` 行が出ないこと、を pin。

### Changed

- `ccd/nightly.py` `_process_one_auto_fix_candidate` /
  `_process_one_propose_candidate` ── dispatch + R5 + R4 + guard の per-iteration
  ロジックを :func:`ccd.loop.run_fix_loop` 経由に置換。verifier closure に R5
  (`_verify_r5`) / R4 (suite runner) / guard (inspector + diff capture) を注入し、
  FixLoop は IterationVerification の green を基に converged を判定。新ヘルパ
  `_verify_iteration_auto` を切り出して auto / propose 両モードで verifier 内部
  を共有。
- `ccd/nightly.py` `_run_auto_fix_loop` / `_run_propose_loop` ── 新キーワード
  `loop_max_iterations: int = 1` を追加し `_process_one_*_candidate` に流す。
  `run_nightly` が `profile.safety.loop_max_iterations` を読み取って配線。
- `ccd/nightly.py` `_build_default_fix_dispatcher` ── 内部 `_dispatcher` に
  `feedback: Path | None = None` キーワードを追加し `dispatch_with_retry` の
  `initial_feedback=` に転送。`loop_max_iterations=1` では feedback は常に None
  なので v2 prompt shape は完全に維持。
- `ccd/brief.py` ── §B の auto Phase 2 / propose / per-candidate subsection に
  `_format_fix_loop_summary(auto_fix)` を呼ぶ 1 行追加（"- 収束: N iterations" /
  "- 未収束: N iterations (無進捗検知で halt)"）。**iter=1 + converged では行を
  抑制** ── v2 デフォルト profile の朝レポートは見た目不変。
- `README.md` / `docs/DESIGN.md §9` ── テスト数 `645` → `666`、version 表記、
  spec 範囲を `spec_013〜039` に同期。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.21.0` → `0.22.0`
  (minor bump — 新フィールド / 新 FixLoop モジュール / 新 brief 行)。

## [0.21.0] — 2026-06-10

### Added

- spec_038: `safety.max_candidates_per_night` (default `1`、許容範囲 `1..5`)。
  夜間ループの「1晩1候補」制約 (spec_023〜026) を **直列のまま** 解除し、
  `auto` / `propose` 両モードが 1 晩で最大 K 件の候補を順次処理可能に。
  並列化は spec_041 の領分で本 spec は入れない。
- `ccd/nightly.py` ── 新ヘルパ `_select_candidates(..., limit=int)` を追加。
  既存の `_select_template_a_candidate` / `_select_template_b_candidate` は
  全件を返す `_select_template_a_candidates` / `_select_template_b_candidates`
  に拡張、`_select_candidate` 単数版は削除（spec_038 §2-2「選択ロジックの重複を
  残さない」）。`_run_auto_fix_loop` / `_run_propose_loop` の per-candidate
  本体は `_process_one_auto_fix_candidate` /
  `_process_one_propose_candidate` に切り出して直列ループから再利用。
- `NightlyResult.auto_fix_extras: tuple[AutoFixOutcome, ...]` 追加。K=1 では
  常に空タプルで v2 外形を維持、K>1 で 2 件目以降の per-candidate 結果を保持。
- 朝レポート §B 複数候補対応 ── 新レンダラ
  `_render_section_b_multi` が `### 候補 i/N` 小節を候補ごとに掲載。
  K=1 の夜は現行 §B（Phase 1 / Phase 2 / propose）を見た目不変で出力
  （spec_038 §2-4「0件/1件は現行と同じ見た目」）。
- 候補間 PAUSE / 未push バックログ cap 再評価 ── K>1 で候補処理の合間に
  `_ai_workspace/PAUSE` の出現と backlog cap 復活を `_run_auto_fix_loop` /
  `_run_propose_loop` の**両方**で再判定し（spec_038 §2-3 逐語「未push バックログ
  cap と PAUSE を再評価」を mode 制限なしで適用）、超過/存在なら残候補を
  スキップして合成 skip 結果（`remaining candidate(s) skipped: ...`）を 1 件追加。
  propose は merge しないため backlog は通常 0 のまま no-op だが、auto と
  ブレーキ semantics を揃え operator が両モードで同じ brake を期待できる
  ようにする。1 候補の HALT は残候補の処理を止めない（halt は候補単位）。
- 新規 pytest 12 件（profile 5 + nightly 7）── デフォルト K=1 の外形不変、
  K=3 × 4 候補で top-3 のみ処理、候補2件目前のバックログ cap 超過 →
  remainder スキップ（auto / propose の両方）、候補1件目 HALT で候補2件目が
  走ること、K=1 の brief に複数候補マーカーが出ないこと、K=3 の brief に
  `### 候補 i/N` が並ぶこと。

### Changed

- `ccd/profile.py` `SafetyConfig` ── `max_candidates_per_night: int = 1`
  フィールド追加（pydantic validator `_k_in_range` で 1..5 に loud-fail 制限）。
  `render_profile` も新フィールドを TOML-shaped 出力に反映。
- `ccd/nightly.py` ── `_run_auto_fix_loop` / `_run_propose_loop` の戻り型を
  `AutoFixOutcome` → `tuple[AutoFixOutcome, tuple[AutoFixOutcome, ...]]` に
  変更（primary + extras）。`run_nightly` がこのタプルを `NightlyResult.auto_fix`
  と `auto_fix_extras` に分配。
- `ccd/brief.py` `run_brief` ── `auto_fix_extras: Sequence[AutoFixOutcome] = ()`
  キーワードを追加。`_render_section_a` / `_render_section_d` /
  `_render_section_f` に extras を渡し、複数候補時は §A の見出しを「N 件直列
  処理 (merge X / proposal Y / HALT Z / skip W)」に、§D の per-candidate HALT
  は §B に既出のため抑制（duplication 回避）。
- `README.md` / `docs/DESIGN.md §9` ── テスト数 `633` → `645`、version 表記、
  spec 範囲を `spec_013〜038` に同期。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.20.5` → `0.21.0`
  (minor bump — 新フィールド / 新 NightlyResult フィールド / 新 brief キーワード)。

## [0.20.5] — 2026-06-02

### Removed

- `scripts/launchers/nightly_all_wrapper.sh` ── detached 起動後の
  `disown 2>/dev/null || true` を削除。subagent fresh review (SOP 2〜4 サイクル目) で
  繰り返し「必要性に疑問」と指摘されていた既知残 (C)。**WSL Ubuntu-24.04 で実測**した
  結果、`nohup`（SIGHUP 無視）+ `setsid`（新セッション）で既に detach 済みであり、
  `huponexit=off` の非対話 bash では素の `&` でもプロセスが親 exit 後に生存することを
  確認（disown 無しでも生存）。disown は対話シェル + `huponexit on` という本番
  （タスクスケジューラ → wsl.exe → 非対話 bash）経路では発生しない edge case 用の
  冗長防御だったため削除（verify→simplify）。挙動は不変（本番経路で disown は一度も
  効いていなかった）。

### Added

- `tests/test_launchers.py` ── disown 復活防止の回帰テスト 1 件
  （コメント言及は許容しつつ disown コマンドの不在と nohup setsid の維持を assert）。

### Changed

- `README.md` / `docs/DESIGN.md §9` ── テスト数 `632` → `633`、version 表記、
  spec 範囲を `spec_013〜037` に同期。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.20.4` → `0.20.5`。

## [0.20.4] — 2026-06-01

### Fixed

- spec_036: spec_035 ship 直後の subagent fresh review (SOP 運用 4 サイクル目) で
  抽出した盲点を構造修正。
  - **A**: wrapper の `using ccd:` 行と `venv activate exit:` 行が別々のサブシェルで
    **別々に activate** しており、報告する ccd と測定する exit が別プロセスだった
    （honest 診断の自己矛盾）。1 回の activate に統合し、同一行で記録するよう修正。
  - **B**: PROJECT ハードコード検出の guardrail テストが `/home/` の文字列依存で
    `/Users/`・`$HOME`・変数経由を見逃していた。whitelist 方式（既知の 2 代入形のみ
    許可）に汎用化。
  - **C-test**: WARNING テストの明示渡しケースに「渡した PROJECT が採用される」
    assert を合流。
  - **D**: wrapper に `set -u` 相性のコメントを明示。

### Added

- `tests/test_launchers.py` ── 二重 activate 解消の回帰テスト 1 件
  （`using ccd:` と `venv activate exit:` が同一行であること）。

### Changed

- `scripts/launchers/nightly_all_wrapper.sh` ── 診断ログの二重 activate を 1 回に統合、
  set -u 相性コメント追加。
- `tests/test_launchers.py` ── guardrail を whitelist 方式に、WARNING テストに PROJECT
  採用 assert 合流。
- `README.md` / `docs/DESIGN.md §9` ── テスト数 `631` → `632` に同期。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.20.3` → `0.20.4`。

## [0.20.3] — 2026-06-01

### Added

- `examples/register_nightly.ps1.template` ── 週次タスク登録の明示渡し版テンプレ
  を repo 管理に。`register_nightly.ps1` は `_ai_workspace/` 配下で git 管理外の
  ため、運用切替時の参照点として配置（spec_035 §2-i）。
- `tests/test_launchers.py` ── 4 件追加：
  - PROJECT ハードコード復活を検出する guardrail（silent regression 防護網）
  - venv activate exit code がログ記録されることの確認
  - examples テンプレの存在確認
  - 引数なし呼び出しで WARNING がログに出ること（明示渡しでは出ないこと）

### Changed

- `scripts/launchers/nightly_all_wrapper.sh` ── venv activate の exit code を別行で
  明示ログ（`using ccd:` だけでは venv/system の ccd を判別できない問題を解消、
  spec_035 §3-b）。引数なし呼び出し時にログへ WARNING を記録（運用切替忘れの
  事後発覚、spec_035 §2-ii）。
- `docs/DESIGN.md §9.6` ── launcher pattern に「再現条件」サブパラグラフ追加。
  単純な `&` は valid・`& ;` パターンのみ構文エラーという境界条件を明記（spec_035
  §1 + §3-c）。
- `README.md` ── `v0.20.1 / 625 tests` → `v0.20.3 / 631 tests` に同期。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.20.2` → `0.20.3`。

## [0.20.2] — 2026-05-28

### Fixed

- spec_034: spec_033 ship 直後の subagent fresh review (SOP 運用 2 件目) で抽出した
  3 件の盲点を構造修正。
  - **A**: wrapper の `PROJECT` ハードコードを相対解決に。repo を別パスに
    clone しても動くよう relocation 耐性を持たせた。第 1 引数で明示渡しも可能。
  - **B**: wrapper 起動時に `command -v ccd` の出力をログに記録。venv activate
    失敗時に system Python の古い ccd が呼ばれても診断可能になった。
  - **D (erratum)**: spec_033 で DESIGN.md §9.6 と CHANGELOG `[0.20.1]` に
    書いた「`LastTaskResult=2` = `ERROR_FILE_NOT_FOUND` マッピング」は **誤認**。
    実際は bash 構文エラーが exit 2 で wsl.exe 経由で Windows に透過した結果で、
    exit code 2 が `ERROR_FILE_NOT_FOUND` (=2) と同じ数値だっただけ。本 spec で
    DESIGN.md §9.6 の機序記述を訂正。

### Added

- `tests/test_launchers.py` ── PROJECT 相対解決のテスト 2 件（tempdir に
  wrapper をコピー → repo root が導出される / 第 1 引数による明示渡し優先）。

### Changed

- `scripts/launchers/nightly_all_wrapper.sh` ── PROJECT 相対解決、command -v ccd
  ログ追加、PROJECT 値のログ記録。
- `docs/DESIGN.md §9.6` ── launcher pattern の機序記述を訂正。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.20.1` → `0.20.2`。

## [0.20.1] — 2026-05-28

### Fixed

- spec_033: Windows タスクスケジューラ経由起動が `LastTaskResult=2` で失敗する
  7 件目の欠陥を構造修正。原因は register_nightly.ps1 の `$NightlyCmd` に
  here-string で複数行 bash を埋めていたが、wsl.exe → bash -c の経路で
  改行が解釈されず `& ; echo` の構文衝突を起こしていたこと（手動
  `ccd nightly-all` では露見せず、タスクスケジューラ経路だけで露見する性質）。
  `scripts/launchers/nightly_all_wrapper.sh` を新規追加し、launcher pattern として
  DESIGN.md §9.6 に明文化。`register_nightly.ps1` テンプレート (_ai_workspace 配下、
  git 管理外) は中島さんが手動で `$NightlyCmd` を `"bash $ProjectDir/scripts/launchers/nightly_all_wrapper.sh"`
  に書き換えて再登録すること。

### Added

- `scripts/launchers/nightly_all_wrapper.sh` (新規) ── タスクスケジューラから
  呼ばれる wrapper script。複数行 bash 処理を集約。launcher pattern の代表例。
- `tests/test_launchers.py` (新規) ── wrapper script の bash 構文チェック
  (`bash -n`) と shebang 妥当性確認。

### Changed

- `docs/DESIGN.md §9.6` ── launcher pattern を明文化（spec_033 サブセクション）。
- `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py` ── `0.20.0` → `0.20.1`。

## [0.20.0] — 2026-05-27

spec_032 — **mutmut とネスト構造の互換性（欠陥 6）の構造修正**。spec_030 で「`mutants_total = 0` の沈黙失敗」を HALT として可視化、spec_031 で iso-venv install の沈黙失敗を `IsoVenvProvisioningError` として捕まえた後も、axis-knowledge-rag に対する mutation チャンネルは sweep #3〜#5 で安定して 0 mutants HALT を返し続けた。sweep #5 で spec_031 の post-install validation を通過した（mutmut バイナリ・pytest バイナリ・対象 package が iso-venv に揃っている）にもかかわらず mutmut が 0 mutants を返す事実から、**原因は install ではなく mutmut とネスト構造（`backend/src/...`）の互換性問題**であることが確定。spec_032 は **profile から mutmut の実行パラメータ（cwd / paths_to_mutate / tests_dir / extra_args）を注入できるようにする** ことで、mutmut 2.x の以下 3 仮説のいずれにも対応可能な汎用解を提供する (minor bump = 新機能 + schema 拡張):

1. mutmut が `backend/src/...` のような階層を source root として認識できない
2. mutmut が `backend/tests` のような階層を test ディレクトリとして自動検出できない
3. mutmut の default cwd（repo root）からの相対パス解決が `backend/src/...` で破綻

これで v2 設計思想で実走で炙り出された **全 6 件の欠陥が構造修正済み**（5 件構造修正 + 1 件防護網捕獲 → **6 件全部構造修正**）に昇格する。

### Added

- **`ccd/profile.py` に `MutationConfig` モデルを追加** (spec_032 §2-1):
  - 新規 `MutationConfig` (pydantic `BaseModel`, `extra="forbid"`):
    - `mutation_paths: list[str]` (空リスト reject、各要素 non-empty)。
    - `cwd: str | None = None` ── iso-clone 配下のサブディレクトリ。`None` でレガシー spec_018 の挙動（mutmut が clone root で起動）を維持。
    - `tests_dir: str | None = None` ── `mutmut run --tests-dir <tests_dir>` に渡す値。`None` で mutmut の auto-discover に任せる。`cwd` 配下相対。
    - `extra_args: list[str] = []` ── `mutmut run` コマンドラインの末尾に逐語追加。CCD は内容を whitelist しない（spec §3「既存防護網に委譲」── 不正な flag は mutmut の non-zero exit / spec_030 の 0-mutants HALT で結果的に捕まる）。
  - `DiscoveryConfig` に新規 `mutation: MutationConfig | None = None` フィールドを追加。`None` でレガシー `discovery.mutation_paths` 経路（spec_018）を bit-for-bit 維持。
  - `model_validator(mode="before")` で「`discovery.mutation_paths` と `[discovery.mutation]` の同時指定」を reject（ambiguity 防止、loud is better than silent）。
  - 新規 helper `effective_mutation_config(discovery)` ── レガシー / 新形式どちらも `MutationConfig` で受け取れる統一ビュー。call site の分岐を排除。
  - `render_profile` を新形式の出力に対応 ── `[discovery.mutation]` テーブルを round-trippable に emit、レガシー形式（top-level `mutation_paths`）と排他的に切り替え。
- **`ccd/profile.py:_validate_mutation_config_paths` を追加** (spec_032 §2-1):
  - profile 読み込み時に、`[discovery.mutation]` ブロックが指定するパス群が target repo に実在するかを検証。失敗は全部集めて 1 回の `ProfileError`（新規例外、`ValueError` の subclass）で raise。
  - 検証する 3 項目: (a) `cwd` ディレクトリが存在 / (b) `mutation_paths` の各 entry が `repo_root[/cwd]` 配下で存在 / (c) `tests_dir` が `repo_root[/cwd]` 配下で存在。
  - エラーメッセージには **不在パスの絶対パス** を全部含める（spec_031 の `IsoVenvProvisioningError` と同じ思想 ── 操作者が「何が見つからなかったか」を 1 行で読める）。
  - レガシー `discovery.mutation_paths`（top-level、spec_018 形式）は **path existence 検証の対象外** ── 後方互換のため（既存の spec_018 deployment で target repo が profile の expect する場所に未チェックアウトでも load できる）。
  - `load_profile_with_source` と `load_profile_registry` の両経路から呼び出し ── 単一 profile / registry sweep の両方で fail-fast を保証。
- **`ProfileError` 例外を追加** (spec_032 §2-1):
  - `ValueError` の subclass ── 既存の `try/except ValueError` 経路は壊さない。
  - spec_032 の path-existence 検証専用の例外型として導入、将来の profile post-load 検証拡張にも使える。
- **`ccd/discover.py:MutmutRunner` に `cwd` / `tests_dir` / `extra_args` パラメータを追加** (spec_032 §2-2):
  - `__init__` シグネチャに 3 つの新 optional kwarg を追加 ── すべて `None` / `[]` がデフォルトで、spec_014/spec_019 の既存挙動を bit-for-bit 維持。
  - `run()` 内で:
    - mutmut の subprocess cwd を `cwd` 指定時は `<clone>/<cwd>` に切り替え（mutmut のネスト構造 workaround：`cd <subdir> && mutmut run --paths-to-mutate <subdir-relative>`）。
    - argv に `--tests-dir <tests_dir>` を `tests_dir` 指定時に挿入。
    - argv の末尾に `extra_args` を逐語追加。
    - `mutmut results` / `mutmut show` も同じ `run_cwd` で起動 ── 一貫した cwd で `.mutmut-cache` を読みに行く。
    - `_collect_killed_mutants_from_cache(run_cwd)` ── cache は mutmut が起動した cwd 配下に書かれるので、cwd 切替時は cache 読み込み元も切り替える。
  - 新規 helper `_build_mutmut_run_argv(*, binary, paths_arg, tests_dir, extra_args)` ── pure function、テスト容易性 + 「argv assembly のロジックを 1 箇所に集中」のため。テスト #2 がこの helper を直接 unit test し、`MutmutRunner.run()` の monkeypatch 経由 integration test もパスする。
- **`ccd/discover.py:run_channel` に `mutation_config` kwarg を追加** (spec_032 §2-2):
  - `MutmutRunner` のデフォルトインスタンス化時に、`mutation_config.cwd` / `.tests_dir` / `.extra_args` を constructor に forward。
  - `mutation_config is None` のとき（CLI 単独起動 / nightly fallback）は MutmutRunner() でレガシー挙動を維持。
- **`ccd/nightly.py:_run_channels` に `mutation_config` kwarg を追加** (spec_032 §2-2):
  - `run_nightly` で `effective_mutation_config(profile.discovery)` を導出、`_run_channels` 経由で mutation channel に forward。
  - 既存の `mutation_paths` 引数は引き続き渡す（call sites の最小 diff のため）。

### Changed

- **`_ai_workspace/profiles/axis-knowledge-rag.toml`** ── spec_032 形式に書き換え:
  - 旧: `[discovery] mutation_paths = ["backend/src/normalizer.py"]`
  - 新: `[discovery.mutation] cwd = "backend"` + `mutation_paths = ["src/normalizer.py"]` + `tests_dir = "tests"` + `extra_args = []`
  - mutmut は iso_clone/backend を cwd として `mutmut run --paths-to-mutate src/normalizer.py --tests-dir tests` を実行する。
- **`_ai_workspace/profiles/ccd.toml`** ── **無変更** (spec §2-4「既存挙動を変えない」証拠 ── レガシー形式が引き続き動作することを構造的に pin)。

### Added (tests)

- **`tests/test_mutation_invocation.py` を新規追加** (16 件、すべて注入ベース):
  - **unit test #1 — profile schema parse** (4 件):
    - 正常系: `[discovery.mutation]` ブロックが `cwd` / `mutation_paths` / `tests_dir` / `extra_args` フィールド付き `MutationConfig` に round-trip。
    - default 値: optional キー（cwd / tests_dir / extra_args）省略時は `None` / `None` / `[]`。
    - レガシー wrapping: `discovery.mutation is None` の profile（spec_018 形式）が `effective_mutation_config` で `MutationConfig` に wrap される。
    - 排他性: top-level `mutation_paths` + `[discovery.mutation]` を同時に書くと load 時に `ValueError` (message に "mutually exclusive")。
  - **unit test #2 — mutmut argv + cwd assembly** (7 件):
    - `_build_mutmut_run_argv` 単体: minimal / `--tests-dir` 追加 / `extra_args` 末尾追加 の 3 件。
    - `MutmutRunner.run()` 経由（subprocess.run monkeypatch + iso-venv stub）: 既定 cwd は clone root / `cwd="backend"` で `<clone>/backend` に切替 / `tests_dir="tests"` で `--tests-dir tests` 挿入 / `extra_args=["--use-coverage"]` で末尾追加 の 4 件。
  - **unit test #3 — profile validation fail-fast** (5 件):
    - 存在しない `cwd` → `ProfileError` (message に "discovery.mutation.cwd directory not found" + フルパス)。
    - 存在しない `mutation_paths` entry → `ProfileError` (message に "mutation_paths entry not found" + フルパス)。
    - 存在しない `tests_dir` → `ProfileError` (message に "discovery.mutation.tests_dir not found" + フルパス)。
    - 複数失敗の集約 → 1 つの `ProfileError` message に cwd / mutation_paths / tests_dir の 3 件すべてが含まれる（spec_031 の 「全部チェックする」流儀を継承）。
    - レガシー形式は存在検証の対象外 → `mutation_paths = ["definitely/nonexistent.py"]` でも load 成功（後方互換）。
- 既存 mutation 系テスト（`tests/test_discover.py` 等）は **無修正で green** ── MutmutRunner の新 kwarg はすべて optional、デフォルト値で spec_014/spec_019 の挙動を bit-for-bit 維持。

### Constraints

- **触ったファイル**: `ccd/profile.py` (MutationConfig + ProfileError + effective_mutation_config + _validate_mutation_config_paths + render_profile 拡張 + DiscoveryConfig mutation field + model_validator) / `ccd/discover.py` (MutmutRunner kwargs + _build_mutmut_run_argv + run_cwd 切替) / `ccd/nightly.py` (effective_mutation_config import + _run_channels mutation_config forward) / `_ai_workspace/profiles/axis-knowledge-rag.toml` / `tests/test_mutation_invocation.py` (新規 16 件) / `tests/test_smoke.py` (version assert) / `CHANGELOG.md` / `pyproject.toml` / `ccd/__init__.py` / `README.md` / `docs/DESIGN.md` (§9.8 欠陥 6 構造修正の節)。
- **触っていないファイル** (spec §3 「触ってはいけない」遵守):
  - `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,adversarial,ai_review,brief,guard,translate,sweep}.py` ── **1 行も touch していない**。
  - `ccd/cli.py` ── 1 行も touch していない（本 spec は CLI サブコマンドを追加しない）。
  - `_ai_workspace/profiles/ccd.toml` ── **1 バイトも touch していない**（既存挙動の証）。
  - `docs/architecture.md` / `docs/data/*.json` ── 触っていない。
  - 既存 mutation 系テスト（`tests/test_discover.py` 等）── 1 行も touch していない。
- **既存挙動不変**:
  - CCD 自身の profile（レガシー `mutation_paths`）は spec_018 と bit-for-bit 同じ挙動。
  - MutmutRunner() （引数なし）も spec_014/spec_019 と同じ挙動 ── cwd は clone root、--tests-dir なし、extra_args なし。
  - レガシー path validation は走らない ── 後方互換完全。
- 安全境界レベル 2 不変 ── ローカル commit のみ、push しない / ブランチ操作・merge しない。

### Verification

- `ruff check .` → All checks passed!
- `pytest -q` → 622 passed (新規 16 件 + 既存 606 件、回帰なし)。
- `python3 -m ccd --version` → `ccd 0.20.0`。
- 構造修正のスコープ: profile から mutmut の実行パラメータを注入できる構造が完成。axis silo での実走確認（mutmut が依然 0 mutants を返すか、それとも mutation を生成するか）は人間（中島）が手動で実施する（spec §6 ── CC のスコープ外、実走で原因切り分け）。

## [0.19.1] — 2026-05-27

spec_031 — **iso-venv install の沈黙失敗を防ぐ post-install 検証**。v0.19.0 で spec_030 が「`mutants_total = 0` の沈黙失敗を HALT として可視化」したが、その後の sweep #3 / #4 で axis-knowledge-rag に対する mutation チャンネルが安定して HALT する実走結果を観察した：

| sweep | mutation_paths | 結果 |
|---|---|---|
| #3 (spec_030 後) | `backend/src/_decay.py` | mutants_total=0 → **HALT に昇格・可視化** |
| #4 (切り分け実験) | `backend/src/normalizer.py`（underscore なし、81 行）| mutants_total=0 → **同様に HALT** |

`normalizer.py`（underscore prefix なし、十分 mutate 可能な NFKC/カナ統一/lowercase 関数群を含む）に切り替えても 0 mutants だった事実から、**原因は mutmut の underscore 慣習スキップではなく、iso-venv 内に対象 repo の package が正しく install されていない**ことが確定。`_provision_iso_venv` の install ステップ（`pip install -e . mutmut pytest`）は exit 0 で返るが、実際には iso-venv 内に必要なパッケージが揃っておらず、mutmut が「test も対象 package も import できないので 0 mutants を返す」状態を生んでいた。pip の exit code に頼った既存の `subprocess.CalledProcessError` catch では捕まらない silent fail。spec_031 は install ステップ完了後に必須要素が iso-venv に存在するか検証する防護層を追加する (patch bump = install validation の堅牢化):

### Added

- **`ccd/discover.py:_provision_iso_venv` に 3 段階 post-install 検証を追加** (spec_031 §2-1):
  - 既存の `subprocess.run(install_args, check=True, ...)` 完了直後に `_validate_iso_venv_post_install` を呼び出し、3 つのチェックを **すべて** 実行 (spec_021 ガード 5 と同じ「全部チェックする」流儀 ── 最初の失敗で打ち切らない):
    1. **`mutmut` バイナリ存在チェック** ── `iso_venv_bin / "mutmut"` が `is_file()` を満たすか。
    2. **`pytest` バイナリ存在チェック** ── `iso_venv_bin / "pytest"` が `is_file()` を満たすか。
    3. **対象 repo の dist 名の importability チェック** ── `_extract_pyproject_project_name(workspace)` で `pyproject.toml` の `[project] name` を取得、iso-venv 内の `python -c "from importlib.metadata import version, PackageNotFoundError; ..."` 経由で `importlib.metadata.version(dist_name)` を呼び、`PackageNotFoundError` を捕まえる。
  - 失敗したチェックを集めて 1 回の `IsoVenvProvisioningError` で raise: message は `"post-install validation failed:\n  - ..."` 形式で全失敗を bullet 列挙。
  - halt_reason に具体名 (不在バイナリの絶対パス / 不在 dist 名 / importlib.metadata の stderr) が出る ── 朝レポート §D で操作者が「具体的に何が install できなかったか」を 1 行で読める。
  - `dist_name` が `None` (pyproject.toml が無い / `[project]` テーブルが無い / `[project] name` が無い / TOML malformed) の場合は **package チェックをスキップ** ── バイナリチェックだけ走る。古典 setuptools 等の repo を排除しない (過剰実装の回避)。
  - `subprocess.run` の例外ハンドリング: `FileNotFoundError` (iso-python 自体が無い) / `subprocess.TimeoutExpired` (probe がハング) も errors リストに追記。
  - probe の stderr が長い場合は `_POST_INSTALL_STDERR_MAX = 2048` バイトで truncate (朝レポート §D が読みづらくなるのを防ぐ、`...[truncated]` suffix を付与)。
  - importlib.metadata probe には独自 `try / except PackageNotFoundError: raise SystemExit(f"PackageNotFoundError: {exc}")` の Python ワンライナーを `-c` 経由で実行 ── これにより package の `__init__` が走らず、target package の optional dependency 未充足 (chromadb / streamlit / 等) で false-fail しない。**dist の visibility だけ** をチェックする最小契約。
- **`ccd/discover.py:_extract_pyproject_project_name` ヘルパを追加** (spec_031 §2-2):
  - `<workspace>/pyproject.toml` の `[project] name` を `tomllib.loads` で取得して返す。
  - missing pyproject / `OSError` / `TOMLDecodeError` / `[project]` テーブル無し / `name` 不在 / 空文字列 → **silent fallback to `None`** (古典 setuptools 等の repo を排除しないため)。
  - `tomllib` は Python 3.11+ 標準ライブラリ ── CCD は 3.11+ 必須 (pyproject.toml の `requires-python`) なので追加依存なし。

### Added (tests)

- **`tests/test_discover.py` に spec_031 セクション追加** (11 件、すべて注入ベース):
  - `_extract_pyproject_project_name` 4 件:
    - 正常系: `[project] name = "ccd-knowledge-rag"` → `"ccd-knowledge-rag"` を返す。
    - pyproject 不在 → `None`。
    - `[project]` テーブル無し → `None`。
    - TOML malformed → `None` (silent fallback)。
  - `_provision_iso_venv` post-install 検証 7 件:
    - **正常系**: mutmut + pytest + package すべて揃っていれば `iso_venv_bin` を return ── これまでの healthy case の挙動は不変。
    - **異常系 (a)**: mutmut バイナリ欠如 → message に `"mutmut binary not found"` を含む `IsoVenvProvisioningError`。pytest と package は green、message に他文言が混入しないことも assert (検証ロジックの精度 pin)。
    - **異常系 (b)**: pytest バイナリ欠如 → message に `"pytest binary not found"` を含む `IsoVenvProvisioningError`。
    - **異常系 (c)**: package import 失敗 → message に `"package 'ccd-knowledge-rag' not importable"` + stderr の `"PackageNotFoundError"` snippet が含まれる。
    - **複数失敗の集約**: mutmut + pytest 両方欠如 + package 不在の 3 重失敗で、3 件すべてが 1 つの例外 message に列挙される。message header `"post-install validation failed"` の出現は 1 回だけ ── 「最初の失敗で打ち切らない / 全部集めて 1 回 raise」原則の構造的 pin。
    - **pyproject `[project] name` 不在のスキップ**: バイナリだけ揃っていれば package チェックをスキップ、probe が呼ばれた形跡なし (`probe_was_called["hit"] is False`) を直接 assert。
    - **pyproject 不在のスキップ**: 同上 ── pyproject.toml そのものが無い repo でも、バイナリだけ揃っていれば provisioning は通過。
  - すべて `subprocess.run` を monkeypatch して fake iso-venv を tmp_path 配下に build する方式 ── 実 `python -m venv` / 実 pip / 実 importlib lookup は走らせない (spec_026 §3 / spec_028 §2-4 / spec_029 §2-5 が確立した「dispatch 内に実走検証を入れない」原則を継承)。
- **既存 `test_provision_iso_venv_creates_clone_local_python` (integration-style 実 venv 起動テスト) は不変** ── pip が使える環境では正常系として通る (本 spec の挙動変更は healthy case を素通りさせる設計のため)、不可な環境では既存通り `pytest.skip` する。

### Constraints

- **触ったファイル**: `ccd/discover.py` (`_provision_iso_venv` 拡張 + `_validate_iso_venv_post_install` + `_extract_pyproject_project_name` + `tomllib` import + 定数 `_POST_INSTALL_STDERR_MAX`) / `tests/test_discover.py` (8 件の新規テスト + import 追加) / `tests/test_smoke.py` (version assert) / `CHANGELOG.md` / `pyproject.toml` / `ccd/__init__.py`。
- **触っていないファイル** (spec §3 「触ってはいけない」遵守):
  - `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,adversarial,ai_review,brief,profile,guard,translate,nightly,sweep}.py` のコアロジック ── **1 行も touch していない**。
  - `ccd/cli.py` / `docs/` / `docs/data/*.json` ── 触っていない。
  - **spec_030 で入れた HALT 経路** (`run_discovery` の 0-mutants HALT / `brief` の §D / `sweep` の skip 経路) は **1 行も touch していない** ── spec_031 はその上流 (`_provision_iso_venv`) を厳しくするだけ。
- **既存挙動不変**: 正常な iso-venv (CCD 自身の sweep) は新検証を通過し、これまでと同じ `iso_venv_bin` を返す。挙動の違いは「install 沈黙失敗時に `IsoVenvProvisioningError` が raise されるか黙って 0 mutants を返すか」だけ。
- 安全境界レベル 2 不変 ── ローカル commit のみ、push しない / ブランチ操作・merge しない。

### Verification

- `ruff check .` → All checks passed!
- `pytest -q` → 606 passed (新規 11 件 + 既存 595 件)。
- `python3 -m ccd --version` → `ccd 0.19.1`。

## [0.19.0] — 2026-05-27

spec_030 — **Phase 2.5 実走で発覚した 2 つの「沈黙失敗」の構造修正**。v0.18.0 で複数施策の巡回 (`ccd nightly-all`) が点火した直後、CCD 自身 + axis-knowledge-rag の 2 施策で sweep を回したところ、ふたつの欠陥が顕在化した:

1. **adversarial が CCD のパーサに固定**：axis-knowledge-rag プロファイルから adversarial channel を起動したのに、対象パーサが CCD ハードコード（`ccd.protocol.parse_spec` / etc.）に走り、`UnicodeDecodeError` 8 件の発見が axis のレポート silo に書かれた ── 完全な誤検出。
2. **`mutants_total = 0` の沈黙失敗**：axis-knowledge-rag の `_decay.py` (61 行、mutate 可能な式多数) の mutation channel が **0 mutants** を返した。実態は iso-venv の `pip install` が axis の重い依存（chromadb / google-generativeai / streamlit）の install に失敗していた可能性が高いが、`IsoVenvProvisioningError` は raise されず、spec_019 のカナリア (`mutants_total ≥ threshold && killed == 0`) は **`mutants_total = 0` を素通り**する設計。

両方とも v2 の「**正直な計測**」原則（`docs/DESIGN.md §9.4 / §9.6` ── 「観測できていない失敗を推測で埋めない」「無意味なレポートを出さず停止する」）の穴。任意 repo に向けたときの沈黙失敗を防ぐ補強を 1 spec で扱う (minor bump = 新機能 + 沈黙失敗の可視化):

### Added

- **`ccd/profile.py` に `AdversarialConfig` / `ParserTarget` を追加** (spec_030 §2-1):
  - 新規 `ParserTarget` (pydantic `BaseModel`, `extra="forbid"`):
    - `import: str` (TOML 上は `import`、Python 上は `import_` で alias)。完全修飾名相当の正規表現 (`module(.sub)*.attr` ── 英数字 + `.` + `_`、先頭非数字、ドット 1 つ以上) でバリデート。シェルインジェクションやパス区切りタイポを load 時点で reject。
    - `input_kind: Literal["path", "bytes", "str"] = "path"`。spec_015 の `_Parser.fn(Path) → object` 契約をデフォルト維持。`"bytes"` / `"str"` は fixture の bytes/str を直接渡す形に wrap。
  - 新規 `AdversarialConfig` (pydantic `BaseModel`, `extra="forbid"`):
    - `parsers: list[ParserTarget]`。**空リストは reject** (typo であって意図ではない、disable したいなら `channels` から `"adversarial"` を外す)。
  - `DiscoveryConfig` に新規 `adversarial: AdversarialConfig | None = None` フィールド。`None` は「未設定」（`[discovery.adversarial]` が TOML に無い）。`[]` 空リストは validator で reject。
  - 既存 `KNOWN_CHANNELS` / `mutation_paths` / `channels` / `schedule` 等は **1 行も touch していない** ── spec §3 「コアロジック不変」遵守。
  - `render_profile` を adversarial 出力に対応 ── `[[discovery.adversarial.parsers]]` 行を round-trippable に emit。
  - 新規定数 `_ADVERSARIAL_IMPORT_RE` ── 完全修飾名の正規表現を 1 箇所に集中。
- **`ccd/discover.py:run_discovery` に 0-mutants HALT 分岐を追加** (spec_030 §2-2):
  - 既存の `outcome.error` 分岐の **直後**に新規 HALT 分岐:
    - 条件: `outcome.error is None` **かつ** `not outcome.mutants` **かつ** `target_paths` 非空。
    - `DiscoveryResult(success=False, ..., halt_reason="mutation setup likely failed: 0 mutants generated for non-empty targets ...")` を返す ── 報告ファイル (md/json) **未作成** (既存 `outcome.error` 経路と同じ扱い)。
    - halt_reason には 4 つの可能性 (iso-venv 依存 install エラー / mutmut path 不一致 / test discovery 失敗 / genuinely trivial Python file) を文面に列挙、最後に「`profile.mutation_paths` で抑制したい場合の opt-out なし — YAGNI、必要になったら追加」と記述。
  - 既存 spec_019 カナリア (`_detect_broken_mutation_setup`、`mutants_total ≥ threshold && killed == 0`) は **不変で並列稼働**。両者は別バリアントを埋める ── 0 mutants HALT と spec_019 カナリアを並列に持つことで「mutmut が走らなかった」「mutmut は走ったが mutation を見ていない」の両方を捕まえる。
  - **偽陽性は許す・偽陰性は許さない**：`__init__.py` 等の trivial ファイルで誤 HALT が出る可能性は受容。opt-out (profile.suppress_zero_mutants 等) は本 spec では入れない (YAGNI、本当に必要になってから)。
- **`ccd/adversarial.py` に `resolve_parser_targets` を追加** (spec_030 §2-3):
  - 既存 `run_adversarial(parsers=None)` シグネチャは **不変** ── `parsers=None` のとき `default_parsers()` を使うフォールバック挙動も bit-for-bit 維持 (spec_015 single-CLI invocation 後方互換)。
  - 新規 `resolve_parser_targets(targets: Iterable[ParserTarget]) -> tuple[_Parser, ...]`:
    - 各 `ParserTarget.import` を `importlib.import_module` + `getattr` で解決。
    - `input_kind` に応じて `_Parser.fn` を wrap (`"path"` → そのまま / `"bytes"` → `read_bytes` / `"str"` → `read_text(errors="replace")`)。
    - 解決失敗 (`ImportError` / `AttributeError` / non-callable) は **`ValueError` で loud に raise** ── silently `default_parsers` にフォールバックしない (沈黙失敗の防止が本 spec の主旨)。
  - 内部ヘルパー `_resolve_dotted_name(dotted: str) -> Callable` ── 最後のドットで `module_name` と `attr_name` を分割、`importlib.import_module(module_name)` + `getattr(mod, attr_name)`、callable チェック。
  - 既存 `default_parsers()` の docstring を更新 ── 「**単体実行 CLI fallback**」と明示、sweep 経路では fallback を使わない旨を明記。
  - `__all__` に `resolve_parser_targets` を追加。
- **`ccd/discover.py:run_channel` に `adversarial_parsers` 引数を追加** (spec_030 §2-3):
  - 新規キーワード引数 `adversarial_parsers: Any = None` (`tuple[_Parser, ...] | None` のダックタイプ)。
  - adversarial channel 経路で `adversarial_parsers is not None` のときに `run_adversarial(parsers=adversarial_parsers, ...)` に forward。
  - `adversarial_parsers=None` (single-CLI / 既存 `ccd discover --channel adversarial`) のときは `run_adversarial(parsers=None)` で `default_parsers()` にフォールバック ── spec_015 既存テストは **無修正で green**。
- **`ccd/nightly.py:run_nightly` に `adversarial_parsers` / `channel_skips` 引数を追加** (spec_030 §2-3):
  - `adversarial_parsers: Any = None`: sweep 経路で profile-driven parsers を渡すための seam。`_run_channels` 経由で `run_channel(channel="adversarial", adversarial_parsers=...)` に forward。
  - `channel_skips: dict[str, str] | None = None`: sweep が施策のチャンネルを「実行せずに skip」する旨と理由を通知する seam。skip対象は `effective_profile.discovery.channels` から除外して `_run_channels` を呼び、skip 後に **synthetic `ChannelOutcome`** (`success=False`, `halt_reason=<理由>`) を append ── 朝レポート §D が verbatim に surface する。
  - `_run_channels` シグネチャに `adversarial_parsers: Any = None` を追加、adversarial channel のときだけ `kwargs["adversarial_parsers"] = adversarial_parsers` を入れる構造。
  - `run_brief_fn(...)` 呼び出しに `channel_outcomes=tuple(channel_outcomes)` を追加。
  - **`_run_auto_fix_loop` / `_run_propose_loop` の dispatch / R5 / R4 / guard / merge / patch save 本体は 1 行も touch していない** (spec §3 遵守)。
- **`ccd/sweep.py:_process_policy` に adversarial routing を追加** (spec_030 §2-3):
  - `fallback_mode=False` (genuine registry sweep) かつ profile.discovery.channels に `"adversarial"` を含む施策について:
    - `profile.discovery.adversarial is None` → `channel_skips["adversarial"] = "adversarial channel skipped: profile に [discovery.adversarial.parsers] が未設定..."` を立てて `run_nightly` に forward。CCD のハードコードパーサは **走らせない** (Phase 2.5 誤検出の構造的解決)。
    - `profile.discovery.adversarial is not None` → `resolve_parser_targets(...)` で解決して `adversarial_parsers` に forward。解決失敗 (`ValueError`) も skip 扱い (silently default にしない)。
  - `fallback_mode=True` (`profiles/` ディレクトリ無し、legacy `ccd_profile.toml`) では skip / 注入のいずれも行わず、spec_015 既存挙動を bit-for-bit 維持 (デフォルトのフォールバックパーサが走る)。
  - 既存 `discover_dir` / `brief_dir` / `proposal_dir` のパスリダイレクト・失敗隔離 (论点4)・横断インデックス出力は **不変**。
- **`ccd/sweep.py` の `_summarize_nightly` に HALT カウント追加** (spec_030 §2-4):
  - `result.channels_run` 内の `success=False && halt_reason` 数をカウント、`f"... — HALT {count} 件 (§D 参照)"` を suffix として append。
  - 横断インデックスの 1 行サマリで沈黙失敗が見えるようになる (Phase 2.5 misfire を index 段階で捕捉)。
- **`ccd/brief.py` に `channel_outcomes` 引数を追加 + §A / §D を拡張** (spec_030 §2-4):
  - `run_brief(..., channel_outcomes: Sequence[ChannelOutcome] | None = None)` ── nightly orchestrator が channel halts / skips を渡す seam。
  - `_render_section_a` に新引数 `channel_halt_count: int = 0`、`channel_halt_count > 0` のとき `f"**HALT {count} 件** (§D 参照)"` を §A の bullet 列に append (沈黙失敗を front page で surface)。
  - `_render_section_d` に新引数 `channel_outcomes: Sequence[ChannelOutcome] | None = None`:
    - `channel_outcomes` の `success=False && halt_reason` を channel 名でインデックス化。
    - `summary.channels_missing` 内の channel について `channel_outcomes` に対応する halt_reason があれば「**<ラベル>** halt: <reason>」を出力 (mutation 0-mutants HALT / adversarial skip がここ)。なければ既存の「discover_NNN.json が見つからなかった (未実行)」フォールバック。
    - 未マッチの outcomes (familiar でない channel) も最後に surface ── silently 落とさない (`docs/DESIGN.md §9.4`「正直な計測」)。
  - 既存 §B / §C / §E / §F は **1 行も touch していない** (spec §3 「render 系コアロジック」を最小変更で達成)。
- **`_ai_workspace/profiles/ccd.toml` に `[[discovery.adversarial.parsers]]` を明示** (spec_030 §2-5):
  - CCD 既定の 4 パーサ (`ccd.protocol.parse_spec` / `parse_result` / `ccd.run_writer.load_records` / `reconcile_run_file`) を `input_kind = "path"` で `[discovery.adversarial.parsers]` 配列に明示。
  - これによりハードコードリストは「**`ccd/adversarial.py:default_parsers` (= 単体実行 fallback)**」の 1 箇所のみに集中、運用設定とコードの責務が分離。
  - `_ai_workspace/profiles/axis-knowledge-rag.toml` は本 spec の作業範囲外 (中島さんが運用で `[discovery.adversarial.parsers]` を書くかは別判断、書かなければ skip される現状で問題なし、spec §2-5 後段)。

### Added (tests)

- **`tests/test_profile.py` に AdversarialConfig / ParserTarget の検証テスト (+10 件)** ── defaults / TOML round-trip / 空リスト reject / 不正 `input_kind` reject / 不正 `import` 文字列 reject (3 バリエーション) / `mutation_paths` 不変 pin。
- **`tests/test_discover.py` に 0-mutants HALT の検証テスト (+4 件 + 既存 3 件の更新)** ── `outcome.error` 経路との分離 / killed mutant で HALT 不発 / spec_019 カナリアと並列稼働 (文言混在なし) / 既存 `test_zero_mutants_is_graceful` を `test_zero_mutants_is_halt_for_non_empty_targets` に rename して spec_030 文言を assert。
- **`tests/test_adversarial.py` に `resolve_parser_targets` の検証テスト (+8 件)** ── ドット名解決 / `input_kind` 3 種の wrap (`bytes` / `str` は `tests/_adversarial_targets_for_test.py` をターゲット利用) / 解決失敗 3 種で `ValueError` / `run_adversarial` の injection vs fallback。
- **`tests/_adversarial_targets_for_test.py` を新規追加** ── テスト用解決ターゲット。
- **`tests/test_ai_review.py` の `test_cli_discover_default_channel_remains_mutation` / `_channel_mutation_explicit_still_works` を mutant 投入に更新** (spec_030 HALT 不発)。
- **`tests/test_sweep.py` に sweep adversarial routing テスト (+4 件)** ── 未設定 → skip / 設定済 → parsers 注入 / 解決失敗 → skip 文言 / fallback_mode は変化なし (spec_015 carry)。
- **`tests/test_brief.py` に §D / §A surfacing テスト (+5 件 + `_extract_section` ヘルパー)** ── mutation 0-mutants HALT / adversarial skip が §D に halt 文言で出る / §A の HALT カウント / no-halts では §A noise なし / `channel_outcomes` 省略時は既存挙動。
- **`tests/test_smoke.py` の `test_version_is_0180` → `test_version_is_0190`**、assert を `"0.19.0"` に追従。

### Constraints

- **`ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,ai_review,translate,guard}.py` のコアロジックは 1 行も touch していない** (spec §3 「触ってはいけない」逐語遵守)。
- **`ccd/nightly.py` の変更は計 4 箇所のみ** ── (a) `run_nightly` シグネチャに 2 引数追加 (default `None`)、(b) `_run_channels` の adversarial channel 経路に kwarg 追加、(c) `run_brief_fn` 呼び出しに `channel_outcomes=` 追加、(d) skip channel 分岐。**`_run_auto_fix_loop` / `_run_propose_loop` の dispatch / R5 / R4 / guard / merge / patch save 本体は不変**。
- 自律修正ループ (`nightly.py`)・インチキ修正ガード (`guard.py`)・翻訳 (`translate.py`)・提案モード (spec_028)・複数施策の巡回 (spec_029) の本体ロジックは不変。本 spec は発見チャンネル層への追加と HALT 経路の補強のみ。
- **`docs/` / `docs/data/*.json` は触っていない** (Phase 3 全体の `docs/DESIGN.md` 更新は別 spec の責務、spec_027 / spec_028 / spec_029 result と同じ精神)。
- **`ccd discover --channel adversarial` 単体実行は不変** ── プロファイル context 無しは `default_parsers()` フォールバック (spec_015 既存テストは無修正で green)。
- **既存の単一プロファイル運用も不変** ── `profiles/` 無しの legacy single-profile fallback で adversarial が `default_parsers()` を使う挙動は bit-for-bit 維持。
- 安全境界レベル 2 は不変。
- ローカル commit のみ、push しない／ブランチ操作・merge しない。

### Verification

- ``ruff check .`` → ``All checks passed!``
- ``pytest -q`` → ``595 passed in 19.33s`` (spec_013〜029 既存 564 件 + spec_030 新規 +31 件 ── 全件 green)。
- ``python3 -m ccd --version`` → ``ccd 0.19.0``。

## [0.18.0] — 2026-05-26

spec_029 — **v2 Phase 3 の 3 本目の spec**。spec_028 で **提案モード (`fix_mode="propose"`)** が入り、CCD のモードが3つ（`auto` / `propose` / `off`）になった。これでクライアント施策の repo に向けられる中身は揃った ── が、CCD は依然 **1 施策 (1 repo) しか相手にできない**（プロファイルは `_ai_workspace/ccd_profile.toml` 1枚、`ccd nightly` は `--repo` を1つ取るだけ）。施策が増えると「タスクを N 個手で登録」になってしまう。

spec_029 は **複数施策の巡回運用** の仕組みを入れる（minor bump = 新機能）:

- 施策ごとに1プロファイルを置けるレジストリ (`_ai_workspace/profiles/`)。
- 全施策を順に回す新サブコマンド **`ccd nightly-all`**（12個目）。
- 1施策の失敗が他施策を止めない隔離（论点4）。
- 施策横断のインデックス（週1、まずこれを見る、`docs/DESIGN.md §9.6` "既定は簡潔" の延長）。

運用イメージ：CCD 自身 (`fix_mode=auto`) ＋サムライ施策 (`fix_mode=propose`) ＋トラベルメール (`fix_mode=propose`) の3プロファイルを `_ai_workspace/profiles/` に置く → 週次タスクが `ccd nightly-all` を1回叩く → 3施策を順に回し、CCD は自分を自律修正し、クライアント施策には修正案を出す → 朝、中島さんが横断インデックスを見て各施策の詳細を確認する。

これで v2 の「夜間自律保守ループ」が**任意の repo にプロファイルで横展開できる**状態になる（`docs/DESIGN.md §9.7` 论点1 のティア概念の完成形）。

### Added

- **`ccd/profile.py` にプロファイルレジストリのローダを追加** (spec_029 §2-1):
  - `PROFILES_DIR_REL = Path("_ai_workspace") / "profiles"` 定数（`DEFAULT_PROFILE_REL` の隣）。
  - 新規 `_POLICY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")` ── ファイル名（＝施策名）のバリデーション。ディレクトリ名・パスの一部になるので英数・ハイフン・アンダースコアのみ許可、それ以外を含むファイルは `ValueError` で顕在化（黙って無視しない）。
  - 新規 `@dataclass(frozen=True) class PolicyEntry` ── `name: str` / `profile: Profile` / `source: Path | None`。施策名はファイル名 stem、profile TOML に `name` フィールドは**足さない**（ファイル名と中身のズレ事故を避ける、spec §2-1 逐語遵守）。
  - 新規 `load_profile_registry(repo, profiles_dir=None) -> list[PolicyEntry]`：
    - `profiles/` ディレクトリが**在ればそれを使う**（`*.toml` 全部を読み、`PolicyEntry` の list を施策名アルファベット順で返す）。
    - **無ければ従来どおり単一 `ccd_profile.toml` を 1 施策（施策名 `ccd`）として扱う**（spec §2-1 後方互換）── `load_profile_with_source` に委譲して既存挙動 bit-for-bit carry。
    - 空ディレクトリは**空 registry**（fallback ではない）── 移行途中のオペレータが空 `profiles/` を作って様子見できる。`profiles/` が在るのに silently fallback する怪しい挙動は避ける。
    - TOML parse / pydantic schema 違反は `ValueError` でファイルパス込みで surface（`load_profile` と同じうるさい契約）。
  - 既存 `load_profile` / `load_profile_with_source`（単一）は**不変** ── `ccd nightly`（単一施策）と `ccd profile` がそのまま使う（spec §2-1 「不変で残す」遵守）。
  - `DEFAULT_FALLBACK_POLICY_NAME = "ccd"` 定数を export（テスト + 横断インデックスが参照）。
- **`ccd/sweep.py` を新規追加（巡回ロジック）** (spec_029 §2-2 / §2-3):
  - 新規モジュール ── `ccd nightly-all` の中身が肥大しないよう `ccd/nightly.py` に足さず別ファイル（CC 判断、spec §6 「`ccd/sweep.py` 等にするかは CC 判断」）。
  - 新規 `@dataclass(frozen=True) class PolicyOutcome` ── 施策 1 件分の結果（`name` / `success` / `error` / `result: NightlyResult | None` / `report_path` / `source`）。例外で死んだ施策と内部 halt した施策を区別。
  - 新規 `@dataclass class SweepResult` ── sweep 全体（`success` / `today` / `policies: list[PolicyOutcome]` / `index_path`）。`success` は全施策を**試行し切ったか**で決まる（個々の失敗では flip しない、论点4）。
  - 新規 `run_nightly_all(*, repo, profiles_dir=None, today=None, nightly_runner=None, **nightly_kwargs)`：
    - レジストリを読んで施策を**直列で**処理（spec §2-2 「処理順は直列」逐語遵守）。
    - 各施策について `discover_dir` / `brief_dir` / `proposal_dir` を **CCD 側の施策名サブディレクトリ**に組み立てて `run_nightly` に渡す → クライアント施策 (`propose`/`off`) の repo には 1 バイトも書かない（spec §2-3 论点3 / プライバシー隔離）。
    - **失敗隔離**：施策 N が例外を投げたら `PolicyOutcome(success=False, error=...)` に記録して `N+1` 以降を続行（spec §2-2 论点4 「1 施策の事故が他施策を止めない」逐語遵守）。
    - 全施策を試行し終えたら横断インデックスを書く（後述）。
  - 新規 `render_index(*, today, policies, fallback_mode=False, ccd_repo=None) -> str`：施策ごと 1 行のサマリ＋詳細リンクの Markdown レンダラ。`merged` / `proposed` / `HALT` / `skipped` / `失敗` / `PAUSE` / `発見のみ` の 7 状態を 1 行で区別。**詳細レポートの再レンダリングはしない** ── 目次＋一言だけ、「既定は簡潔」をインデックスでも守る (`docs/DESIGN.md §9.6`)。
  - `NightlyRunner = Callable[..., NightlyResult]` 型 ── テストが fake nightly を注入できる seam（実 `run_nightly` を呼ばずに巡回構造だけ exercise）。
- **`ccd/nightly.py` の `run_nightly` に出力先 override を 3 つ追加** (spec_029 §2-3):
  - `discover_dir: Path | None = None` ── `_run_channels` 経由で `run_channel` に forward。
  - `brief_dir: Path | None = None` ── `run_brief_fn` に forward（`run_brief` は既に spec_017 から `brief_dir` を受け取る、API 不変）。
  - `proposal_dir: Path | None = None` ── `_run_propose_loop` 経由で `_save_proposal_patch` に forward。
  - 既存 default の `None` は **flat layout 維持**（spec_020 の `<repo>/_ai_workspace/{discover,nightly}/` をそのまま使う）── 単一施策で `ccd nightly` を直接叩く既存運用は bit-for-bit 不変。
  - sweep が指定するときだけ CCD 側のサブディレクトリ（`<ccd_repo>/_ai_workspace/{discover,nightly}/<施策名>/`）に向く。
- **`ccd nightly-all` サブコマンド (12 個目)** ── `ccd/cli.py`:
  - `--repo`（既定 cwd） ── CCD の作業ディレクトリ（`profiles/` を読み、全成果物をここに書く）。
  - `--profiles-dir`（既定 `<repo>/_ai_workspace/profiles/`） ── テスト用 override。
  - `main()` に `nightly_runner: Any | None = None` seam を追加 ── テストが fake sweep runner を注入可能。
  - stdout: `policies processed: N` ＋ `  - <name>: ok|failed (<reason>)` を施策ごと 1 行 ＋ 最後に `cross-policy index: <path>`。
  - 失敗した施策があっても CLI exit code は 0（sweep itself は「全施策を試行し切った」で正常終了、spec §2-2 「`nightly-all` 自体は全施策を試行し切ったら正常終了」逐語遵守）。
  - **既存サブコマンド (`ccd nightly` / `ccd profile`) は不変** ── spec §3 遵守、test_cli_nightly_all_keeps_nightly_subcommand_unchanged で構造的に pin（サブコマンド総数 11 → 12）。
- **`_ai_workspace/register_nightly.ps1` を `ccd nightly-all` 呼び出しに変更** (spec §2-4):
  - 旧 `ccd nightly --repo $ProjectDir` → 新 `ccd nightly-all --repo $ProjectDir`。
  - 冒頭コメントを「複数施策の巡回運用」用に追記更新（spec_029 の追加経緯 + `profiles/` が無ければ単一プロファイルに自動 fallback する旨を明記）。
  - タスク名 `CcdNightlyMaintenance` ・ 週次トリガー (spec_027) ・ WakeToRun / StartWhenAvailable / MultipleInstances IgnoreNew / ExecutionTimeLimit 6h は不変（既登録タスクとの不整合を避ける、spec_027 のポリシーをそのまま継承）。

### Added (tests)

- **`tests/test_sweep.py` (+23 件、新規ファイル)** ── 全テスト注入ベース（実 mutmut / claude / git を動かさない、spec §2-5 / §6）:
  - レジストリ:
    - `test_registry_reads_every_toml_in_profiles_dir` ── 複数 `.toml` を置いて全施策が名前つきで読める、ソート順
    - `test_registry_falls_back_to_single_profile_when_dir_missing` ── `profiles/` 無し → 単一 `ccd_profile.toml` で 1 施策（施策名 `ccd`）
    - `test_registry_fallback_returns_all_defaults_when_no_legacy_profile` ── `profiles/` も `ccd_profile.toml` も無し → fallback は 1 施策（全 default）
    - `test_registry_empty_directory_yields_empty_registry` ── `profiles/` 有り＋空 → 空 registry（fallback ではない）
    - `test_registry_rejects_invalid_policy_name` ── 不正文字のファイル名で `ValueError`
    - `test_registry_rejects_malformed_toml` ── TOML parse error
    - `test_registry_rejects_schema_violation` ── pydantic schema error
    - `test_registry_load_profile_unchanged_by_spec_029` ── 既存 `load_profile`（単一）の挙動 bit-for-bit 不変
  - sweep 巡回 + 失敗隔離（注入ベース）:
    - `test_sweep_runs_every_policy_in_order` ── 複数施策を順に処理
    - `test_sweep_redirects_each_policy_outputs_under_ccd_workspace` ── per-policy `discover_dir` / `brief_dir` / `proposal_dir` が CCD 側の施策名サブディレクトリに向く
    - `test_sweep_fallback_preserves_legacy_flat_paths` ── 単一プロファイル fallback では path overrides が `None`（spec_020 flat 維持）
    - `test_sweep_isolates_per_policy_failure_and_continues` ── 施策 N が raise しても N+1 以降が走り、sweep 自体は success=True
    - `test_sweep_records_internal_halt_as_failure` ── `run_nightly` が `success=False` で返ったケースも記録（次施策続行）
  - 横断インデックス:
    - `test_sweep_writes_cross_policy_index` ── `index_YYYY-MM-DD.md` に施策ごと 1 行サマリ + 相対リンク
    - `test_sweep_index_marks_failed_policies` ── 失敗施策が「**失敗**」で目立つ
    - `test_sweep_index_fallback_mode_carries_marker` ── 単一プロファイル運用時のインデックスに移行ヒント
    - `test_index_empty_registry_renders_meaningful_message` ── 0 施策でも index 出力（「処理対象なし」）
    - `test_render_index_summarises_each_outcome_kind` ── 5 種の outcome（merged / proposed / HALT / 発見のみ / 失敗）の 1 行サマリを直接 pin
  - CLI:
    - `test_cli_nightly_all_invokes_sweep` ── `ccd nightly-all --repo <path>` で stdout に施策別行 + index path
    - `test_cli_nightly_all_surfaces_failure_lines` ── 失敗施策が `<name>: failed (...)` で出るが exit 0
    - `test_cli_nightly_all_registry_error_exits_nonzero` ── レジストリ・レベルのエラーは exit 1
    - `test_cli_nightly_all_keeps_nightly_subcommand_unchanged` ── サブコマンド総数 11 → 12、`ccd nightly` も従来どおり
  - プライバシー隔離:
    - `test_sweep_does_not_write_to_target_repo_for_propose_off` ── `propose`/`off` 施策の target repo に書き込みが発生しないことを構造的に pin（全 output dir が CCD 側の `_ai_workspace/` に landing する）
- **`tests/test_smoke.py::test_version_is_0170` → `test_version_is_0180`**、assert を `"0.18.0"` に。

### Constraints (spec §3)

- **触ってよい**: `ccd/profile.py`（レジストリローダ追加）/ `ccd/sweep.py`（新規、巡回ロジック）/ `ccd/nightly.py`（出力先 override 3 引数を追加）/ `ccd/cli.py`（`nightly-all` サブコマンド）/ `_ai_workspace/register_nightly.ps1` / `tests/` / `CHANGELOG.md` / `pyproject.toml` / `ccd/__init__.py` ── すべて遵守。
- **触ってはいけない（コアロジック）**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,translate,guard,brief}.py` ── すべて遵守（**1 行も touch していない**）。
  - 特に **`ccd/brief.py` 不変** ── 横断インデックスは `ccd/sweep.py` で構築（CC 判断、spec §2-3「`ccd/brief.py` ＋ 巡回ロジック」のうち巡回ロジック側にまとめた、`brief.py` の朝レポートそのものは touch する必要が無い構造）。
  - 特に **`ccd/translate.py` / `ccd/guard.py` / `ccd/discover.py` / `ccd/adversarial.py` 不変** ── 発見・翻訳・ガード・提案/自律ループの中身は spec_028 までのものをそのまま使う（spec §3 「`nightly-all` は中身を呼ぶだけ」逐語遵守）。
- 既存サブコマンド・既存挙動は不変。`ccd nightly`（単一施策）・`ccd profile` は API 不変、`run_nightly`（単一）の path override 3 引数は `None` default で既存挙動 bit-for-bit carry。spec_013〜028 の既存テスト（541 件）はすべて不変で green。
- 安全境界レベル 2 は不変 ── `propose`/`off` 施策は対象 repo に書き込まない（構造的に：sweep が `discover_dir` / `brief_dir` / `proposal_dir` を CCD 側に固定）、`auto` も push しない（GitOps Protocol に push 系メソッドが無い構造も不変）。
- ローカル commit のみ、**push しない／ブランチ操作・merge しない**（spec §3 逐語遵守）。

### Verification

- **`pytest -q`**: 全テスト green（spec_028 時点の 541 件 + 新規 23 件 - smoke version test 1 件 rename + assert string 更新 = 計 **564 件**）。
- **`ruff check .`**: `All checks passed!`。
- **`python3 -m ccd --version`** → `ccd 0.18.0`（smoke の subprocess テストが確認）。
- **`python3 -m ccd nightly-all --help`** → 新サブコマンドの help が出る。


## [0.17.0] — 2026-05-25

spec_028 — **v2 Phase 3 の 2 本目の spec**。Phase 2（spec_021〜026）で自律修正ループが完成し、spec_027 で **週次ケイデンス**が入った後、Phase 3 の 2 本目として **提案モード（`fix_mode="propose"`）** を導入する（minor bump = 新機能）。

経緯: Phase 2 完成版の挙動は「発見 → 修正 → ガード → 検証 → **ローカル merge**」。これは CCD 自身に対しては妥当だが、**クライアント施策の repo に勝手に merge させるのは強すぎる**。一方で「問題を見つけて報告するだけ」では物足りない ── 「ここが問題、こう直すといい」という**修正案まで**出してほしい（中島さんの希望）。そこで **3 つ目のモード「提案（propose）」** を入れる ── 隔離クローン内で修正案を生成し、R5/R4/ガードを通したうえで朝レポートに **diff + `git apply` ワンライナー + パッチファイル**を載せる。**適用はしない**（merge / commit / push のいずれもしない）。

これで CCD のモードは 3 つ:

| モード | 挙動 | 想定ティア |
|---|---|---|
| `auto` | 発見→修正→検証→ガード→**ローカル merge**（適用する） | 第 1 ティア（CCD 自身） |
| `propose` | 発見→修正案生成→検証→ガード→**レポートに diff**（適用しない） | クライアント施策 |
| `off` | 発見→報告のみ | 最小構成 |

### Changed (BREAKING — schema migration)

- **`SafetyConfig.autonomous_fix` (bool) を `SafetyConfig.fix_mode` (str 3 値) に置き換え**（spec_028 §2-1）:
  - 新フィールド: `fix_mode: str = "off"`、許容値 `KNOWN_FIX_MODES = ("auto", "propose", "off")`、不正値は `field_validator` で `ValueError`。
  - **default は `"off"`**（安全側 ── 新規プロファイルは勝手に修正も提案もしない）。
  - **旧 `autonomous_fix` フィールドは削除**（後方互換エイリアスなし）── `extra="forbid"` なので旧フィールドが TOML に残っていれば明示エラーになり、移行漏れが顕在化する。これは意図どおりの「うるさい移行」。
  - 移行マッピング: 旧 `autonomous_fix = true` → 新 `fix_mode = "auto"`、旧 `autonomous_fix = false` → 新 `fix_mode = "off"`。意味論的に等価。
  - 検証用プロファイル `_ai_workspace/ccd_profile.toml` を `fix_mode = "auto"` に追従（さもないと次回 `ccd nightly` 実走でロードエラー）。
  - `render_profile` の `[safety]` セクションも `autonomous_fix = ...` を `fix_mode = "..."` に差し替え。
- **`pyproject.toml` / `ccd/__init__.py` version `0.16.0` → `0.17.0`**（新機能 = minor bump、spec §2-5）。
- **`tests/test_smoke.py::test_version_is_0160` → `test_version_is_0170`**、assert を `"0.17.0"` に。

### Added

- **`ccd/nightly.py` に提案モード (`_run_propose_loop`) を追加**（spec_028 §2-2）:
  - `run_nightly` が `profile.safety.fix_mode` の 3 値で分岐: `"off"` → 何もしない（spec_020 挙動を bit-for-bit 維持）/ `"auto"` → 既存 `_run_auto_fix_loop`（挙動不変・merge する）/ `"propose"` → 新規 `_run_propose_loop`（merge しない）。
  - **propose loop の核心**: 発見→翻訳までは auto と共有（`_select_candidate` / `translate_finding` は両モード共通）。その後 **`_isolated_clone` (spec_014 の使い捨てクローン) の中で**修正係を dispatch し、R5/R4/guard を**クローン内のパス**に対して走らせる。検証＋ガードが通れば、`git diff main..<branch>` を採取して `<live_repo>/_ai_workspace/nightly/proposals/proposal_YYYY-MM-DD_<spec_auto_id>.patch` に保存。クローンは context-manager exit で破棄。
  - **核心の不変条件**: 提案モード実行後、**実 repo にはブランチも未コミット変更も一切残らない**。クローンに対する `create_and_checkout_branch` / `dispatch` / `diff` / `merge` 系のすべての書き込みは破棄される。実 repo への唯一の書き込みはパッチファイル 1 件（gitignored な `_ai_workspace/` 配下）。
  - **検証/ガード失敗時**: 提案を破棄（パッチ書き出しなし、merge も呼ばない）。`AutoFixOutcome.proposed=False`、`halt_reason` に「proposal guard halted」「proposal R5 failed」「proposal R4 failed」等の anchor 文字列。朝レポート §D で 1 行表示される。
  - **既存コスト境界**: dispatch 実時間上限 40 分・`PAUSE` キルスイッチ・「発見ゼロは正常終了」は propose モードでも有効（同じ `_dispatch_with_timeout` を流用、`PAUSE` は `run_nightly` の入口で短絡）。**未 push バックログ停止 (spec_025 (b)) は auto 専用のまま** ── propose モードは merge しないので未 push の自律修正が溜まらない（テストで pin）。
- **`AutoFixOutcome` に 4 フィールド追加**:
  - `mode: str = "auto"` ── `"auto"` / `"propose"` / (skipped の場合は) `"off"`。auto モード既存挙動は default で carry。
  - `proposed: bool = False` ── propose モードが verified proposal を生成した。
  - `proposal_patch_path: Path | None = None` ── 保存されたパッチファイルの絶対パス（`<live_repo>/_ai_workspace/nightly/proposals/...`）。
  - `proposal_diff: str = ""` ── 朝レポート §B 提案版に埋め込む verified diff。
  - 既存フィールド・既存テスト（spec_023〜026）に対しては default 値で carry されるので bit-for-bit 不変。
- **`IsolatedWorkspace` seam を追加** ── `Callable[[Path], ContextManager[Path]]`。production default は `_default_isolated_workspace`（`ccd.discover._isolated_clone` をそのままラップ）。テストは fake factory を注入して使い捨てクローンの中身を制御。
- **`ccd/brief.py` に §B 提案版 (`_render_section_b_propose`) を追加**（spec_028 §2-3）:
  - `auto_fix.proposed=True` のとき §B を提案版に切り替え: テンプレ / signature / spec_auto / branch (クローン内) / R5/R4/ガード / **`proposal_diff` 埋め込み** / **`git apply` ワンライナー** / **パッチファイルパス**。
  - `auto_fix.proposed=False`（propose ループ走ったが verification 弾いた）のとき: §B は Phase 1 版のまま、§D に「**提案モード rejected** (...): 提案を生成したが検証/ガードで弾いた — 〜」の 1 行のみ。**unverified な diff は §B body に絶対出さない**（spec §2-3「動くと確認済みの修正案だけを出す」）。
  - §A 一行判定にも propose 用ヘッドラインを追加（merged / proposed / HALT / skipped の 4 状態を区別）。
  - §F honesty section に提案モード専用の文言を追加（「merge / commit / push のいずれも実行していない」「採用判断は人間」）。
  - **auto モードの §B Phase 2 版（spec_025）は不変** ── テスト `test_phase2_auto_brief_unchanged_by_spec_028` で構造的に pin。
- **`ccd/cli.py` の `ccd nightly` stdout** に propose 用の 3 行を追加: `propose: proposed <spec_auto_id> (patch=..., signature=...)` / `propose: HALT ... — <reason>` / `propose: skipped (...)`。auto モードの既存 3 行は不変。

### Added (tests)

- **`tests/test_profile.py` (+5 件)**:
  - `test_safety_default_fix_mode_is_off` ── default が `"off"`
  - `test_safety_fix_mode_auto_via_toml` / `test_safety_fix_mode_propose_via_toml` ── 各値の受理
  - `test_safety_fix_mode_unknown_value_raises_value_error` ── 不正値で `ValueError`
  - **`test_safety_legacy_autonomous_fix_field_now_rejected`** ── 旧 `autonomous_fix` TOML は `extra="forbid"` で明示エラー（移行漏れ pin）
  - 既存 5 件は `fix_mode` 化に追従して書き換え（`test_safety_section_appears_in_render_profile` の assert を `'fix_mode = "off"'` 等に更新）。
- **`tests/test_nightly.py` (+12 件)** ── 注入ベース（実 mutmut / claude / git を動かさない）:
  - `test_propose_mode_happy_path_writes_patch_without_touching_live` ── happy path 全部入り pin（**実 repo のブランチ・top-level tree が実行前と同一**を構造的に assert）
  - `test_propose_mode_skipped_when_no_candidate` / `test_propose_mode_off_fix_mode_no_loop_runs`
  - `test_propose_mode_guard_halt_drops_proposal_and_writes_no_patch` ── guard HALT で **merge ゼロ・パッチゼロ・§D 情報のみ**
  - `test_propose_mode_r5_fail_drops_proposal` / `test_propose_mode_r4_fail_drops_proposal` / `test_propose_mode_dispatch_failed_drops_proposal`
  - `test_propose_mode_never_calls_merge_or_unpushed_counter` ── spec_025 (b) は auto 専用、propose は consult しない
  - `test_propose_mode_template_b_happy_path` ── 敵対的 finding でも propose 経路が回る
  - `test_propose_mode_cli_stdout_surfaces_propose_line` ── `ccd nightly` stdout に `propose: proposed` が出る
  - `test_propose_mode_passes_finding_to_brief` ── `AutoFixOutcome` が `auto_fix=...` 経由で brief に届く
  - `test_propose_default_isolated_workspace_is_disposable_clone` ── production default seam が `_isolated_clone` を包んで使い捨て / yields a fresh tmp dir / exit で rmtree
  - 既存 71 件は不変で green。auto モードの 4 経路 (PAUSE / merged / HALT / skipped) shape は spec_023〜026 のまま。
- **`tests/test_brief.py` (+5 件)**:
  - `test_section_b_propose_rendered_when_proposed` ── 修正案・diff・R-evidence・`git apply` ワンライナー・パッチパスが §B に出る、§F に「merge していない」
  - `test_section_b_propose_includes_apply_command_with_repo` ── `git -C <abs_repo> apply <abs_patch>` 形式
  - `test_section_d_includes_rejected_proposal_one_liner` ── 弾かれた夜は §B は Phase 1、§D に 1 行
  - `test_section_a_surfaces_propose_headline` ── §A の一行判定にも propose ヘッドライン
  - `test_phase2_auto_brief_unchanged_by_spec_028` ── auto モードの §B Phase 2 版は不変 pin（push origin main 健在 / git apply は出ない）

### Constraints (spec §3)

- **触ってよい**: `ccd/profile.py`（`fix_mode` 化）/ `ccd/nightly.py`（モード分岐 + 提案ループ）/ `ccd/brief.py`（§B 提案版 + §D）/ `ccd/cli.py`（stdout + seam 配線）/ `_ai_workspace/ccd_profile.toml`（`fix_mode="auto"` 追従）/ `tests/` / `CHANGELOG.md` / `pyproject.toml` / `ccd/__init__.py` ── すべて遵守。
- **触ってはいけない（コアロジック）**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,translate,guard}.py` ── すべて遵守（1 行も touch していない）。特に:
  - **`ccd/translate.py` 不変** ── `spec_auto` の中身はモード非依存（auto も propose も同じ修正指示を使う、spec §3 逐語遵守）。
  - **`ccd/guard.py` 不変** ── 提案モードでもガードは同じものを diff に対して走らせる（強制であって指示でない、の思想は不変）。
  - **`ccd/discover.py` 不変** ── 隔離クローンのヘルパ `_isolated_clone` を `nightly.py` から **import するだけ**（spec §3 で明示的に許可された使い方）。共有モジュールへの切り出しは行わなかった ── import で十分（Open question 1 参照）。
- **`docs/`** / **`docs/data/*.json`** ── 触っていない。Phase 3 全体の `docs/DESIGN.md` 更新は別 spec の責務（spec_027 の補足 6 と同じ精神）。
- 安全境界レベル 2 は不変 ── 提案モードは merge も push もしない（auto モードの merge も従来どおり push しない）。`GitOps` Protocol に push 系が無い構造も不変。
- 既存サブコマンド・既存挙動（特に `fix_mode="auto"` ＝旧 `autonomous_fix=True` の経路）は bit-for-bit 不変 ── spec_023〜026 の既存テスト 71 件が無修正 (autonomous_fix → fix_mode の API 移行のみ) で green。

### Verification

- **`pytest -q`**: **541 passed** in ≈25s（spec_027 時点の 524 件から、profile 既存の `autonomous_fix` 5 件を `fix_mode` 5 件に置き換え + 5 新規 propose 関連 / nightly +12 propose / brief +5 propose で正味 +17）。spec_013〜027 の既存テストはすべて不変で green。smoke の version assert 1 件は rename + 文字列差し替え。
- **`ruff check .`**: `All checks passed!`。
- **`python3 -m ccd --version`** → `ccd 0.17.0`（smoke の subprocess テストが確認）。


## [0.16.0] — 2026-05-25

spec_027 — **v2 Phase 3 の最初の spec**。Phase 2 (spec_021〜026) で自律修正ループが完成し、`ccd nightly` の end-to-end 実走で「ループが自分のテスト隙間を発見し、正しい修正を書き、ガード・検証を通してローカル merge する」ところまで実証できた次の段として、**ケイデンス（実行頻度）** をプロファイルに導入する（minor bump = 新機能）。

経緯: Phase 2 までは「毎晩（nightly）」前提でしかスケジューラに登録できなかったが、**開発途中のシステムに毎晩自律修正を回すのは実用的でない**（動く標的を毎晩追う / 夜間の自律修正コミットが昼の人間の開発と衝突する）。詰め直した結論は「**自律修正は行う。ただし頻度は週次にする**」── これが本来求めていた運用形態。

本 spec は**プロファイルのモデルとスケジューラ登録テンプレートだけ**を変更する軽い spec。`ccd/nightly.py` の自律修正ループ本体・発見・ガード・翻訳・検証経路には **1 行も触れない** ── `ccd nightly` は呼ばれたら 1 回ループを回すだけ、頻度はスケジューラ（PS1）が決める、という役割分担は不変。

### Added

- **`ccd/profile.py` の `ScheduleConfig` にケイデンス 2 フィールド追加**:
  - **`cadence: str`** ── 実行頻度。`"nightly"`（毎晩）/ `"weekly"`（週1）のいずれか。**default は `"weekly"`**（spec_027 §2-1 の運用判断）。既知の値以外は `field_validator` で `ValueError` ── `channels` / `fix_templates` の既存バリデータと同じ流儀。
  - **`weekly_day: str`** ── 週次実行の曜日。default `"Sunday"`。Windows タスクスケジューラの `New-ScheduledTaskTrigger -DaysOfWeek` にそのまま渡せるフル英名（`Monday`〜`Sunday`）。
  - **新規 module 定数**: `KNOWN_CADENCES: tuple[str, ...] = ("nightly", "weekly")` / `KNOWN_WEEKDAYS: tuple[str, ...] = (Monday … Sunday)`。
  - **入力正規化（spec §6 の CC 判断）**: `weekly_day` は title-case 正規化を入れた ── `"sunday"` / `"SUNDAY"` / `"Sunday"` のいずれも受理し、プロファイルには `"Sunday"`（PowerShell の `-DaysOfWeek` が直接受ける canonical 形）で保存。短縮名（`"Sun"`）は受けない（PowerShell が解釈しない表記を受けると task 登録時まで失敗が遅延する ── 早めに loader で stop）。
  - **`cadence` 未指定の TOML は `cadence="weekly"`** ── 既存 `_ai_workspace/ccd_profile.toml`（cadence 行なし）は完全に後方互換、自動で週次に切り替わる。
- **`_ai_workspace/register_nightly.ps1` を週次対応**:
  - 編集ポイントに **`$Cadence`**（既定 `"weekly"`）と **`$WeeklyDay`**（既定 `"Sunday"`）を追加。コメントで「profile の `schedule.cadence` / `schedule.weekly_day` と一致させる」と明記。
  - `$trigger` 生成を `switch ($Cadence)` で分岐: `"weekly"` → `New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $NightlyAt` / `"nightly"` → `New-ScheduledTaskTrigger -Daily -At $NightlyAt` / その他 → `Write-Error` で停止（不正値を黙って Daily にしない）。
  - 登録完了メッセージは cadence に応じた `$TriggerDesc`（週次なら `"weekly on $WeeklyDay at $NightlyAt"`、毎晩なら `"daily at $NightlyAt"`）。
  - 冒頭コメント / 編集ポイント説明を更新。タスク名 `CcdNightlyMaintenance` は据え置き（リネームは登録済みタスクとの不整合を生む、spec_027 §2-2）。
- **`ccd profile` の出力に新2フィールドを emit** ── `render_profile` の `[schedule]` セクションに `cadence = "..."` / `weekly_day = "..."` を `nightly_at` と並べて出す。round-trip（renderer 出力を再ロード）で同じ Profile になる。

### Added (tests)

- **`tests/test_profile.py` (+18 件)** — spec_027 §2-4 のテスト要件:
  - `KNOWN_CADENCES` / `KNOWN_WEEKDAYS` 定数の存在と中身（曜日 7 つ揃っている）
  - `Profile()` の default で `cadence=="weekly"` / `weekly_day=="Sunday"` / `nightly_at=="02:00"`（nightly_at 不変保証）
  - `cadence="nightly"` / `cadence="weekly"` 両受理
  - `cadence="daily"` 等の不正値で `ValueError`
  - `weekly_day="Funday"` 等の不正値 / 短縮名 `"Sun"` で `ValueError`
  - `weekly_day="sunday"` / `"WEDNESDAY"` の case-insensitive 入力 → `"Sunday"` / `"Wednesday"` に正規化
  - cadence 未指定の既存 TOML 形（spec_018〜026 の deployed 形）ロード時に `cadence="weekly"` になる **後方互換 pin**
  - `cadence="nightly"` + `weekly_day="Wednesday"` の組み合わせも受理（cadence=nightly でも weekly_day はフィールドとして無害に保持される、将来切り替え用）
  - `ccd profile` の出力に `cadence` / `weekly_day` / `nightly_at` の 3 行が `[schedule]` に出る（default 値と override 値の両方）
  - **renderer round-trip pin** ── full profile を render → コメント行を除去 → 再ロードで equal な Profile になる
- **`tests/test_profile.py::test_register_nightly_ps1_supports_cadence`** ── PS1 テンプレートのテキスト検査（spec §2-4 の方針通り execute はしない）。`$Cadence` / `$WeeklyDay` の編集ポイント、`-Weekly` / `-DaysOfWeek` / `-Daily` の両分岐、不正値で `Write-Error` 停止、`$Cadence`/`$WeeklyDay` の default が profile の default（`"weekly"` / `"Sunday"`）と一致することを assert。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.15.1` → **`0.16.0`**（**新機能 = minor bump**、spec §2-5）。
- `tests/test_smoke.py::test_version_is_0151` → **`test_version_is_0160`**、assert を `0.16.0` に。
- `ccd/profile.py` module docstring に spec_027 でケイデンスが入った旨を追記。`ScheduleConfig` の docstring を新2フィールドの意味・default・`weekly_day` が nightly では無視される点・`nightly_at` をリネームしなかった理由を含めて全面更新。

### Constraints (spec §3)

- **触ってよい**: `ccd/profile.py`（`ScheduleConfig` + 関連 docstring）/ `_ai_workspace/register_nightly.ps1` / `tests/test_profile.py` / `tests/test_smoke.py` / `CHANGELOG.md` / `pyproject.toml` / `ccd/__init__.py`。
- **触ってはいけない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief,guard,translate,nightly}.py` のコアロジック ── すべて遵守（1 行も touch していない）。特に **`ccd/nightly.py` は触らない** ── `ccd nightly` 自体は cadence を読まない（呼ばれたら 1 回走るだけ）。`docs/` / `docs/data/*.json` ── 触っていない。
- `ccd nightly` コマンド名・`CcdNightlyMaintenance` タスク名はリネームしない（侵襲的）── 遵守。
- 既存サブコマンド・既存挙動は不変。**追加のみ**。ローカル commit のみ、**push しない／ブランチ操作・merge しない** ── 遵守。

### Verification

- **`pytest -q`**: **521 passed** in 29.53s（既存 503 件 + 新規 18 件）。spec_013〜026 の既存テストはすべて不変で green（特にプロファイル関連 spec_018/023/024 の既存テストが壊れていない）、smoke の version assert 1 件だけ rename + 文字列差し替え。
- **`ruff check .`**: `All checks passed!`。
- **`python3 -m ccd --version`** → `ccd 0.16.0`（smoke の subprocess テストが確認）。


## [0.15.1] — 2026-05-25

spec_026 — `ccd nightly` の end-to-end 実走で発覚した 2 つのバグの修正（patch bump）。v2 Phase 2 の機構自体（ガード・翻訳・テンプレ A / B・コスト境界・朝レポート §B Phase 2）は spec_021〜025 で完成しており、本 spec は**ループが正しい修正を実際に完了できる**ようにする後始末。

実走で起きたこと: 自律修正ループは discover → 候補選択 → 翻訳（`spec_auto_001`）→ ブランチ作成（`auto/spec_auto_001`）→ 修正 dispatch まで通り、修正係（CC）は `ccd/protocol.py:46` の生存改変（`continue → break`）を殺す本物のテスト（`test_parse_spec_skips_leading_non_heading_lines`）を書いた。改変ありで失敗 / 原本で成功も実証済みで、自律修正の中身は正しかった。しかし 2 バグでループは修正を完了できなかった:

- **バグ① (偽 HALT)**: 翻訳が生成した `spec_auto_001` には「コミットせよ」という指示が無く、修正係はテストを書いたが**コミットしなかった**。すると `dispatch_one` が「result ファイルあり・commit 0 件」→ `agent_misread` 分類 → ループは正しく書けた修正を HALT した（偽陽性 ── 论点6 の「偽陽性可・偽陰性不可」の安全側ではあるが、このままループは 1 件も修正を完了できない）。
- **バグ② (HALT の後始末漏れ)**: 修正 dispatch が HALT した後、`tests/test_protocol.py` の未コミット変更が `main` の作業ツリーに残り、`auto/spec_auto_001` ブランチも残骸として残った。HALT 経路がリポジトリをクリーンな状態に戻していなかった。

両方を直す。安全性（失敗修正を merge しない）は守られていた ── 本 spec は「ループが正しい修正を実際に完了できる」ようにする修正。

### Fixed

- **バグ① — `ccd/translate.py` のテンプレ A / B 制約文** — `spec_auto_NNN.md` の §3 制約ブロックに 2 つの新規節を追加（テンプレ A・テンプレ B 両方に独立に）:
  - **`_CONSTRAINT_COMMIT_REQUIRED`** (テンプレ A) / **`_CONSTRAINT_B_COMMIT_REQUIRED`** (テンプレ B): 「**修正は現在の feature branch（`auto/<このタスクの spec_auto_id>`）に `git commit` せよ**（論理単位で、メッセージは任意）。あなたは既にこの feature branch 上で起動されている ── 作業を書き終えたら、必ずその branch に commit を積むこと（**コミットは禁止ではなく必須**）。commit が 0 件のまま result ファイルだけ書いて終了すると、自律修正ループはこのタスクを `agent_misread` として HALT する（spec_026 §1 の偽 HALT の原因）。」
  - **`_CONSTRAINT_NO_PUSH_BRANCH_MERGE`** (テンプレ A) / **`_CONSTRAINT_B_NO_PUSH_BRANCH_MERGE`** (テンプレ B): 「**`git push` の実行・別ブランチへの切り替え（`git checkout main` 等）・新規ブランチの作成・`main` への merge は禁止**。push と main への local merge は自律修正ループ側（`ccd/nightly.py` の `GitOps` seam）が行う ── 本タスクの担当範囲は feature branch 上で commit するところまで。ここで禁止しているのは「push しない／他ブランチに移らない／自分で merge しない」のみであって、**「commit しない」ではない**（混同しないこと ── 前者の文言を後者と読み違えるのが spec_026 で直したバグの原因）。」
  - **同じ workflow なので A / B のテキストはほぼ並列**。spec_022 / spec_024 の docstring に書かれた「A と B の制約定数は独立 ── 一方が他方を流用しない」方針に従い、別の module 定数として並べる（将来片方だけ変えたい時に他方が影響を受けないように）。
  - 両定数は §3 制約ブロックの**先頭 2 行**に挿入（既存 5 つの制約より前 ── 修正係が最初に読む位置）。
- **バグ② — `ccd/nightly.py:_run_auto_fix_loop` の全 HALT 経路の作業ツリー復元** — `GitOps` Protocol に 2 つの新規メソッドを追加し、ループの HALT 経路すべてから呼ぶ:
  - **`GitOps.discard_local_changes(*, repo: Path) -> None`** — `SubprocessGitOps` 実装は `git reset --hard HEAD` + `git clean -fd` の 2 コマンド（tracked-modified と untracked の両方を確実に消す）。
  - **`GitOps.delete_branch(*, repo: Path, branch: str) -> None`** — `SubprocessGitOps` 実装は `git branch -D <branch>`（未 merge ブランチでも force-delete）。
  - **`_restore_repo_after_halt(*, gops, repo, branch)`** — 新ヘルパ。3 ステップを順に呼ぶ: (1) `discard_local_changes`（未コミット変更破棄、auto branch 上で）→ (2) `checkout("main")` → (3) `delete_branch`。**各ステップは独立に `try/except` で囲み**、1 つが失敗しても残りが走る（best-effort、git が深く壊れていても朝レポートは描画できる）。
  - **`_delete_feature_branch_after_merge(*, gops, repo, branch)`** — 新ヘルパ。成功 merge 経路用。merge 自体は既に `main` を最新化して working tree もクリーンなので、branch 削除のみ実行（`discard` / `checkout` は不要）。
  - 全 HALT 経路（branch 作成失敗 / dispatch exception / dispatch !done / R5 失敗 / R4 失敗 / ガード HALT）から `_restore_repo_after_halt` を呼ぶ ── 以前は dispatch 系の 2 経路で `_safe_checkout_main` のみだったので、本 spec で `_safe_checkout_main` を削除し、より完全な `_restore_repo_after_halt` に置き換え。
  - 成功 merge 経路では `_delete_feature_branch_after_merge` を呼んで auto ブランチを片付ける（merge コミットは `main` に残る、既存挙動を壊さない）。
  - HALT 経路の場合、ループ終了後のリポジトリは実行前と実質同一（クリーンな `main`、auto ブランチなし、未コミット変更なし）── 论点7 の pre-flight の「リポジトリがクリーン」前提を構造的に立てる。

### Added (tests)

- **`tests/test_translate.py` (+9 件、30 件 → 39 件)** — spec_026 §2-3 のテスト要件:
  - **テンプレ A constraint** — body に「feature branch」「commit」「auto/」「コミットは禁止ではなく必須」が含まれる / 「git push」「別ブランチ」「新規ブランチ」「merge」「禁止」が含まれる / 「コミットするな」「コミットしてはならない」等の禁止表現は含まれない（「コミットは禁止」は「ではなく必須」の文脈でのみ許容）/ `_CONSTRAINT_COMMIT_REQUIRED` / `_CONSTRAINT_NO_PUSH_BRANCH_MERGE` が verbatim で本文に出る。
  - **テンプレ B constraint** — 同じ 4 観点をテンプレ B 側で pin（`_CONSTRAINT_B_COMMIT_REQUIRED` / `_CONSTRAINT_B_NO_PUSH_BRANCH_MERGE`）。
- **`tests/test_nightly.py` (+8 件、63 件 → 71 件)** — spec_026 §2-3 のテスト要件:
  - **`_FakeGitOps`** に `discards: list[Path]` / `deletes: list[str]` の 2 フィールド + `discard_local_changes` / `delete_branch` メソッドを追加（既存テストは不変、新規 list は default factory で空）。
  - **6 つの HALT 経路の復元 assertion** ── dispatch failure（`status="failed"`）/ dispatch exception（dispatcher が raise）/ R5 失敗 / R4 失敗 / ガード HALT / branch 作成失敗 ── 各経路で「`discards != []`」「`"main" in checkouts`」「`branch in deletes`」を独立に pin。各テストは halt_reason の正しさも併せて確認（既存 spec_023 / 024 のテストと同じ流儀）。
  - **成功 merge 経路の不変保証** ── `test_success_merge_deletes_feature_branch_but_keeps_main` で `merges == [branch]`（既存挙動）+ `branch in deletes`（新規）+ `discards == []`（success path は discard しない）+ `"main" not in checkouts`（success path は明示的な checkout("main") を加えない、merge_branch_into_main が内部で行うので）。
  - **best-effort 性の保証** ── `test_halt_restore_swallows_exceptions_per_step` ── discard / checkout / delete_branch のすべてが raise する fake GitOps で、それでも `run_nightly` が exception で落ちず brief を描画、halt_reason は元の R5 失敗が surface（cleanup-step の exception ではなく）。
  - **Protocol surface の保証** ── `test_gitops_protocol_has_spec_026_methods` で `GitOps.discard_local_changes` / `GitOps.delete_branch` の属性 + `SubprocessGitOps` のそれらが callable であることを assert（将来の uncautious 削除を防ぐ）。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.15.0` → **`0.15.1`**（**バグ修正 = patch bump**、spec §2-4）。
- `tests/test_smoke.py::test_version_is_0150` → **`test_version_is_0151`**、assert を `0.15.1` に。
- `ccd/nightly.py:_safe_checkout_main` を削除し、機能を強化した `_restore_repo_after_halt` に置き換え（古いヘルパは 1 機能のみだったので、3 ステップ復元には不十分だった ── 完全置換）。
- `ccd/nightly.py:GitOps` Protocol に `discard_local_changes` / `delete_branch` の 2 メソッドを追加 ── 既存 4 メソッド (`create_and_checkout_branch` / `diff` / `merge_branch_into_main` / `checkout`) は不変。`SubprocessGitOps` も新 2 メソッドを実装、既存 4 メソッドは不変。

### Constraints (spec §3)

- **触ってよい**: `ccd/translate.py`（テンプレ A / B の制約文言）、`ccd/nightly.py`（`_run_auto_fix_loop` の HALT 復元）、`tests/test_translate.py` / `tests/test_nightly.py` / `tests/test_smoke.py`、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`。
- **触ってはいけない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief,profile,guard}.py` のコアロジック ── すべて遵守（1 行も touch していない）。`docs/` / `docs/data/*.json` ── 触っていない。
- テストで実 mutmut / 実 claude / 実 git スケジューラを動かさない（注入ベース、`_FakeGitOps` に新 2 メソッドを追加して `discard` / `delete` を仮想化）── 遵守。
- 実 `ccd nightly` の end-to-end 再実走は本 spec では行わない ── 両修正はユニットテストで固める（spec §3 の指針通り、spec_019 §2-3 の「dispatch 内に実走検証を入れると長時間化する」教訓を踏襲）。
- 既存サブコマンド・既存挙動（特に成功 merge 経路）は不変 ── `test_success_merge_deletes_feature_branch_but_keeps_main` が「merge コミットが main に残る」を pin。
- ローカル commit、**push しない／ブランチ操作・merge しない** ── 遵守（本 spec の修正は `feat/spec_026` ブランチ上で 1 件の commit）。

### Verification

- **`pytest -q`**: **503 passed** in 27.94s（486 → +17: translate +9、nightly +8）。spec_013〜025 の既存テスト 486 件すべて green、smoke の version assert 1 件だけ rename + 文字列差し替え（spec §2-4 の要件）。
- **`ruff check .`**: `All checks passed!`。
- **`python3 -m ccd --version`** → `ccd 0.15.1`（smoke の subprocess テストが確認）。


## [0.15.0] — 2026-05-25

v2 Phase 2 の最終 spec — spec_025。spec_021〜024 で自律修正ループ（ガード・翻訳・テンプレ A / B）が点火・実証された次の段として、**運用の歯止め**（コスト/停止境界）と**朝レポート §B のアップグレード**を入れて、ループを安全に夜間運用できる状態に仕上げる（`docs/DESIGN.md §9.6` 论点8 / 论点9）。これで v2 Phase 2 が完成する。

歯止めは 4 つ：(a) dispatch 実時間 40 分上限（threading-based timeout、超過したら failed 扱い）、(b) 未push 自律修正 3 件で新規 dispatch を一時停止（朝レポートで promote 促し）、(c) `_ai_workspace/PAUSE` ファイルがあればその夜 nightly は何もしない（中島さんの非常ブレーキ）、(d) 発見ゼロは正常終了（エラーではなく「今夜は何もなし」）。

朝レポート §B は、自律修正が **merge した夜**だけ Phase 2 版に切り替える：(1) 何を発見し何を直したかの narrative、(2) 修正の diff を埋め込み（論点6 の R3 で size 抑制済み）、(3) R5 / R4 / ガードの検証証拠、(4) そのまま貼れる `git push origin main` ワンライナー。自律修正が無かった夜（gate off / skip / HALT）は Phase 1 版 §B （発見のみ）のまま ── 「既定は簡潔・例外時のみ伸びる」（§9.6）を維持。

安全境界レベル 2 は引き続き構造的：`GitOps` Protocol が push 系メソッドを持たないので push 不能、§B Phase 2 の push コマンドはあくまで **operator 向けの提案**（自動実行はしない、論点 2「朝、人間が差分を見て手動 push」）。

### Added

- **`ccd/nightly.py` 拡張** — spec_025 §2-1 のコスト/停止境界 4 件。
  - **(a) Dispatch wall-clock 上限** — `_AUTO_FIX_DISPATCH_TIMEOUT_S = 40 * 60` の module 定数 + `run_nightly` の `dispatch_timeout_s: float | None = None` kwarg。`_dispatch_with_timeout` ヘルパが `concurrent.futures.ThreadPoolExecutor(max_workers=1)` で dispatcher 呼び出しを wrap し、`future.result(timeout=...)` で超過を検出。超過時は `FixDispatchOutcome(status="failed", halt_reason="dispatch timed out after Ns (spec_025 §2-1(a))")` を返す ── 下層の `claude` subprocess は Python の thread cancel で完全 kill できない構造的制約があり、その制約は docstring に明記。
  - **(b) 未push 自律修正バックログ上限** — `_AUTO_FIX_UNPUSHED_BACKLOG_LIMIT = 3` の module 定数 + `UnpushedCounter = Callable[[Path], int]` seam + `unpushed_counter` / `unpushed_backlog_limit` kwarg。`_default_unpushed_counter` が `git log origin/main..main --pretty=format:%s` を実行し `"auto-merge:"` プレフィクスを数える（spec_023 の `SubprocessGitOps.merge_branch_into_main` がこの prefix で merge を作る）。閾値以上で `AutoFixOutcome(skipped=True, skip_reason="un-pushed autonomous-fix commits at or above limit (N un-pushed, limit M); review and `git push origin main` before the loop resumes")` を返す ── 朝レポートが「未push の自律修正が N 件。レビューして push してから続けます」を render する素材。Counter 例外時は backlog 0 扱い（counter 故障で loop を silent disable しない）。
  - **(c) PAUSE ファイル kill switch** — `_AUTO_FIX_PAUSE_REL = Path("_ai_workspace") / "PAUSE"`。`run_nightly` 入口の pre-flight より前にチェックし、ファイルがあれば `NightlyResult(success=True, paused=True, halt_reason="paused: _ai_workspace/PAUSE present")` を即返。**何も実行しない**（channel / fix / brief / mirror 全部 skip）。`NightlyResult.paused: bool` field を追加。`_ai_workspace/` 配下は gitignored なのでうっかり commit される事故なし。
  - **(d) 発見ゼロは正常終了** — 既存の "no template-X candidate available" skip 経路が既に `success=True` を返していたのを構造的に維持。`_render_section_a` の "発見なし" headline に「(今夜は何もなし — エラーではない)」の補足を追加して operator が朝レポートで判定しやすくした。
- **`ccd/nightly.py:AutoFixOutcome.merge_diff: str = ""`** — fix が merge した夜だけ pre-merge diff（guard が R3 で size 抑制済みなので小さい）を捕まえて outcome に乗せる。`_run_auto_fix_loop` の guard step 直前で `diff_text = gops.diff(repo=repo, base="main", head=branch)` を捕まえ、merge 成功時だけ `surfaced_diff = diff_text` を outcome に詰める。**halt 時は空のまま** ── 朝レポートに un-merged な diff を埋め込まない構造的保証。
- **`ccd/brief.py` 拡張** — §B Phase 2 upgrade（spec_025 §2-2）。
  - **`run_brief` シグネチャに `auto_fix: AutoFixOutcome | None = None`** を追加。`auto_fix.merged is True` のときだけ Phase 2 §B が rendered される、それ以外（None / skipped / halted）は Phase 1 §B のまま（spec_017 の挙動 bit-for-bit 不変）。
  - **`_render_section_b_phase2(auto_fix, repo)`** — 4 部構成:
    1. **発見と修正** — テンプレ (A: ミューテーション → test-only / B: 敵対的 → 本番修正)、signature、spec_auto_id、candidate_count、branch、ローカル merge / no push。
    2. **検証の証拠** — R5（テンプレ別ラベル: A は "target mutation killed"、B は "parser now raises a graceful error"）、R4 (pytest -q)、ガード (R1〜R3) の pass/fail。HALT 時は理由文字列も surface。
    3. **修正の diff** — `auto_fix.merge_diff` を ````diff … ```` ブロックに埋め込み。`_PHASE2_DIFF_CAP = 16 * 1024` 超は truncate + 「`git show` で全体確認」の footer。`merge_diff == ""` 時（テスト dispatch 等で seam が埋めない構造的ケース）は「diff が記録されていません」を render（捏造で埋めない正直さ）。
    4. **push コマンド** — `_compose_push_command(repo)` が `git -C <abs_repo> push origin main` を生成（operator がどの shell からでも copy-paste 可能）。repo 不明時は `git push origin main` にフォールバック。
  - **header / preamble の Phase 2 切替** — header の "Phase 1 (発見のみ)" を "Phase 2 (昨夜の自律修正あり)" に切り替え、preamble で「§B に diff と検証証拠と push コマンドを掲載 ── レビューしてから手動で push してください」を出す。
  - **§A の一行判定強化** — `auto_fix` 3 ステータス（merged / halted / skipped）を §A の 1 行目に surface ── operator が scroll せず headline で読める。`merged` は spec_auto_id と template を含む「昨夜の自律修正 1 件をローカル merge」、`halted` は halt_reason 引用、`skipped` は skip_reason 引用。
  - **§D の HALT/SKIP 拡張** — channel halts と並べて autonomous-fix の skipped/halted を「自律修正 skipped: <reason>」「自律修正 HALT (`<spec_auto_id>`, template <X>): <halt_reason>」として出す。spec_023 の既存 §D は channel-only だったので、Phase 2 で loop の状態も同じセクションに集約。
  - **§F (正直さの節) の Phase 2 切替** — fix が merged した夜は「push は実行していない」「次の発見チャンネルは走らせていない」の 2 点だけ簡潔に出す（既存の長文 Phase 1 boilerplate は載せない、論点 2 レベル 2 を逐語で立てる）。
- **`ccd/cli.py` 拡張** — `main()` / `_cmd_nightly` に `unpushed_counter` / `unpushed_backlog_limit` / `dispatch_timeout_s` の 3 seam を追加（既存 6 seam と同じ流儀）。PAUSE 短絡時に "nightly: paused (PAUSE file present — no channels / no fix / no brief)" を stdout に出して return 0。既存サブコマンドの shape は不変。
- **`tests/test_nightly.py` (+17 件、46 件 → 63 件)** — spec_025 §2-3 のテスト要件:
  - **PAUSE** — PAUSE ファイル存在で channel / fix / brief / mirror が一切呼ばれず paused=True を返す / PAUSE absent では normal run / CLI が "paused" を stdout に出して return 0。
  - **未push backlog** — counter==3 で skipped + skip_reason に "un-pushed autonomous-fix commits" + "3 un-pushed" + "limit 3" + "git push" / counter==2 で normal merge / `unpushed_backlog_limit=1` override で 1 件でも止まる / counter が raise しても loop は止めない（counter 故障で silent disable しない）。
  - **dispatch timeout** — slow dispatcher (sleep 2.0s) + `dispatch_timeout_s=0.2` で dispatch_status="failed" + halt_reason に "timed out" + "spec_025 §2-1(a)" / R4/R5/guard が呼ばれない / module 定数の値が 40*60 / backlog limit 定数が 3。
  - **発見ゼロ正常終了** — discover JSON なしで gate ON → skipped + success=True + brief が render される。
  - **`merge_diff` capture** — merge 成功時に diff が outcome に乗る / R5 失敗 HALT 時は merge_diff="" のまま。
  - **§B Phase 2 end-to-end** — real `run_brief` + merged outcome で生成された report.md に "Phase 2" / "## B. 昨夜の自律修正" / "```diff" / "tests/test_protocol.py" / "R5 pass" / "R4" / "ガード" / "push origin main" が含まれ、Phase 1 §B header (`## B. 機械的チャンネルの発見`) が**含まれない** / 同じ条件で merged=False (R5 survived) なら Phase 1 §B のまま + §D に "自律修正 HALT" / auto_fix=None なら従来通り。
- **`tests/test_brief.py` (+10 件、20 件 → 30 件)** — brief 単体での Phase 2 §B 検証:
  - §B Phase 2 が merged outcome で render される（finding / spec_auto / branch / R-evidence / diff / push 全部 surface）/ Phase 1 §B header (`機械的チャンネルの発見`) は同時に出ない。
  - push コマンドが `git -C <abs_repo>` 形式（どの shell からでも paste 可能）。
  - auto_fix=None / skipped / halted では Phase 1 §B のまま、各々 §D に skipped/HALT が surface。
  - 大きすぎる diff (16KB 超) は truncate + 「切り詰めました」footer。
  - テンプレ B の §B は「テンプレ B (敵対的入力 ungraceful → 本番修正 + 再現テスト)」と「graceful error」の R5 ラベルを surface。
  - §A に auto_fix の headline が出る（merged + spec_auto_001 が §A 内）。
  - 発見ゼロでも §A に「今夜は何もなし — エラーではない」が surface。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.14.0` → **`0.15.0`**（**新機能 = minor bump**、spec §2-4）。
- `tests/test_smoke.py::test_version_is_0140` → **`test_version_is_0150`**、assert を `0.15.0` に。
- `ccd/brief.py:_render_section_b` の Phase 1 版は完全に保持（既存 brief テスト 20 件全 green）。Phase 2 切替は新ヘルパ `_render_section_b_phase2` を別に生やす形（既存 helper を破壊的に変更しない）。
- `ccd/nightly.py:_run_auto_fix_loop` のシグネチャに `unpushed_counter` / `unpushed_backlog_limit` / `dispatch_timeout_s` の 3 引数を追加。3 引数とも `run_nightly` から forward され、kwargs default を介して module 定数にフォールバック ── 既存呼び出し（テストの直接 `_run_auto_fix_loop` 呼び出しはなく、すべて `run_nightly` 経由）の shape 不変。

### Constraints (spec §3)

- **触ってよい**: `ccd/nightly.py`（コスト/停止境界）、`ccd/brief.py`（§B アップグレード）、`ccd/cli.py`（CLI で paused surface + seam forwarding）、`tests/test_nightly.py` / `tests/test_brief.py` / `tests/test_smoke.py`、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`。
- **触っていない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,guard,translate,profile}.py` のコアロジックは **1 行も変更していない**（spec §3 で明示）。プロファイル経由の PAUSE フラグは導入していない（spec §3「（またはプロファイルのフラグ）」は *or*、ファイル方式で要件を満たすので最小サーフェスを選択）。`docs/` も触らない（spec §3、Phase 2 完成に伴う `docs/DESIGN.md` の §9.5〜9.7 更新は別 spec の責務）。
- 安全境界レベル 2：ループは依然 **push しない**。`GitOps` Protocol が push 系メソッドを持たない構造的保証は spec_023 から継承、§B Phase 2 の push コマンドは operator 向けの**提案テキスト**であって自動実行はしない（論点 2: 朝、人間が差分を見て手動 push）。
- **テストで実 mutmut・実 claude を呼ばない** — 既存 fakes（`_FakeFixDispatcher` / `_FakeSuiteRunner` / `_FakeMutationRechecker` / `_FakeGuardInspector` / `_FakeGitOps`）に加え、(a) 用に `_slow_dispatcher` (`time.sleep(2.0)`) を inline で書く、(b) 用に `unpushed_counter=lambda: 3` を inline。`_default_unpushed_counter` は実 git に shellout するが、test で呼ばない（すべて override）。
- **spec_013〜024 の既存挙動・既存テスト 461 件は完全不変**（smoke の version assert 名 1 件のみ rename + 文字列差し替え、spec §2-4 要件）。`pytest -q` で **486 passed**（461 → +17 nightly + 10 brief - 2 dup = 486）。
- すべて**追加のみ**。ローカル commit のみ、**push しない／ブランチ操作しない**（spec §3）。

## [0.14.0] — 2026-05-25

v2 Phase 2 のリスク傾斜後半 — spec_024。spec_023 で**テンプレ A（ミューテーション → test-only）**の自律修正ループが点火・実証された次の段として、**テンプレ B（敵対的入力の ungraceful クラッシュ → 本番コード修正＋再現テスト）** を自律化（`docs/DESIGN.md §9.5/§9.7`）。テンプレ B は**本番コードに触る**ため A より一段リスクが高い ── だから A が信用できてから点火する（§9.7 リスク傾斜）。

実弾：Phase 1 で敵対的入力チャンネルが発見した `UnicodeDecodeError` 漏洩（4 パーサすべてが `read_text(encoding="utf-8")` を wrap していない）。これがテンプレ B が直す最初の対象。

段階有効化：**プロファイル `safety.fix_templates` で対象テンプレを `["A"]` / `["A", "B"]` と指定可能**。既定は安全側（`["A"]` のみ）── 新規リポジトリでは `autonomous_fix=true` にしてもテンプレ B は走らない、operator が明示的に `["A", "B"]` に書き換えてから点火する設計（论点1 tier × §9.7 リスク傾斜）。

テンプレ A→B の優先順位：両テンプレ有効でも **A 優先**（test-only は構造的に安全）── A 候補があれば A、なければ B にフォールスルー。B 候補は朝レポートに必ず surface、明日の夜に拾える。

「**優雅に失敗させる**のであって**成功させる**ではない」── テンプレ B の最重要制約。修正後のパーサが当該の壊れた入力を**黙って受理**したら R5 は失敗（`graceful_success` ≠ `graceful_error`）。

### Added

- **`ccd/translate.py` 拡張** — テンプレ B（spec_024）。
  - **エントリ関数の振り分け** — `finding.channel` でテンプレを選ぶ。`"mutation"` → テンプレ A、`"adversarial"` → テンプレ B、その他 → 報告専用降格。各テンプレは独立した fit-check (`_why_template_*_does_not_fit`) + レンダラ (`_render_template_*`) を持ち、一方が他方を流用しない（後で片方の制約が変わっても他方が誤って影響を受けない）。
  - **`Finding` 拡張** — `parser` / `case_name` / `exception_type` / `exception_message` の 4 フィールドを optional で追加（既定 `""`）。mutation 用 Finding はこれらが空のまま spec_022 と shape 不変。adversarial 用は `Finding._from_adversarial_dict(payload, source_report=...)` で `discover_NNN.json` の `findings` エントリから組み立てる ── `file` は `parser` の dotted-name から `_parser_dotted_to_file` で導出（`ccd.protocol.parse_spec` → `ccd/protocol.py`）、`line=0`、`status="ungraceful"` を埋める。
  - **テンプレ B 本文構造** — テンプレ A と同じ 7 セクション骨格、内容はテンプレ B 用:
    1. **§1 文脈** — parser × case × exception_type、exception_message を引用、現行 main で再現する事実を明記。
    2. **§2 やってほしいこと** — (1) `<file>` の `<parser>` を修正、許可リスト例外をクリーンに raise、(2) 再現テストを 1 本だけ追加（黙って受理は禁止）。
    3. **§3 制約（テンプレ B 逐語）** — 5 つの制約をモジュール定数 `_CONSTRAINT_B_GRACEFUL_FAIL_NOT_ACCEPT` / `_CONSTRAINT_B_REPRODUCER_GATE` / `_CONSTRAINT_B_EXISTING_TESTS_IMMUTABLE` / `_CONSTRAINT_B_NO_SKIP_MARKERS` / `_CONSTRAINT_B_ALLOWED_SET` から逐語で焼き込み。`test_template_b_constraint_phrases_are_verbatim` がモジュール定数と spec 本文の包含関係を直接 assert（一方を書き換えるともう一方とズレてテストが赤くなる）。
    4. **§4 検証要件** — 再現テスト fail-then-pass / 黙って受理しない / `pytest -q` 緑 / `ruff check .` clean / `ccd guard --template B --allowed <file> tests/` HALT-free（R3＝本番 diff サイズ上限あり）。
    5. **§5 許可ファイル集合** — `<file>` ＋ `tests/` の 2 つのみ、`<file>` 以外の `ccd/` 配下のすべての本番コードを禁止。
    6. **§6 出力先** — `_ai_workspace/bridge/outbox/result_auto_NNN.md`（A と同形）。
    7. **§7 メタ情報** — provenance（signature・parser・case・exception）、別名前空間・AI 不使用。
  - **`_parser_dotted_to_file`** — dotted-name → 源ファイル path 変換（`ccd.protocol.parse_spec` → `ccd/protocol.py`）。失敗時（空 / dot なし / 空セグメント）は `""` を返してテンプレ B の fit-check で halt。
- **`ccd/profile.py` 拡張** — `safety.fix_templates` 段階有効化（spec_024）。
  - **`SafetyConfig.fix_templates: list[str] = ["A"]`** — `["A"]`（既定）/ `["A", "B"]` / `["B"]` を受ける、空リスト / 重複 / 未知の文字（`"Q"`）は ValueError。`KNOWN_FIX_TEMPLATES = ("A", "B")` を module-level constant に。
  - **既定 `["A"]`** ── 新規プロファイルは template B 無効、operator が明示的に `["A", "B"]` に書き換えてから B が走る（论点1 tier × §9.7 リスク傾斜の実装）。
  - **`render_profile` 拡張** — `[safety]` セクションに `fix_templates = ["A"]` を追加。
- **`ccd/nightly.py` 拡張** — テンプレ B 経路 + R5 検証 + ガード設定。
  - **`AdversarialRechecker` 型 + seam** — テンプレ B の R5 verification。`(repo, parser, case_name) → "graceful_error" | "graceful_success" | "ungraceful" | "unknown"` の 4 値分類。**`"graceful_error"` のみ R5 pass** ── `"graceful_success"`（黙って受理、spec_024 §3 禁止）と `"ungraceful"`（まだ壊れている）と `"unknown"`（parser/case が見つからない、保守的に halt）はすべて R5 失敗。
  - **`_default_adversarial_rechecker`** — `ccd.adversarial.default_cases()` から fixture を再構成して `default_parsers()` の named parser を in-process で呼ぶ。`GRACEFUL_EXCEPTIONS` → `graceful_error`、`UNGRACEFUL_OVERRIDES`（UnicodeError 系）→ `ungraceful`、その他 `Exception` → `ungraceful`、例外なし → `graceful_success`。実 mutmut / 実 claude / live-repo write 一切なし。
  - **`_run_auto_fix_loop` 拡張** — `fix_templates: tuple[str, ...]` を引数に。`_select_candidate` が priority order（A→B）で候補を選ぶ ── A 有効かつ候補あれば A、なければ B 有効かつ候補あれば B。**A 優先**: test-only は構造的に安全、B 候補は朝レポートに retained。
  - **`_select_template_b_candidate`** — 候補解決順序: ① adversarial channel outcome の `report_json_path` → ② `<repo>/_ai_workspace/discover/discover_*.json` のうち `channel="adversarial"` を持つ最新。`findings` リストを舐めて pre-filter（parser / case_name / exception_type / file 全部非空）を通る最初の 1 件。
  - **`_latest_discover_json(want_channel=...)`** — disk fallback が channel を判別。mutation JSON（top-level `channel` キーなし）と adversarial JSON（`channel: "adversarial"`）を取り違えない ── これがないと sequence number が高い adversarial JSON が誤って mutation 候補解決の入力になりうる（`test_disk_fallback_distinguishes_mutation_vs_adversarial_json` で pin）。
  - **テンプレ別の allowed_files / R3** — テンプレ A は `_AUTO_FIX_ALLOWED_FILES_A = ("tests/",)` 固定、テンプレ B は `[finding.file, "tests/"]` 動的、`guard_inspector(..., template=template)` で R3（本番 diff サイズ上限）が template="B" 時のみ enforced。
  - **`_verify_r5` template-aware** — テンプレ A は `mutation_rechecker(...)` → `"killed"`、テンプレ B は `adversarial_rechecker(...)` → `"graceful_error"`。各テンプレが自分の R5 を呼ぶ。
  - **HALT 文言の固定 anchor 拡張** — `_HALT_R5_FAILED_B`（"adversarial case did not become a graceful error"）/ `_HALT_R5_FAILED_B_SILENT`（"parser silently accepted the broken input"、spec_024 §3 明示）/ `_HALT_NO_CANDIDATE`（template-A-only の歴史的 anchor を保持、A+B の場合は `_compose_no_candidate_reason` が "template-A or template-B" を生成）。
  - **`AutoFixOutcome.template`** ── `"A"` / `"B"` を埋める（spec_023 で常に `"A"` だったのを動的化）。
- **`ccd/cli.py` 拡張** — `main()` / `_cmd_nightly` シグネチャに `adversarial_rechecker: Any | None = None` を追加。既存 5 seam (`fix_dispatcher` / `suite_runner` / `mutation_rechecker` / `guard_inspector` / `git_ops`) と同じ流儀で `ccd nightly` に forward。CLI からは `cli.main(..., adversarial_rechecker=...)` で注入、production CLI 利用では自動的に default が使われる。`auto-fix:` stdout 行の shape は不変。
- **`tests/test_translate.py` (+15 件、31 件 → 46 件)** ── テンプレ B happy path / verbatim constraints / dict input / 採番 / 決定性 / 降格（parser/case/exception missing、unparseable parser）/ `parse_spec` round-trip / `_parser_dotted_to_file` 4 known parsers + 4 broken shapes / A→B sequence sharing。
- **`tests/test_nightly.py` (+12 件、34 件 → 46 件)** ── テンプレ B の gate（A only でスキップ）/ happy path（B merge）/ R5 silent_success halt / R5 ungraceful halt / guard R3 halt with B template / guard R1 halt with named-file allowed-set / A priority over B / B fallthrough when no A / B-only no-candidate / disk fallback by channel / 1晩1候補 / default adversarial rechecker 4 分類。
- **`tests/test_profile.py` (+7 件、26 件 → 33 件)** ── `fix_templates` default `["A"]` / `["A", "B"]` enable / unknown letter ValueError / empty list ValueError / duplicate ValueError / `[safety]` render に `fix_templates = ["A"]` / 両テンプレ enable で render に `["A", "B"]`。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.13.0` → **`0.14.0`**（**新機能 = minor bump**、spec §2-5）。
- `tests/test_smoke.py::test_version_is_0130` → **`test_version_is_0140`**、assert を `0.14.0` に。

### Constraints (spec §3)

- **触ってよい**: `ccd/translate.py`（テンプレ B 追加）、`ccd/nightly.py`（テンプレ B 経路 + adversarial_rechecker seam）、`ccd/profile.py`（`SafetyConfig.fix_templates`、`KNOWN_FIX_TEMPLATES`）、`ccd/cli.py`（adversarial_rechecker seam forwarding）、`tests/test_translate.py` / `tests/test_nightly.py` / `tests/test_profile.py` / `tests/test_smoke.py`、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`。
- **触っていない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief,guard}.py` のコアロジックは **1 行も変更していない**（spec §3 で明示）。再利用は import 経由のみ（`run_channel` / `dispatch_with_retry` / `inspect_diff` / `translate_finding` / `parse_spec` / `MutmutRunner` / `run_discovery` / `default_parsers` / `default_cases` / `GRACEFUL_EXCEPTIONS` / `UNGRACEFUL_OVERRIDES` を読み取りのみ）。`docs/` も触らない。
- **テンプレ A（spec_023）の挙動は不変** ── spec_013〜023 の既存 427 テストはすべて green。default `Profile()` は `safety.fix_templates=["A"]` を持ち、spec_023 の loop は同じ shape で走る。
- **`spec_024` 自身（CC の今回の dispatch）はパーサのコードを書き換えない** ── ループ機構を作るだけ。実弾の `protocol.py` / `run_writer.py` の UnicodeDecodeError 漏洩は、本 spec 完了後にプロファイルを `["A", "B"]` に切り替えた `ccd nightly` の実行が（spec_auto 経由で）直す（spec §3 注）。
- 安全境界レベル2：**ローカル merge まで、push しない**。`GitOps` Protocol が push 系メソッドを持たないので構造的に push 不能（spec_023 から継承）。
- **テストで実 mutmut・実 claude を呼ばない** — テンプレ B の R5 は `_FakeAdversarialRechecker` で注入、production default は `default_parsers` / `default_cases` を import して in-process で呼ぶだけ（subprocess なし）。
- すべて**追加のみ**。ローカル commit のみ、**push しない／ブランチ操作・merge しない**（spec §3）。

## [0.13.0] — 2026-05-25

v2 Phase 2 の本丸 — spec_023。spec_021 で**インチキ修正ガード**（`ccd/guard.py`）、spec_022 で**翻訳器**（`ccd/translate.py`）が揃った次の段として、**自律修正ループを閉じる** ── 発見を無人で直す `[発見]→[翻訳]→[修正]→[検証]→[ローカルmerge]→[朝レポート]` を `ccd nightly` に組み込む（`docs/DESIGN.md §9.5/§9.7`）。

リスク傾斜（`docs/DESIGN.md §9.7`）に従い、**まずテンプレ A（ミューテーション → test-only）のみ**を自律化する ── test-only は本番コードを構造的に壊せない、最も安全な自律変更。テンプレ B（本番コードに触る）は spec_024 で、A が信用できてから点火。安全境界レベル2：**ローカル merge まで、push しない**。

ループは **プロファイル `safety.autonomous_fix` で gate される**（spec_018 → spec_023 拡張、論点1 tier）。既定 OFF — 新規プロファイルは Phase 1 挙動（discover + 朝レポート）のみ。CCD 自身のプロファイルだけ ON にしてループを点火。**1晩1候補**（论点3）── 複数 actionable があっても 1 件だけ処理、残りは朝レポートに surface。失敗時は HALT・merge せず・朝レポートに理由（论点4 layer 5、無限リトライしない）。

`AutoFixOutcome` データクラスが「skipped（gate off / 候補なし）」「dispatched + 各検証結果（R4 / R5 / guard）」「merged or halt_reason」を全て揃えて返し、朝レポート（spec_017 brief）が同じ shape を読める形を担保。

### Added

- **`ccd/profile.py` 拡張** — Phase 2 gate `SafetyConfig`。
  - **`SafetyConfig` pydantic model (extra=forbid)** — `autonomous_fix: bool = False`（既定 OFF）。docstring に「クライアント repo は OFF 既定、CCD 自身は ON に opt-in」「Phase 2 が成熟すれば `push` / cost ceilings / un-pushed backlog threshold を同じ `[safety]` 配下に追加する」を明記。
  - **`Profile.safety: SafetyConfig`** — 既存 `discovery` / `schedule` と並ぶ第 3 のセクション。デフォルトは `SafetyConfig()`、TOML の `[safety]` セクション（または完全省略）から組み立て。
  - **`render_profile`** — TOML-shaped 出力に `[safety]\nautonomous_fix = false` を追加。`_toml_bool` 補助関数も追加。
  - 既存 `test_unknown_field_raises_value_error` が `safety = "branch-only"` を未知フィールド例にしていたのを `mystery_knob = "wat"` に変更（safety は今や既知のサブセクションなので例として不適切に）。意図は同じ ── 未知フィールドは silently 落とさず `ValueError`。
- **`ccd/nightly.py` 拡張** — Phase 2 自律修正ループを `run_nightly` に組み込み。
  - **`AutoFixOutcome` dataclass (frozen)** — `skipped` / `skip_reason` / `spec_auto_id` / `spec_auto_path` / `finding_signature` / `candidate_count` / `template` / `branch` / `dispatched` / `dispatch_status` / `r5_killed` / `r4_suite_passed` / `guard_passed` / `guard_halt_reasons` / `merged` / `halt_reason`。朝レポートとテストが同じ shape を読める形に純化。
  - **`NightlyResult.auto_fix: AutoFixOutcome | None`** — gate ON のとき必ず populate（候補なしなら `skipped=True`）、gate OFF のとき `None`（spec_020 挙動を bit-for-bit 保持）。
  - **`_run_auto_fix_loop`** — `[候補選択 → 翻訳 → ブランチ作成 → dispatch → R5 → R4 → ガード → ローカル merge or HALT]` を直線的に並べた。各段で例外を吸って `halt_reason` に変換 ── 1 つの broken seam がループを crash させない（spec_020 の channel 例外吸収と同じ精神）。
  - **`_select_template_a_candidate`** — 候補解決順序: ① mutation channel outcome の `report_json_path` → ② `<repo>/_ai_workspace/discover/discover_*.json` の最大連番。`actionable` リストを舐めて、pre-filter（`file` 非空 / `line > 0` / `mutation` 非空 / `status == "survived"`）を通る最初の 1 件を返す。残りは朝レポートが拾う。
  - **6 つの注入 seam** — `fix_dispatcher` / `suite_runner` / `mutation_rechecker` / `guard_inspector` / `git_ops` / 補助の `agent_runner` / `mutation_runner`。テストは fake で注入、production は `_build_default_fix_dispatcher`（`dispatch_with_retry` を `max_attempts=1` で wrap、论点4 layer 5）、`_default_suite_runner`（`pytest -q`）、`_build_default_mutation_rechecker`（`MutmutRunner` + spec_019 iso-venv 再利用、`paths=[finding.file]` で単一ファイルに絞る、signature で同定）、`_default_guard_inspector`（`inspect_diff` 1:1）、`SubprocessGitOps`（`git checkout -b` / `git diff main..HEAD` / `git checkout main && git merge --no-ff <branch>`）。
  - **`GitOps` Protocol** — `create_and_checkout_branch` / `diff` / `merge_branch_into_main` / `checkout` の 4 メソッドのみ。**push 系メソッドを意図的に持たない** ── push したくても seam 自体が存在しないので構造的に push 不能（`test_autonomous_fix_does_not_push` で `not hasattr(gops, "push")` を pin、安全境界レベル2 を物理的に保証）。
  - **テンプレ A 限定** — `_AUTO_FIX_ALLOWED_FILES = ("tests/",)` を `guard_inspector` に固定で渡す。テンプレ B が混入する経路がない（`finding.channel != "mutation"` は `translate_finding` 側で `skipped=True` に降格、loop はそこで止まる）。`branch = f"auto/{spec_auto_id}"` 命名で git log で機械生成ブランチが一目で判別可能。
  - **HALT 文言の固定 anchor** — `_HALT_NO_CANDIDATE` / `_HALT_GUARD_HALT` / `_HALT_R5_FAILED` / `_HALT_R4_FAILED` / `_HALT_DISPATCH_FAILED` をモジュール定数化、朝レポートの集計用 anchor として再利用可能（テンプレ A の `_CONSTRAINT_*` と同じパターン）。
- **`ccd/cli.py` 拡張** — `ccd nightly` が自律修正の outcome を stdout に1行で surface。
  - `auto-fix: merged <spec_auto_NNN> (branch=auto/spec_auto_NNN, signature=...)` / `auto-fix: HALT ... — <reason>` / `auto-fix: skipped (<reason>)` の 3 形。gate OFF のときはこの行を一切出さない（spec_020 挙動を bit-for-bit 保持、`test_cli_nightly_off_profile_no_auto_fix_line` で pin）。
  - `main()` シグネチャに `fix_dispatcher` / `suite_runner` / `mutation_rechecker` / `guard_inspector` / `git_ops` の 5 つの seam を追加（既存 `channel_runner` / `brief_runner` / `windows_mirror` と同じ流儀）。CLI からの注入は `cli.main(..., fix_dispatcher=...)` 形で、production CLI 利用では自動的に default seam が使われる。
- **`tests/test_profile.py`** — 5 件の新規テスト:
  - `test_safety_default_is_autonomous_fix_off` — `Profile().safety.autonomous_fix is False`
  - `test_safety_autonomous_fix_can_be_enabled_via_toml` — `[safety]\nautonomous_fix = true` → True
  - `test_safety_unknown_subfield_raises_value_error` — `[safety]\npush = "..."` → ValueError（`extra="forbid"` を pin）
  - `test_safety_section_appears_in_render_profile` / `test_safety_section_renders_true_when_enabled` — `ccd profile` 出力に `[safety]` セクション
- **`tests/test_nightly.py`** — 16 件の新規テスト（既存 18 件 + 新規 16 件 = 34 件）:
  - **fakes**: `_FakeGitOps` / `_FakeFixDispatcher` / `_FakeSuiteRunner` / `_FakeMutationRechecker` / `_FakeGuardInspector` / `_write_mutation_discover_json` / `_autofix_profile` — テストで実 mutmut・実 claude・実 git を呼ばない（spec §3）。
  - **gate OFF**: `test_autonomous_fix_off_means_no_loop_runs` / `test_autonomous_fix_off_default_profile_no_loop` — `auto_fix is None`・seam 一切呼ばれない。
  - **happy path**: `test_autonomous_fix_happy_path_merges_locally` — translate → branch → dispatch → R5=killed → R4=passed → guard=pass → merge、全 seam が正しい引数で 1 回ずつ呼ばれる。
  - **push しない (level 2)**: `test_autonomous_fix_does_not_push` — `not hasattr(gops, "push")` を assert（seam の surface に push が無いことを構造的に pin）。
  - **HALT 分岐**: `test_autonomous_fix_halts_when_guard_halts` / `test_autonomous_fix_halts_when_r5_fails` / `test_autonomous_fix_halts_when_r4_fails` / `test_autonomous_fix_halts_when_dispatch_fails` — 4 経路すべてで merge しない・halt_reason に分類入り・後段の seam を呼ばない。
  - **1晩1候補 (论点3)**: `test_autonomous_fix_processes_exactly_one_candidate` — actionable=3 でも dispatcher 呼び出し 1 回、`candidate_count=3` で残数を朝レポート用に保持。
  - **候補解決**: `test_autonomous_fix_reads_from_channel_outcome_when_available` — channel outcome の `report_json_path` が discover dir よりも優先。
  - **降格**: `test_autonomous_fix_downgrade_when_translate_rejects` — `status="killed"` のみの actionable → pre-filter が rejects、`skipped=True`、dispatcher 呼ばれない。
  - **CLI**: `test_cli_nightly_prints_auto_fix_merged_line` / `test_cli_nightly_prints_auto_fix_halt_line` / `test_cli_nightly_prints_auto_fix_skipped_line` / `test_cli_nightly_off_profile_no_auto_fix_line` — 3 形 + OFF 不出力。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.12.0` → **`0.13.0`**（**新機能 = minor bump**、spec §2-7）。
- `tests/test_smoke.py::test_version_is_0120` → **`test_version_is_0130`**、assert を `0.13.0` に。

### Constraints (spec §3)

- **触ってよい**: `ccd/nightly.py`、`ccd/profile.py`（`SafetyConfig` / `Profile.safety` / `render_profile`）、`ccd/cli.py`（seam 追加 + `auto-fix:` stdout）、`tests/test_nightly.py` / `tests/test_profile.py` / `tests/test_smoke.py`、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`。
- **触っていない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief,guard,translate}.py` のコアロジックは **1 行も変更していない**（spec §3 で明示的に禁止）。再利用は import 経由のみ（`run_channel` / `dispatch_with_retry` / `inspect_diff` / `translate_finding` / `parse_spec` / `MutmutRunner` / `run_discovery` を読み取りのみ）。`docs/` も触らない。
- **テンプレ A 限定** — 本 spec は test-only の自律化のみ。`_AUTO_FIX_ALLOWED_FILES = ("tests/",)` が固定で `guard_inspector` に渡る経路しかなく、テンプレ B（本番コード修正）が混入する経路がない（spec §3 / §4）。
- 安全境界レベル2：**ローカル merge まで、push しない**。`GitOps` Protocol が push 系メソッドを持たないので構造的に push 不能（テストで pin）。
- **テストで実 mutmut・実 claude を呼ばない** — 6 つの seam を fake で注入、production default は subprocess wrapper（CCD 自身が `ccd nightly` を本番で走らせるときのみ使われる）。
- すべて**追加のみ**。ローカル commit のみ、**push しない／ブランチ操作・merge しない**（spec §3）。

## [0.12.0] — 2026-05-25

v2 Phase 2 の 2 本目 — spec_022。spec_021 で**インチキ修正ガード**（`ccd/guard.py`）が静的検査単独で実証された次の段として、**翻訳器** を追加。発見（`discover_NNN.json` の生存改変 1 件）を、CC に投げられる修正 spec（`spec_auto_NNN.md`）に変換する（`docs/DESIGN.md §9.5` 論点5）。本 spec はテンプレ A（ミューテーション生存改変 → test-only 修正）のみを実装 ── テンプレ B（敵対的入力 → 本番コード修正）は spec_024 の責務。

論点5 の核心は **翻訳は AI を一切使わない機械的なテンプレート穴埋め** であること。発見は Phase 1 で曖昧さゼロに絞り込まれているので grill-me で詰めるべき穴がない。翻訳ステップは「修正係に指示書を手渡す」段階 ── その指示そのものは AI が手を加えるとスコープを広げたり制約を緩めたりしうるので、純粋な機械的テンプレ穴埋めにする。本 spec の翻訳器は **AI を 1 度も呼ばない・決定的**（同じ発見 → 同じ spec_auto 本文）で、`tests/test_translate.py::test_translation_is_deterministic_same_finding_same_body` がこれを byte-identical 比較で pin。

`spec_auto_NNN` は **別名前空間**（`_ai_workspace/bridge/inbox/` に `spec_auto_` プレフィクスで置く）── 人間が grill-me で練った `spec_NNN` 連番と git 履歴・朝レポートで一目で判別できるように混ぜない。連番は inbox 内の `spec_auto_*.md` の最大 +1（存在しなければ 001）。

### Added

- **`ccd/translate.py` 新モジュール** — Phase 2 の翻訳器（テンプレ A のみ）。
  - **`translate_finding(finding, *, repo, inbox_dir=None, outbox_dir=None, channel="mutation", source_report="", today=None) -> TranslateResult`** — エントリ関数。発見 1 件 → `spec_auto_NNN.md` 1 件。`finding` は `Finding` dataclass または `discover_NNN.json` の `actionable` エントリ dict をそのまま受け取れる（`Finding.from_dict` で正規化）。`today` は決定性テスト用の注入 seam（既定 `datetime.now(UTC).date()`）。
  - **`Finding` dataclass (frozen)** — `channel` / `file` / `line` / `mutation` / `status` / `signature` / `source_report`。`from_dict(payload, *, channel, source_report)` で `discover_NNN.json` の actionable エントリから組み立てる（壊れた line 値は 0 に倒して downstream の template-fit check で halt させる ── 例外で loop を落とさない）。
  - **`TranslateResult` dataclass (frozen)** — `success` / `spec_auto_id` / `spec_auto_path: Path \| None` / `finding: Finding` / `template: str` / `halt_reason: str`。frozen なので外側から書き換え不可（`test_translate_result_is_frozen_dataclass` で pin）。
  - **テンプレ A 本文構造** — 7 セクション、すべて自己完結（標準の dispatch プロンプトでそのまま回せる）:
    1. **§1 文脈（事実）** — file:line・mutation 引用・`<old> → <new>` 分解（mutmut 既定の "→" 矢印を検出）・mutmut 出力の証拠アンカー。
    2. **§2 やってほしいこと** — このロジックを縛るテストを **1 本だけ** 書く、改変時に特定アサーションで失敗・現行 main で成功・既存テスト数 +1。
    3. **§3 制約（テンプレ A 逐語、本タスクで侵食してはならない）** — 5 つの制約を逐語で焼き込み（モジュールトップレベルの定数 `_CONSTRAINT_TEST_ONLY` / `_CONSTRAINT_EXISTING_TESTS_IMMUTABLE` / `_CONSTRAINT_NO_SKIP_MARKERS` / `_CONSTRAINT_DETERMINISTIC` / `_CONSTRAINT_ALLOWED_SET`、`test_constraint_phrases_are_verbatim` で逐語 pin）。論点5 の「指示は侵食不能な剛体」を物理的に保証。
    4. **§4 検証要件** — 改変時 fail / main で pass / `pytest -q` 全緑 / `ruff check .` clean / `ccd guard --template A --allowed tests/` で HALT しない（spec_021 の静的ガード呼び出し方を明示）。
    5. **§5 許可ファイル集合（R1 ファイル許可リスト、逐語宣言）** — 「触れてよい ＝ `tests/` のみ」を逐語で書き、触れてはならないファイル群（`ccd/` 以下・`_ai_workspace/` 以下・`docs/`・`pyproject.toml`・`.github/` 等）を列挙。`ccd guard` の `--allowed tests/` 引数と一致する宣言（呼び出し側がこれを直接読んで R1 を適用）。
    6. **§6 出力先** — `_ai_workspace/bridge/outbox/result_auto_NNN.md`（`ccd/protocol.py::_derive_result_id` の `spec_*` → `result_*` 変換と整合）。
    7. **§7 メタ情報** — 翻訳元発見の signature / channel / status / レポートを記録、`spec_auto_*` 別名前空間と AI 不使用・決定性を明記。
  - **報告専用降格** — `_why_template_a_does_not_fit(finding)` で発見がテンプレ A に収まるかチェック。`channel != "mutation"` / `status != "survived"` / `file` 空 / `line <= 0` / `mutation` 空 のいずれかで `TranslateResult(success=False, halt_reason="finding does not fit template A — downgraded to report-only: ...")` を返す（spec_auto は書き出さない）。テンプレ A は構造上常に収まるはずだが、将来テンプレ B / AI チャンネル等が増えたときの保険として明文化・実装。
  - **`_next_spec_auto_seq(inbox_dir)`** — `spec_auto_*.md` の最大連番 +1（存在しなければ 1）。人間の `spec_NNN.md` は無視（regex `^spec_auto_(\d+)\.md$` で match）。inbox 不在なら mkdir。
  - **AI なしの構造的強制** — `ccd/translate.py` は `AgentRunner` / `ClaudeCodeRunner` / `dispatch_one` / `dispatch_with_retry` を一切 import せず、関数シグネチャにも `runner: AgentRunner` 引数を持たない。`test_translator_does_not_import_any_agent_runner` がこのモジュール surface の forbidden symbol を pin。
- **`tests/test_translate.py`** — 17 テスト:
  - **happy path**: `spec_auto_001.md` 生成 / テンプレ A 全要素 / 制約逐語 / dict 入力 / `parse_spec` で parseable
  - **採番**: inbox に既存 spec_auto を置いた状態で max+1 / 連続 2 件で 001 → 002 / inbox 不在で mkdir
  - **決定性**: 同じ finding + 同じ today で 2 つの fresh inbox に書いて byte-identical / AI runner forbidden import
  - **報告専用降格**: channel `adversarial` / status `killed` / file 空 / line=0 / mutation 空、すべて success=False + halt_reason、spec_auto 未生成
  - **データクラス健全性**: `Finding.from_dict` の壊れた line 値正規化 / `TranslateResult` frozen

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.11.0` → `0.12.0`（**新機能 = minor bump**、spec §2-6）。
- `tests/test_smoke.py::test_version_is_0110` → `test_version_is_0120`、`__version__ == "0.12.0"` を assert。

### Constraints (spec §3)

- **触ってよい**: `ccd/translate.py`（新規）、`tests/test_translate.py`（新規）、`tests/test_smoke.py`（version assert）、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`。
- **触っていない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief,profile,nightly,guard,cli}.py` のコアロジックは **1 行も変更していない**（`cli.py` も含めて未変更 ── 本 spec の `translate` CLI 化は spec_023 のループ配線で同時に行う方が自然と判断、明示的な CLI が必要なら同 spec で `_cmd_translate` を追加できる）。`docs/` / `docs/data/*.json` も触らない。
- 翻訳は **AI を一切呼ばない**。純粋な機械的テンプレート穴埋め（モジュールが `AgentRunner` 系を import していないこと自体をテストで pin）。
- 生成するのは spec の "種" ではなく、そのまま dispatch できる **完全な修正 spec**（発見が曖昧さゼロなので grill-me 不要 ── §2/§3/§4/§5 が full instruction として並ぶ）。
- すべて**追加のみ**。**push しない／ブランチ操作・merge しない**（spec §3）。



## [0.11.0] — 2026-05-25

v2 Phase 2（**自律修正ループ点火**）の最初の spec — spec_021。Phase 2 は「発見を無人で直す」ループを閉じる段階で、確定事項(2)「インチキ修正の危険」（自律修正係がテストを消す/assert を緩めることで失敗を消す）を退治することが第一の責務。Phase 1 が「発見ファースト」だったのと同じ精神で、Phase 2 は **ガードファースト** ── インチキ修正ガードを**先に・単独で**実装し、crafted な diff（インチキ diff・正当な diff）で実証してから、ループ本体を点火する。

`ccd/guard.py` を新規追加し、**git diff だけを検査する強制層**として実装。`result_NNN.md` やエージェントの自己申告は一切読まない（diff は事実、申告は主張）。`ccd guard` サブコマンド（11個目）で人間が手動でブランチ diff を検査でき、Phase 2 の次の spec（loop wiring）から再利用可能なエントリ関数 `inspect_diff(*, diff, allowed_files, template, max_prod_diff_lines)` を提供。**自律修正ループ自体はまだ作らない** ── ガード単独の証明だけが本 spec のスコープ。

### Added

- **`ccd/guard.py` 新モジュール** — Phase 2 のインチキ修正ガード（静的検査層）。論点6 の R1〜R3 を実装、R4（既存スイートが緑）・R5（標的テストが撃破）は**動的検査なので本 spec では扱わない**（spec_023 のループ配線で合成）。
  - **`inspect_diff(*, diff, allowed_files, template, max_prod_diff_lines=60) -> GuardResult`** — エントリ関数。unified diff テキストを引数で受け取る（git に依存しない、テストで crafted diff を直接食わせられる）。`template` は "A"（tests/ のみ）or "B"（本番1ファイル + tests/）。`allowed_files` は呼び出し側が宣言する許可ファイル集合（ファイル/ディレクトリプレフィクス/グロブ）。
  - **R1（ファイル許可リスト）** — diff が触れる各ファイルが `allowed_files` に含まれるか（ディレクトリプレフィクスマッチ含む）。範囲外は HALT。
  - **R2（`tests/` 追加のみ）** — 既存テストファイルの行削除/変更を禁止。`is_new_file=False` のテストファイルに `-` 行があれば HALT。テストファイル削除（`deleted file mode`）も HALT。新規 `@pytest.mark.skip` / `@pytest.mark.skipif` / `@pytest.mark.xfail` / `@unittest.skip` / `pytest.skip(...)` / `pytest.xfail(...)` マーカー（**新規テストファイル含む**）が現れたら HALT。テストファイル定義は `tests/` 配下の `.py` ファイル。
  - **R3（本番 diff 有界、テンプレB のみ）** — テンプレB の本番ファイル（非テスト）の `+` + `-` 行数合計に上限（既定 60 行、`max_prod_diff_lines` で上書き可）。超過で HALT（狭いはずの修正で大 diff = スコープ超過のシグナル）。
  - **自己保護 denylist (`DENYLIST_GLOBS`)** — `ccd/guard.py` 自身・`ccd/nightly.py`（および将来の `ccd/nightly_*.py` / `ccd/scheduler*.py`）・`.github/**`・`pyproject.toml`・`setup.py`・`setup.cfg`・`.pre-commit-config.yaml`・`_ai_workspace/discover/blocklist.txt`・`_ai_workspace/ccd_profile.toml`・`ccd_profile.toml`。**diff がこれらに触れたら、`allowed_files` に明示的に含まれていても無条件で HALT**（「番人を誰が見張るか」 ── 自律修正ループは自分自身のゲートを緩められない）。rename の場合は old_path 側もチェックして、protected ファイルを rename で逃げる経路を塞ぐ。
  - **偽陽性可・偽陰性不可** — バイナリ diff・rename・mode 変更・パース不能 diff はすべて **safe-halt**。「正当な修正を誤って止める」は朝レポートに出て人間が見るだけで安上がり、「インチキを通す」は絶対に避ける、を機械化。
  - **`GuardResult` dataclass (frozen)** — `passed` / `halt_reasons: tuple[str, ...]`（HALT 時に「どのルールがなぜ作動したか」を人間可読で全件列挙、短絡しない）/ `files_touched: tuple[str, ...]`（朝レポート向け）/ `template`。
  - **`FileDiff` dataclass (frozen)** — `_parse_diff` の出力単位。`old_path` / `new_path` / `added_lines` / `removed_lines` / `added_text` / `removed_text` / `is_binary` / `is_rename` / `is_mode_change` / `is_new_file` / `is_deleted_file`、`path` プロパティ（新側優先、削除時のみ旧側）。
  - **`fetch_diff(repo, base, head) -> str`** — `git -C <repo> diff <base>..<head>` の薄いラッパ。CLI 用、テストは `inspect_diff` を直接呼ぶ。
- **`ccd guard` サブコマンド（11 個目）** — `--repo`（既定 cwd）/ `--base`（既定 main）/ `--head`（既定 HEAD）/ `--template {A,B}`（必須）/ `--allowed PATH...`（許可ファイル集合）/ `--max-prod-diff-lines`（既定 60、R3 閾値）。stdout に `template` / `diff range` / `files touched` / `guard: pass` を出力、HALT 時は stderr に `guard: HALT (N reason(s))` + 各 halt 理由 1 行を出力。pass で exit 0、HALT で exit 1。`git diff` が失敗（不正な ref 等）したら `guard halted: git diff failed: ...` を stderr に出して exit 1（空 diff で silent pass しない）。
- **`tests/test_guard.py`** — 25+ テスト、**handcrafted unified diff** で各インチキ手口を捕まえることを実証（ガードファーストの中核証明）:
  - **正当 diff pass**: テンプレA の既存テストへの追加 / 新規テストファイル / テンプレB の小さな本番 diff + テスト追加。
  - **R2 HALT**: 既存テスト行削除 / assert 弱化（`assert x == 6` → `assert x > 0`）/ `@pytest.mark.skip` 付与 / 新規テストファイル内の `@pytest.mark.xfail` / テストファイル丸ごと削除。
  - **R1 HALT**: テンプレA で `ccd/` に触れる diff。
  - **denylist HALT**: `ccd/guard.py` / `pyproject.toml` / `.github/workflows/ci.yml` / `ccd/nightly.py` を許可集合に入れても HALT。
  - **R3 HALT**: テンプレB で本番 diff が limit 超過。
  - **R3 非適用**: テンプレA では R3 を一切走らせない（`max_prod_diff_lines=1` でも tests/ pass）。
  - **safe-halt**: バイナリ / rename / mode 変更 / 空 diff（→ pass）/ 不明テンプレート / 複数違反全件列挙 / `GuardResult` frozen 確認 / `DENYLIST_GLOBS` スモーク。
  - **CLI end-to-end (実 git)**: 正当 diff で `ccd guard` rc=0 / テスト削除で rc=1 + `R2` / denylist + 許可集合で rc=1 + `denylist` / テンプレB 大 diff で rc=1 + `R3` / 不正 ref で rc=1 + `guard halted`。実 git リポジトリを `tmp_path` 配下に init して `git diff main..HEAD` を本当に走らせる（end-to-end の唯一の git 統合点）。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.10.0` → `0.11.0`（**新機能 = minor bump**、spec §2-7）。
- `tests/test_smoke.py::test_version_is_0100` → `test_version_is_0110`、`__version__ == "0.11.0"` を assert。
- **`ccd/cli.py`** — `guard` サブパーサ追加（`--repo` / `--base` / `--head` / `--template` / `--allowed` / `--max-prod-diff-lines`）、`main()` のディスパッチに `if args.command == "guard":` 分岐、`_cmd_guard` ハンドラ追加、`import subprocess` 追加、`from ccd.guard import ...` を `from ccd.discover import ...` と `from ccd.integrate import ...` の間に挿入（alphabetical order）。既存サブコマンド（10 個）の挙動・引数・stdout は**完全に保持**（追加のみ）。

### Constraints (spec §3)

- **触ってよい**: `ccd/guard.py`（新規）、`ccd/cli.py`（`guard` サブコマンド）、`tests/test_guard.py`（新規）、`tests/test_smoke.py`（version assert）、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`。
- **触っていない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief,profile,nightly}.py` のコアロジックは **1 行も変更していない**。`docs/` / `docs/data/*.json` も触らない。
- **動的検査 R4 / R5 は混入させない**（spec §3、§6 の Open questions）── 本 spec は静的検査ガード単独の実証だけが責務、ループ配線（spec_023）で R1〜R5 を合成する。
- ガードは git diff を `subprocess` で取得するか、unified diff テキストを引数で受け取る。`result_NNN.md` 等の自己申告は読まない（実装上 `inspect_diff` のシグネチャに `diff: str` しか受け取らないことで機械的に強制）。
- すべて**追加のみ**。**push しない／ブランチ操作・merge しない**（spec §3）。



v2 Phase 1 の**最後の spec** — spec_020。発見3チャンネル（spec_013/014/019 ミューテーション、spec_015 敵対的入力、spec_016 AI推論）・朝レポート（spec_017 `ccd brief`）・プロファイル基盤（spec_018 `ccd profile`）が揃った状態で、残る「**スケジューラ骨格**」を追加。`ccd nightly` サブコマンド（10個目）と Windows タスクスケジューラ登録スクリプト・テンプレートを実装し、夜間に無人でプロファイルの有効発見チャンネルを順に走らせ、朝レポートを描画して Windows 側からも読めるパスにミラーコピーする線形オーケストレーションを完成させる。これで v2 Phase 1（発見のみ・自律修正なし）が完成する。

論点7 の完全な tick-controller 状態機械（`idle → discovering → translating → patching → ... → done_tonight`）は修正ループの多段ステージが必要で、Phase 2 の話。**Phase 1 はその線形な骨格だけ**を作る ── pre-flight 確認 → 発見チャンネル実行 → 朝レポート描画 → Windows ミラー、を `run_nightly` 1 関数で完結。発見チャンネル・brief・profile のロジックは**1 行も触らず**、`run_channel` / `run_brief` / `load_profile` を再利用するだけの組み合わせ層。

### Added

- **`ccd/nightly.py` 新モジュール** — Phase 1 の線形夜間オーケストレータ。
  - **`run_nightly(*, repo, profile=None, profile_path=None, channel_runner=None, brief_runner=None, windows_mirror=None, today=None) -> NightlyResult`** — エントリ関数。(1) プロファイル読み込み（注入 or `load_profile`）、(2) 軽 pre-flight、(3) `profile.discovery.channels` 順に `channel_runner` 呼び出し（mutation だけ `mutation_paths` を渡す、他は `paths=None`）、(4) `brief_runner` で朝レポート描画、(5) `windows_mirror` で Windows 側にコピー、(6) `NightlyResult` を返す。
  - **`NightlyResult` dataclass** — `success` / `profile` / `channels_run` (tuple[ChannelOutcome, ...]) / `brief_report_wsl` / `brief_report_windows` / `halt_reason` / `.channels_executed` property（実行した channel 名 tuple）。`success` は pre-flight + brief が成功した時のみ True ── 個別 channel halt（spec_019 のカナリア halt 等）は `success` を倒さない（operator は他チャンネルの発見と朝レポートを依然受け取れるべき）。
  - **`ChannelOutcome` dataclass** — `channel` / `success` / `halt_reason` / `report_md_path` / `report_json_path` の 5 フィールド。3 種の channel result（`DiscoveryResult` / `AdversarialResult` / `AIReviewResult`）から共通する 4 フィールドを抽出。
  - **`_pre_flight(repo) -> str`** — Phase 1 用の軽量 pre-flight。`repo` が存在しディレクトリであること、`<repo>/_ai_workspace/` を作成/書き込み可能であることのみを確認。論点7 の本格的 pre-flight（HEAD=main / クリーン / 未push バックログ閾値）は Phase 2 の責務 ── Phase 1 は発見のみで（mutation は spec_014 隔離内、adversarial は in-process tmp dir、ai は read-only、`ccd brief` は gitignore 下の `_ai_workspace/nightly/`）live リポジトリを汚さないので軽い確認で十分。docstring に Phase 2 で何を足すかを明記。
  - **`_default_mirror(report_md_path) -> Path | None`** — 既定の Windows ミラー。`$CCD_WINDOWS_MIRROR_ROOT` 環境変数 → `/mnt/c/Users/$WIN_USER/ccd-nightly/` → `/mnt/c/Users/$USER/ccd-nightly/` の順で解決、`/mnt/c` 不在なら `None`（ソフトフェイル ── WSL コピーが真）。`shutil.copy2` で markdown レポートを一個コピー。
  - **チャンネル例外の捕捉** — 1 つのチャンネルが mid-run で raise しても他チャンネル・朝レポートが止まらないよう、`_run_channels` は `except Exception` で `ChannelOutcome(success=False, halt_reason=<exc class+msg>)` に変換して続行。
- **`ccd nightly` サブコマンド（10 個目）** — `--repo`（既定 cwd）/ `--profile`（任意、TOML パス明示）。stdout に `channels executed: ...` / 各チャンネルの ok/halted 1 行 / `morning report (wsl): ...` / `morning report (windows): ...`（ミラー declined なら `(mirror declined — /mnt/c unavailable)`）。pre-flight halt または brief halt で stderr に `nightly halted: ...` + 非ゼロ終了。`cli.main(channel_runner=..., brief_runner=..., windows_mirror=...)` で 3 つの注入 seam を expose（テストで実 mutmut/実 claude/実 `/mnt/c` を踏まない）。
- **`_ai_workspace/register_nightly.ps1` Windows タスク登録テンプレート** — 論点7 確定設定:
  - タスク名 `CcdNightlyMaintenance`（既存 `AxisKnowledgeRagAutoDispatch` と別、共存可能）。
  - 毎日 `$NightlyAt`（既定 02:00、profile の `schedule.nightly_at` に揃える前提）。
  - `wsl.exe -d Ubuntu-24.04 -- bash -c "..."` で `cd <repo> && nohup setsid bash -c '. .venv/bin/activate; ccd nightly --repo <repo> >> logs/nightly_task.log 2>&1' < /dev/null > /dev/null 2>&1 &` ── **デタッチ起動**（既存 `auto_dispatch_controller.sh` のパターン、ミューテーションは数時間走るので同期実行しない）。
  - `WakeToRun` / `StartWhenAvailable` / `MultipleInstances IgnoreNew` / `AllowStartIfOnBatteries` / `DontStopIfGoingOnBatteries` / `ExecutionTimeLimit 6h`。
  - 環境依存のテンプレート（`$TaskName` / `$ProjectDir` / `$WslDistro` / `$NightlyAt` がユーザ編集ポイント）。実際の登録は人間が走らせる。
- **`tests/test_nightly.py`** — 16 テスト: 有効チャンネルだけ実行 / mutation のみ `mutation_paths` を受け取る / 全 3 チャンネル既定で動く / pre-flight halt（repo 不在 / ディレクトリでない）でチャンネル未実行 / 朝レポート描画 + Windows ミラー / ミラー `None` 返却で success 維持 / ミラー OSError swallowed / チャンネル halt が他チャンネル・brief を止めない / チャンネル例外が halt 化されて続行 / brief halt → overall halt / 決定性（同じ入力で同じ channels_executed）/ `profile_path` 指定でディスクからロード / CLI end-to-end / CLI pre-flight halt で rc=1 / CLI `--profile` フラグ honor / 実 `run_brief` を fake channel runner と組み合わせた integration / `ChannelOutcome` フィールド保持。すべて **fake runner で完結、実 mutmut/実 claude/実 `/mnt/c` を呼ばない**。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.9.1` → `0.10.0`（**新機能 = minor bump**、spec §2-5）。
- `tests/test_smoke.py::test_version_is_091` → `test_version_is_0100`、`__version__ == "0.10.0"` を assert。
- **`ccd/cli.py`** — `nightly` サブパーサ追加（`--repo` / `--profile`）、`main()` のディスパッチに `if args.command == "nightly":` 分岐、`_cmd_nightly` ハンドラ追加。`main()` シグネチャに `channel_runner` / `brief_runner` / `windows_mirror` の 3 つの注入引数を追加（既存テスト互換、すべて kw-only & 既定 None）。既存サブコマンド（dispatch / chain / report / dashboard / retrospect / discover / brief / profile / reconcile）の挙動・引数・stdout は**完全に保持**。

### Constraints (spec §3)

- **触ってよい**: `ccd/nightly.py`（新規）、`ccd/cli.py`（`nightly` サブコマンド + 注入 seam）、`_ai_workspace/register_nightly.ps1`（新規テンプレート）、`tests/test_nightly.py`、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`、`tests/test_smoke.py`。
- **触っていない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief,profile}.py` のコアロジックは **1 行も変更していない** — `run_channel` / `run_brief` / `load_profile` は import & 再利用するだけ。`docs/` / `docs/data/*.json` も触らない。
- 既存サブコマンド（9 つ）の挙動・引数・stdout は不変 — `cli.main()` シグネチャに追加した kw-only 注入引数は既定 None なので、既存呼び出しは無修正で動く。
- テストで実 mutmut・実 claude・実 git・実 `/mnt/c` を走らせない — `channel_runner` / `brief_runner` / `windows_mirror` の 3 注入 seam ですべてオフライン化。
- すべて**追加のみ**。**push しない／ブランチ操作・merge しない**（spec §3）。

## [0.9.1] — 2026-05-25

`ccd discover --channel mutation` の **0-killed 偽 survivor 問題**を修正 — spec_019。`ccd/` 全体に対するフル・ミューテーション実走で 1274 mutant 中 killed=0 / survived=1273 / suspicious=1 という結果が出ていた。309 件のテストが通っているコードベースで撃破率0%は原理的にありえず、1273件の "actionable survivor" は本物のテスト隙間ではなくツール統合のアーティファクトで、**発見レポートは丸ごと信用できない**状態だった。

### 根本原因 (PEP 660 editable install + mutmut の出力仕様の2つが噛み合った)

1. **PEP 660 editable install の MetaPathFinder** — `.venv/lib/python3.12/site-packages/__editable___cowork_cc_dispatch_0_5_0_finder.py` が `sys.meta_path` に居座り、`MAPPING = {'ccd': '/home/.../Cowork-CC-dispatch/ccd'}` で**ライブ・リポジトリの `ccd/`** を指していた。spec_014 の `_isolated_clone` ＋ `PYTHONPATH` 先頭詰めでは、PYTHONPATH ベースの `PathFinder` 解決より MetaPathFinder が優先される場面があるため、mutmut が clone 側に書いたミューテーションをテストが一切観測しない。
2. **`mutmut results` は killed mutant を出力しない** — actionable な survivor / timeout のみ列挙する仕様。spec_014 の `_parse_mutmut_results` テキストパーサは killed セクションが存在しないため `killed=0` を返してしまい、たとえ実際にはテストが多数 mutant を撃破していても CCD は「全部 survived」と報告する経路があった (`mutmut results` だけを信じるとそうなる)。

### Fixed

- **`ccd/discover.py:_provision_iso_venv` 新ヘルパ** — clone の中に専用 venv (`.ccd-iso-venv`) を `python -m venv` で建て、`pip install -e <clone> mutmut pytest` で **clone 自身** を PEP 660 editable install + mutmut/pytest を投入する。これによって iso-venv 内で `import ccd` を解決するとき、新しい editable finder の MAPPING が clone の `ccd/` を指す ── mutmut/pytest がこの iso-venv の Python で走れば、テストは clone 側のミューテーションされた `ccd` を import する (ライブ・リポジトリの finder と競合せず一意に解決される)。`--system-site-packages` は採用せず — このフラグは「親 venv ではなくシステム Python」の site-packages を継承するため mutmut/pytest が iso-venv で見つからなくなる (`pip` の wheel キャッシュが効くので再インストールのコストは数秒〜十数秒で、数時間の discover バッチに対して許容範囲)。
- **`ccd/discover.py:MutmutRunner._resolve_binary`** — iso-venv 内の `mutmut` スクリプト (`<clone>/.ccd-iso-venv/bin/mutmut`) を優先解決し、親 venv の `mutmut` (= ライブ・リポジトリの Python に繋がる) を絶対に踏まない。フォールバックは `shutil.which` で従来挙動を保つ。
- **`ccd/discover.py:_workspace_env(iso_venv_bin=...)`** — mutmut の既定ランナー `python -m pytest -x --assert=plain` は `python` を `$PATH` で解決する。iso-venv の `bin/` を先頭詰めし、`VIRTUAL_ENV` も合わせて差し替えることで、サブプロセスの `python` が iso-venv の Python に確実に解決される。
- **`ccd/discover.py:_collect_killed_mutants_from_cache`** — `mutmut results` テキストパース後に **mutmut の SQLite キャッシュ `.mutmut-cache`** から `ok_killed` ステータスの mutant 全件を `SourceFile` / `Line` テーブルと join して直接読み、`Mutant(status="killed")` レコードとして mutant リストに追加する。これで `status_breakdown` に正しい killed 数が乗り、カナリア検知が真の 0-killed 状態だけに反応する。
- **`ccd/discover.py:_detect_broken_mutation_setup` カナリア検知 (spec_019 §2-2)** — `run_discovery` が summary を組み立てた後、**mutants_total ≥ 5 かつ killed_total == 0 なら halt** する。`success=False` / `halt_reason="mutation setup is broken: canary mutant survived — 0 killed out of N mutants ..."` を返し、**discover_NNN.md / .json は書かない** (0-killed の無意味な発見レポートは下流の brief / dashboard / 将来の auto-fix loop に渡してはいけない)。309 件のテストが通っているコードベースで「5+ mutant 全部 survived」は構造的に不可能 — 「偶然 0-killed」が起こり得ない閾値で偽陽性を抑え、spec_019 の再発を機械的に防ぐ。
- **再実走確認** — `ccd discover --paths ccd/protocol.py` をフル実走し、mutmut が **131 mutant 中 killed=106 / survived=25 / timeout=0** を出すこと (撃破率 **~81%**) を確認。spec_019 以前の「kill=0 / survived=N」状態から完全に脱した (`_ai_workspace/discover/discover_003.{md,json}` に証跡)。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.9.0` → `0.9.1` (**バグ修正 = patch bump**、spec §2-4)。
- `tests/test_smoke.py::test_version_is_090` → `test_version_is_091`、`__version__ == "0.9.1"` を assert。
- **`ccd/discover.py:_ISOLATION_IGNORE`** に `_ISO_VENV_DIR_NAME` (= `.ccd-iso-venv`) を追加 — 万一 clone が clone 内に nested される運用が将来生まれた時に古い iso-venv が複製されないための防御 (現状は無害)。

### Tests added (spec_019)

- `test_canary_halts_when_many_mutants_but_zero_killed` ── 1273/0 シナリオを再現し、`success=False` / `halt_reason` に `"mutation setup is broken"` ＋ `"0 killed out of 1273"`、md/json が書かれていないことを assert。
- `test_canary_passes_when_at_least_one_killed` ── 1 件でも killed があればカナリアは反応しない (撃破率0%が **構造的** な場合だけ halt)。
- `test_canary_does_not_fire_below_threshold` ── `CANARY_MIN_MUTANTS_FOR_HALT` 未満の小さい run は素直に report を出す (ごく少数 mutant が等価変換で全て survived するケースを潰さない)。
- `test_canary_does_not_fire_for_zero_mutants` ── そもそも mutant が出なかった graceful run は halt しない。
- `test_detect_broken_mutation_setup_pure_function` ── カナリア述語の閾値挙動を直接 unit-test (閾値が将来ズレないよう assertion で固定)。
- `test_cli_canary_halt_surfaces_through_discover` ── CLI で `rc=1` / stderr に `discovery halted` ＋ `mutation setup is broken` が出ることを end-to-end で証明。
- `test_workspace_env_prepends_iso_venv_bin_to_path` / `test_workspace_env_without_iso_venv_does_not_touch_path` ── `$PATH` 先頭詰めの挙動を pin。
- `test_provision_iso_venv_creates_clone_local_python` ── 統合テスト: 実際に `python -m venv` ＋ `pip install -e .` を走らせ、iso-venv の Python が clone の `ccd` を import することを確認 (PEP 660 finder 競合の回帰防止)。
- `test_provision_iso_venv_wraps_venv_failure` / `test_mutmut_runner_returns_error_when_iso_venv_provisioning_fails` ── provisioning 失敗時に `IsoVenvProvisioningError` → `MutationRunOutcome.error` → `run_discovery` halt の経路を pin。
- `test_collect_killed_mutants_from_cache_reads_killed_rows` / `test_collect_killed_mutants_from_cache_missing_returns_empty` / `test_collect_killed_mutants_from_cache_bad_schema_returns_empty` ── SQLite キャッシュリーダの3経路 (正常 / キャッシュ無し / 壊れたファイル) を pin。
- 既存の `test_mutmut_runner_subprocess_targets_isolated_clone_not_live_repo` / `test_mutmut_runner_isolation_survives_real_git_writes_to_workspace` は `_provision_iso_venv` を monkeypatch するよう更新 (実 venv を作らず offline でテスト)。

### Constraints (spec §3)

- spec_014 の git 隔離テスト (`test_isolated_clone_simulated_mutmut_leak_does_not_pollute_live_repo` 等) は**そのまま green** — git 汚染防止は 1 mm も触っていない (clone 内 venv を建てても `.git` の隔離は変わらない、remote stripping も変わらない、try/finally による破棄も変わらない)。
- spec_013/015/016 の `--channel mutation/adversarial/ai` の挙動・出力フォーマットは**不変** — 修正は `MutmutRunner` の内部実装と `run_discovery` の追加 halt 経路 (カナリア) のみで、`run_channel` ディスパッチ / `DiscoveryResult` shape / `discover_NNN.md` テンプレートは変えていない。
- spec_017/018 の brief / profile は**完全に不変** — `ccd brief` は `discover_NNN.json` を読むだけ、`ccd profile` は profile TOML だけ。
- テストで実 mutmut を走らせない方針は維持 (`FakeMutationRunner` ベース)。§2-3 の再実走確認は実 mutmut で行い、結果は本 CHANGELOG エントリと `result_019.md` に記載。
- 触ってよい範囲: `ccd/discover.py` / `tests/test_discover.py` / `CHANGELOG.md` / `pyproject.toml` / `ccd/__init__.py` / `tests/test_smoke.py`。コアモジュール (`models` / `protocol` / `dispatch` / `chain` / `integrate` / `metrics` / `dashboard` / `run_writer` / `retry` / `backfill` / `agent` / `retrospect` / `adversarial` / `ai_review` / `brief` / `profile`) は **1 行も触っていない**。`docs/` / `docs/data/*.json` も触らない。

## [0.9.0] — 2026-05-24

v2 Phase 1 のプロファイル基盤 — spec_018。`docs/DESIGN.md §9.3` の論点1で確定したとおり、v2 のループは**初日からプロファイル駆動で設計する**。プロファイル = 対象リポジトリ・発見戦略・スケジュール、といった設定一式で、「CCD 自己保守」は「プロファイル1個のループ」、将来クライアントリポジトリを足すのは「プロファイルを足す設定作業」に落ちる。spec_018 はそのモデルとローダを `ccd/profile.py` に追加し、`ccd profile` サブコマンド (9 つ目) で実効プロファイルを表示・検証できるようにする。

**spec_018 はモデル＋ローダ＋表示用サブコマンドの追加のみ** — `ccd discover` / `brief` / `dispatch` などの既存サブコマンドは**再配線しない**。プロファイルを実際に消費して夜間実行を駆動するのはスケジューラ (spec_019) の責務。これにより spec_018 は既存挙動をゼロ変更で済む。

### Added

- **`ccd/profile.py` 新モジュール** — pydantic ベースの `Profile` モデル + TOML ローダ。
  - **`Profile`** モデル — `repo: str = "."` / `discovery: DiscoveryConfig` / `schedule: ScheduleConfig` の 3 フィールド構成。すべて既定値つき。`extra="forbid"` でスキーマ違反 (未知フィールド・誤字) を黙って無視せず明確なエラーにする。
  - **`DiscoveryConfig`** — `channels: list[str] = [mutation, adversarial, ai]` / `mutation_paths: list[str] = [ccd]`。`channels` は `KNOWN_CHANNELS` (= spec_013/015/016 の発見 3 チャンネル) に厳格に制限。
  - **`ScheduleConfig`** — `nightly_at: str = "02:00"`。`HH:MM` (00:00–23:59) 形式を field validator で検証。
  - **`load_profile(repo, path=None) -> Profile`** — 既定 `<repo>/_ai_workspace/ccd_profile.toml` を読む。ファイルが無ければ全既定値の `Profile()` を返す (graceful — プロファイル未設定でも CCD は動く)。TOML パースエラー・スキーマ違反は `ValueError` で raise (offending file path を必ず含める)。**捏造しない／黙って既定に倒さない** (spec §2-1)。
  - **`load_profile_with_source(repo, path=None) -> ProfileLoadResult`** — `ccd profile` 用。プロファイル本体に加えて、`source` (実際に読まれたパス、ファイルが無ければ `None`) と `expected_path` (常にチェック対象パス) を返す。
  - **`render_profile(result) -> str`** — `ccd profile` の出力レンダラ。TOML 互換シンタックスで実効プロファイルを描画 (operator がコピペで `ccd_profile.toml` に貼り戻せる形)、先頭コメントで「どのファイルから読んだか／既定を使ったか」を明示。
- **`ccd profile` サブコマンド (9 つ目)** — `--repo`(既定 cwd) / `--profile`(任意、TOML パス明示)。実効プロファイルを stdout に表示。プロファイル不正なら stderr に `profile error: ...` を書いて非ゼロ終了。既存サブコマンド (`dispatch` / `chain` / `report` / `dashboard` / `retrospect` / `discover` / `brief` / `reconcile`) の挙動・引数・stdout は**完全に保持**。
- **`tests/test_profile.py`** — 18 テスト: フル profile TOML 読み取り / 全フィールドが正しく入る / 既定パス & 明示パス / プロファイルファイル不在で全既定値 (graceful) / 部分プロファイル (一部フィールドのみ) は記述分が反映され残りは既定 / 不正 TOML は `ValueError` / 未知フィールド (Phase 2 `safety` 等) は `ValueError` / 未知チャンネルは `ValueError` / `nightly_at` の `HH:MM` 検証 / 型違い (list 期待で string) は `ValueError` / 既定の決定性 / 読み込みプロファイルの決定性 / `resolve_profile_path` の挙動 / CLI が既定値を表示 / CLI がロードされたファイルを表示 / CLI `--profile` の明示パス / CLI 不正で非ゼロ終了 / CLI 不正 TOML で非ゼロ終了。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.8.0` → `0.9.0` (**新機能 = minor bump**、spec §2-6)。
- `tests/test_smoke.py::test_version_is_080` → `test_version_is_090`、`__version__ == "0.9.0"` を assert。
- **`ccd/cli.py`** — `profile` サブパーサ追加 (`--repo` / `--profile`)、`main()` のディスパッチに `if args.command == "profile":` 分岐、`_cmd_profile` ハンドラを追加。既存サブコマンド (8 つ) の挙動・引数・stdout は**完全に保持**。

### Constraints (spec §3)

- **触ってよい**: `ccd/profile.py` (新規)、`ccd/cli.py` (`profile` サブコマンドのみ)、`tests/`、`CHANGELOG.md`、`pyproject.toml`、`ccd/__init__.py`。
- **触ってない**: `ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect,discover,adversarial,ai_review,brief}.py` のコアロジックは 1 行も変更していない (再利用は無し、純粋に新規モジュール追加)。`docs/` も触らない。
- TOML パースは Python 標準の `tomllib` (3.11+ 標準)。新規依存は足していない。
- **Phase 2 フィールドは予約**: `safety` (`branch-only` / `push`)、コスト境界、未push バックログ閾値等は `ccd/profile.py` の docstring に「Phase 2 で実装予定」として文書化 (本 spec では実装しない、YAGNI、§9.7 の Phase 分け)。`extra="forbid"` のおかげで Phase 2 フィールドを誤って TOML に書いた operator は明確なエラーで気付ける。
- 既存サブコマンドの挙動は不変 — spec_018 はモデル＋ローダ＋新サブコマンドの追加のみ、再配線なし (spec §2-4)。

## [0.8.0] — 2026-05-24

v2 Phase 1 の人間向け成果物 — spec_017。spec_013/014（ミューテーション）/ spec_015（敵対的入力）/ spec_016（AI推論）の発見3チャンネルが個別に出す `_ai_workspace/discover/discover_NNN.{md,json}` を **1枚の朝レポート**に集約するレンダラ `ccd brief` を追加。`docs/DESIGN.md §9.6` の朝レポート構造を Phase 1（発見のみ・自律修正なし、§9.7）に適応した6セクション (A〜F) の Markdown を `_ai_workspace/nightly/report_YYYY-MM-DD.md` に出す。

**本サブコマンドは純粋なレンダラ** — 発見チャンネル自体は走らせない（フル・ミューテーションの数時間を朝レポート生成に焼き込まない；チャンネルを走らせて朝レポートを生成する一連の自動化はスケジューラ spec_019 の責務）。集約・要約・描画のみを行い、Phase 1 不変条件「**自律修正していない**」をレポート §F に明示する。機械的発見（事実）と AI推論の所見（主張）は §B / §C で視覚的に明確に区別。

### Added

- **`ccd/brief.py` 新モジュール** — `run_brief(*, repo, inputs, brief_dir, discover_dir, today) -> BriefResult`。(1) `_ai_workspace/discover/` を走査して各チャンネル (`mutation` / `adversarial` / `ai`) の **最新の `discover_NNN.json`** を 1 件ずつ拾う（`inputs` でテスト用に明示注入も可）、(2) 拾ったペイロード群からチャンネル横断の決定的サマリ (`BriefSummary`) を Python で算出、(3) `_ai_workspace/nightly/report_YYYY-MM-DD.md` に 6 セクションの朝レポートを書く、(4) `BriefResult(success, report_path, summary, channels, halt_reason)` を返す。
- **データクラス** — `BriefResult` / `BriefSummary` / `ChannelReport`。`BriefSummary` は `channels_picked` / `channels_missing` / `mutation_actionable` / `adversarial_ungraceful` / `ai_findings` / `mechanical_findings_total` の 6 フィールド — 機械的発見（事実）と AI 所見（主張）を**別フィールドに分離**して、サマリ計算でうっかり主張を事実に混ぜないようにしている。
- **朝レポートの 6 セクション (spec_017 §2-2、Phase 1 適応版)**:
  - **A. 一行判定** — 機械的発見 N 件、AI 所見 M 件（報告専用）、一部チャンネル未実行の旨を 1 行で。
  - **B. 機械的チャンネルの発見** — `file:line` 形式の actionable mutation + パーサ × ケースの ungraceful 例外漏洩を**事実**として列挙。
  - **C. AI推論の所見 (報告専用)** — 冒頭 `> ⚠️` 引用ブロックで「主張 / 検証済み事実ではない / 非決定的 / 人間判断必須 / 自律修正の引き金にしない」を明示。§B と視覚的に明確に区別。
  - **D. halt・スキップ項目** — 未実行のチャンネル、`halt_reason` を持つチャンネル。**中身がある時だけ現れる**。
  - **E. バックログ・推移** — 機械的発見の合計件数と AI 所見数（参考）、採用した `discover_NNN.json` パス一覧。
  - **F. 起きなかったこと (正直さの節)** — **「Phase 1 は自律修正していない」を常に明示**。AI 所見を引き金にしないこと、`bridge/inbox/` への自動投入をしないこと、brief 生成では発見チャンネル自体を走らせていないことを明文化。
- **チャンネル属性の検出** — `adversarial` / `ai` の `discover_NNN.json` は top-level `"channel"` キーを持つ（spec_015 / 016 の実装）。`mutation` チャンネル (spec_013) は **`channel` キーを持たない**ので、`summary.tool` ＋ `actionable: list` の組み合わせで shape 検出。spec_017 §3「`ccd/{discover,adversarial,ai_review}.py` のコアロジックを変更しない」を守るため、JSON 側にフィールドを足すのではなく brief 側で属性を補う方針。
- **`ccd brief` サブコマンド (8 つ目)** — `--repo`（既定 cwd、`_resolve_repo`）/ `--inputs`（任意、明示する `discover_NNN.json` パス群）。stdout に `morning report: <path>` / `factual summary: mechanical=N (mutation=A, adversarial=B) ai=M (report-only)` / 未実行チャンネルがあれば `channels not yet executed: ...` を出す。サブコマンド名は `report` が `ccd report`（メトリクス）で使用済みのため `brief` に。
- **`tests/test_brief.py`** — `BriefResult` 戻り値 / 6 セクション A〜F 全部が含まれる / 機械的発見が `file:line` で列挙される / AI 所見が「報告専用・主張」を明示して §B と区別される / §F に「Phase 1 は自律修正していない」が含まれる / 同じ入力で `BriefSummary` が決定的 / `today` 注入で出力ファイル名が `report_YYYY-MM-DD.md` / 一部チャンネル未実行が graceful / 全チャンネル未実行も graceful / ゼロ件発見でも簡潔に出る / mutation channel が `channel` キー無しでも shape 検出される / 同じチャンネルで複数 seq がある場合は最新だけが採用される / `inputs=` 引数で明示注入できる / `ccd brief` CLI が end-to-end 動作 / `--inputs` フラグ動作。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.7.0` → `0.8.0`（**新サブコマンド = minor bump**、spec §2-5）。
- `tests/test_smoke.py::test_version_is_070` → `test_version_is_080`、`__version__ == "0.8.0"` を assert。
- **`ccd/cli.py`** — `brief` サブパーサ追加（`--repo` / `--inputs`）、`main()` のディスパッチに `if args.command == "brief":` 分岐、`_cmd_brief` ハンドラを追加。既存サブコマンド (`dispatch` / `chain` / `report` / `dashboard` / `retrospect` / `discover` / `reconcile`) の挙動・引数・stdout は完全に保持。

### Constraints (spec §3)

`ccd brief` は**純粋なレンダラ** — 発見チャンネル (`ccd discover`) を**走らせない**。`mutmut` / `git` / `subprocess` を使わない。`discover_NNN.{md,json}` を**読むだけ**で、`ccd/{discover,adversarial,ai_review}.py` を含むコアロジックには 1 行も触らない（spec §3）。`_ai_workspace/bridge/inbox/` への自動投入も、自動 spec 化も、自動 dispatch もしない。Phase 1 不変条件「**発見のみ・自律修正なし**」を §F で明示。既存サブコマンドの挙動は不変。

## [0.7.0] — 2026-05-24

v2 Phase 1 第三（最後）の発見チャンネル — spec_016。spec_013/014 のミューテーション・チャンネル（緑のテストが見ていない隙間を出す）、spec_015 の敵対的入力チャンネル（壊れた入力での例外漏洩を出す）に続き、**AI推論による発見チャンネル**を `ccd discover --channel ai` として追加。エージェントに `ccd/` のソースを読ませ、「ここ危なくない？」「このエラー処理抜けてない？」「関数名と実装が乖離してない？」といった**意味的・意図的な懸念**を所見として挙げさせる。機械的な道具が原理的に見つけられない種類のバグを拾える。

**ただし論点3で確定したとおり、AI推論の出力は主張でありオラクルを持たない**（再現性がなく、機械的にバグと証明できない）。よって **自律ループの引き金にはしない・報告専用チャンネル**とする。所見は人間が判断する。ミューテーション／敵対的入力が「事実→自律ループ」なのに対し、AI推論は「主張→人間判断」。信頼度で経路を分けるのが本チャンネルの設計思想。`docs/DESIGN.md §9.4` の発見3チャンネル構成の完結。

### Added

- **`ccd/ai_review.py` 新モジュール** — `run_ai_review(runner, *, repo, discover_dir) -> AIReviewResult`。`ccd/retrospect.py` と同型の構造を踏襲: (1) `ccd/*.py` を決定的に列挙（事実アンカー）、(2) `discover_NNN` 採番を取得（mutation/adversarial と共有）、(3) 自己完結したレビュー用 spec を生成（§2-3 の制約全部入り）、(4) `AgentRunner.run` を直接呼ぶ（`dispatch_one` の分類は通さない — `retrospect.py` 流、commit が無い分析タスクなので）、(5) 所見ファイル群 (`*.md`) を glob → パース → 集約、(6) `discover_NNN.md` + `.json` を書く。`FakeAgentRunner` でテスト可能、実 `claude` を呼ばない。
- **データクラス** — `AIReviewFinding(slug, location, concern, why_risky, source_file)` / `AIReviewSummary(target_package, files_reviewed, files_total, findings_total)` / `AIReviewResult(success, report_md_path, report_json_path, summary, findings, review_spec_path, findings_dir, halt_reason, runner_invoked, raw_finding_paths)`。フィールド名は spec_013 の `DiscoveryResult` / spec_015 の `AdversarialResult` と共通の `success` / `report_md_path` / `report_json_path` / `halt_reason` を揃え、CLI が 3 チャンネルを一様に扱える形に。
- **所見ファイル受け渡し方式** — エージェントが `_ai_workspace/discover/ai_review/findings_{NNN}/<slug>.md` に **1 所見 = 1 ファイル**で書く（`retrospect` の `proposals/` と同じ発想）。各ファイルは `- **Location**: \`ccd/<file>.py:<line>\`` / `- **Concern**: ...` / `- **Why risky**: ...` の bullet 形式。CCD 側は line-by-line regex で 3 フィールドを抽出。Location 欠落の所見は **落とさず `(unspecified)` で surfacing** — レビュー用 spec が「証拠アンカー必須」と命じても、エージェントが破った時は人間に見せて判断させるのが正直。
- **レビュー用 spec の制約 (spec_016 §2-3)** — エージェントへの指示に「**証拠アンカー必須**（`ccd/<file>.py:<line>` 引用、汎用アドバイス禁止）」「**捏造しない**（実在するコードだけを根拠）」「**報告のみ**（コード修正・テスト追加・spec 化禁止）」「**触れてよい範囲**（読むのは `ccd/`、書くのは `findings_dir/` だけ）」を全部本文に含める。spec body は dispatch プロンプトでそのまま回せる自己完結形式。
- **レポートでの視覚的区別 (spec_016 §2-2)** — `discover_NNN.md` 冒頭に `> ⚠️ **報告専用チャンネル**` の引用ブロック、§1 で `非決定的` を明示、§3 に「他チャンネルとの違い」セクション（mutation = 事実→自律修正可、adversarial = 事実→自律修正可、ai = 主張→人間判断）を含めて、朝レポートで開いた人間が一目で区別できる形に。
- **決定性についての正直さ (spec_016 §2-4)** — 対象パッケージ（`ccd`）/ ファイル一覧 / ファイル数は **決定的に Python で算出**し事実サマリに記載。所見件数は記録するが「非決定的・再実行で変わりうる」と明示。捏造しない — ゼロ件は捏造で埋めない。
- **`ccd discover --channel ai`** — `ccd/cli.py` の `discover` サブパーサに `ai` を選択肢として追加（既定 `mutation` 不変、`adversarial` も不変）。`run_channel` に `agent_runner: AgentRunner | None = None` パラメータを追加して AI チャンネル経路で注入できるように。`cli.main()` が `runner`（既存の `AgentRunner`）を `_cmd_discover` 経由で `run_channel` に渡す。
- **`ccd/discover.py:CHANNEL_AI`** 定数追加、`SUPPORTED_CHANNELS` を `(mutation, adversarial, ai)` に拡張。
- **`tests/test_ai_review.py`** — 22 件のテスト。レビュー用 spec が §2-3 制約全部入り（証拠アンカー / 捏造禁止 / 報告のみ）であること、ファイル一覧が spec body に埋め込まれること、`FakeAgentRunner` で end-to-end 動作、所見が `(location, slug)` で決定的にソートされること、所見ゼロ件 / エージェントが何も書かなかった場合の graceful、`ccd/` が無い repo での graceful halt、Location 欠落所見の `(unspecified)` surfacing、複数行 `Why risky` の保存、`discover_NNN` 採番が他チャンネルと共有、findings dir が `_ai_workspace/discover/ai_review/findings_NNN/` 配下、CLI `--channel ai` end-to-end、`--channel mutation`（既定）と `--channel adversarial` が不変、不正 `--channel` 拒否、`--paths` が ai では silently 無視、dataclass frozen。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.6.0` → `0.7.0`（**新チャンネル = minor bump**、spec §2-7）。
- `tests/test_smoke.py::test_version_is_060` → `test_version_is_070`、`__version__ == "0.7.0"` を assert。
- **`ccd/discover.py:run_channel`** — `agent_runner` パラメータを追加、`channel == "ai"` 経路を追加（lazy import で `ccd.ai_review.run_ai_review` を呼ぶ）。既存の `mutation` / `adversarial` 経路は完全に不変。
- **`ccd/cli.py:_cmd_discover`** — シグネチャ `(args, runner)` → `(args, mutation_runner, agent_runner)` に変更（内部関数なので外部影響なし）、`run_channel` に `agent_runner` を渡す、表示分岐に `ai` チャンネル節を追加（`target=` / `files=` / `findings= (non-deterministic)` ＋ 各 finding の `location — concern`）。spec_013 / 015 の mutation / adversarial 経路の stdout フォーマットは**完全に保持**。`--channel` の help string に `ai` 説明を追記。

### Constraints (spec §3)

`ccd discover --channel ai` は**報告専用**。発見されたクラッシュ・隙間とは違い、AI 推論の所見は**自律修正の引き金にしない**（人間判断必須）。`_ai_workspace/bridge/inbox/` への自動投入、自動 spec 化、自動 dispatch は**一切しない**（`ccd retrospect` の human-in-the-loop 規律と同じ）。spec_013 / 014 / 015 のミューテーション・敵対的入力チャンネルの挙動・出力・既存テストは全件 green を維持。`AgentRunner` 抽象は再利用するだけで変更しない（`FakeAgentRunner` でテスト、実 `claude` は呼ばない）。レビュー用 spec が課す制約（証拠アンカー必須 / 捏造禁止 / コード修正禁止）はプロンプト本文に明示的に含まれる。すべて追加のみ — 既存サブコマンド・関数の挙動は不変。

## [0.6.0] — 2026-05-24

v2 Phase 1 第二の発見チャンネル — spec_015。spec_013 / 014 で実装した**ミューテーション・チャンネル**（緑のテストが見ていない隙間を出す）に並べて、**敵対的入力チャンネル**を `ccd discover --channel adversarial` として追加。「現実に起きうる壊れた入力で CCD のパーサが無様にクラッシュする」箇所を発見する（`docs/DESIGN.md §9.4`）。CCD は spec / result / run JSON を大量に読むのでパース境界が広く、緑のテストでは観測しにくい頑健性バグを、**吟味済み固定リスト**を本物のパーサに食わせて炙り出す。自律修正は Phase 2 — 本 spec は発見のみ。

### Added

- **`ccd/adversarial.py` 新モジュール** — `run_adversarial(*, repo, discover_dir, parsers, cases) -> AdversarialResult`。固定 18 ケース × CCD 本物のパーサ 4 系統を in-process で評価し、各（パーサ × ケース）を **graceful**（許可リスト例外でクリーンに拒絶 or 成功）/ **ungraceful**（許可リスト外の例外漏洩 = 発見）に決定的に分類、発見レポート (`_ai_workspace/discover/discover_NNN.md` + `.json`) を書き出す。一時ファイルは `tempfile.TemporaryDirectory(prefix="ccd_adversarial_")` 内に閉じ、live リポジトリには discover_NNN レポートのみが残る。
- **対象パーサ 4 系統** — `ccd.protocol.parse_spec` / `ccd.protocol.parse_result` / `ccd.run_writer.load_records` / `ccd.run_writer.reconcile_run_file`。これらは**再利用・観察するだけで変更しない**（spec §3 — 発見クラッシュの修正は Phase 2）。`reconcile_run_file` は読み取り後に書き戻すパスを含むが、一時ファイルに食わせるので live 側への書き戻しは構造的に発生しない。
- **吟味済み固定 18 ケース** — 現実に起きうる壊れ方の curated set:
  1. `01_empty_file` — 0 バイト
  2. `02_whitespace_only` — 空白だけ
  3. `03_truncated_spec_mid_body` — spec が途中で切れた
  4. `04_truncated_json_mid_value` — JSON が値の途中で切れた
  5. `05_invalid_utf8_bytes` — 不正 UTF-8 バイト列
  6. `06_utf8_bom_prefix` — UTF-8 BOM 先頭
  7. `07_null_bytes_in_middle` — 途中に null バイト
  8. `08_spec_missing_title_heading` — タイトル見出し無し
  9. `09_result_missing_status_header` — Status 行無し
  10. `10_result_invalid_status_value` — 未知の Status enum 値
  11. `11_json_trailing_garbage` — JSON 末尾にゴミ
  12. `12_json_unclosed_brace` — 閉じ括弧無し
  13. `13_json_records_not_a_list` — `records` が文字列
  14. `14_json_record_field_type_mismatch` — record の `started_at` が数値
  15. `15_yaml_like_frontmatter_garbage` — `---` 風 frontmatter が壊れた YAML
  16. `16_extremely_long_field_value` — 1 フィールドに ~256 KiB
  17. `17_png_bytes_as_spec` — PNG ヘッダバイト
  18. `18_unknown_future_schema_version` — 未知の `version` 番号
- **graceful 許可リスト** — `ValueError` / `pydantic.ValidationError` / `json.JSONDecodeError`（`ValueError` 派生）/ `FileNotFoundError`。CCD の既存パーサが構造エラーを表現する型に一致。それ以外の例外漏洩は ungraceful = 発見扱い。
- **`ccd discover --channel {mutation,adversarial}`** — `ccd/cli.py` の `discover` サブパーサに `--channel` 引数を追加。既定 `mutation`（**spec_013 挙動 完全不変**）、`--channel adversarial` で敵対的入力チャンネルを実行。チャンネルの振り分けは `ccd/discover.py:run_channel` に集約（mutation は `run_discovery`、adversarial は `run_adversarial` をディスパッチ）。
- **`ccd/discover.py` の channel 定数** — `CHANNEL_MUTATION` / `CHANNEL_ADVERSARIAL` / `DEFAULT_CHANNEL` / `SUPPORTED_CHANNELS`。argparse の `choices` と `--channel` 既定値、`run_channel` のディスパッチで共有して typo 防御。
- **`tests/test_adversarial.py` — 27 件** — 固定リストの吟味（18 ケース、unique、stable order、各「壊れ方」が網羅されている）/ 対象パーサ 4 系統の確認 / **graceful 分類**（`ValueError` / `JSONDecodeError` / `pydantic.ValidationError` / 成功）/ **ungraceful 分類**（`AttributeError` / `KeyError` / `IndexError` / `TypeError` / `RecursionError` / `UnicodeDecodeError`、パラメタライズで 5 件）/ 許可リストが spec の §2-2 と一致 / 本物パーサ統合（固定リスト × 4 パーサ、`UnicodeDecodeError` が全パーサで PNG / invalid_utf8 ケースで発見される）/ 事実サマリ決定性（同じ入力で同じ数値、findings 順序も決定的）/ 発見レポート md / json の内容 / discover_NNN 採番が mutation チャンネルと共有 / **一時ファイルが live リポジトリに書かれない**（live 配下の追加ファイルは `discover_NNN.{md,json}` 限定、`.bin` リーク無し）/ tmp ディレクトリが終了時に掃除される / CLI 既定が `mutation`（spec_013 挙動 不変）/ `--channel mutation` 明示 / `--channel adversarial` end-to-end / 不正な `--channel` を argparse `choices` で拒否 / `--paths` は adversarial では無視される。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.5.1` → `0.6.0`（**新チャンネル = minor bump**、spec §2-6）。
- `tests/test_smoke.py::test_version_is_051` → `test_version_is_060`、`__version__ == "0.6.0"` を assert。
- **`ccd/cli.py:_cmd_discover`** — channel スイッチを追加。`run_channel(channel, repo, paths, mutation_runner)` を呼び、結果型でディスプレイを分岐（mutation は mutmut 系の `mutants=` / `actionable:` 出力、adversarial は `parsers=` / `cases=` / `evaluations=` / `ungraceful:` 出力）。spec_013 のテストが期待する mutation 経路の stdout フォーマットは**完全に保持**。`--paths` は mutation 専用（adversarial では `run_channel` で破棄される）。
- **`ccd/discover.py`** — 新規定数 `CHANNEL_MUTATION` / `CHANNEL_ADVERSARIAL` / `DEFAULT_CHANNEL` / `SUPPORTED_CHANNELS`、新規エントリ関数 `run_channel`。既存の `run_discovery` / `MutmutRunner` / `MutationRunner` / `FakeMutationRunner` のシグネチャ・挙動は無変更（spec_013 / 014 の既存テストは全件 green を維持）。

### Constraints (spec §3)

`ccd discover --channel adversarial` は**発見のみ**。発見されたクラッシュの修正は Phase 2（Phase 1 ではコアパーサ `ccd/protocol.py` / `ccd/run_writer.py` / `ccd/models.py` には触らない）。固定リストは**吟味済み・現実に起きうる壊れ方**を満たす curated set で、ランダムファズではない（同じ入力で同じ findings、再現可能）。一時ファイルは `tempfile.TemporaryDirectory` に閉じ、live リポジトリには discover_NNN レポートのみが書かれる。ハング検出は対象外 — in-process でタイムアウトを設けず、例外漏洩のみを観測する（spec §6、無限ループ系の壊れ入力は本 spec の対象外）。`mutation` チャンネルの挙動・出力・既存 CLI は完全に不変（既定 `--channel mutation` で spec_013 挙動を保つ）。

## [0.5.1] — 2026-05-24

v2 Phase 1 安全性修正 — spec_014。spec_013 で実装した `ccd discover` を `ccd/` 全体でフル実走したところ、mutmut の in-place 改変が CCD のテスト隔離を破り、git 操作が**実 CCD リポジトリに漏れ出して `main` に迷子コミット (`impl spec_100`) を作る**事故が発生した（push はされず、復旧済み）。発見ステップは副作用ゼロでなければならない。`MutmutRunner` が mutmut を**隔離された使い捨てコピー**上で実行するよう修正し、実リポジトリ・その `.git`・ブランチ・`origin` リモートが**構造的に影響を受け得ない**状態にする。発見レポートは引き続き実リポジトリの `_ai_workspace/discover/` に書く（隔離されるのは mutmut の実行だけ）。「live モード」オプションは設けない（footgun）。

### Fixed

- **発見ステップの隔離欠陥（spec_014）** — `MutmutRunner.run` は mutmut の subprocess 呼び出し（`mutmut run` / `mutmut results` / `mutmut show <id>`）を**全て**新規追加した `_isolated_clone(repo)` コンテキスト内で実行するようになった。隔離環境は tmp 配下に `shutil.copytree` で作る独立した使い捨てコピーで、(a) live の `ccd/` が in-place 改変されない、(b) 改変がテスト隔離を破って git 書き込み（commit/branch/checkout/push）が漏れても、コピー側の独立した `.git`（remote 全削除済み）に閉じ込められて実リポジトリに到達しない、(c) 終了時（成功・失敗・例外）に try/finally で確実に破棄される、(d) `PYTHONPATH` を隔離コピーで先頭詰めにして pytest が必ずコピー側の `ccd/` を import する（editable install が live を指していてもコピー優先）、の 4 条件を満たす。

### Added

- **`_isolated_clone(src) -> Path`** — `@contextmanager`。`tempfile.mkdtemp` で作った tmp ルート配下に `shutil.copytree` で src を複製し、`_strip_git_remotes` で全リモートを削除して yield、終了時に tmp ルートごと `shutil.rmtree(..., ignore_errors=True)`。除外パターン (`_ai_workspace`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mutmut-cache`, `.venv`, `venv`, `build`, `dist`, `*.egg-info`, `node_modules`) で重い／不要な階層をスキップ。`git clone --local` ではなく `copytree` を選んだのは「ディスク上の現状（未コミット編集を含む）に対する mutation testing」を保つため。
- **`_strip_git_remotes(clone)`** — clone 配下に `.git` がなければ no-op、あれば `git -C <clone> remote` で列挙して 1 つずつ `remote remove`。git 不在は `FileNotFoundError` で握りつぶし（gracefully no-op）。
- **`_workspace_env(workspace) -> dict[str, str]`** — `os.environ.copy()` に `PYTHONPATH=<workspace>(:<orig>)` を頭詰めで返す。mutmut が走らせる pytest が必ず**コピー側の** `ccd/` を import するようにする継ぎ目（spec_014 §2-1 (d) — editable install が live を指していてもコピー優先）。
- **隔離証明テスト 9 件 — `tests/test_discover.py`**:
  - `test_isolated_clone_simulated_mutmut_leak_does_not_pollute_live_repo` — **spec §2-4 のコア証明**。tmp 配下に実 git リポジトリを作り、`_isolated_clone` の中で実際の `git commit` / `git branch` を**漏らす操作**を実行し、その後 live 側の HEAD・log・ブランチ・remote が**バイト一致で不変**であることを assert。
  - `test_isolated_clone_strips_origin_remote_so_push_has_no_target` — clone 側の remote が空集合になる。
  - `test_isolated_clone_cleans_up_on_exception` — with ブロック内で例外を投げても workspace が削除される（try/finally）。
  - `test_isolated_clone_excludes_heavy_or_unsafe_dirs` — `_ai_workspace` / `.venv` / `__pycache__` / `*.egg-info` / `.mutmut-cache` が clone に含まれない。
  - `test_isolated_clone_captures_uncommitted_edits` — copytree なので未コミット編集も clone に乗る（git clone --local 不採用の根拠）。
  - `test_strip_git_remotes_is_a_noop_without_dot_git` — `.git` 無しでもクラッシュしない。
  - `test_workspace_env_prepends_pythonpath` / `test_workspace_env_works_when_pythonpath_unset` — `PYTHONPATH` の先頭詰め＆既存パスの保持。
  - `test_mutmut_runner_subprocess_targets_isolated_clone_not_live_repo` — `subprocess.run` をモンキーパッチして、`MutmutRunner.run` が mutmut binary を呼ぶ全 subprocess の `cwd` が live ではなく `ccd_discover_iso_*` 配下であること＋`PYTHONPATH` が workspace 詰めであることを検証。
  - `test_mutmut_runner_isolation_survives_real_git_writes_to_workspace` — `MutmutRunner` レベルでの **§2-4 端末証明**。モンキーパッチした「悪意ある mutmut」が cwd で実際に `git commit` するシナリオで live 側不変を assert。

### Changed

- **`MutmutRunner.run`** — mutmut の 3 つの subprocess (`run` / `results` / `show <id>`) を全て `with _isolated_clone(repo) as workspace` の中で実行。`cwd` を `workspace` に固定、`env` を `_workspace_env(workspace)` で構築（`PYTHONPATH` 先頭詰め）。`_show` のシグネチャは `(binary, repo, mid)` → `(binary, workspace, env, mid)` に変更（内部メソッド、外部影響なし）。
- `pyproject.toml` / `ccd/__init__.py` version `0.5.0` → `0.5.1`（安全性 patch bump）。
- `tests/test_smoke.py::test_version_is_050` → `test_version_is_051`、`__version__ == "0.5.1"` を assert。

### Constraints (spec §2-3, §3)

`ccd discover` は**常に**隔離環境で実行する。「live のワーキングツリーで走らせる」オプションは設けない（footgun）。CLI 引数 (`--repo` / `--paths`) と出力フォーマット (`_ai_workspace/discover/discover_NNN.md` + `.json`) は spec_013 と完全に不変 — 挙動（隔離）の修正のみで、API/出力は無変更。`FakeMutationRunner` ベースの spec_013 既存 22 テストは引き続き green（fake は実際の mutation を行わないので隔離不要・無関係に動く）。コアロジック (`ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect}.py`) と `ccd/cli.py` は無変更。実 `mutmut` も隔離機構の subprocess (git) もテストでは tmp_path に閉じる（spec §3）。

## [0.5.0] — 2026-05-24

v2 Phase 1 第一弾 — spec_013。**ミューテーションテスト・チャンネルを `ccd discover` サブコマンド**として実装。`ccd` 自身のコードに小さな改変 (mutant) を仕込み、テストが捕まえるか試す。生き残った改変 = テストの隙間を、**安定な署名 (`file:line:mutation`) 付き**で発見レポートに列挙する。スケジューラ・他チャンネル (敵対的入力／AI推論)・自律修正は含まない (後続 spec / Phase 2)。手で叩いて発見の信号対雑音比を実データで見るための最小スライス。

### Added

- **`ccd/discover.py` 新モジュール** — `run_discovery(runner, *, repo, paths, discover_dir) -> DiscoveryResult`。注入された `MutationRunner` でミューテーションツールを起動 → `Mutant` リストに正規化 → 決定的な事実サマリ算出 (`DiscoverySummary`: mutant 総数 / status 内訳 / 生存数 / ファイル別生存数 / blocklist 内訳 / actionable 内訳) → `_ai_workspace/discover/blocklist.txt` で actionable / blocklisted に分割 → 発見レポート (`_ai_workspace/discover/discover_NNN.md` + `.json`) を書き出す。
- **`MutationRunner` プロトコル + `MutmutRunner` (subprocess) + `FakeMutationRunner` (テスト用)** — `AgentRunner` と同型の差し替え可能境界。`MutmutRunner` は `mutmut run` → `mutmut results` → 各 ID に対して `mutmut show <id>` でファイル・行・改変内容を抽出し、`(file, line, mutation)` の**安定署名**を持つ `Mutant` を返す。mutmut 不在・タイムアウト・パース失敗は `MutationRunOutcome.error` を立てて graceful (`run_discovery` 側でクラッシュなく halt)。
- **`ccd discover` サブコマンド (7 つ目)** — `--repo` / `--paths`。`main()` への mutation_runner 注入は dispatch/chain/retrospect と同じ形 (テストで `FakeMutationRunner` を渡せる)。生成された discover_NNN.md / .json のパス、事実サマリの要点、actionable mutant の `file:line` 一覧を stdout に表示。
- **`tests/test_discover.py` — 19 件** — 発見レポート生成 / 事実サマリの決定性 / actionable リスト出力 / **blocklist 適用 (signature マッチで actionable から blocklisted に移る)** / blocklist 不在の graceful 処理 / `discover_NNN` 自動採番 (既存ファイル温存) / discover ディレクトリ自動作成 / mutant ゼロ / 生存ゼロ / **mutation tool 失敗の graceful halt** / `--paths` フラグ配線 / CLI 経由 end-to-end / mutmut 出力パーサ (results・show、ID リスト・範囲・unified diff)。
- **`[project.optional-dependencies] maintain`** — `mutmut>=2.4,<3` を新規依存グループとして追加。dev (ruff/pytest) と分けた。`pip install -e ".[maintain]"` で実行可能 (`ccd discover` 実行時のみ必要、コード import・`pytest` は mutmut 無しで通る)。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.4.0` → `0.5.0` (新機能 = minor bump)。
- `tests/test_smoke.py::test_version_is_040` → `test_version_is_050`、`__version__ == "0.5.0"` を assert。

### Constraints (spec §3)

`ccd discover` は発見のみ。**自律修正は一切しない** (Phase 2)。スケジューラ・他チャンネル (敵対的入力／AI推論) も含まない (後続 spec)。生成される actionable リストは「テストの隙間の候補」であり、blocklist への追記は **人手** (エージェントによるトリアージ・自動追記は Phase 2)。mutmut 不在・実行失敗・パース失敗は graceful (クラッシュ・トレースバック禁止)。コアロジック (`ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill,agent,retrospect}.py`) は再利用のみで無変更 — discover は薄いオーケストレーション + 注入可能 runner で完結。

## [0.4.0] — 2026-05-24

v1.8 — spec_012。`ccd retrospect` を追加。`ccd` の dispatch 履歴・`result_*.md`・直近 git 履歴をエージェントに読ませ、「spec → dispatch → fix ループのどこが非効率か」を**定性的に分析**させ、改善提案を `_ai_workspace/retro/proposals/<slug>.md` に 1 ファイルずつ出力する最初のフィードバック経路。集計データを「読み返す」経路を作り、`ccd` 自身の改善ループの種にする。提案の自動 spec 化・自動 dispatch はしない (human-in-the-loop)。

### Added

- **`ccd/retrospect.py` 新モジュール** — `run_retrospect(runner, *, repo, runs_dir, limit, retro_dir)`。証拠収集 (`collect_evidence` → run JSON 群 + `result_*.md` + `git log --oneline -N` & 直近 `--stat`) → 決定的な事実サマリ算出 (`FactualSummary`: dispatch 総数・status 内訳・failure_category 内訳・result ファイル数・直近 commit 数) → レビュー用 spec 生成 (`_ai_workspace/retro/retro_spec.md`) → `AgentRunner.run` 経由でエージェントに分析させる → エージェントが書いた `_ai_workspace/retro/retro_NNN.md` と `_ai_workspace/retro/proposals/*.md` の有無で成否を判定。`retro_NNN.md` の番号は既存ファイルの最大値+1 で自動採番。
- **`ccd retrospect` サブコマンド (6 つ目)** — `--repo` / `--runs-dir` / `--limit N`。`main()` への runner 注入は既存 dispatch/chain と同じ形 (テストで `FakeAgentRunner` を渡せる)。生成された review spec / retro 本体 / 各 proposal のパスを stdout に表示する。
- **`tests/test_retrospect.py` — 13 件** — 証拠収集 / レガシー `logs/*_run.json` 対応 / 事実サマリの決定性 / 生成された review spec の内容 (証拠パス + §3 制約文 + 出力先) / エージェントが retro+proposals を書いたとき success=True / proposals 複数ファイル対応 / `retro_NNN` 自動採番 / **履歴ゼロでも graceful (runner を呼ばず success=False + halt_reason)** / 失敗ケース (retro 不在 / proposal 不在) / CLI 経由の end-to-end / `--runs-dir` / `--limit` フラグ。

### Changed

- `pyproject.toml` / `ccd/__init__.py` version `0.3.0` → `0.4.0`（新機能 = minor bump）。
- `tests/test_smoke.py::test_version_is_030` → `test_version_is_040`、`__version__ == "0.4.0"` を assert。

### Constraints (spec §3)

retrospect は提案を出すだけ。`_ai_workspace/bridge/inbox/` への自動投入も自動 dispatch も**しない**。生成される提案は spec の "種" (フル spec は書かない、grill-me 規律を保つ)。すべての指摘は特定の run/result/commit を証拠として引用させる (生成された review spec に「証拠アンカー必須」「捏造しない」を明記)。コアロジック (`ccd/{models,protocol,dispatch,chain,integrate,metrics,dashboard,run_writer,retry,backfill}.py`) は再利用のみで無変更 — `AgentRunner` 抽象を再利用し、retrospect は薄いオーケストレーション層に留めた。

## [0.3.0] — 2026-05-23

v1.7 — spec_011。リトライ／自己修復ループを完成させた。`DispatchRecord.attempts` と `metrics.retry_recovery_rate` は spec_002 以来「設計済みだが未配線」だったが、dispatch が失敗したら失敗内容（特に smoke = ruff/pytest の出力）を次の試行のプロンプトに食わせ、エージェント自身に直させるループを動かす。

### Added

- **`ccd/retry.py` 新モジュール** — `dispatch_with_retry(spec, runner, *, repo, max_attempts, smoke_commands, feedback_dir)`。`dispatch_one` を最大 `max_attempts` 回まで呼びつつ、各 DONE attempt の直後に `run_smoke` で smoke を実行し、smoke が落ちたら `smoke_failed` の失敗として扱う。retryable = `smoke_failed` / `agent_misread` / `transient` / `interrupted` / `None`（分類不能）、即 halt = `environment` / `merge_conflict` / `BLOCKED`。`subprocess.TimeoutExpired` は retryable な interrupted として捕捉してループを継続、それ以外の例外は呼び出し元（`run_chain` / `_cmd_dispatch`）へ伝播（spec_010 の `HALTED + INTERRUPTED` 経路に乗る）。
- **フィードバックファイル** — リトライ時に `_ai_workspace/logs/spec_NNN.feedback.md` を書き、前回の試行回数 / `status` / `failure_category` / smoke 出力（ruff / pytest の stdout・stderr を head+tail で抜粋）/ 前回 `result_NNN.md` の先頭 800 文字 / 「フィーチャーブランチに残っている前回の作業を土台に原因を直して再実装すること」という指示を書き込む。
- **`--max-attempts N` フラグ** — `ccd dispatch` / `ccd chain` 両方に追加。CLI 既定 **3**（意見を持つ）、ライブラリ既定 1（後方互換）。
- **`AgentRunner.run` の `feedback: Path | None` 引数** — `ClaudeCodeRunner` / `FakeAgentRunner` 双方が optional 引数として受け取り、`feedback` が `None` でなければ「前回の試行は失敗しました。`<feedback path>` を読んで原因を直してから再実装してください」をプロンプト末尾に追加。`feedback=None`（初回）のプロンプトは spec_011 以前と完全に同一。
- **`integrate.run_smoke()` 公開関数** — 旧 `_run_smoke` を公開エイリアスにリネーム。`integrate()` のロジック・返り値は無変更で、`dispatch_with_retry` が同じ smoke 実装を再利用できるようになっただけ。

### Changed

- **`dispatch_one(spec, runner, *, repo, feedback=None)`** — `feedback` 引数追加（optional、デフォルト `None`）。`runner.run` へ素通しするだけで、`_classify` / `attempts=1` の設定は無変更（最終的な attempts の値付けは `dispatch_with_retry` の責務）。docstring の "no retry — that's spec_005's concern" を実態に合わせて更新。
- **`run_chain(specs, runner, ..., max_attempts=1)`** — `max_attempts` 引数追加（optional、デフォルト 1 = 既存挙動）。spec ループ内の `dispatch_one(...)` 呼び出しを `dispatch_with_retry(..., max_attempts=...)` に差し替え。spec_010 で入れた `try/except` / `on_start` / `on_finish` / 例外時の `HALTED+INTERRUPTED` 経路はそのまま維持。
- **`FakeAgentRunner.calls`** — 3-tuple `(spec_id, workdir, feedback)` に拡張。リトライ時に feedback パスが伝播したかをテストで assert できる。既存テストの 2-tuple 比較を 3-tuple へ更新（`call[0]` での id 取り出しはそのまま）。
- `pyproject.toml` / `ccd/__init__.py` version `0.2.2` → `0.3.0`（新機能なので minor bump）。

### Fixed

- **`metrics.retry_recovery_rate` が 0/0 のまま動かない** — `DispatchRecord.attempts` フィールドと `first_pass_rate` / `retry_recovery_rate` は最初から設計されていたが、`dispatch_one` が `attempts=1` をハードコードしていたため `attempts>1` のレコードが生まれず、`retry_recovery_rate` の分母は永遠に 0 だった。`dispatch_with_retry` の導入で実データが入る（`attempts==1 and DONE` → `first_pass_rate` 分子、`attempts>1 and DONE` → `retry_recovery_rate` 分子）。`metrics.py` のロジックは無変更で、入力に attempts>1 の record が混ざるだけで実値が出る。

## [0.2.2] — 2026-05-23

v1.6 patch — spec_010。オーケストレータの例外で run JSON が消える穴と、プロセスが死んだとき in-flight の dispatch が失なわれる穴を塞いだ。失敗は捏造せず、観測した事実だけを `HALTED` / `INTERRUPTED` として残す。

### Added

- **`FailureCategory.INTERRUPTED`** — 「`ccd` は dispatch を開始したが、オーケストレータの死／未処理例外／`--timeout` 超過により終端ステータスを観測できなかった」を表す失敗カテゴリ。末尾追加のみ、後方互換。`metrics.py:_failure_taxonomy` は `for cat in FailureCategory` で回しているので集計に自動追従。
- **`ccd reconcile <path|dir>` サブコマンド** — 指定 run JSON ファイル、または `<dir>` 配下の `*.json` 全部の `RUNNING` record を `HALTED + INTERRUPTED` に変換。`finished_at` は `None` のまま（実際の終了時刻は不明 — 捏造した所要時間を `_duration_stats` に渡さない）。
- **`--timeout SECONDS` フラグ** — `dispatch` / `chain` の per-spec runner timeout（既定 `None` = 無制限、現状の挙動維持）。超過は `subprocess.TimeoutExpired` として `HALTED + INTERRUPTED` の record になる。
- **`MetricsReport.running`** — `RUNNING` record を `done` / `partial` / `failures` のどれにも入れず独立カウント。`render_report()` に `- Running: <n>` 行追加。「まだ終わってない」を「失敗した」と分類するのは spec_009 が正した不正直さと同種なので回避。
- **`ccd/run_writer.py` 新モジュール** — `RunWriter` クラス（in-flight `RUNNING` マーカー + atomic `os.replace` でのインクリメンタル書き込み + 起動時の孤児 RUNNING 自動 reconcile + carry-forward）、`reconcile_run_file()` / `reconcile_path()` 関数、`halted_interrupted_record()` ヘルパ。

### Changed

- **クラッシュ安全な永続化**: `cli.py:_save_run`（末尾1回書き込み）を廃止し、`RunWriter` が runner 呼び出しの**前**に `RUNNING` マーカーを、**後**に最終 record を書く形式に。chain は spec を 1 件処理するたびに run JSON を書き直すので、途中でプロセスが死んでも完了済み step は必ずディスクに残る。書き込みはすべて同一ディレクトリの一時ファイル ＋ `os.replace()` で atomic。
- **例外安全**: `run_chain` / `_cmd_dispatch` が `_create_feature_branch` の `RuntimeError`、runner の `subprocess.TimeoutExpired`、その他の未処理例外を spec 単位で捕捉し、`HALTED + INTERRUPTED` の record を増分 writer で確定 → chain は halt（残りの spec は実行しない、既存方針）。「例外でラン JSON が書かれない」経路をゼロに。
- **自動 carry-forward**: `dispatch` / `chain` 起動時に `--save` 先のファイルに `RUNNING` record があれば、それを `HALTED + INTERRUPTED` に reconcile して新しいランの record リストの先頭に引き継ぐ。stderr に `salvaged N interrupted dispatch(es) from a previous run` を出す。前回ファイルに `RUNNING` が無い通常ケースでは carry-forward は起きず、挙動は現状と同一。
- **ダッシュボードのカバレッジ注記文言を v1.6 に更新** (`_render_quality_note`)。`ccd` が中断 dispatch を `HALTED` / `INTERRUPTED` として記録するようになったこと、残る構造的死角は (a) `ccd` が開始する前に落ちたケース と (b) bash bridge 時代の履歴データ のみ、を明示。「完全網羅」とは書かない正直さは維持。`docs/data/*.json` は無変更で `docs/index.html` のみ再レンダリング。
- `pyproject.toml` / `ccd/__init__.py` version `0.2.1` → `0.2.2`。

### Fixed

- **穴1 — 例外でラン全体が消える**: `subprocess.TimeoutExpired`、`_create_feature_branch` の git エラー、`FileNotFoundError` などが `dispatch_one` → `run_chain` → `_cmd_chain` の誰にも catch されず、`_save_run` に到達せずラン JSON が書かれなかった。timeout した spec も、その前に完走した spec も全部消える挙動を修正。
- **穴2 — プロセス自体が死ぬ**: `_save_run` がラン末尾で 1 回だけ呼ばれていたため、`ccd chain` が `kill -9` / OOM / 再起動で落ちると同様にラン全体が消えていた。増分・atomic 永続化 ＋ 起動時の自動 reconcile で「`ccd` が開始した dispatch は必ず観測される」状態に。



v1.5 patch — spec_009。バックフィルが書式差で取りこぼしていた result を回収し、`partial` を独立した一級ステータスとして昇格、ダッシュボードに**生存バイアス**のカバレッジ注記を追加。失敗の捏造はしない。

### Added

- **`DispatchStatus.PARTIAL`** — `partial` を正式なステータスに追加（既存値・並びは不変、後方互換）。「ほぼ完了したが軽微な未完作業（auth 待ち等）」を `done` にも失敗にも入れずに保持できる。
- **`MetricsReport.partial`** — `partial` 件数を `done` / `failures` と並べて独立カウント。成功分子（`dispatch_success_rate` / `autonomous_completion_rate` / `first_pass_rate`）には混ぜず、失敗タクソノミーや `safe_halt_rate` の分母にも入れない。
- **ダッシュボードのカバレッジ注記** — `result_*.md` を残せずに halt した dispatch は parser から構造的に観測不能であることを `_render_quality_note` で明示。ヒーロー帯下に `done` / `partial` / `failed` の色分けピル、run 一覧テーブルに `partial` 列を追加。

### Changed

- **`ccd/backfill.py` の status パーサを寛容化**: 装飾（先頭/末尾の絵文字・記号）、括弧付き接尾辞（`(...)` / 全角 `（...）`）、em-dash 以降の trailing prose を剥がし、`completed` / `complete` / `完了` を `done` に正規化。`partial` は `DispatchStatus.PARTIAL` に。ヘッダブロックに status が無い場合はドキュメント全体（YAML frontmatter 含む）を最後に走査する。
- **spec_id の探索強化**: ヘッダ・title から拾えない場合、本文中の `spec_NNN` 言及、最後の手段として `result_NNN.md` ファイル名から fallback。
- **データ再生成**: `docs/data/*.json` を更新（96 件 / 91 done + 5 partial）、`docs/index.html` 再レンダリング。トップに「カバレッジ注記」、ヒーロー下に done/partial 内訳ピル。
- `pyproject.toml` / `ccd/__init__.py` version `0.2.0` → `0.2.1`。

### Fixed

- バックフィルが既知 21 件の result を「些細な書式差」で skip していた問題（`result_002 / 012 / 016 / 019 / 024 / 030 / 031 / 032 / 034 / 036 / 037 / 040 / 046 / 049 / 051 / 054` ＋ 実務案件由来の数件）。マッピングに乗らない真に不明な status のみ skip 継続。

## [0.2.0] — 2026-05-23

v1.5 第二弾。spec_007 で実装したバックフィル / 匿名化 run JSON を入力に、ポートフォリオ用の自己完結型・静的 HTML ダッシュボードを生成する `ccd dashboard` を追加。

### Added

- **spec_008 — `ccd dashboard` 静的 HTML ダッシュボード**
  - `ccd/dashboard.py` 新規。`_ai_workspace/runs/*.json` の `RunFile` 群を読み、全 `DispatchRecord` をプール集計した `MetricsReport` から 1 ファイルの自己完結型 HTML を生成する。
  - 4 パネル: **ヒーロー帯**（自律完走率を大書 + dispatch 成功率 / 一発合格率 / リトライ回復率 / 安全停止率 / 総 dispatch 数 / 案件数 / 所要時間 mean/median）、**失敗タクソノミー**（横棒インライン SVG）、**run トレンド**（dispatch 時系列の累積率折れ線、インライン SVG）、**run 一覧テーブル**（プロジェクト/世代タグ/spec 数/done・fail/所要時間 — `<details>` で per-spec 展開、JS 不使用）。
  - チャートはすべてインライン SVG。`<script>` / `<link>` / `<iframe>` / `<img>` / `http(s)://` 参照ゼロ。テストで明示検証。
  - **データ品質の正直表示**: 世代タグ（`bash_prototype` / `ccd_native`）をチップ表示し、バックフィル由来で `attempts` / `intervention` が欠損する指標（一発合格率・リトライ回復率・自律完走率）は注記で「上限見積もり」と明示。
  - `ccd/cli.py` に `dashboard` サブコマンド追加（既定 `_ai_workspace/runs/` → `docs/index.html`、`--runs-dir` / `--output` / `--repo` で上書き可）。`dispatch` / `chain` / `report` の挙動は無変更。
  - `python -m ccd.dashboard` 直接起動も可能。
  - 標準ライブラリのみ。新規ランタイム依存なし。

### Changed

- `pyproject.toml` version `0.1.0` → `0.2.0`。
- `ccd/__init__.py` `__version__` 同上。
- `README.md`: `ccd dashboard` の使い方と GitHub Pages 公開の前提を追記。

## [0.1.0] — 2026-05-22

v1 初版。spec_001 — spec_006 で実装した dispatch / 統合 / 連鎖 / 計測 / CLI 統合を束ねたもの。

### Added

- **spec_001 — Python スケルトン + CI**
  - `ccd/` パッケージ、`pyproject.toml`、`ccd` コマンド (`pip install -e ".[dev]"` で導入)。
  - `python -m ccd --version` → `ccd 0.1.0`。
  - GitHub Actions（Python 3.11 / 3.12 で ruff + pytest）。

- **spec_002 — モデルとブリッジプロトコル**
  - `ccd/models.py`: `Spec` / `Result` / `DispatchRecord` / `DispatchStatus` / `FailureCategory` （pydantic v2）。
  - `ccd/protocol.py`: `parse_spec` / `write_spec` / `parse_result` / `write_result` — `spec_NNN.md` / `result_NNN.md` の read/write。

- **spec_003 — 単発 dispatch**
  - `ccd/agent.py`: `AgentRunner` Protocol、`ClaudeCodeRunner`（実エージェント）、`FakeAgentRunner`（テスト用）。
  - `ccd/dispatch.py`: `dispatch_one(spec, runner, repo)` — エージェント実行 → 結果分類 → `DispatchRecord` を返す。result file の有無、`status`、commit 数から `done` / `failed(agent_misread|environment|smoke_failed|...)` / `blocked` を判定。

- **spec_004 — 統合と連鎖**
  - `ccd/integrate.py`: `integrate(record, repo, branch)` — smoke (`ruff check .` + `pytest -q`) → 成功時のみ `git merge --no-ff` で `main` に取り込み。失敗時は `main` を汚さない。
  - `ccd/chain.py`: `run_chain(specs, runner, repo)` — 複数 spec を順に `dispatch_one` → `integrate`、失敗で halt し以降の spec をスキップ。

- **spec_005 — 計測**
  - `ccd/metrics.py`: `aggregate(ChainResult | Sequence[DispatchRecord]) -> MetricsReport` と `render_report(MetricsReport) -> str`。
  - 7 メトリクス: dispatch success rate / autonomous completion rate / safe halt rate / duration（mean+median）/ first-pass rate / retry recovery rate / failure taxonomy。

- **spec_006 — CLI 統合・ドキュメント**
  - `ccd/cli.py`: サブコマンド `dispatch` / `chain` / `report` を追加。直近の dispatch/chain 結果を `_ai_workspace/logs/last_run.json` に保存し、`ccd report` がそこから集計する。
  - `README.md`: 実用的な使い方ドキュメントに刷新。
  - `docs/architecture.md`: mermaid 図と各モジュールの責務一覧を追加。
  - `CHANGELOG.md`: 本ファイル。
