# Cowork-CC-dispatch — 設計ドキュメント

> リポジトリ: https://github.com/kazikimaguro13/Cowork-CC-dispatch
> ステータス: v1 設計確定 / 実装前
> このファイルは新リポジトリの `docs/DESIGN.md` に配置することを想定。

## 1. 概要

Cowork-CC-dispatch は、**1つの AI エージェントが別の AI エージェントを管理して開発を進める**ためのオーケストレーション基盤である。

戦略・仕様策定側のエージェント（Cowork）が仕様書（spec）を起草して実装側のエージェント（Claude Code）へ dispatch し、実装結果（result）を受け取り、レビューと smoke test を経て `main` へ merge する。失敗時は安全に halt する。複数の spec を連鎖的に自律実行することもできる。

本リポジトリは、この仕組みを実運用してきたアドホックなシェルスクリプト群を、**設計された Python システムとして再構築し、信頼性を計測可能にする**ことを目的とする。

## 2. 背景 / 動機

本プロジェクトの前身は、Cowork（デスクトップ）と Claude Code（CLI）を組み合わせて実際の開発を回すために書かれた一連のシェルスクリプトである。spec を inbox に置き、dispatch スクリプトが CC を起動し、result を outbox に書き戻す bridge パターンで、複数機能のリリースを実際にこのフローで行ってきた。

しかし実装はアドホックなシェルスクリプトの集合で、ネストしたシェル経由のクオート処理など保守性・信頼性に課題があった。**パターン自体の有効性は日常運用で実証済み**であり、本プロジェクトはこれを設計されたシステムへ再構築し、かつ「どれだけ信頼できるか」を定量化する。

## 3. v1 スコープ

### 含むもの

- **単発ループ**: 1 spec → dispatch → CC 実装 → レビュー → `main` merge（失敗時 halt）
- **連鎖実行**: 複数 spec を順次自律実行、失敗で安全停止
- **計測**: §5 のメトリクス収集

### 含まないもの（v1 では作らない）

- 監視 UI（→ v1.5）
- 汎用エージェント・フレームワーク化
- その他の「全部盛り」。**スコープを締めることを最優先する**

## 4. 設計方針

- **言語**: Python。クリーンに新規実装する（既存 bash 版はプロトタイプ扱い）。
- **対象エージェント**: Claude Code に特化して作り込む。ただしエージェント呼び出しは差し替え可能な境界（インターフェース）として分離し、将来の拡張余地のみ残す。**v1 で汎用化は実装しない**（テスト不能な抽象化を避ける）。
- **ブートストラップ**: 既存 bash bridge を使って本リポジトリ自体の実装を CC に dispatch することは可能（プロトタイプが後継を生成する）。

## 5. 計測メトリクス

「動く」だけでなく「どれだけ信頼できるか」を数値で示す。

### スコアボード系（現状把握）

1. **dispatch 成功率** — 投げた spec のうち、そのまま merge できた割合
2. **自律完走率** — 連鎖実行で人の介入ゼロで完走したタスクの割合（**主指標**）
3. **安全停止率** — CC 失敗時に正しく halt できた割合
4. **spec あたり所要時間** — dispatch から result までの実時間

### 改善ループ系（次に何を直すか）

5. **失敗・介入の原因分類** — 失敗および人手介入を毎回カテゴリ記録（spec 不備 / CC 誤読 / smoke 失敗 / マージ衝突 / 環境 / transient）。改善対象を特定する**中核指標**。
6. **一発合格率** — 最初の result がそのままレビュー通過した割合
7. **リトライ自動復旧率** — transient 失敗を人手なしで回復できた割合

（任意）8. **spec あたりコスト** — 消費トークン / 概算金額

## 6. ロードマップ

- **v1**: §3 のスコープ（単発 + 連鎖 + 計測）
- **v1.5**: 監視 UI — v1 が出力する計測値を可視化するダッシュボード（UI は計測済みコアの下流なので順序は後）
- **v1.7**: 自己修復ループ（Loop α — dispatch 内の失敗→feedback→retry）
- **v1.8**: `ccd retrospect` — dispatch 履歴の自己レトロスペクティブ
- **v2**: 夜間自律保守ループ（Loop β）— §9 参照。Phase 1（発見だけ）→ Phase 2（自律修正点火）→ Phase 3（拡張）
- **以降**: 汎用化 / OSS プロダクト化 など

## 7. 成果物

- 本リポジトリ（設計された Python 実装 + テスト + ドキュメント）
- アーキテクチャ図
- 技術記事（設計判断と計測結果の解説）

## 8. 未決事項

- 技術記事の公開先・切り口
- Python の構成（CLI / ライブラリ、パッケージレイアウト）
- 計測 8（コスト）の採否

## 9. v2 — 夜間自律保守ループ（実装完了 / version 0.24.0）

