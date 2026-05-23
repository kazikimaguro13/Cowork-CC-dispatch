# result_009: backfill パーサの寛容化 + 生存バイアスの正直な開示

- **Spec**: spec_009
- **Author**: Claude Code (dev-b)
- **Completed**: 2026-05-23
- **Branch**: feat/spec_009
- **Status**: done

## 1. やったこと

spec_009 のとおり、(A) `ccd/backfill.py` のステータスパーサを寛容化して既知 21 件のスキップを回収し、(B) `DispatchStatus.PARTIAL` を新規追加して metrics で **`done` にも失敗にも混ぜず独立カウント**、(C) ダッシュボードに**生存バイアスのカバレッジ注記**と `done` / `partial` / `failed` の内訳ピルを追加した。`docs/data/*.json` と `docs/index.html` を再生成し、ヒーロー値が 100% から 94.8% に下がる（5 件 partial が表面化する）ことを目視 + テストで確認した。失敗の捏造・ログ解析はしていない（観測できていないことを注記で開示するに留めた）。push / ブランチ操作・merge は一切していない。

### 2-1. `ccd/models.py` — `PARTIAL` 追加

```python
class DispatchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    HALTED = "halted"
    PARTIAL = "partial"  # ← 追加のみ
```

末尾追加なので既存値・並び順は不変、`StrEnum` の文字列比較も維持。`tests/test_models.py::test_dispatch_status_values` の集合に `"partial"` を足すだけで他はすべてグリーン。

### 2-2. `ccd/backfill.py` — パーサ寛容化

仕様 §2-2 のすべての書式差を吸収する `_normalize_status_value(raw)` を切り出した:

1. **括弧付き接尾辞の除去** — `[(（]` (半角 + 全角左括弧) で `split` し、最初のチャンクを採用。`done (clasp push のみ認証待ち)` → `done`、`partial（コード変更・git push は完了。...）` (全角) → `partial`。
2. **em-dash 以降の trailing prose 除去** — `[—–]` (em-dash / en-dash) で split。`✅ done — 全成功条件クリア、push 済み` → `✅ done`。**ハイフンマイナス `-` は意図的に対象外**（`in-progress` のような正規 status 候補を破壊しないため、保守的に wide-dash のみ対象）。
3. **装飾の除去** — `re.sub(r"^[^\w]+", "", s)` / `re.sub(r"[^\w]+$", "", s)` で両端の非単語文字（絵文字 ✅、句読点）を剥がす。`\w` は Unicode 単語文字を含むので日本語は保たれる。
4. **小文字化 + 同義語マッピング** — `_STATUS_SYNONYMS = {"completed": "done", "complete": "done", "完了": "done"}`。spec 明示の `completed → done` に加え、実データに頻出する `complete` (result_037) と `完了` (result_031 の `✅ 完了 (全成功条件クリア)`) を追加。
5. **`partial` は `DispatchStatus.PARTIAL`** — `DispatchStatus(normalized)` がそのまま PARTIAL を返すので、`_STATUS_SYNONYMS` には乗せない（synonym は「別名 → 標準値」、PARTIAL はそれ自体が標準値）。

正規化後に `DispatchStatus(normalized)` を `try` し、未知の値 (例: `floomp`) は `None` を返して skip 継続。**「マッピングに乗らない真に不明な値のみ skip」**（spec §2-2 末尾の要件）を満たす。

加えて status / spec_id の探索範囲をヘッダブロック外へ拡張した:

- `_find_status_in_body(text)` — `_LOOSE_STATUS_RE` (`^\s*(?:[-*]\s*)?\*{0,2}status\*{0,2}\s*:\s*...`) でドキュメント全体を行スキャン。マークダウン bold (`**Status**:`)、bare (`status:` / `Status:`)、YAML frontmatter (`status: done`) のいずれも吸収。最初に妥当な status にヒットした行で確定 (チェックリストや本文中のサンプルブロックの影響を受けない)。
- `_find_spec_id_in_body(text)` — `\bspec_(\d{3,})\b` を全文検索。YAML frontmatter の `spec: spec_054` や本文中の Branch 名 `feat/spec_034-citation-highlighting` から拾える。
- `_spec_id_from_filename(path)` — 最後の手段。`result_NNN.md` のファイル名から `NNN` を抽出して `spec_NNN` を返す。本文に spec の言及が一切ない em-dash タイトル (e.g. `# result_NNN — 何か`) でも回収できる。

