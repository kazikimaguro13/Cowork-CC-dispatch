# Changelog

本プロジェクトの注目すべき変更を記録する。フォーマットは [Keep a Changelog](https://keepachangelog.com/) に準ずる。

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