> ステータス: 全3フェーズ + Phase 2.5（複数施策 sweep 運用 + 沈黙失敗の構造修正 + mutmut ネスト構造互換性 + launcher pattern 構造修正 + launcher pattern の運用品質向上 + 修正の品質メタ評価 + 修正の自己整合性メタ評価 + 冗長な disown 削除の実測決着）+ v3 1/5（top-K 直列、1晩1候補制約の解除）+ v3 2/5（FixLoop ── 収束ループ + 無進捗検知）+ v3 3/5（隔離の統一 ── auto モードの clone-and-patch 化と Integrator 導入）+ v3 4/5（WorkerPool ── 複数 CC dispatch の並列化と直列 Integration queue）実装完了（spec_013〜041、version 0.24.0、テスト682、サブコマンド12）。実装の記録と実走で発覚した欠陥は §9.8 を参照。

### 9.1 概要

v2 は、プロジェクトを**夜間に無人で保守するループ**を追加する。これにより本システムは2つのループを持つ:

- **Loop α（v1.7 既存）** — dispatch ごとの自己修復。1 回の dispatch の中で「失敗 → feedback → retry」が閉じる。
- **Loop β（v2 新規）** — 夜間保守ループ。スケジューラが無人で `[発見] → [翻訳] → [修正] → [検証] → [朝レポート]` を回す。

### 9.2 設計の出発点 — 2つの「動かない前提」

Loop β の設計は、先に2つの失敗モードを確定させてから始めた。

1. **発見が核** — 全部緑のテストを夜中に再実行しても何も出ない。緑のテストはコードの「見ている部分」しか保証せず、バグは必ず「見ていない隙間」に潜む。新しい問題を能動的に炙り出す機構がなければ Loop β は空回りする。
2. **インチキ修正の危険** — 自律的な修正エージェントは、テストを削除する・assert を緩めることで「失敗」を消せてしまう。修正係を信用しないガードが要る。

この2点が、フェーズ分け（§9.7）の背骨になる — **各フェーズはこのうち1つを隔離して退治する**。

### 9.3 対象と安全境界（論点1・論点2）

**対象** — Loop β の第一対象は **CCD 自身の自己保守**。ループは初日から**プロファイル駆動**で設計する（プロファイル = 対象リポジトリ・テストコマンド・発見戦略・安全設定・スケジュール等の設定一式）。「自己保守専用」は「プロファイル1個のループ」にすぎず、汎用化は後からプロファイルを足す設定作業になる。クライアント案件リポジトリは **第2ティア = 「発見＋レポートのみ・自律修正なし」** として後から opt-in で足せる構造とする。

**安全境界 = レベル2** — Loop β は「ブランチで修正 → 検証 → ローカル `main` に merge → merge 後 smoke 再チェック」までを無人で行い、**`git push` はしない**。push は翌朝、人間が diff を見て手動で行う。ルール: **人間が起点の dispatch は push 可、Loop β（無人）の dispatch は push 不可**。push は CI・公開ダッシュボード・共有履歴に触れる唯一の不可逆な操作であり、そこだけ人間に残す。

### 9.4 発見機構（論点3・論点4）

**3チャンネル構成**:

- **ミューテーションテスト** — コードに小さな改変を仕込み、テストが捕まえるか試す。生き残った改変 = テストの隙間。**自律ループの引き金**（発見が「事実」で、場所つき、検証オラクルを持つ）。
- **敵対的入力テスト** — 現実に起きうる壊れ方（途中で切れたファイル・空ファイル・文字コード違い・必須項目欠落等）の**吟味済み固定リスト**をパーサに食わせる。無様なクラッシュ = バグ。**自律ループの引き金**。
- **AI推論による発見** — エージェントにコードを読ませ「危ない箇所」を推論で挙げさせる。出力は**主張**でありオラクルを持たない（再現性なし）ため、**自律ループの引き金にはしない。報告専用チャンネル**として朝レポートに載せ、人間が判断する。

1晩に修正する候補は **1個**に限る。Loop β の成果物は、その性質上**ほとんどが「新しいテスト」**であり「バグ修正コミット」ではない（生き残り改変を殺す = テストを足す）。これはインチキ修正ガード（テストを増やすのは可）と構造的に同じ方向を向く。

**偽バグ防止 — 貫く1つのゲート**: *どの発見も、「現行 `main` で決定的に再現する失敗テストに落とし込めた」ときだけ自律ループの引き金になる。落とし込めない発見は報告専用チャンネルに回す。* ミューテーションの偽バグ = 等価改変（観測差ゼロで殺せない）、敵対的入力の偽バグ = 優雅な拒否（クラッシュではない正しいエラー）。両者ともこのゲートで自動的にふるい落ちる。

ミューテーション側の等価改変は5層で防ぐ: 生き残り改変は「タスク」でなく「候補」/ 修正前にエージェントがトリアージ / 検証ゲートが本当の防波堤（等価改変は失敗テストが原理的に書けない）/ 人間が確定する blocklist（**エージェントは提案のみ、登録は人間**）/ 1候補1試行の諦め予算。敵対的入力側は「優雅 = CCD 定義のクリーンなエラー」を厳密に定義し、固定リストで現実的・決定的に保つ。

