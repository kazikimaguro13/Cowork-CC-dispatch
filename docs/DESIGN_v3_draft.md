# CCD v3 — 夜間並列自律保守（multi-worker × 収束ループ）設計ドラフト

- **Author**: Cowork (中島)
- **Created**: 2026-06-10
- **Status**: draft（spec_042 で docs/DESIGN.md §10 に取り込む）
- **前提バージョン**: v0.20.5 / 643 tests / spec_037 まで完了

## 10.1 概要・動機

v2 の夜間ループは「1晩1候補・1 dispatch・直列」。発見チャンネル（mutation / adversarial）は
一晩に複数の候補を出すのに、修正は 1 件/晩しか進まない。v3 はこのスループット制約を
**複数候補 × 並列ワーカー × 候補ごとの収束ループ** の3点で解除する。

設計原則は1つ: **v2 の安全境界（guard 5ルール・隔離・正直メトリクス・HALT 優先）を 1mm も緩めない。**
速度は安全境界の「内側」でだけ上げる。

## 10.2 loop スキル（ralph loop）との関係

Anthropic 公式 plugin **ralph-wiggum**（通称 ralph loop）は「Stop hook で Claude の終了を
ブロックし、同一セッションに同じ prompt を completion promise（自己申告の完了宣言文字列）
まで再投入し続ける」内側ループ。CCD はこれを**そのまま使わない**。採るのは**外側ループ**:

| 観点 | ralph-wiggum | CCD v3 収束ループ |
|---|---|---|
| ループ位置 | セッション内（Stop hook） | セッション外（dispatch 再起動） |
| 完了判定 | 自己申告の promise 文字列 | R5/R4/guard の**機械検証** |
| 反復間の記憶 | 同一コンテキスト継続 | feedback ファイル（retry.py 機構） |
| 暴走停止 | max-iterations のみ | iterations + wall-clock + **無進捗検知** |

理由は3つ。(a) CCD の根本原則「修正係の自己申告を一切信用しない」— promise 文字列は
guard 思想と非両立。(b) 非対話 `claude -p` dispatch なので反復ごとにコンテキストを
リセットでき、40分 wall-clock cap がそのまま効く。(c) 既存 `dispatch_with_retry`
（spec_011）の一般化で実装できる — 新概念ではなく **retry の昇格**。

ralph から借りるのは「**完了条件を満たすまで同じ仕事に戻し続ける**」という構えだけ。
判定者は AI でなく機械検証、が CCD 流。

参考: claude.com/plugins/ralph-loop / anthropics/claude-code plugins/ralph-wiggum

## 10.3 アーキテクチャ

```
discover（隔離実行、既存）
   ↓
select: top-K 候補（K = max_candidates_per_night、テンプレ A 優先 → B）
   ↓
WorkerPool（並列度 P = parallelism）
   ├─ worker 1: isolated clone + FixLoop ──→ VerifiedPatch
   ├─ worker 2: isolated clone + FixLoop ──→ VerifiedPatch
   └─ ...（K 候補を P 並列で処理）
   ↓ 完了順
Integrator（直列・lock 保持、live repo に触れる唯一の主体）
   ├─ fix_mode=auto:    patch apply → live で guard+suite 再検証 → main へ local merge
   └─ fix_mode=propose: proposals/ に保存（既存挙動）
   ↓
朝レポート（per-worker 節 + 収束/並列メトリクス）
```

**隔離の統一（最重要の構造判断）**: v2 では auto モードだけが live のワーキングツリーで
修正していた。v3 は **auto も propose 型の clone-and-patch に一本化**する。全ワーカーは
使い捨て隔離クローンの中で働き、live に触れるのは直列 Integrator のみ。これで

1. 並列ワーカー同士が live を奪い合う事故が**構造的に**起きない（論点: 排他を規約でなく構造で）
2. auto / propose のコードパスが1本になり分岐が減る
3. ワーカーのクラッシュ・暴走 git 書き込みは clone に閉じる（spec_014 の発見隔離と同じ思想）

**Integrator の規律**: 完了順に1件ずつ apply → live 再検証 → merge。apply 失敗
（先行 merge との衝突）は **rebase も再 dispatch もせず drop** して朝レポートに1行
（偽陽性は許す・偽陰性は許さない）。夜間の自動 rebase は「検証済み patch と違うもの」を
merge する行為であり、guard 思想に反する。