`parse_result_file` 本体の優先順は **header field → title → body → filename**。下に行くほど確度が下がるが、anonymization 層で renumber されるため衝突しない。

実データに対する確認 (`python -m ccd.backfill --config _ai_workspace/backfill_sources.json`):

| project | before (skipped) | after (回収) | partial 件数 |
|---|---|---|---|
| axis-knowledge-rag | 18 / 33 | **33 / 33** | 3 |
| Cowork-CC-dispatch | 8 / 8 | 8 / 8 | 0 |
| 実務案件A | 14 / 14 | 14 / 14 | 0 |
| 実務案件B | 24 / 25 | **25 / 25** | 1 |
| 実務案件C | 11 / 16 | **16 / 16** | 1 |
| **合計** | **75 / 96** | **96 / 96** | **5** |

仕様の挙げた 21 件のスキップ (axis 15 件: 002/019/024/030/031/032/034/036/037/040/046/049/051/054/056、実務案件 6 件: 業務改善通知 022、請求関連 008/012/014/015/016) **全件回収済み**。これ以上の skip は出ていない。なお spec の文字列リストにある `result_002 / 012 / 016` は読みやすさのため省略形 (各プロジェクト由来) と理解した — 実装は具体プロジェクトに依存しない汎用ルール。

### 2-3. `ccd/metrics.py` — `PARTIAL` を独立計上

- `MetricsReport` に `partial: int` フィールド追加。
- `aggregate()` 内で 3 グループに分割: `done_records` / `partial_records` / `fail_records`。`fail_records` は **`DONE` でも `PARTIAL` でもない** records (`status is not DispatchStatus.DONE and status is not DispatchStatus.PARTIAL`)。
- **成功分子**: `dispatch_success_rate` / `autonomous_completion_rate` / `first_pass_rate` の numerator は `done_records` 由来のみ。PARTIAL は **絶対に成功側に入らない**。denominator は `total`（PARTIAL 込み）なので、partial が増えると成功率は下がる（spec 意図）。
- **失敗側**: `failure_taxonomy` / `safe_halt_rate.denominator` は `fail_records` のみ。PARTIAL は **失敗カテゴリにも safe-halt 分母にも入らない**（spec §2-3 の「失敗タクソノミーにも混ぜず」要件）。
- `retry_recovery_rate.numerator` は `attempts > 1 and status is DispatchStatus.DONE` のままに保ち、PARTIAL は除外。
- `render_report()` の Markdown 出力に `- Partial: <n>` 行を追加（既存の Total / Done / Failures と並列）。

新規テスト `test_partial_records_are_counted_independently` / `test_partial_is_not_treated_as_failure_for_safe_halt` / `test_aggregate_default_partial_zero_when_no_partial_records` / `test_render_report_surfaces_partial_count` の 4 件で reconcile を実証。

### 2-4. `ccd/dashboard.py` — カバレッジ注記 + done/partial 内訳

**カバレッジ注記** (`_render_quality_note` 拡張): 仕様文をそのまま読者向け文章に整形し、**`has_backfill` の有無に関わらず常に表示**（生存バイアスは backfill 由来でなくとも構造的に発生するため）。

```
カバレッジ注記: 集計対象は result_*.md を残した dispatch のみです。
途中で halt して result を残さなかった失敗は構造的に含まれません (生存バイアス)。
表示されている成功率は「result を書き残せた dispatch の中での」成功率であり、
母集団全体の成功率の上限の目安として読んでください。
```

backfill 由来 (`bash_prototype`) のときだけ表示される旧バナー（attempts/intervention 欠損の注記）はそのまま残し、カバレッジ注記の下に並べる（性質が違う 2 種類の注記を別段落に分けた — カバレッジ = 構造的、attempts/intervention = データ欠損）。

**done / partial 内訳ピル** (`_outcome_breakdown`): ヒーロー値直下に `done` / `partial` / `failed` の色分けピル (`.outcome-done` 緑 / `.outcome-partial` 黄 / `.outcome-failed` 赤) を表示。count=0 のカテゴリは描画しない (`if count == 0: continue`)。CSS パレットは既存の `--success` / `--warn` / `--danger` をそのまま流用したので新規変数なし。

**run 一覧テーブル**: `partial` 列を追加 (`done` と `failed` の間に挿入)。`<td colspan>` も 7 → 8 に追従。各行で `partial = sum(1 for r in records if r.status is DispatchStatus.PARTIAL)`、`failed = len(records) - done - partial` で算出。details の per-spec ステータス表示は PARTIAL 行の failure_category 列を `—` (空) に（失敗ではないので「不明」と書くと誤解を招く）。