**発見の隔離（実走で発覚した修正、2026-05-24）**: ミューテーションテストは対象コードを実際に書き換えて実行する。対象が CCD のように自分自身が git／ファイル操作を行うツールの場合、改変によってテストのリポジトリ隔離が破れ、git 書き込みが実リポジトリに漏れうる ── Phase 1 の `ccd discover` を `ccd/` 全体でフル実走した際、実際に迷子コミット（`impl spec_100`）が実リポジトリの `main` に発生した。よって発見ステップは必ず**隔離した使い捨て環境**（git worktree 等）で実行し、live のワーキングツリーでミューテーションを走らせない。隔離環境は (a) in-place 改変が live のコードに触れない、(b) 漏れた git 書き込み（commit／branch／push）が実リポジトリ・その .git・ブランチ・origin に影響しない、を満たすこと。これは §9.6 の pre-flight クリーン確認では防げない種類の汚染（発見実行中に発生する）なので、発見チャンネル実装側の責務とする（spec_014 で `ccd discover` に適用）。

### 9.5 修正ループ（論点5・論点6）

**翻訳 = AI不使用の機械的テンプレート穴埋め**。発見は §9.4 で曖昧さゼロに絞り込まれているため、grill-me で詰めるべき穴がない。翻訳器は2つの固定テンプレートに発見の事実を流し込むだけ:

- **テンプレA（ミューテーション → spec）** — test-only。生き残り改変を殺すテストを1本足す。本番コードは触らない（改変が捕まらなかった = テスト隙間であり、コードのバグではない）。
- **テンプレB（敵対的クラッシュ → spec）** — 当該パーサを優雅なエラーに直す + 再現テストを足す。

インチキ防止の制約はテンプレートに**逐語で焼き込む**。翻訳は Loop β の中で唯一 AI 判断をゼロにするステップ — 修正係に渡す指示そのものは侵食不能な剛体であるべきだから。テンプレートに収まらない発見は報告専用に降格する。自律修正 spec は `spec_auto_NNN` の**別名前空間**に置き、人間の grill-me 済み spec 系列と混ぜない。

**インチキ修正ガード** — 修正係の自己申告を一切信用せず、修正完了後・merge 前に**実際の diff** を機械的に検査する強制層。論点5 の制約が「指示」なら、論点6 のガードは「強制」。diff に対する5ルール:

1. **ファイル許可リスト** — diff が触れてよいのは spec が宣言したファイル集合のみ（テンプレA: `tests/` のみ / テンプレB: 名指しした本番ファイル1つ + `tests/`）。範囲外を1バイトでも触れば HALT。CI 設定・発見ツール設定・ガード自身は構造的に触れない。
2. **`tests/` は追加のみ** — 既存テストファイルの行は削除・変更不可。新テスト関数の追記・新規テストファイルのみ可。テスト数・assert 数は減らない、新規の skip/xfail マーカーは現れない。
3. **本番 diff は有界**（テンプレB のみ）— サイズ上限超過は HALT（狭いはずの修正で大 diff = スコープ超過のシグナル）。
4. **既存スイートは全部緑のまま** — 既存テスト群が免疫系。本番コードのガットはここで捕まる。
5. **標的テストが正しい理由で失敗 → 修正後に成功**（決定的、N回）。

3原則: ガードは指示でなく**強制**（diff という事実のみを見る）/ ガードは**自己改変不能**（ガード自身とループ本体は恒久 denylist）/ **偽陽性は許す・偽陰性は許さない**（迷ったら HALT）。

### 9.6 運用（論点7・論点8・論点9）

**スケジューリング** — 既存の実証済みパターン（tick-controller + Windows タスクスケジューラ、`auto_dispatch_controller.sh` 系）を流用する。スケジューラが30分ごとに短く controller を叩き、controller は状態機械として「claude 稼働中 → 待機 / 完了 → 前進」を判断、dispatch 本体は `nohup setsid` でデタッチ実行。Loop β はこれを WSL（Ubuntu-24.04）上で走らせ、毎晩 02:00 開始・約3時間の窓・専用タスク `CcdNightlyMaintenance`。既存パターンからの変更点5つ: push しない / 発見フェーズを前段に追加 / 毎日トリガー / pre-flight 安全確認（リポジトリがクリーンか）/ 1晩1候補。

**コスト・停止境界** — コスト制御は厳密なドル会計ではなく**構造**に置く（1晩1候補・1 dispatch・時間窓）。追加の歯止め: dispatch ごとの実時間上限40分（超過で kill）/ **未push バックログ停止** — ローカル `main` に未push の Loop β 修正が3件たまったら新規修正を止め朝レポートで promote を促す / 手動キルスイッチ（`PAUSE` ファイル）/「何も見つからない」晩は正常終了。

