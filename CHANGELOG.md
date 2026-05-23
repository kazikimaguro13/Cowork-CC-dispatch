# Changelog

本プロジェクトの注目すべき変更を記録する。フォーマットは [Keep a Changelog](https://keepachangelog.com/) に準ずる。

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