**実データでの効果** (`docs/index.html` を `python -m ccd.dashboard --runs-dir docs/data --output docs/index.html` で再生成して確認):

- 自律完走率ヒーロー値: 旧 `100.0%` → 新 **`94.8%`** (91 done / 96 total)
- 内訳ピル: `91 done` (緑) + `5 partial` (黄) の 2 ピル表示
- カバレッジ注記の Section が冒頭に登場
- run 一覧テーブルに `partial` 列 (axis 3 / 業務改善通知 1 / 請求関連 1)

「100% done」に見えない、かつ partial の存在が一目で分かるダッシュボードになった。

### 2-5. テスト

新規追加 19 件 + 既存リネーム 1 件:

- `tests/test_backfill.py` — `+9 件`
  - `test_parse_recognizes_partial_status` — `partial` → `PARTIAL`
  - `test_parse_maps_completed_synonym_to_done` — `completed` → `done`
  - `test_parse_strips_emoji_prefix_and_em_dash_trail` — `✅ done — ...` → `done`
  - `test_parse_strips_parenthetical_suffix` — `done (clasp push のみ認証待ち)` → `done`
  - `test_parse_strips_full_width_parenthetical_suffix` — `partial（…）` (全角括弧) → `PARTIAL`
  - `test_parse_handles_lowercase_status_with_decoration` — bare `status:` + `✅ completed` → `done`
  - `test_parse_falls_back_to_status_outside_header_block` — YAML frontmatter → `done`
  - `test_parse_falls_back_to_filename_for_spec_id` — em-dash タイトル + 本文 spec 言及 → `spec_016`
  - `test_parse_filename_fallback_when_no_body_mention` — 本文に spec 言及ゼロ → `result_077.md` から `spec_077`
  - `test_parse_still_skips_unknown_status` — `floomp` のような未知値は引き続き skip
  - `test_parse_does_not_fabricate_status_when_absent` — status 完全欠落は引き続き skip（捏造しない検証）
- `tests/test_metrics.py` — `+4 件`
  - `test_partial_records_are_counted_independently` — 5 records (2 done / 2 partial / 1 failed) の混合で全 numerator / denominator を直接 assert
  - `test_partial_is_not_treated_as_failure_for_safe_halt` — partial のみで構成された run で taxonomy が空、safe_halt_rate.denominator == 0
  - `test_aggregate_default_partial_zero_when_no_partial_records` — partial ゼロ時の `report.partial == 0`
  - `test_render_report_surfaces_partial_count` — Markdown 出力に `Partial: 1` 行
- `tests/test_dashboard.py` — `+4 件`
  - `test_dashboard_renders_survival_bias_coverage_note` — `カバレッジ注記` + `生存バイアス` + `result を残さなかった` 文字列の出現
  - `test_dashboard_breakdown_shows_done_partial_failed_separately` — 3 種の `outcome-*` 描画 + 表のヘッダ `<th class="num">partial</th>`
  - `test_dashboard_done_partial_breakdown_omits_zero_categories` — count=0 のカテゴリは pill タグが付かない
  - `test_dashboard_hero_not_100_percent_when_partials_present` — ヒーローが 50% (not 100%) になる回帰検知
- `tests/fixtures/backfill/` — `+7 件`
  - `result_010.md` (partial)
  - `result_011.md` (completed → done)
  - `result_012.md` (✅ done — em-dash)
  - `result_013.md` (done (paren))
  - `result_014.md` (lowercase `status:` + 絵文字 + completed)
  - `result_015.md` (YAML frontmatter)
  - `result_016.md` (em-dash タイトル + ファイル名 fallback)

既存テスト変更:

- `tests/test_models.py::test_dispatch_status_values` — 集合に `"partial"` 追加
- `tests/test_smoke.py::test_version_is_020` → `test_version_is_021` (リネーム + アサーション値)
- `tests/test_backfill.py` の 5 件の `_stage_outbox(tmp_path)` 呼び出しを `fixture_names=[...001-004...]` に絞り込み（新規 fixture 010-016 が混入して既存 count assertion を壊さないため）

最終: **`ruff check .` clean / `pytest -q` 147 passed**（旧 128 → 新 147、+19）。

### 2-6. データ再生成