**朝レポート** — Loop β の唯一の人間向け成果物。日付つき Markdown、6セクション構成（一行判定 / 昨夜の自律修正 + diff 埋め込み + push コマンド / AI推論チャンネルの所見〔報告専用〕/ halt・スキップ項目 / 状態・バックログ・推移 / 起きなかったことの正直な節）。既定は簡潔・例外時のみ伸びる。WSL 側を真とし Windows 側にミラーコピー（WSL に触れず読める）。クラッシュ耐性のため夜を通して逐次書き足す。Phase 1 はファイルのみ。

#### Launcher pattern (spec_033 / spec_034 で機序訂正)

タスクスケジューラ → wsl.exe → bash -c の経路は、bash here-string の改行を
意図通り解釈しない罠を持つ ── 1 行に複数の `; echo` や `& echo` を含む
コマンドを here-string で渡すと、`& ; echo` のような構文衝突を起こして
LastTaskResult=2 で停止する。手動 `bash` では動くがタスクスケジューラ経由
だけで露見する性質を持ち、運用切替時に初めて炙り出された（v2 で 7 件目の
構造修正、Phase 2.5 完了後の運用検証で発覚）。

**機序（spec_034 で訂正）** ── タスクスケジューラ → wsl.exe → bash -c "..."
の経路で複数行 here-string を渡すと、bash は改行を `;` と解釈せず、`& `
の直後の文字列を構文エラーとして検出する（`& ; echo` のような形）。bash が
**exit code 2 で停止**し、wsl.exe はそれを Windows 側に透過、Windows タスク
スケジューラは exit 2 を `LastTaskResult=2` として記録する。これは
**`ERROR_FILE_NOT_FOUND` (=2) と同じ数値**だが、意味は「ファイルが見つからない」
ではなく **「子プロセスが exit 2 を返した」** である ── spec_033 起草時の
機序記述（「ERROR_FILE_NOT_FOUND マッピング」）は技術誤認だった。本 spec_034
で訂正する（実機再現：`bash -c "echo a & ; echo b"` を WSL Ubuntu-24.04 で
直接実行 → `bash: -c: 行 1: 予期しないトークン \`;' 周辺に構文エラーがあります`
→ `exit: 2`、構文エラーが exit 2 を返すことを実測確認）。

> **再現条件（spec_035 で追記）** ── この構文エラーは「すべてのバックグラウンド実行」で起きるわけ
> ではない。単純な `echo a && nohup setsid bash -c 'echo x' & echo b`（`& echo`
> の form）は**構文的に valid** で exit 0（result_034 実験 1）。構文エラー（exit 2）
> になるのは、改行が `;` に解釈されて `& ;`（`& ` の直後に `;`）という連結が
> 生じる特定条件下のみ（result_034 実験 2・3：`bash -c "echo a & ; echo b"` →
> `予期しないトークン ';' 周辺に構文エラー` → exit 2）。タスクスケジューラ →
> wsl.exe → bash -c の経路で複数行 here-string が 1 行に joined されるとき、
> 改行位置によってこの `& ;` 連結が発生しうる、というのが本欠陥の核心。
> launcher pattern（wrapper を別ファイル化して 1 行で呼ぶ）はこの連結条件を
> 構造的に回避する。

**設計原則**: タスクスケジューラ → wsl.exe → bash -c に渡すコマンドは
1 行に保ち、複数行ロジックは repo 内の wrapper script に集約する。
代表例が `scripts/launchers/nightly_all_wrapper.sh` で、`register_nightly.ps1`
（_ai_workspace 配下のテンプレート、git 管理外）からは
`bash $ProjectDir/scripts/launchers/nightly_all_wrapper.sh "$ProjectDir"` の
1 行で呼ぶ（spec_034 で wrapper が PROJECT を相対解決するようになり、引数なし
呼び出しでも動くが、`register_nightly.ps1` から明示渡しすると repo の relocation
耐性が運用側にも伝播する）。これにより、起動経路の挙動を変えるときは wrapper
だけ修正で済み、タスクの再登録は不要。

**spec_034 の運用品質向上** ── wrapper の `PROJECT` を相対解決
（`scripts/launchers/` から `../..` で repo root を導出、`readlink -f` で正規化、
第 1 引数があれば明示渡し優先）して repo を別パスに clone しても動くようにし、
`PROJECT` 値と `command -v ccd` の解決結果をログに記録して、venv activate 失敗
時に system Python の古い ccd が呼ばれても診断可能にした。`tests/test_launchers.py`
に `PROJECT` 相対解決と明示渡しを検証する 2 件を追加（合計 5 件）。

### 9.7 フェーズ分け（論点10）

各フェーズは単体で出荷可能・単体で価値があり、§9.2 の2危険のうち1つを隔離して退治する。

