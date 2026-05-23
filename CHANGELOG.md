# Changelog

本プロジェクトの注目すべき変更を記録する。フォーマットは [Keep a Changelog](https://keepachangelog.com/) に準ずる。

## [0.2.1] — 2026-05-23

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