```
python -m ccd.backfill --config _ai_workspace/backfill_sources.json
# → _ai_workspace/runs/*.json 5 ファイル更新
cp _ai_workspace/runs/*.json docs/data/
python -m ccd.dashboard --runs-dir docs/data --output docs/index.html
# → docs/index.html 再生成
```

`docs/data/*.json` は 5 ファイル全部更新（diff stat で示すと `axis-knowledge-rag.json: +171/-13` 行が中心 — partial が混ざることで record 数 18→33 と status 値が増えた行）。`docs/index.html` も再レンダリング。`_ai_workspace/runs/` は `.gitignore` 配下なのでコミットしない（spec 制約に従う／result_008 と同じ運用）。

### 2-7. CHANGELOG + version

- `pyproject.toml` / `ccd/__init__.py` — `0.2.0` → **`0.2.1`** (patch bump、v1.5 系の追加でなく回収/補正なので minor ではなく patch)。
- `CHANGELOG.md` — `[0.2.1] — 2026-05-23` エントリ追加。Added / Changed / Fixed の 3 セクションで `PARTIAL` / parser 寛容化 / カバレッジ注記 / data 再生成を要約。

## 2. 変更ファイル

新規:

- `tests/fixtures/backfill/result_010.md` ... `result_016.md` — 書式差 fixture 7 件
- `_ai_workspace/bridge/outbox/result_009.md` — 本ファイル

更新:

- `ccd/models.py` — `DispatchStatus.PARTIAL` 追加（+1 行）
- `ccd/backfill.py` — `_normalize_status_value` / `_coerce_status` / `_find_status_in_body` / `_find_spec_id_in_body` / `_spec_id_from_filename` / 関連正規表現定数 + `parse_result_file` の優先順拡張（+109/-3 行）
- `ccd/metrics.py` — `MetricsReport.partial`、`aggregate()` の 3 グループ分割、`render_report` に Partial 行（+21/-3 行）
- `ccd/dashboard.py` — `_render_quality_note` にカバレッジ注記、`_render_hero` に `_outcome_breakdown` ピル、run 一覧テーブルに partial 列、CSS に `.outcome-*` クラス（+86/-21 行）
- `tests/test_models.py` / `test_smoke.py` / `test_backfill.py` / `test_metrics.py` / `test_dashboard.py` — 上記新規テスト + 既存テスト微修正
- `pyproject.toml` / `ccd/__init__.py` — version `0.2.0` → `0.2.1`
- `CHANGELOG.md` — `[0.2.1]` エントリ追加
- `docs/data/*.json` × 5 — 再生成（96 records、partial 5）
- `docs/index.html` — 再生成

未変更（spec §3 の触ってはいけない範囲に従い無変更）:

- `ccd/agent.py` / `dispatch.py` / `chain.py` / `integrate.py` / `cli.py` / `protocol.py` — コアロジック不変。PARTIAL 追加に伴う追従不要 (これらは `DispatchStatus` を文字列比較せず enum 値で扱うため後方互換)。
- `docs/DESIGN.md` / `docs/architecture.md` — 設計の正典は無変更
- `_ai_workspace/` のスクリプト類 / `backfill_sources.json` — 無変更

## 3. 成功条件チェック

- [x] `DispatchStatus` に `PARTIAL` 追加（追加のみ、後方互換）
- [x] backfill が書式差 result 21 件を回収（96/96 件パース成功、status 持ちは全件回収）
- [x] `partial` が `done` にも失敗タクソノミーにも混ざらず独立計上 (`MetricsReport.partial` + 全テスト reconcile)
- [x] ダッシュボードに生存バイアスのカバレッジ注記、`done`/`partial` 内訳表示
- [x] backfill 再実行でデータ再生成、ダッシュボードが「100% done」でなくなる（94.8% 表示 + 5 partial ピル）
- [x] `ruff check .` clean / `pytest -q` green（147 passed、新規 19 件含む）
- [x] CHANGELOG + version 更新 (`0.2.0` → `0.2.1`)
- [x] ローカル commit 済み（push していない／ブランチ操作なし）

## 4. コミット一覧

`feat/spec_009` 上で 1 件の新規 commit（実装 + テスト + 再生成データを 1 単位として束ねた — レビュアが「v0.2.1 = parser 寛容化 + PARTIAL + 生存バイアス開示」を 1 commit で読みやすい）。具体的なハッシュは git log で確認。

## 5. 判断メモ