- **Phase 1 — 「発見だけ」**。発見3チャンネル・朝レポート（ファイル）・スケジューラ骨格・プロファイル基盤を作るが、**ループは何も直さない**（全発見をレポートに載せるだけ = 第2ティアの能力を CCD 自身に適用したもの）。自律変更ゼロ = リスクゼロ。確定事項(1)「発見が核」を、発見の信号対雑音比を実データで見ることで隔離検証する。Phase 1 は内部的に複数 spec になる。
- **Phase 2 — 自律修正ループ点火**。翻訳・修正 dispatch・検証ゲート・インチキ修正ガード・ローカル merge・コスト停止境界を追加し、ループを閉じる。内部のリスク傾斜: **先にミューテーション側（テンプレA = test-only、本番コードを構造的に壊せない最も安全な自律変更）** を自律化し、信用できてから **敵対的入力側（テンプレB = 本番コードに触る）** を点火。確定事項(2)「インチキ修正」を、発見が Phase 1 で実証済みの状態で隔離検証する。
- **Phase 3 — 拡張と磨き込み**。実装したのは3点 ── (1) **週次ケイデンス**（毎晩の自律修正は「開発途中の動く標的を夜ごとに追う」ことになり昼の人間の作業と衝突するため、週1に詰め直した。spec_027）/ (2) **提案モード**（発見した問題の修正案を生成・検証し、適用はせず朝レポートに diff を載せる第3のモード。`auto`／`propose`／`off` の3モードを信頼度で使い分ける ── CCD 自身は `auto`、外部のクライアント施策は `propose`。spec_028）/ (3) **複数施策の巡回運用**（リポジトリ1つ＝プロファイル1枚、週次タスクが全施策を巡回、1施策の失敗が他施策を止めない。spec_029）。後回し（任意）── ライブアーティファクト化した朝レポート / Slack 通知 / grounded AI 発見（AI推論が再現失敗テストを添えられれば自律ループに昇格）/ 条件つき auto-push（実績で勝ち取る）/ ダッシュボードへの夜間統計表示。

### 9.8 実装の記録（2026-05）

§9 の設計を 29 本の spec（spec_013〜041）に割り、1 本ずつ Claude Code に dispatch して実装した（CCD 自身の bridge 機構を使ったドッグフーディング）。version 0.4.0 → 0.24.0、テスト 206 → 682、サブコマンド 6 → 12。spec_038 で v3 シリーズ 1/5（top-K 直列、1晩1候補制約の解除）を投入 ── 既定 K=1 で v2 外形完全一致、operator opt-in で 1..5 を直列処理。spec_039 で v3 シリーズ 2/5（FixLoop ── 収束ループ + 無進捗検知）を投入 ── 既定 `loop_max_iterations=1` で v2 単発と外形完全一致、operator opt-in で 1..5 イテレーションを R5/R4/guard が green になるまで繰り返す。完了判定は自己申告 promise でなく機械検証で行い、無進捗 2 連続検知で早期 halt して ralph 型のトークン底なし消費を構造的に防ぐ。spec_040 で v3 シリーズ 3/5（隔離の統一 ── auto モードの clone-and-patch 化と Integrator 導入）を投入 ── auto も propose と同じ使い捨て隔離クローンで fix を実行し、live への書き込みは直列 Integrator のみ（apply 失敗 / live 再検証失敗で drop + restore、rebase も再 dispatch もしない）。spec_041 で v3 シリーズ 4/5（WorkerPool ── 複数 CC dispatch の並列化と直列 Integration queue）を投入 ── K 候補を並列度 P (1..4) のワーカープールで処理し、完了 patch を完了順に直列 Integrator へ。`max_merges_per_night` cap + PAUSE / 未push backlog / 夜間窓 wall-clock の 4 ゲートが integration 前に再評価され、trip した残 patch は退避 (`_ai_workspace/nightly/proposals/dropped_*.patch`)。既定 P=1 で spec_038〜040 と外形完全一致。

- **Phase 1**（spec_013〜020）— 発見3チャンネル・発見の隔離・朝レポート・スケジューラ骨格・プロファイル基盤。
- **Phase 2**（spec_021〜025）— インチキ修正ガード・翻訳・自律修正ループ（テンプレA/B）・コスト停止境界。
- **spec_026** — Phase 2 完成後の `ccd nightly` end-to-end 実走で発覚した2バグ（偽HALT／HALT後の後始末漏れ）の修正。
- **Phase 3**（spec_027〜029）— 週次ケイデンス・提案モード・複数施策の巡回運用。
- **Phase 2.5 沈黙失敗の構造修正**（spec_030〜032）— Phase 2.5 の実走 sweep で発覚した「**任意 repo に向けたときの沈黙失敗 + ネスト構造互換性**」3 件を構造修正。

**実走で初めて炙り出された欠陥 6 件 — すべて構造修正済み** — 紙の設計レビューでは見抜けず、実際に動かしたことでのみ表面化:

