# Cowork-CC-dispatch

**AI エージェントが別の AI エージェントに開発タスクを委譲し、実装・検証・統合・計測までを自律で回すオーケストレーション基盤。**

1 つのエージェント（spec を書く側）がもう 1 つのエージェント（実装する側 = [Claude Code](https://docs.claude.com/claude-code)）に開発タスクを dispatch する。`ccd` はその実行を分類し、スモークテストを通し、`main` に取り込み、結果からメトリクスを集計する。複数 spec の連鎖実行、失敗時の自己修復リトライ、オーケストレータがクラッシュしても記録を失わない耐障害性まで持つ。

**v2 では、これに加えて「夜間（週次）に無人で自分自身を保守するループ」（Loop β）を実装した** ── 発見 3 チャンネル（ミューテーション・敵対的入力・AI 推論）でテストの隙間を炙り出し、インチキ修正ガード（修正係の自己申告ではなく実 diff を機械検査）と R5/R4 検証ゲートを通った修正だけをローカル `main` に merge する。`auto` / `propose` / `off` の 3 モードを信頼度で使い分け、プロファイル 1 行で施策ごとに「どこまで自走させるか」を刻む。設計詳細と実走で炙り出された 3 つの欠陥は [`docs/DESIGN.md §9`](docs/DESIGN.md) を参照。

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
- **Loop β（夜間自律保守ループ・v2）** — 週次トリガーで `[発見] → [翻訳] → [修正 dispatch] → [検証] → [ガード] → [ローカル merge] → [朝レポート]` を無人で回す。発見 3 チャンネル（ミューテーション・敵対的入力・AI 推論）で「事実なら自律・主張なら人間」と信頼度で経路を分け、複数施策をプロファイルで巡回。
- **インチキ修正ガード（v2）** — 自律修正が「テストを消して失敗を消す」「assert を緩める」などで偽の合格を作る危険に対し、修正係の自己申告を一切信用せず**実 git diff を機械検査**する強制層。5 ルール（許可ファイル / `tests/` 追加のみ / 本番 diff 有界 / 既存スイート緑維持 / 標的テストの正しい失敗→成功）+ 3 原則（強制であって指示でない / 自己改変不能 / 偽陽性は許す・偽陰性は許さない）。
- **安全境界レベル 2 — push は人間に残す（v2）** — Loop β は「ブランチで修正 → 検証 → ローカル `main` に merge」まで無人。**`git push` はしない**。`GitOps` Protocol に push 系メソッドを構造的に持たせず、コードが push したくても呼ぶ先がない。朝、人間が diff を見て手動 push する。

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
ccd --version          # ccd 0.24.0
ruff check .
pytest -q              # 682 passed
```

## 使い方

CLI は 12 のサブコマンドからなる。それぞれが内部の関数を呼ぶ薄い層。v1 系（5 個：`dispatch` / `chain` / `report` / `dashboard` / `reconcile`）は dispatch オーケストレーションと計測。v2 系（7 個：`discover` / `brief` / `profile` / `guard` / `retrospect` / `nightly` / `nightly-all`）は夜間自律保守ループ（Loop β）の各層。

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

### v2 — Loop β（夜間自律保守ループ）

v2 で追加された 7 つのサブコマンド。設計詳細は [`docs/DESIGN.md §9`](docs/DESIGN.md)、各段階の決定記録は spec_013〜029 を参照。

| サブコマンド | 役割 |
|---|---|
| `ccd discover` | 発見 3 チャンネル（ミューテーション・敵対的入力・AI 推論）。隔離クローン内で実行し、実 repo を汚染しない（spec_014） |
| `ccd brief` | 朝レポート生成。Phase 1 版（発見のみ）/ Phase 2 版（自律修正の diff・検証証拠・`git push` ワンライナー）/ 提案版（修正案 diff・`git apply` ワンライナー）の 3 版を場面で切り替え |
| `ccd profile` | プロファイル表示。`fix_mode`（`auto`/`propose`/`off`）・週次ケイデンス・対象 repo・発見チャンネル等を 1 枚で確認 |
| `ccd guard` | インチキ修正ガード単体実行。任意の diff に対し 5 ルールを機械検査（手動チェック・単独実証用） |
| `ccd retrospect` | 過去 run の振り返り集計 |
| `ccd nightly` | 単一 repo に対し Loop β を 1 回回す（discover → 翻訳 → 修正 dispatch → R5/R4 検証 → ガード → ローカル merge → brief） |
| `ccd nightly-all` | プロファイルレジストリ（`_ai_workspace/profiles/*.toml`）を読み、全施策を直列で巡回。1 施策の失敗が他施策を止めない。週次タスクが呼ぶ入口 |

**3 つのモード**（`safety.fix_mode` で施策ごとに選ぶ）:

- `auto` — 発見 → 修正 → 検証 → ガード → **ローカル merge**（適用する）。CCD 自身の自己保守用。
- `propose` — 発見 → 修正案生成 → 検証 → ガード → **レポートに diff**（適用しない、隔離クローン内で完結し対象 repo に 1 バイトも書き込まない）。クライアント施策用。
- `off` — 発見 → 報告のみ（修正案なし）。最小構成。

> **運用ステータス**: v2 全 3 フェーズ + Phase 2.5（複数施策の sweep 運用 + 沈黙失敗の構造修正）の**実装は完了**（spec_013〜041、version 0.24.0、682 tests、subcommand 12）。Phase 2.5 の実走で炙り出された **v2 設計思想由来の欠陥 6 件すべて構造修正済み**（spec_030/031/032）。タスクスケジューラ経由起動の launcher pattern 構造修正も完了（spec_033、v0.20.1）── 週次タスク登録の信頼性向上。さらに subagent fresh review SOP の運用で抽出された launcher pattern の運用品質（relocation 耐性・診断ログ・機序訂正、spec_034 v0.20.2）と修正の品質メタ評価（防護網テスト・honest 診断ログ・運用テンプレ可視化、spec_035 v0.20.3）、修正の自己整合性メタ評価（二重 activate の統合・guardrail 汎用化・診断テストの穴埋め、spec_036 v0.20.4）、冗長な disown 削除の実測決着（verify→simplify、spec_037 v0.20.5）まで反映。v3 シリーズ 1/5 として「1晩1候補」制約の解除（top-K 直列、spec_038 v0.21.0）を投入 ── 既定 K=1 で v2 外形完全一致、operator opt-in で K=2..5 を直列処理。v3 シリーズ 2/5 として **FixLoop ── 収束ループ + 無進捗検知**（spec_039 v0.22.0）を投入 ── 既定 `loop_max_iterations=1` で v2 単発と外形完全一致、operator opt-in で 1..5 イテレーションを R5/R4/guard が green になるまで繰り返す（自己申告 promise でなく機械検証で完了判定）。v3 シリーズ 3/5 として **隔離の統一 ── auto モードの clone-and-patch 化と Integrator 導入**（spec_040 v0.23.0）を投入 ── auto モードも propose と同じ使い捨て隔離クローンで fix を実行し、live への書き込みは直列 Integrator のみが行う形に統一。v3 シリーズ 4/5 として **WorkerPool ── 複数 CC dispatch の並列化と直列 Integration queue**（spec_041 v0.24.0）を投入 ── K 候補を並列度 P (1..4) のワーカープールで処理し、完了 patch を直列 Integrator に投入。`max_merges_per_night` cap + PAUSE / 未push backlog / 夜間窓 wall-clock の 4 ゲートが integration 前に再評価され、trip した残 patch は退避。既定 P=1 で spec_038〜040 と外形完全一致。複数週の無人運用による実績作りはこれからの段階。「実装完了」と「運用できる」の差は埋まっていない、というのが現在地。

## レイアウト

```
ccd/                       # import パッケージ（配布名は cowork-cc-dispatch）
  __init__.py              # __version__
  __main__.py              # `python -m ccd` エントリ
  cli.py                   # CLI 実装（12 サブコマンド：v1 5 個 + v2 7 個）
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
  retrospect.py            # 過去 run の振り返り集計（v2）
  # ── Loop β（v2、夜間自律保守ループ） ──
  discover.py              # 発見 3 チャンネル（mutation / adversarial / ai）+ 隔離クローン
  adversarial.py           # 敵対的入力テスト
  ai_review.py             # AI 推論による発見（報告専用チャンネル）
  brief.py                 # 朝レポート（Phase 1/2/提案版を場面で切り替え）
  profile.py               # プロファイル（fix_mode / 発見設定 / 週次ケイデンス）
  guard.py                 # インチキ修正ガード — diff 5 ルール機械検査
  translate.py             # 発見 → spec_auto 翻訳（AI 不使用・テンプレ穴埋め）
  nightly.py               # 単一 repo に対し Loop β を 1 回回す
  sweep.py                 # プロファイルレジストリを巡回し各 repo に nightly
tests/                     # pytest（682 tests）
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