- **`完了` を synonym に追加**: spec §2-2 は `completed → done` のみ明示。しかし実データの `result_031.md` が `✅ 完了 (全成功条件クリア)` を使っており、spec の §2-2 末尾「目標: 既知のスキップ 21 件（...result_031...）が回収される」の表現と齟齬する。`完了` を synonym に乗せないと回収できないため、`_STATUS_SYNONYMS` に `"完了": "done"` を追加した。`完成` は実データに出ていないので保守的に見送り。将来 `完了済み` 等が出てきたら同じテーブルに足すだけで済む（拡張点を明示）。
- **em-dash 分離は wide-dash のみ対象 (hyphen-minus 除外)**: 当初 `[—\-,/]` で split していたが、`in-progress` のような一語の status 候補（pending との中間状態を表す legacy 表現）が `in` に切られて誤判定する。実データに em-dash 区切りは `result_032` の `✅ done — 全成功条件クリア` の 1 種類のみで、ハイフンマイナス区切りは見当たらないため、保守的に `[—–]` (em-dash + en-dash) のみに絞った。これで `in-progress` は `in-progress` のまま正規化 → DispatchStatus に当たらず skip（既存挙動を維持）。
- **status 探索の本文走査で最初のヒットを採用**: チェックリストや本文サンプルコード内に `Status: ...` 風の行が混ざる可能性を考えたが、`_LOOSE_STATUS_RE` は行頭アンカー `^\s*(?:[-*]\s*)?` を要求するので、コードブロック内（インデント深い）や箇条書きでない散文行（タブ文字含まない・bullet 接頭辞ない）は弾かれる。それでも誤検出が出たら、行番号の早いものを優先する（YAML frontmatter / ヘッダブロックが最初に来るのが慣習）という挙動で十分安全と判断。`return` で即終了するため、後段のサンプルコード内擬似 status は影響しない。
- **spec_id の fallback 連鎖を「ヘッダ → タイトル → 本文 → ファイル名」の 4 段にした**: ファイル名 fallback は最後に置く。理由は、本文に `feat/spec_NNN-...` ブランチ名がある場合、その NNN がタイトルの NNN（≒ filename NNN）と一致するのが普通だが、もしも稀に乖離していた場合は **作者が明示したブランチ名のほうが正**と読む（filename は機械的、本文は人間の意図）。妥当な順序。
- **カバレッジ注記を `has_backfill` フラグに関係なく常時表示**: 生存バイアスは「result ファイルからの backfill」固有でなく、`ccd dispatch` ネイティブでも理論的に同じ問題が出る（dispatch が segfault で死ねば run JSON も書かれない）。なので注記は backfill 状況とは独立に「集計対象は result を残せた dispatch のみ」と書く。`attempts/intervention` 欠損のバナーだけが backfill 条件で残る。
- **`docs/index.html` を `docs/data/*.json` から生成**: spec §2-6 が `docs/data/*.json` も再生成と書いているため、`_ai_workspace/runs/` (gitignored) → `docs/data/` (公開) の流れにした。result_008 の判断（`docs/index.html` を commit しない）は 946773e で「`docs/data/*.json` と `docs/index.html` を一緒に commit」する運用に変わっており、それを踏襲して今回も両方更新する。
- **`done / partial` ピルの hero 配置を「ヒーロー値の真下、グリッドの上」にした**: 配置候補は 3 つあった: (a) ヒーロー値の上 (b) ヒーロー値の真下 (c) 内訳セルを `hero-grid` に通常セルとして追加。(c) は `_hero_cell("done / partial", ...)` で 1 セル使うアプローチも併設し、グリッド側にも `91 done / 5 partial / 0 failed` の単一セルを置いた。冗長だが、(b) は色分けで一目で分かるピル、(c) は一覧で全数字確認できる単一セル — 役割が違うので両立。ヒーロー値の上に置くと自律完走率の視線誘導を阻害するため (a) は不採用。
- **`<td colspan>` を 8 に更新**: run 一覧テーブルの `partial` 列追加で colspan を 7 → 8 に変更。これは details row の colspan のみ。既存ブラウザでテーブル幅が変わる可能性はあるが、`grid-template-columns: minmax(...)` で吸収される。
- **`failure_category` 列を partial 行で `—` に**: detail 表示で PARTIAL は failure_category を持たないので、表示が「不明」だと誤解を生む（partial は「失敗の理由不明」ではなく「成功と失敗の中間」）。`status is DispatchStatus.DONE or PARTIAL` のとき `—`、それ以外で None なら `不明` と分岐。
- **version は minor でなく patch**: spec も「(v1.5 patch)」と明示しており、機能追加（PARTIAL）はあるものの v0.2.x の枠組みを変えないので `0.2.0` → `0.2.1`。次の minor (v0.3.0) は spec_010 以降で来る想定。
- **既存テスト `_stage_outbox(tmp_path)` の絞り込み**: 新規 fixture (result_010-016) を `tests/fixtures/backfill/` に置いたことで、`ALL_FIXTURES = sorted(FIXTURES.glob("result_*.md"))` を使う既存テスト 5 件が「6 fixtures 中 4 valid」前提を壊す。`fixture_names=[001-004 のみ]` を明示渡しすることで影響を局所化。`_stage_outbox` ヘルパ自体は無変更（既に `fixture_names: list[str] | None` 引数を持っていた）。
- **secret_body_marker_NNN を fixture body に置いた**: 既存 fixture が `SECRET_BODY_MARKER_NNN` を本文に埋めて anonymize 検証していたパターンを踏襲。新規 fixture 010-016 も同じパターンで markers を入れた。`test_write_run_file_excludes_body_and_commits` テストは 001-004 のみを使う形に絞ったが、010-016 もパース通過後に anonymize 層で同じ扱いを受けるので body 漏洩のリスクはない（その安全性は既存テストの 4 fixture で十分担保）。