1. **発見が実リポジトリを汚染**（迷子コミット `impl spec_100`、Phase 1 実走）→ §9.4 の隔離制約 ＋ spec_014 で修正
2. **ミューテーション撃破率 0%**（PEP 660 editable install の import hook ＋ mutmut パーサ取り違え、Phase 1 実走）→ spec_019 でカナリア検証つきで修正、撃破率 81% へ
3. **正しい修正を書いたループが偽 HALT**（翻訳の制約文言を修正係が誤読、Phase 2 end-to-end 実走）→ spec_026 で修正
4. **adversarial チャンネルがクライアント施策で CCD パーサに誤検出**（Phase 2.5 初回 sweep）→ spec_030 で対象パーサをプロファイル化、未設定なら sweep で skip
5. **`mutants_total = 0` の沈黙失敗**（Phase 2.5 初回 sweep）→ spec_030 で HALT 化、spec_031 で iso-venv install validation を追加して `IsoVenvProvisioningError` を厳密化
6. **mutmut が axis のネスト構造（`backend/src/...`）で 0 mutants を返す**（Phase 2.5 sweep #3〜#5 実走）→ spec_032 で profile から mutmut の実行パラメータ（cwd / paths_to_mutate / tests_dir / extra_args）を注入可能にして構造修正

**欠陥 6 構造修正（spec_032、2026-05-27）** — mutmut 2.x はネスト構造（`backend/src/...`）でリポジトリルートを source root として認識できない / test ディレクトリの auto-discover が破綻する / cwd 起点の相対パス解決が崩れる、の 3 仮説いずれかで axis-knowledge-rag に対し安定的に 0 mutants を返していた（spec_031 の post-install validation を通過しても再現するため install 失敗ではなく mutmut 側の互換性問題と確定）。spec_032 は `[discovery.mutation]` ブロックを profile schema に追加し、`cwd` / `mutation_paths` / `tests_dir` / `extra_args` を注入できるようにした ── mutmut の knownな workaround「`cd <subdir> && mutmut run --paths-to-mutate <subdir-relative-path>`」を profile から指定できる構造解。axis profile は `cwd = "backend"` + `mutation_paths = ["src/normalizer.py"]` + `tests_dir = "tests"` に切り替え。spec_030 の防護網が捕まえていた状態から、**構造修正済み**へ昇格 ── これで「5 件構造修正 + 1 件防護網捕獲」から「**6 件全部構造修正**」に。

安全設計（隔離・ガード・カナリア・偽陽性優先）を機能より先に組んでいたため、6 件すべて「実害で済み、気づかぬまま悪化することはなかった」── spec_030〜032 で防護網捕獲から構造修正への昇格を完遂、v2「正直な計測」原則の補強が完成した。

**spec_033（2026-05-28、launcher pattern 構造修正、v0.20.1）** — Phase 2.5 sweep #6 でタスクスケジューラ経由 (`wsl.exe -d Ubuntu-24.04 -- bash …`) で起動した nightly-all が、対話的セッションと異なる環境（HOME/PATH 未設定、venv 未活性）で失敗する沈黙パスを発覚。launcher 側で `PATH=$HOME/.local/bin:$PATH` + 明示 venv 起動 + ログを `_ai_workspace/logs/launcher.log` に集約する pattern に切り替え（`_ai_workspace/launchers/` の dispatch / nightly-all テンプレート両方）。これで **タスクスケジューラ経由の自動起動が信頼できるレベル**になり、Phase 2.5 の「週次タスク登録 → 複数週運用」が現実的な運用に踏み込める状態へ。

## 10. v3 — 夜間並列自律保守（multi-worker × 収束ループ、実装完了 / version 0.25.0）

> ステータス: 全5本 spec_038〜042 実装完了（version 0.25.0、テスト ~700、サブコマンド 12）。
> 既定値は v2 と外形完全一致（K=1, P=1, loop_max_iterations=1）── spec を merge しても
> 何も変わらず、有効化は operator が profile を書き換えたときのみ。

### 10.1 概要・動機

v2 の夜間ループは「1晩1候補・1 dispatch・直列」。発見チャンネル（mutation / adversarial）は
一晩に複数の候補を出すのに、修正は 1 件/晩しか進まない。v3 はこのスループット制約を
**複数候補 × 並列ワーカー × 候補ごとの収束ループ** の3点で解除する。

設計原則は1つ: **v2 の安全境界（guard 5ルール・隔離・正直メトリクス・HALT 優先）を 1mm も緩めない。**
速度は安全境界の「内側」でだけ上げる。

### 10.2 loop スキル（ralph loop）との関係

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

### 10.3 アーキテクチャ

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

### 10.4 収束ループ（FixLoop）

候補ごと・clone 内で: `dispatch → R5/R4/guard 検証 → 失敗なら feedback md を書いて再 dispatch`。
打ち切り3条件（どれか到達で halt、すべて朝レポートに理由明記）:

1. **loop_max_iterations**（既定 3）
2. **候補あたり wall-clock**（既存の 40 分 cap をループ全体に適用）
3. **無進捗検知** — 失敗シグネチャ（FailureCategory + R5 失敗理由の正規化ハッシュ）が
   2 回連続同一なら、残り iteration があっても早期 halt。盲目的 ralph との差別化点であり、
   トークンの底なし消費（ralph の既知問題: 50 iterations で $50-100+）への構造的対策。

