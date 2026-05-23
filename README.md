# Cowork-CC-dispatch

オーケストレーション基盤 — 1 つの AI エージェント（Cowork）が別の AI エージェント（Claude Code）に開発タスクを dispatch し、結果を受け取り、スモークテスト → `main` への取り込みまでを自動化する。複数 spec を連鎖実行し、結果から計測レポートを出す。

設計の全体像は [`docs/DESIGN.md`](docs/DESIGN.md)、モジュール間の流れは [`docs/architecture.md`](docs/architecture.md) を参照。

## 必要環境

- Python 3.11+
- git
- (推奨) WSL Ubuntu / Linux / macOS
- 実エージェント連携時のみ: `claude` CLI （[Claude Code](https://docs.claude.com/claude-code)）

## インストール

```bash
git clone https://github.com/kazikimaguro13/Cowork-CC-dispatch.git
cd Cowork-CC-dispatch
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

動作確認:

```bash
ccd --version          # ccd 0.2.0
ruff check .
pytest -q
```

## 使い方

CLI は 4 つのサブコマンドからなる。それぞれが内部の関数（`dispatch_one` / `run_chain` / `aggregate` + `render_report` / `dashboard.render_to`）を呼ぶだけの薄い層。

### `ccd dispatch <spec>`

1 つの spec をエージェントに投げ、結果を分類して記録する。

```bash
ccd dispatch _ai_workspace/bridge/inbox/spec_001.md
```

- spec 1 件を `ClaudeCodeRunner` で実行し、結果の `DispatchRecord` を `_ai_workspace/logs/last_run.json` に保存する。
- 終了コード: dispatch が `done` なら 0、それ以外は 1。
- `--repo PATH` で対象リポジトリ、`--save PATH` で保存先を上書き可。

### `ccd chain <specs...>`

複数 spec を順に「dispatch → integrate（smoke + merge）」で回す。途中で失敗したらそこで halt し、以降の spec は実行しない。

```bash
ccd chain \
  _ai_workspace/bridge/inbox/spec_001.md \
  _ai_workspace/bridge/inbox/spec_002.md \
  _ai_workspace/bridge/inbox/spec_003.md
```

- 各 spec ごとに `feat/<spec_id>` ブランチを切って dispatch → smoke (`ruff check .` + `pytest -q`) → `git merge --no-ff` まで実行。
- 連鎖結果は `_ai_workspace/logs/last_run.json` に保存。
- 終了コード: 全 spec 成功で 0、途中 halt で 1。

### `ccd report`

直近の `ccd dispatch` / `ccd chain` の結果から 7 つのメトリクスを集計して Markdown レポートを出力する。

```bash
ccd report
```

出力例:

```
# Metrics report

- Total specs: 6
- Done: 6
- Failures: 0

## Scoreboard

- Dispatch success rate: 6/6 (100.0%)
- Autonomous completion rate: 6/6 (100.0%)
- Safe halt rate: 0/0 (0.0%)
- Duration: mean 12.34s, median 11.20s (n=6)

## Improvement loop

- First-pass rate: 6/6 (100.0%)
- Retry recovery rate: 0/0 (0.0%)

## Failure taxonomy

- (no failures)
```

- 入力 JSON のパスは `--from PATH` で上書き可。
- 記録が無いとき: 終了コード 2、stderr に `no run record at <path>`。

### `ccd dashboard`

`_ai_workspace/runs/` 配下に溜まった run JSON 群（v1 の `_save_run` 経由 / v1.5 の `ccd.backfill` 経由のどちらでもよい）を 1 つの自己完結型 HTML に集計する。チャートはすべてインライン SVG、外部リソース参照ゼロ、JS 無し。

```bash
ccd dashboard
```

- 既定で `_ai_workspace/runs/*.json` を読み、`docs/index.html` に書き出す。
- `--runs-dir PATH` / `--output PATH` で上書き可。`--repo PATH` でリポジトリルートも上書き可。
- 4 パネル:
  1. **ヒーロー帯** — プール集計の主指標（自律完走率を大きく、横に dispatch 成功率・一発合格率・リトライ回復率・安全停止率・総 dispatch 数・案件数・所要時間 mean/median）。
  2. **失敗タクソノミー** — `FailureCategory` 別の横棒 SVG。
  3. **run トレンド** — dispatch 時系列に並べた累積 dispatch 成功率 / 自律完走率 / 一発合格率の SVG 折れ線。
  4. **run 一覧テーブル** — プロジェクト × 世代タグ × spec 数 × done/fail × 所要時間。各行は `<details>` で per-spec 明細に展開（JS 不使用）。
- 世代タグ（`bash_prototype` / `ccd_native`）は明示表示し、バックフィル由来 (`bash_prototype`) で `attempts=1` / `intervention=false` に固定されている指標は注記で「上限見積もり」と明記する。
- GitHub Pages を `main` / `docs` で公開すれば、`docs/index.html` がそのままポートフォリオページとして配信できる（公開設定はリポジトリ側で行う）。

## レイアウト

```
ccd/                       # import パッケージ（配布名は cowork-cc-dispatch）
  __init__.py              # __version__
  __main__.py              # `python -m ccd` エントリ
  cli.py                   # CLI 実装（dispatch / chain / report / dashboard）
  models.py                # Spec / DispatchRecord / Result / RunFile / 各種 enum
  protocol.py              # spec_NNN.md / result_NNN.md の read/write
  agent.py                 # AgentRunner（ClaudeCodeRunner + FakeAgentRunner）
  dispatch.py              # 1 spec を dispatch して結果を分類
  integrate.py             # smoke + merge to main
  chain.py                 # 複数 spec を連鎖実行
  metrics.py               # 7 指標の集計と Markdown レポート
  backfill.py              # 旧 result_NNN.md から匿名化 run JSON を生成（v1.5）
  dashboard.py             # 静的 HTML ダッシュボード生成（v1.5）
tests/                     # pytest
docs/
  DESIGN.md                # 設計の正典（変更しない）
  architecture.md          # モジュール構成と流れ
CHANGELOG.md
.github/workflows/ci.yml   # ruff + pytest を Python 3.11 / 3.12 で実行
```

## 開発フロー

spec → dispatch → 実装 → smoke (ruff + pytest) → merge。詳細・スコープ・計測指標は [`docs/DESIGN.md`](docs/DESIGN.md) を、モジュール間の関係は [`docs/architecture.md`](docs/architecture.md) を参照。

## ライセンス

MIT