## 6. Open questions

- 補足 1: **`bash_prototype` 注記とカバレッジ注記の関係**: 今回 backfill 由来かどうかに関係なく**カバレッジ注記を常時表示**する設計にしたが、`ccd_native` だけで構成されるリポジトリで「backfill していないのにカバレッジ注記が出る」と冗長に見えるか？ 中島さん判断: 表示を `bash_prototype` 込みのときだけにする / 常時表示する（現状） / `total_runs > 0 and total_dispatches > 0` のときだけ表示する、の 3 択。今回は最も安全（過大表示しない）方針で常時。
- 補足 2: **`metric_taxonomy` に `unknown` カテゴリ枠を作って PARTIAL 用に転用するか?** spec §2-3 は明示的に「失敗タクソノミー（`FailureCategory`）にも混ぜず、独立カウントする」と書いているので避けた。将来「partial の原因分類」(`auth_pending` / `infra_blocked` / `out_of_scope` 等) が必要になれば、`PartialCategory` enum を別に作るのが筋（`FailureCategory` の semantic を曖昧にしない）。今は count しか出していない。
- 補足 3: **`HALTED` ステータスは引き続き `failures` 扱い**: `DispatchStatus.HALTED` は `DONE` でも `PARTIAL` でもないので、現行コードは `failures` カウントに入る。これは spec の意図と一致（halt は途中で止まった失敗）。**ただし**「halt して result を書いた」場合のみ集計に入る — result を書かずに死んだ halt は構造的に観測不能、これがまさにカバレッジ注記で開示する生存バイアス。意図どおりだが、文書として `HALTED` の扱いを明文化したいなら DESIGN.md に追記する余地あり (今回 spec §3 で DESIGN.md は触らない指示なので未対応)。
- 補足 4: **`docs/data/` を gitignored にすべきか?** 現状 commit している（946773e の運用踏襲）が、git blame 履歴が `cp _ai_workspace/runs/*.json docs/data/` 由来で意味が薄い。GitHub Pages 配信が要件 (`docs/index.html` を main から配信) なので `docs/data/` 自体は commit 不要かもしれない（`docs/index.html` が読み込んでいるわけでなく、ダッシュボード生成のための入力 snapshot）。これは公開運用の判断、CC スコープ外として保留。
- 補足 5: **`__init__.py` の `__all__`**: 今回 `partial` 関連の export はない（既存 `__version__` のみ）。`from ccd.models import DispatchStatus` でユーザは `.PARTIAL` にアクセス可能、API 露出は十分。
- 補足 6: **失敗の捏造の境界**: spec §3「失敗を捏造しない」を厳密に解釈し、`result_*.md` を残さず終わった spec を勝手に `FAILED` 計上しない / chain ログ (`_ai_workspace/logs/`) の解析もしない、を実装に反映。カバレッジ注記が「観測していないこと」を読者に伝える唯一の経路。将来「halt 検出ログ → 失敗計上」を仮にやるとしても spec_010 以降の別仕様で扱うべき（観測の正直さと統計の網羅性はトレードオフ、Ben の判断必要）。