収束 = 機械検証 green のみ。`converged: bool` と `iterations: int` を record に持たせ、
v1 からの一貫テーマ「成功率は観測できた母集団の中の率」で集計する。

### 10.5 安全境界（継承 + 追加）

継承: PAUSE ファイル / 未push バックログ cap 3 / guard 5ルール / pre-flight / zero-finding 正常終了。
追加:

- **max_merges_per_night**（既定 3）— 並列化で1晩の merge 数が跳ねるのを防ぐ。バックログ
  cap と整合（1晩で cap まで埋め切らない）。
- **rate-limit 現実**: 並列 claude プロセスは同一アカウントの限度を分け合う。P 既定 2、
  上限 4。transient 失敗は FixLoop の retryable 側に落ちる（既存分類を流用）。
- **全フィールド既定値 = v2 現行挙動**（K=1, P=1, loop_max_iterations=1）。spec を merge
  しただけでは何も変わらず、有効化は operator が profile を書き換えたときのみ。

### 10.6 profile 追加（SafetyConfig）

```toml
[safety]
fix_mode = "auto"
fix_templates = ["A"]
max_candidates_per_night = 2   # K: 1..5、既定 1（v2 互換）
parallelism = 2                # P: 1..4、既定 1（v2 互換）
loop_max_iterations = 3        # 既定 1（v2 互換 = 単発 dispatch）
max_merges_per_night = 3       # 既定 3
```

### 10.7 メトリクス（正直さの継承）

- `convergence_rate` — 予算内で green に到達した候補率（母集団 = ループ起動候補数を明記）
- `iterations_to_green` 分布 — 1 で収束が多いならループは保険、2-3 が多いならループが価値
- `marginal_parallel_yield` — worker 2 以降が生んだ merge 数。**並列化が無価値なら無価値と
  数字が言う**ようにする（生存バイアス対策と同じ姿勢）
- `conflict_drop_rate` — Integrator での drop 率。高いなら候補同士が同一ファイルに集中して
  おり、K を上げる意味がない、というシグナル
- `dispatch_minutes_per_merged_fix` — コストの代理変数

### 10.8 spec 分割（spec_038〜042、各 spec 単体 merge 可・halt-on-failure 連鎖）

| spec | 内容 | 既定挙動の変化 |
|---|---|---|
| 038 | top-K 候補選択 + 直列複数候補処理 + brief 複数対応 | なし（K=1） |
| 039 | FixLoop（収束ループ + 無進捗検知）を auto/propose に配線 | なし（iterations=1） |
| 040 | 隔離統一 — auto を clone-and-patch 化、Integrator 導入 | auto の内部経路のみ（外形同一） |
| 041 | WorkerPool 並列化 + 直列 Integration queue + merge cap | なし（P=1） |
| 042 | メトリクス + dashboard + DESIGN §10 / README / CHANGELOG 同期 | なし |

順序の根拠: 040（隔離統一）が 041（並列）の前提。039 は 040 と独立だが、ループを先に
入れておくと 040 の検証パスがループ込みで一本化できる。

### 10.9 技術記事への接続

v3 で記事の柱が1本増える: 「**ralph loop を信用しない形で取り込む** — 自己申告 promise を
機械検証に置換した外側ループ」+「並列化の限界効用を自分のメトリクスで測る（marginal_parallel_yield）」。
v1〜v2 の「メトリクスが嘘をついた→根治」の続編として一貫する。

### 10.10 実装の記録（2026-06）

§10 の設計を spec_038〜042 の 5 本に分け、1 本ずつ Claude Code に dispatch して実装した
（CCD 自身の bridge 機構によるドッグフーディング）。version 0.20.5 → 0.25.0、テスト
643 → ~700、サブコマンド数は据え置き（v3 は SafetyConfig 拡張と CLI flag 追加のみで、
新サブコマンドは増やさない）。

- **spec_038（v0.21.0）** — top-K 候補選択 + 直列複数候補処理。「1 晩 1 候補」制約を
  解除し、`safety.max_candidates_per_night` (1..5) を導入。K=1 の既定値で v2 外形は
  bit-for-bit 一致。`brief.py` の §B を per-candidate subsection に拡張、`AutoFixOutcome`
  に新フィールド `candidate_count` / `template`。backlog cap が候補間で再評価され、
  trip 後は残り候補を skip（dispatcher.calls 1 件で K=3 を pin する spec_038 テストが
  既存 v2 互換の anchor）。
- **spec_039（v0.22.0）** — FixLoop（収束ループ + 無進捗検知）。候補ごとに
  `dispatch → R5/R4/guard 検証 → 失敗なら feedback → 再 dispatch` を `loop_max_iterations`
  回繰り返す。完了判定は機械検証のみ（promise 不使用）。無進捗 2 連続で早期 halt
  （ralph 型トークン底なし消費への構造的対策）。`AutoFixOutcome` に `iterations` /
  `converged` / `loop_halt_reason` を追加。既定 `loop_max_iterations=1` で v2 と
  bit-for-bit 一致。