## 10.4 収束ループ（FixLoop）

候補ごと・clone 内で: `dispatch → R5/R4/guard 検証 → 失敗なら feedback md を書いて再 dispatch`。
打ち切り3条件（どれか到達で halt、すべて朝レポートに理由明記）:

1. **loop_max_iterations**（既定 3）
2. **候補あたり wall-clock**（既存の 40 分 cap をループ全体に適用）
3. **無進捗検知** — 失敗シグネチャ（FailureCategory + R5 失敗理由の正規化ハッシュ）が
   2 回連続同一なら、残り iteration があっても早期 halt。盲目的 ralph との差別化点であり、
   トークンの底なし消費（ralph の既知問題: 50 iterations で $50-100+）への構造的対策。

収束 = 機械検証 green のみ。`converged: bool` と `iterations: int` を record に持たせ、
v1 からの一貫テーマ「成功率は観測できた母集団の中の率」で集計する。

## 10.5 安全境界（継承 + 追加）

継承: PAUSE ファイル / 未push バックログ cap 3 / guard 5ルール / pre-flight / zero-finding 正常終了。
追加:

- **max_merges_per_night**（既定 3）— 並列化で1晩の merge 数が跳ねるのを防ぐ。バックログ
  cap と整合（1晩で cap まで埋め切らない）。
- **rate-limit 現実**: 並列 claude プロセスは同一アカウントの限度を分け合う。P 既定 2、
  上限 4。transient 失敗は FixLoop の retryable 側に落ちる（既存分類を流用）。
- **全フィールド既定値 = v2 現行挙動**（K=1, P=1, loop_max_iterations=1）。spec を merge
  しただけでは何も変わらず、有効化は operator が profile を書き換えたときのみ。

## 10.6 profile 追加（SafetyConfig）

```toml
[safety]
fix_mode = "auto"
fix_templates = ["A"]
max_candidates_per_night = 2   # K: 1..5、既定 1（v2 互換）
parallelism = 2                # P: 1..4、既定 1（v2 互換）
loop_max_iterations = 3        # 既定 1（v2 互換 = 単発 dispatch）
max_merges_per_night = 3       # 既定 3
```

## 10.7 メトリクス（正直さの継承）

- `convergence_rate` — 予算内で green に到達した候補率（母集団 = ループ起動候補数を明記）
- `iterations_to_green` 分布 — 1 で収束が多いならループは保険、2-3 が多いならループが価値
- `marginal_parallel_yield` — worker 2 以降が生んだ merge 数。**並列化が無価値なら無価値と
  数字が言う**ようにする（生存バイアス対策と同じ姿勢）
- `conflict_drop_rate` — Integrator での drop 率。高いなら候補同士が同一ファイルに集中して
  おり、K を上げる意味がない、というシグナル
- `dispatch_minutes_per_merged_fix` — コストの代理変数

## 10.8 spec 分割（spec_038〜042、各 spec 単体 merge 可・halt-on-failure 連鎖）

| spec | 内容 | 既定挙動の変化 |
|---|---|---|
| 038 | top-K 候補選択 + 直列複数候補処理 + brief 複数対応 | なし（K=1） |
| 039 | FixLoop（収束ループ + 無進捗検知）を auto/propose に配線 | なし（iterations=1） |
| 040 | 隔離統一 — auto を clone-and-patch 化、Integrator 導入 | auto の内部経路のみ（外形同一） |
| 041 | WorkerPool 並列化 + 直列 Integration queue + merge cap | なし（P=1） |
| 042 | メトリクス + dashboard + DESIGN §10 / README / CHANGELOG 同期 | なし |

順序の根拠: 040（隔離統一）が 041（並列）の前提。039 は 040 と独立だが、ループを先に
入れておくと 040 の検証パスがループ込みで一本化できる。

## 10.9 技術記事への接続

v3 で記事の柱が1本増える: 「**ralph loop を信用しない形で取り込む** — 自己申告 promise を
機械検証に置換した外側ループ」+「並列化の限界効用を自分のメトリクスで測る（marginal_parallel_yield）」。
v1〜v2 の「メトリクスが嘘をついた→根治」の続編として一貫する。
