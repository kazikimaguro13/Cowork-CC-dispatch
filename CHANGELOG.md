# Changelog

本プロジェクトの注目すべき変更を記録する。フォーマットは [Keep a Changelog](https://keepachangelog.com/) に準ずる。

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
