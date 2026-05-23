# Cowork-CC-dispatch

**AI エージェントが別の AI エージェントに開発タスクを委譲し、実装・検証・統合・計測までを自律で回すオーケストレーション基盤。**

1 つのエージェント（spec を書く側）がもう 1 つのエージェント（実装する側 = [Claude Code](https://docs.claude.com/claude-code)）に開発タスクを dispatch する。`ccd` はその実行を分類し、スモークテストを通し、`main` に取り込み、結果からメトリクスを集計する。複数 spec の連鎖実行、失敗時の自己修復リトライ、オーケストレータがクラッシュしても記録を失わない耐障害性まで持つ。

```
spec_NNN.md ──dispatch──▶ Claude Code ──実装──▶ result_NNN.md
     │                                              │
     └────────  ccd: 分類 → smoke → merge  ◀─────────┘
                          │
            run JSON（増分・アトミック保存）─▶ metrics / dashboard
```

## 設計上の立場 — 計測の「正直さ」

このプロジェクトが一番こだわっているのは、**メトリクスが嘘をつかないこと**だ。

開発の途中、ダッシュボードが「自走率 100%」と表示した。だが原因を追うと、(1) 集計パーサが書式の揺れた結果ファイルを取りこぼし、(2) そもそも失敗した dispatch は結果ファイルを残さないため、集計対象が構造的に「成功したものだけ」になっていた（**生存バイアス**）。

そこで `ccd` は次の方針を取っている。

- 観測できていない失敗を**推測で埋めない**。代わりに「この成功率は結果を残せた dispatch の中での率」とカバレッジ注記で明示する。
- オーケストレータの例外・プロセス死で失敗が**消える経路をなくす**（後述の耐障害性）。
- 中断された dispatch は `HALTED` + `INTERRUPTED` として**観測した事実だけ**を記録する。

## 主な特徴

- **連鎖実行と自律統合** — 複数 spec を `dispatch → smoke (ruff + pytest) → merge` で順に回す。失敗したらそこで halt し、壊れたコードは `main` に乗らない。
- **自己修復リトライ** — dispatch が失敗（特にテスト失敗）したら、失敗内容を次の試行のプロンプトに食わせてエージェント自身に直させる。`--max-attempts` で試行回数を制御。
- **耐障害性** — run JSON はステップごとにアトミック（一時ファイル + `os.replace`）に書き込まれる。`subprocess.TimeoutExpired` や git エラーで落ちても、それまでの記録は失われず、中断は `INTERRUPTED` として残る。`ccd reconcile` で孤児レコードを後から畳み込める。
- **正直な計測** — 7 つのメトリクス（成功率・自律完走率・安全停止率・所要時間・失敗タクソノミー・一発合格率・リトライ回復率）と、生存バイアスを明示する静的 HTML ダッシュボード。

## 必要環境

- Python 3.11+
- git
- (推奨) WSL Ubuntu / Linux / macOS
- 実エージェント連携時のみ: `claude` CLI（[Claude Code](https://docs.claude.com/claude-code)）

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
ccd --version          # ccd 0.3.0
ruff check .
pytest -q              # 192 passed
```

## 使い方

CLI は 5 つのサブコマンドからなる。それぞれが内部の関数を呼ぶ薄い層。

### `ccd dispatch <spec>`

1 つの spec をエージェントに投げ、結果を分類して記録する。

```bash
ccd dispatch _ai_workspace/bridge/inbox/spec_001.md
```

- spec 1 件を `ClaudeCodeRunner` で実行し、`DispatchRecord` を `_ai_workspace/logs/last_run.json` に保存。
- `--max-attempts N`（既定 3）で失敗時の自己修復リトライ回数、`--timeout SECONDS` で 1 試行あたりのタイムアウト、`--repo` / `--save` で対象リポジトリ・保存先を上書き可。
- 終了コード: dispatch が `done` なら 0、それ以外は 1。

### `ccd chain <specs...>`

複数 spec を順に「dispatch → integrate（smoke + merge）」で回す。途中で失敗したら halt し、以降の spec は実行しない。

```bash
ccd chain \
  _ai_workspace/bridge/inbox/spec_001.md \
  _ai_workspace/bridge/inbox/spec_002.md
```

- 各 spec ごとに `feat/<spec_id>` ブランチを切って dispatch → smoke (`ruff check .` + `pytest -q`) → `git merge --no-ff`。
- run JSON はステップごとに増分・アトミック保存され、途中でプロセスが死んでも完了済み step は残る。

### `ccd report`

直近の `dispatch` / `chain` の結果から 7 メトリクスを集計し Markdown レポートを出力する。

```bash
ccd report
```

出力例:

```
# Metrics report

- Total specs: 6
- Done: 4
- Partial: 1
- Running: 0
- Failures: 1

## Scoreboard

- Dispatch success rate: 4/6 (66.7%)
- Autonomous completion rate: 4/6 (66.7%)
- Safe halt rate: 1/1 (100.0%)
- Duration: mean 842.10s, median 798.40s (n=5)

## Improvement loop

- First-pass rate: 3/6 (50.0%)
- Retry recovery rate: 1/1 (100.0%)

## Failure taxonomy

- smoke_failed: 1 (100.0%)
```

`partial` / `running` は成功にも失敗にも数えない独立カウント。`--from PATH` で入力 JSON を上書き可。

### `ccd dashboard`

`_ai_workspace/runs/` 配下に溜まった run JSON 群を 1 つの自己完結型 HTML に集計する。チャートはすべてインライン SVG、外部リソース参照ゼロ、JS 無し。

```bash
ccd dashboard
```

- 既定で `_ai_workspace/runs/*.json` を読み、`docs/index.html` に書き出す。
- ヒーロー帯（主指標）、失敗タクソノミー、run トレンド折れ線、run 一覧テーブルの 4 パネル。
- 生存バイアスのカバレッジ注記を常時表示し、「この成功率は観測できた母集団の中での率」と明示する。
- GitHub Pages を `main` / `docs` で公開すれば `docs/index.html` がそのまま配信できる。

### `ccd reconcile <path|dir>`

中断された dispatch の孤児レコード（`RUNNING` のまま残ったもの）を `HALTED` + `INTERRUPTED` に畳み込む。プロセスがクラッシュした後の後始末に使う。

```bash
ccd reconcile _ai_workspace/logs/last_run.json
```

- ファイル 1 つ、またはディレクトリ配下の `*.json` 全部を対象にできる。
- 実際の終了時刻は不明なので捏造しない（`finished_at` は埋めない）。

## レイアウト

```
ccd/                       # import パッケージ（配布名は cowork-cc-dispatch）
  __init__.py              # __version__
  __main__.py              # `python -m ccd` エントリ
  cli.py                   # CLI 実装（dispatch / chain / report / dashboard / reconcile）
  models.py                # Spec / DispatchRecord / Result / RunFile / 各種 enum
  protocol.py              # spec_NNN.md / result_NNN.md の read/write
  agent.py                 # AgentRunner（ClaudeCodeRunner + FakeAgentRunner）
  dispatch.py              # 1 spec を dispatch して結果を分類
  retry.py                 # 自己修復リトライループ（dispatch_with_retry）
  integrate.py             # smoke + merge to main
  chain.py                 # 複数 spec を連鎖実行
  run_writer.py            # 増分・アトミックな run JSON 永続化と reconcile
  metrics.py               # 7 指標の集計と Markdown レポート
  backfill.py              # 旧 result_NNN.md から匿名化 run JSON を生成
  dashboard.py             # 静的 HTML ダッシュボード生成
tests/                     # pytest（192 tests）
docs/
  DESIGN.md                # 設計の正典
  architecture.md          # モジュール構成と流れ
CHANGELOG.md
.github/workflows/ci.yml   # ruff + pytest を Python 3.11 / 3.12 で実行
```

## 開発フロー

このリポジトリ自体が `ccd` で開発されている（ドッグフーディング）。spec を書き、`ccd` でエージェントに dispatch し、smoke を通して merge する。設計・スコープ・計測指標の詳細は [`docs/DESIGN.md`](docs/DESIGN.md)、モジュール間の関係は [`docs/architecture.md`](docs/architecture.md) を参照。

## ライセンス

MIT