- **spec_040（v0.23.0）** — 隔離の統一 + Integrator 導入。auto モードを propose と
  同じ使い捨て隔離クローンに一本化、live に触れるのは直列 Integrator のみ。apply 失敗
  / live 再検証失敗は **rebase も再 dispatch もせず drop**。spec_028 で propose 用に
  作った clone 機構を auto に再利用したので新規モジュールは少ない。
- **spec_041（v0.24.0）** — WorkerPool 並列ワーカー + 直列 Integration queue。
  `ThreadPoolExecutor(max_workers=P)` で K 候補を並列処理。`max_merges_per_night` cap +
  PAUSE / 未push backlog / 夜間窓 wall-clock の 4 ゲートが integration 前に再評価され、
  trip した残 patch は退避 (`dropped_*.patch`)。`AutoFixOutcome` に per-worker timestamp
  (`worker_id` / `worker_started_at` / `worker_finished_at`)、`NightlyResult` に
  `parallelism` / `achieved_max_concurrency` / `drop_reasons` を追加。dispatch_count
  semantics 保持のためゲート評価を **2 点（integration 前 + next submission 前）** に分けた
  ── これが「spec_038 既存テストを 1 件も書き換えずに spec_041 を緑にする」鍵
  （result_041 §1-3 で詳述）。
- **spec_042（v0.25.0、本 spec）** — メトリクス + dashboard + DESIGN.md 同期。
  `ccd/metrics.py` に `aggregate_v3()` / `render_v3_report()` / `NightSnapshot` /
  `V3MetricsReport` を追加。`ccd nightly` 完了時に `_ai_workspace/nightly/records/`
  に per-night snapshot JSON を保存し、`ccd report` / `ccd dashboard` がこれを読んで
  v3 節を表示。**各指標に母集団・観測限界の注記つき**（spec_042 §2-1 「数字が正直に
  言う」流儀） ── 0 除算は隠さず「merge=0」と書く、per-worker timestamp が欠損した夜は
  推定でなく「不明」と書く、`marginal_parallel_yield` が観測できないなら None。spec_009
  の流儀どおり古い record JSON に v3 フィールドが無くても落ちない（backfill 寛容性）。

**v3 で予想と違った点（実装で発覚）**:

1. **spec_041 のゲート評価点が「2 点」必要**（事前想定: 1 点）── spec_038 の
   `test_k3_backlog_cap_between_candidates_skips_remainder` (counter `[0, 3]` で
   `dispatcher.calls == 1` を pin) と spec_041 §2-3 「integration 前のゲート」を
   両立させるため、「integration 前」と「next worker submission 前」の 2 点で gate を
   評価する必要があった。P=1 ではこの 2 点が同一瞬間になり spec_038 と完全互換、
   P>1 では「既に in-flight の worker」は最後まで走るが「新規 submit」は止まる、
   という意図された挙動が成立する。
2. **spec_041 の spec_auto 採番が thread-safe でない**（事前想定: dispatch 時に各
   worker が translate を呼ぶ）── `ccd/translate.py` の `_next_spec_auto_seq`
   (spec_018) はプロセスローカルなカウンタで lock を持たない。WorkerPool に投入する
   前に **全候補を main thread で順次 translate** する必要があった（spec_041 §2-2
   逐語実装）。
3. **spec_042 の「夜間 record JSON」は spec_041 までで永続化されていない**（事前想定:
   既に persist 済み）── 実態としては NightlyResult / AutoFixOutcome の **in-memory
   shape** に v3 フィールドが乗っていただけ。spec_042 で per-night snapshot を
   `_ai_workspace/nightly/records/night_<date>.json` に書く persistence 層を追加した。
   `build_night_snapshot(result, night_id)` で NightlyResult を v3 snapshot に
   投影し、`save_night_snapshot()` が JSON dump（best-effort、write 失敗で nightly
   全体は止めない）。
4. **brief の「drop 数」と v3 の「conflict_drop_rate」は定義が違う**（事前想定: 同一）
   ── brief §B 夜サマリの drop は「候補のうち merge しなかった数」（skip も含む広義）、
   v3 metric は「Integration queue で gate trip した数」（狭義）。同じ「drop」という
   日本語を二つの異なる概念に充てる罠だった。spec_042 では brief 側を触らず、v3 metric
   の note 文字列で「Integration drop」と明示する道を選んだ（report と brief が同じ
   merge 数で一致することは保証、drop は別概念として別行に出る）。

実走で発覚した欠陥は v3 では 0 件（既定値 K=1 / P=1 / iter=1 で v2 と外形完全一致を
全 spec で pin しているため、`ccd nightly-all` の実走影響なし）。新しい挙動（P>1 等）
の運用検証はこれから ── operator が profile を段階的に有効化していく段階。

