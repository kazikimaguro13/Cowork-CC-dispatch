"""ccd translate — finding → fix-spec translator, templates A + B (spec_022, spec_024).

v2 Phase 2 の翻訳器。spec_021 で **インチキ修正ガード**（`ccd/guard.py`）が静的検査
単独で実証されたあと、次の階段が **翻訳** ── 発見（`discover_NNN.json` の生存改変
or 敵対的入力の ungraceful クラッシュ）を、CC に投げられる修正 spec
（`spec_auto_NNN.md`）に変換する（`docs/DESIGN.md §9.5` 論点5）。

論点5 の核心は **翻訳は AI を一切使わない機械的なテンプレート穴埋め** であること。
発見は Phase 1 の発見チャンネルで曖昧さゼロに絞り込まれているので、grill-me で
詰めるべき穴がない。翻訳は「（AI の）修正係に指示書を手渡す」ステップ ── その
指示そのものは侵食不能な剛体であるべきで、AI が書くとスコープを広げたり制約を
緩めたりしうる。だから純粋な機械的テンプレ穴埋めにする。

spec_022 がテンプレ A（ミューテーション生存改変 → test-only 修正）を立てた次の段
として、spec_024 で **テンプレ B（敵対的入力の ungraceful クラッシュ → 本番コード
修正＋再現テスト）** を追加した。エントリ関数 :func:`translate_finding` は
``finding.channel`` でテンプレを選ぶ ── ``"mutation"`` → A、``"adversarial"`` → B、
それ以外は降格。テンプレ A / B のレンダラは独立しており（``_render_template_a``
/ ``_render_template_b``）、本文構造・制約逐語定数も別系統 ── 一方が他方を流用
しない（後で片方の制約が変わってももう一方が誤って影響を受けない）。

`spec_auto_NNN` は **別名前空間**。本モジュールは `<repo>/_ai_workspace/bridge/
inbox/` 配下に `spec_auto_NNN.md` プレフィクスで書き出す ── 人間が grill-me で
練った `spec_NNN` 連番と git 履歴・朝レポートで一目で判別できるように。連番は
inbox にすでに存在する `spec_auto_*.md` の最大 +1（存在しなければ 001 から）。

報告専用降格
------------

発見が選ばれたテンプレに収まらなければ（A: channel/status mismatch、file/line/mutation
欠落、B: parser/case_name/exception_type 欠落、等）、翻訳せず ``TranslateResult(
success=False, halt_reason="...")`` を返す。spec_auto は書き出されない。各テンプレ
固定リスト式の ``_why_template_*_does_not_fit`` で fit check を持つ ── 将来 channel
が増えたとき（AI 推論）も同じパターンで足せる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

__all__ = [
    "DEFAULT_INBOX_DIR_REL",
    "DEFAULT_OUTBOX_DIR_REL",
    "Finding",
    "TranslateResult",
    "translate_finding",
]


# --------------------------------------------------------------------------- #
# Layout constants
# --------------------------------------------------------------------------- #

# Where generated spec_auto_NNN.md files land. Same directory the human
# spec_NNN.md files live in — they're distinguished by filename prefix
# (spec_auto_*), so a glob in git log / morning brief / `ls` immediately
# tells human vs machine origin.
DEFAULT_INBOX_DIR_REL = Path("_ai_workspace") / "bridge" / "inbox"

# The bridge outbox path the fix-task is told to write its result to.
DEFAULT_OUTBOX_DIR_REL = Path("_ai_workspace") / "bridge" / "outbox"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    """One actionable finding, normalized for translation input.

    Mirrors the actionable-mutant shape ``ccd/discover.py`` writes to
    ``discover_NNN.json`` (``file`` / ``line`` / ``mutation`` / ``status`` /
    ``signature``), with ``channel`` and ``source_report`` added so the
    translator can record provenance into the generated spec_auto.

    spec_024 adds the adversarial-finding fields (``parser`` / ``case_name`` /
    ``exception_type`` / ``exception_message``) needed by template B. They
    default to empty strings so a mutation Finding constructed without them
    is unchanged from spec_022. For adversarial findings, ``file`` is the
    parser-derived source file (e.g. ``"ccd/protocol.py"`` for
    ``"ccd.protocol.parse_spec"``), ``line=0`` (the whole parser owns the
    case), and ``status="ungraceful"``.
    """

    channel: str
    file: str
    line: int
    mutation: str
    status: str
    signature: str = ""
    source_report: str = ""
    # spec_024 — template B / adversarial-finding fields.
    parser: str = ""
    case_name: str = ""
    exception_type: str = ""
    exception_message: str = ""

    @classmethod
    def from_dict(
        cls,
        payload: dict,
        *,
        channel: str = "mutation",
        source_report: str = "",
    ) -> Finding:
        """Build a Finding from a ``discover_NNN.json`` entry.

        Routes by ``channel`` to the right shape:

        - ``"mutation"`` → reads ``actionable`` entries
          (``file`` / ``line`` / ``mutation`` / ``status`` / ``signature``).
        - ``"adversarial"`` → reads ``findings`` entries
          (``parser`` / ``case`` / ``exception_type`` / ``exception_message``)
          and derives ``file`` from the parser dotted-name (spec_024).
        - other channels → fall through to the mutation shape, which will
          subsequently fail the template-A fit check.

        Missing fields are tolerated and fall to safe defaults so the
        downstream ``_why_template_*_does_not_fit`` check (not this
        constructor) is the single place that rejects ill-shaped findings.
        """

        if channel == "adversarial":
            return cls._from_adversarial_dict(payload, source_report=source_report)

        file = str(payload.get("file") or "")
        try:
            line = int(payload.get("line", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        mutation = str(payload.get("mutation") or "")
        status = str(payload.get("status") or "")
        signature = str(
            payload.get("signature") or f"{file}:{line}:{mutation}"
        )
        return cls(
            channel=channel,
            file=file,
            line=line,
            mutation=mutation,
            status=status,
            signature=signature,
            source_report=source_report,
        )

    @classmethod
    def _from_adversarial_dict(
        cls,
        payload: dict,
        *,
        source_report: str = "",
    ) -> Finding:
        """Build a template-B Finding from an adversarial JSON entry.

        The adversarial channel's JSON shape (see ``ccd/adversarial.py``) is::

            {"parser": "ccd.protocol.parse_spec",
             "case": "05_invalid_utf8_bytes",
             "exception_type": "UnicodeDecodeError",
             "exception_message": "..."}

        ``case`` is the JSON key; we accept ``case_name`` as an alias for
        flexibility. The ``file`` field is derived from the parser's dotted
        name so the loop's allowed-set (``[file, "tests/"]``) and the
        spec_auto's §5 declaration both know which production file the fix
        is allowed to touch.
        """

        parser = str(payload.get("parser") or "")
        case_name = str(payload.get("case") or payload.get("case_name") or "")
        exception_type = str(payload.get("exception_type") or "")
        exception_message = str(payload.get("exception_message") or "")
        file = _parser_dotted_to_file(parser)
        signature = str(
            payload.get("signature")
            or f"{parser}:{case_name}:{exception_type}"
        )
        return cls(
            channel="adversarial",
            file=file,
            line=0,
            mutation="",
            status="ungraceful",
            signature=signature,
            source_report=source_report,
            parser=parser,
            case_name=case_name,
            exception_type=exception_type,
            exception_message=exception_message,
        )


@dataclass(frozen=True)
class TranslateResult:
    """Return value of :func:`translate_finding`.

    ``success=True`` iff a ``spec_auto_NNN.md`` was written. On halt
    (template mismatch / report-only downgrade) ``spec_auto_path`` is
    ``None`` and ``halt_reason`` carries the why; the finding is echoed
    back so the caller (e.g. the spec_023 loop) can route it to the
    morning brief without re-loading the source JSON.
    """

    success: bool
    spec_auto_id: str
    spec_auto_path: Path | None
    finding: Finding
    template: str = ""
    halt_reason: str = ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def translate_finding(
    finding: Finding | dict,
    *,
    repo: Path,
    inbox_dir: Path | None = None,
    outbox_dir: Path | None = None,
    channel: str = "mutation",
    source_report: str = "",
    today: date | None = None,
) -> TranslateResult:
    """Translate one finding into a ``spec_auto_NNN.md`` (templates A or B).

    Pure, deterministic, AI-free: same finding + same ``today`` → byte-identical
    spec body. The function never calls an LLM, never reads the agent's
    self-report, and (apart from filesystem I/O) has no side effects.

    Routing (spec_022 + spec_024):

    - ``channel="mutation"`` → template A (test-only fix).
    - ``channel="adversarial"`` → template B (one production file + tests/).
    - Other channels → ``success=False`` (report-only downgrade — no template
      yet).

    Arguments
    ---------
    finding : Finding | dict
        The actionable finding. ``dict`` inputs are normalized via
        :meth:`Finding.from_dict` so the caller can pass items directly out
        of ``discover_NNN.json``'s ``actionable`` / ``findings`` list.
    repo : Path
        Repo root. ``inbox_dir`` / ``outbox_dir`` resolve relative to it.
    inbox_dir, outbox_dir : Path | None
        Override the default ``<repo>/_ai_workspace/bridge/{inbox,outbox}/``
        locations. Used by tests; production leaves these ``None``.
    channel : str
        Channel of the finding. Used only when ``finding`` is a ``dict``
        (the loader needs to know the JSON shape). Ignored when ``finding``
        is already a ``Finding`` — its own ``channel`` attribute drives the
        routing.
    source_report : str
        Optional pointer back to the originating ``discover_NNN.json`` —
        recorded verbatim in the spec_auto's §7 metadata block for audit.
    today : date | None
        Override today's date (UTC). Tests pass a fixed date to keep the
        determinism check exact; production leaves it ``None``.
    """

    repo = Path(repo).resolve()
    if inbox_dir is None:
        inbox_dir = repo / DEFAULT_INBOX_DIR_REL
    if outbox_dir is None:
        outbox_dir = repo / DEFAULT_OUTBOX_DIR_REL

    if isinstance(finding, dict):
        finding = Finding.from_dict(
            finding,
            channel=channel,
            source_report=source_report,
        )
    elif source_report and not finding.source_report:
        # Allow caller to attach a source_report when passing a pre-built
        # Finding that didn't have one — handy when the dataclass came from
        # a different layer than the JSON loader.
        finding = Finding(
            channel=finding.channel,
            file=finding.file,
            line=finding.line,
            mutation=finding.mutation,
            status=finding.status,
            signature=finding.signature,
            source_report=source_report,
            parser=finding.parser,
            case_name=finding.case_name,
            exception_type=finding.exception_type,
            exception_message=finding.exception_message,
        )

    # Route by channel. Each template owns its own fit-check + renderer so a
    # change to one cannot leak into the other.
    if finding.channel == "mutation":
        return _translate_template_a(
            finding=finding,
            repo=repo,
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
            today=today,
        )
    if finding.channel == "adversarial":
        return _translate_template_b(
            finding=finding,
            repo=repo,
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
            today=today,
        )

    return TranslateResult(
        success=False,
        spec_auto_id="",
        spec_auto_path=None,
        finding=finding,
        template="",
        halt_reason=(
            f"finding does not fit any template — downgraded to "
            f"report-only: channel={finding.channel!r} has no translator "
            "(template A is for 'mutation', template B is for 'adversarial')"
        ),
    )


def _translate_template_a(
    *,
    finding: Finding,
    repo: Path,
    inbox_dir: Path,
    outbox_dir: Path,
    today: date | None,
) -> TranslateResult:
    """Run the template-A pipeline (mutation channel only)."""

    fit_reason = _why_template_a_does_not_fit(finding)
    if fit_reason:
        return TranslateResult(
            success=False,
            spec_auto_id="",
            spec_auto_path=None,
            finding=finding,
            template="",
            halt_reason=(
                f"finding does not fit template A — downgraded to "
                f"report-only: {fit_reason}"
            ),
        )

    inbox_dir.mkdir(parents=True, exist_ok=True)
    seq = _next_spec_auto_seq(inbox_dir)
    spec_auto_id = f"spec_auto_{seq:03d}"
    spec_auto_path = inbox_dir / f"{spec_auto_id}.md"

    today_str = (today or datetime.now(UTC).date()).isoformat()
    body = _render_template_a(
        spec_auto_id=spec_auto_id,
        finding=finding,
        today_str=today_str,
        outbox_dir_rel=_relpath_or_str(outbox_dir, repo),
    )
    spec_auto_path.write_text(body, encoding="utf-8")

    return TranslateResult(
        success=True,
        spec_auto_id=spec_auto_id,
        spec_auto_path=spec_auto_path,
        finding=finding,
        template="A",
        halt_reason="",
    )


def _translate_template_b(
    *,
    finding: Finding,
    repo: Path,
    inbox_dir: Path,
    outbox_dir: Path,
    today: date | None,
) -> TranslateResult:
    """Run the template-B pipeline (adversarial channel only)."""

    fit_reason = _why_template_b_does_not_fit(finding)
    if fit_reason:
        return TranslateResult(
            success=False,
            spec_auto_id="",
            spec_auto_path=None,
            finding=finding,
            template="",
            halt_reason=(
                f"finding does not fit template B — downgraded to "
                f"report-only: {fit_reason} (channel=adversarial)"
            ),
        )

    inbox_dir.mkdir(parents=True, exist_ok=True)
    seq = _next_spec_auto_seq(inbox_dir)
    spec_auto_id = f"spec_auto_{seq:03d}"
    spec_auto_path = inbox_dir / f"{spec_auto_id}.md"

    today_str = (today or datetime.now(UTC).date()).isoformat()
    body = _render_template_b(
        spec_auto_id=spec_auto_id,
        finding=finding,
        today_str=today_str,
        outbox_dir_rel=_relpath_or_str(outbox_dir, repo),
    )
    spec_auto_path.write_text(body, encoding="utf-8")

    return TranslateResult(
        success=True,
        spec_auto_id=spec_auto_id,
        spec_auto_path=spec_auto_path,
        finding=finding,
        template="B",
        halt_reason="",
    )


# --------------------------------------------------------------------------- #
# Template fit
# --------------------------------------------------------------------------- #


def _why_template_a_does_not_fit(finding: Finding) -> str:
    """Return non-empty reason iff the finding cannot be translated by template A.

    Template A is "mutation survivor → test-only fix" — anything else
    downgrades to report-only. The checks are intentionally strict: if any
    required field is empty/zero/wrong, we'd rather halt than emit a
    nonsense spec_auto that the loop dispatches and the guard then has to
    catch on the back end.
    """

    if finding.channel != "mutation":
        return (
            f"channel={finding.channel!r} is not 'mutation' — "
            "template A is for mutation survivors only "
            "(template B for adversarial findings is spec_024)"
        )
    if finding.status != "survived":
        return (
            f"status={finding.status!r} is not 'survived' — "
            "template A only addresses surviving mutants "
            "(killed/timeout/incompetent mutants are not actionable)"
        )
    if not finding.file:
        return "finding.file is empty — template A needs a target source file"
    if finding.line <= 0:
        return (
            f"finding.line={finding.line} is not a positive integer — "
            "template A needs a concrete line number to point the new test at"
        )
    if not finding.mutation:
        return (
            "finding.mutation is empty — template A needs the mutation text "
            "to quote as the evidence anchor"
        )
    return ""


def _why_template_b_does_not_fit(finding: Finding) -> str:
    """Return non-empty reason iff the finding cannot be translated by template B.

    Template B is "adversarial ungraceful crash → production fix + reproducer
    test". Required: channel="adversarial", non-empty parser/case_name/
    exception_type, and a derivable target file. The status check is
    intentionally lax — the adversarial channel only emits a JSON ``findings``
    entry when the outcome was already ungraceful, so we trust the producer
    and key the fit on the *information* needed to address the case (parser,
    case_name, exception_type) rather than re-asserting "ungraceful".
    """

    if finding.channel != "adversarial":
        return (
            f"channel={finding.channel!r} is not 'adversarial' — "
            "template B is for adversarial ungraceful findings only "
            "(template A for mutation survivors is spec_022)"
        )
    if not finding.parser:
        return (
            "finding.parser is empty — template B needs the parser dotted-name "
            "(e.g., 'ccd.protocol.parse_spec') from the adversarial channel report"
        )
    if not finding.case_name:
        return (
            "finding.case_name is empty — template B needs the broken-input "
            "case name (e.g., '05_invalid_utf8_bytes') so the reproducer "
            "test can rebuild the same fixture"
        )
    if not finding.exception_type:
        return (
            "finding.exception_type is empty — template B needs the ungraceful "
            "exception type (e.g., 'UnicodeDecodeError') to assert against "
            "in the reproducer test"
        )
    if not finding.file:
        return (
            f"could not derive target file from parser={finding.parser!r} — "
            "expected a dotted name like 'ccd.protocol.parse_spec' that "
            "maps to 'ccd/protocol.py'"
        )
    return ""


# --------------------------------------------------------------------------- #
# Template A renderer
# --------------------------------------------------------------------------- #

# Verbatim constraint phrases. Lifted into module-level constants so tests
# can pin the exact wording — these strings ARE the "侵食不能な剛体" the
# spec calls out (论点5: instruction must not be softened by AI). Renaming
# any of these is a behaviour change, not a cleanup.
#
# spec_026 §2-1 — the autonomous-fix loop's `_run_auto_fix_loop` runs the
# dispatched fix on a feature branch (``auto/spec_auto_NNN``) and reads
# the resulting commits as the loop's R4 / R5 / guard input. If the fix
# agent does not commit (the human-spec idiom "push しない／ブランチ操作・
# merge しない" can be misread as "don't commit either"), the loop sees
# "result file present, 0 commits" and HALTs with ``agent_misread``. The
# COMMIT_REQUIRED / NO_PUSH_BRANCH_MERGE clauses below resolve that
# ambiguity verbatim: commit IS required, push / branch-switch / new
# branch / merge are forbidden — but commit is NOT a forbidden git op.
_CONSTRAINT_COMMIT_REQUIRED = (
    "**修正は現在の feature branch（`auto/<このタスクの spec_auto_id>`）に "
    "`git commit` せよ**（論理単位で、メッセージは任意）。あなたは既にこの "
    "feature branch 上で起動されている ── 新規テストを書き終えたら、必ずその "
    "branch に commit を積むこと（**コミットは禁止ではなく必須**）。commit が "
    "0 件のまま result ファイルだけ書いて終了すると、自律修正ループはこのタスクを "
    "`agent_misread` として HALT する（spec_026 §1 の偽 HALT の原因）。"
)
_CONSTRAINT_NO_PUSH_BRANCH_MERGE = (
    "**`git push` の実行・別ブランチへの切り替え（`git checkout main` 等）・"
    "新規ブランチの作成・`main` への merge は禁止**。push と main への local "
    "merge は自律修正ループ側（`ccd/nightly.py` の `GitOps` seam）が行う ── "
    "本タスクの担当範囲は feature branch 上で commit するところまで。"
    "ここで禁止しているのは「push しない／他ブランチに移らない／自分で merge "
    "しない」のみであって、**「commit しない」ではない**（混同しないこと ── "
    "前者の文言を後者と読み違えるのが spec_026 で直したバグの原因）。"
)
_CONSTRAINT_TEST_ONLY = (
    "**テストの追加のみ** が許可される。本番コード（`ccd/` 配下含む、テスト "
    "ディレクトリ外のすべて）は **1 バイトも変更してはならない**。本発見は "
    "テスト隙間であり、コードのバグではない。"
)
_CONSTRAINT_EXISTING_TESTS_IMMUTABLE = (
    "**既存テストの削除・改変は禁止**。`tests/` 配下の既存 `.py` ファイルの "
    "`-` 行（行削除・行変更）は一切許可されない（`tests/` 追加のみ規約）。"
)
_CONSTRAINT_NO_SKIP_MARKERS = (
    "**新規の `@pytest.mark.skip` / `@pytest.mark.skipif` / `@pytest.mark.xfail` "
    "/ `@unittest.skip` / `pytest.skip(...)` / `pytest.xfail(...)` マーカーを "
    "追加してはならない**（新規テストファイルへの追加も含む）。"
)
_CONSTRAINT_DETERMINISTIC = (
    "**新規テストは決定的でなければならない**（時刻・乱数・外部 I/O・並列順序 "
    "に依存しない、繰り返し同じ結果が出る）。"
)
_CONSTRAINT_ALLOWED_SET = (
    "**触れてよいファイルは `tests/` 配下のみ**（R1 ファイル許可リスト、§5 で "
    "逐語宣言）。それ以外への 1 バイトの変更も HALT 対象。"
)


def _render_template_a(
    *,
    spec_auto_id: str,
    finding: Finding,
    today_str: str,
    outbox_dir_rel: str,
) -> str:
    old_text, new_text = _split_mutation(finding.mutation)

    result_id = "result_" + spec_auto_id[len("spec_"):]
    output_dest = f"{outbox_dir_rel}/{result_id}.md"

    title = f"`{finding.file}:{finding.line}` の生存改変を殺す（テンプレ A）"

    parts: list[str] = [
        f"# {spec_auto_id}: {title}",
        "",
        "- **Author**: ccd translate (機械生成・AI 不使用、spec_022)",
        f"- **Created**: {today_str}",
        "- **Target**: Claude Code",
        "- **Status**: pending",
        "- **Type**: autonomous-fix (mutation survivor — template A)",
        f"- **Channel**: {finding.channel}",
        "- **Template**: A (test-only)",
        f"- **Source signature**: `{finding.signature}`",
    ]
    if finding.source_report:
        parts.append(f"- **Source report**: `{finding.source_report}`")
    parts += [
        "",
        "## 1. 文脈（事実）",
        "",
        f"mutmut が **`{finding.file}:{finding.line}`** に次の改変を当てた:",
        "",
        "```",
        finding.mutation,
        "```",
    ]
    if old_text is not None and new_text is not None:
        parts += [
            "",
            f"- **改変前**: `{old_text}`",
            f"- **改変後**: `{new_text}`",
        ]
    parts += [
        "",
        f"この改変を当てても **全テストスイートは緑のまま** だった "
        f"(mutmut 報告: `status=survived` / signature: `{finding.signature}`)。"
        f"つまり、このロジックを縛っているテストが現状存在しない ── "
        f"**テスト隙間** である。",
        "",
        f"> 証拠アンカー (mutmut 出力の生引用): `{finding.signature}`",
        "",
        "## 2. やってほしいこと",
        "",
        "このロジックを縛るテストを **1 本だけ** 書く。",
        "",
        f"- 改変 (`{finding.mutation}`) を `{finding.file}:{finding.line}` に "
        f"当てた状態で、新規テストは **特定アサーションで失敗** すること "
        "(generic な `pytest.fail()` や `assert False` ではなく、対象ロジック "
        "の出力差を捕まえる assert)。",
        "- 現行 `main` (改変なし) では新規テストは **成功** すること。",
        "- 既存テストはすべて緑のまま (`pytest -q` で全件 pass)。",
        f"- 追加するテスト数はちょうど **+1**（{_TEST_COUNT_GATE_NOTE}）。",
        "",
        "## 3. 制約（テンプレ A 逐語、本タスクで侵食してはならない）",
        "",
        f"- {_CONSTRAINT_COMMIT_REQUIRED}",
        f"- {_CONSTRAINT_NO_PUSH_BRANCH_MERGE}",
        f"- {_CONSTRAINT_TEST_ONLY}",
        f"- {_CONSTRAINT_EXISTING_TESTS_IMMUTABLE}",
        f"- {_CONSTRAINT_NO_SKIP_MARKERS}",
        f"- {_CONSTRAINT_DETERMINISTIC}",
        f"- {_CONSTRAINT_ALLOWED_SET}",
        "",
        "## 4. 検証要件",
        "",
        "修正後、以下をすべて満たすこと:",
        "",
        f"- [ ] 新規テストは改変 (`{finding.mutation}` at "
        f"`{finding.file}:{finding.line}`) を当てると **特定アサーションで失敗** する。",
        "- [ ] 現行 `main` (改変なし) では新規テストは成功する。",
        f"- [ ] `pytest -q` で **全スイート緑** "
        f"（{_TEST_COUNT_GATE_NOTE}）。",
        "- [ ] `ruff check .` クリーン。",
        "- [ ] `git diff` を `ccd guard --template A --allowed tests/` で検査して "
        "**HALT しない** こと（R1/R2 違反なし、spec_021 の静的ガード）。",
        "",
        "## 5. 許可ファイル集合（R1 ファイル許可リスト、逐語宣言）",
        "",
        "**本タスクが触れてよいファイル** ＝ **`tests/` のみ**。",
        "",
        "具体的に:",
        "",
        "- `tests/` 配下のすべての `.py` ファイルへの **追記** "
        "（既存ファイルへの新規テスト関数追加 or 新規テストファイル作成）。",
        "",
        "**本タスクが触れてはならないファイル**（diff 1 バイトでも HALT）:",
        "",
        f"- `{finding.file}` を含む `ccd/` 以下のすべての本番コード",
        "- `_ai_workspace/` 以下のすべて（プロファイル / 発見レポート / blocklist 等）",
        "- `docs/` 以下のすべて",
        "- `pyproject.toml` / `ccd/__init__.py` / `CHANGELOG.md` "
        "（version bump も本タスクでは行わない）",
        "- `.github/` / `.pre-commit-config.yaml` / `setup.py` / `setup.cfg`",
        "",
        "この許可集合は `ccd guard` の `--allowed tests/` 引数 "
        "と一致する（呼び出し側はこの宣言を直接読んで R1 を適用する）。",
        "",
        "## 6. 出力先",
        "",
        f"修正タスクの result は `{output_dest}` に書くこと "
        f"(spec_auto_NNN ↔ result_auto_NNN の対応、`ccd/protocol.py` の "
        "`_derive_result_id` と整合)。",
        "",
        "## 7. メタ情報（機械生成）",
        "",
        "- このタスクは `ccd translate`（spec_022 / v0.12.0）によって発見 1 件から "
        "**AI 不使用の機械的テンプレート穴埋め** で生成された "
        "(`docs/DESIGN.md §9.5` 論点5)。",
        f"- 翻訳元発見: `{finding.signature}` "
        f"(channel=`{finding.channel}`, status=`{finding.status}`)",
    ]
    if finding.source_report:
        parts.append(f"- 翻訳元レポート: `{finding.source_report}`")
    parts += [
        "- このタスクは `spec_auto_*` **別名前空間** に属し、人間が grill-me で "
        "練った `spec_NNN` 連番とは別管理（git 履歴・朝レポートで「機械が書いた "
        "spec」と一目で判別できるように）。",
        "- 翻訳器は AI を呼ばない・決定的 ── 同じ発見 → 同じ spec_auto 本文。",
        "",
    ]
    return "\n".join(parts)


# Pulled out to keep the §2 and §4 wording in lock-step. Tests pin both
# bullets reference the same gate so any future edit touches one place.
_TEST_COUNT_GATE_NOTE = (
    "ベースラインのテスト数 + 1 = 新規テスト 1 件のみ追加、"
    "既存テストの削除・変更は §3 で禁止"
)


# --------------------------------------------------------------------------- #
# Template B renderer (spec_024)
# --------------------------------------------------------------------------- #

# Verbatim constraint phrases for template B. Same "侵食不能な剛体" pattern
# as template A — module-level constants so the test pins exact wording and
# any change is a deliberate behaviour edit, not a cleanup. Template B's
# constraints are deliberately distinct from A's (production-fix scope vs
# test-only scope), so we keep two separate sets rather than parameterizing
# one set with placeholders.
#
# spec_026 §2-1 — same commit-required / no-push-branch-merge clauses as
# template A. The git workflow is the same for both templates (the loop
# always dispatches on a feature branch and merges locally), so the
# wording is parallel; per spec_022/024 docstring policy we keep two
# separate copies so a future edit to one cannot silently leak into the
# other.
_CONSTRAINT_B_COMMIT_REQUIRED = (
    "**修正（本番コードと再現テストの両方）は現在の feature branch "
    "（`auto/<このタスクの spec_auto_id>`）に `git commit` せよ**（論理単位で、"
    "メッセージは任意）。あなたは既にこの feature branch 上で起動されている "
    "── 修正＋再現テストを書き終えたら、必ずその branch に commit を積むこと "
    "（**コミットは禁止ではなく必須**）。commit が 0 件のまま result ファイル "
    "だけ書いて終了すると、自律修正ループはこのタスクを `agent_misread` として "
    "HALT する（spec_026 §1 の偽 HALT の原因）。"
)
_CONSTRAINT_B_NO_PUSH_BRANCH_MERGE = (
    "**`git push` の実行・別ブランチへの切り替え（`git checkout main` 等）・"
    "新規ブランチの作成・`main` への merge は禁止**。push と main への local "
    "merge は自律修正ループ側（`ccd/nightly.py` の `GitOps` seam）が行う ── "
    "本タスクの担当範囲は feature branch 上で commit するところまで。"
    "ここで禁止しているのは「push しない／他ブランチに移らない／自分で merge "
    "しない」のみであって、**「commit しない」ではない**（混同しないこと ── "
    "前者の文言を後者と読み違えるのが spec_026 で直したバグの原因）。"
)
_CONSTRAINT_B_GRACEFUL_FAIL_NOT_ACCEPT = (
    "**「優雅に失敗させる」のであって「成功させる」ではない**。"
    "壊れた入力を **黙って受理してはならない**。修正後の本番コードは、当該の壊れた "
    "入力に対して CCD の許可リスト例外（`ValueError` / `pydantic.ValidationError` / "
    "`json.JSONDecodeError` / `FileNotFoundError`）のいずれかをクリーンに raise する "
    "こと。例外を吐かずに値を返したら R5 失敗扱い。"
)
_CONSTRAINT_B_EXISTING_TESTS_IMMUTABLE = (
    "**既存テストの削除・改変は禁止**。`tests/` 配下の既存 `.py` ファイルの "
    "`-` 行（行削除・行変更）は一切許可されない（`tests/` への変更は追加のみ）。"
)
_CONSTRAINT_B_NO_SKIP_MARKERS = (
    "**新規の `@pytest.mark.skip` / `@pytest.mark.skipif` / `@pytest.mark.xfail` "
    "/ `@unittest.skip` / `pytest.skip(...)` / `pytest.xfail(...)` マーカーを "
    "追加してはならない**（再現テストへの付与も含む）。"
)
_CONSTRAINT_B_ALLOWED_SET = (
    "**触れてよいファイル ＝ 名指しの本番ファイル 1 つ ＋ `tests/`**（R1 ファイル "
    "許可リスト、§5 で逐語宣言）。それ以外への 1 バイトの変更も HALT 対象。"
    "本番ファイルへの修正は当該パーサのスコープに限る（同ファイル内の他関数の "
    "リファクタや無関係なクリーンアップは scope creep として R3 で halt）。"
)
_CONSTRAINT_B_REPRODUCER_GATE = (
    "**追加するテストは 1 本のみ**（当該壊れ方ケースの再現テスト）。現行 `main` "
    "（本番修正前）では **無様なクラッシュを再現して失敗** すること、修正後は "
    "**クリーンな許可リスト例外を assert** して成功すること。`pytest.raises("
    "ValueError|json.JSONDecodeError|...)` のような具体的な型 assert を使う "
    "（generic な `pytest.raises(Exception)` ではなく）。"
)


def _render_template_b(
    *,
    spec_auto_id: str,
    finding: Finding,
    today_str: str,
    outbox_dir_rel: str,
) -> str:
    result_id = "result_" + spec_auto_id[len("spec_"):]
    output_dest = f"{outbox_dir_rel}/{result_id}.md"

    title = (
        f"`{finding.parser}` × `{finding.case_name}` の "
        f"`{finding.exception_type}` 漏洩を直す（テンプレ B）"
    )

    # Truncate the exception message at render time so the spec body stays
    # readable; the source_report still has the full string for audit.
    short_msg = finding.exception_message
    if len(short_msg) > 200:
        short_msg = short_msg[:200] + "…"

    parts: list[str] = [
        f"# {spec_auto_id}: {title}",
        "",
        "- **Author**: ccd translate (機械生成・AI 不使用、spec_024)",
        f"- **Created**: {today_str}",
        "- **Target**: Claude Code",
        "- **Status**: pending",
        "- **Type**: autonomous-fix (adversarial ungraceful crash — template B)",
        f"- **Channel**: {finding.channel}",
        "- **Template**: B (production-fix + reproducer test)",
        f"- **Target parser**: `{finding.parser}`",
        f"- **Target file**: `{finding.file}`",
        f"- **Adversarial case**: `{finding.case_name}`",
        f"- **Leaking exception**: `{finding.exception_type}`",
        f"- **Source signature**: `{finding.signature}`",
    ]
    if finding.source_report:
        parts.append(f"- **Source report**: `{finding.source_report}`")
    parts += [
        "",
        "## 1. 文脈（事実）",
        "",
        f"敵対的入力チャンネル（spec_015）が **`{finding.parser}`** に対して "
        f"ケース **`{finding.case_name}`** を投げたところ、CCD 定義の許可リスト "
        f"（`ValueError` / `pydantic.ValidationError` / `json.JSONDecodeError` / "
        "`FileNotFoundError`、ただし `UnicodeError` 系は ungraceful 扱い）に "
        f"含まれない例外 **`{finding.exception_type}`** が漏洩した:",
        "",
        "```",
        f"{finding.exception_type}: {short_msg}",
        "```",
        "",
        f"`ccd.adversarial.default_cases()` の `{finding.case_name}` がこの fixture "
        "の定義。同 fixture を再構成して当該パーサに通すと、現行 `main` ではこの "
        "ungraceful クラッシュが再現する（**それが本タスクの再現テストが最初に "
        "失敗しなければならない理由**）。",
        "",
        f"> 証拠アンカー: `{finding.signature}`",
        "",
        "## 2. やってほしいこと",
        "",
        f"(1) **`{finding.file}` の `{finding.parser}` を修正** し、当該壊れた "
        f"入力 (`{finding.case_name}`) に対して **CCD 定義の優雅なエラー** "
        "（許可リスト例外）を raise するようにする。`UnicodeDecodeError` を "
        "そのまま漏らさず、`ValueError` などにラップする（または事前に "
        "テキストデコードを試みて失敗時に明示的に `ValueError` を raise する）。",
        "",
        "(2) **`tests/` 配下に再現テストを 1 本追加** ── 当該 fixture を "
        "in-process でパーサに通し:",
        "",
        f"- 現行 `main`（本番修正前）では **`{finding.exception_type}` で無様に "
        "失敗** を再現する（つまり、本タスクが本番を直す前にテストだけ追加すると "
        "そのテストは赤くなる）。",
        "- 修正後は **`pytest.raises(ValueError)` 等の許可リスト例外 assert** で "
        "成功する（クリーンなエラーをアサート）。",
        "- **「黙って受理」は許さない** ── パーサが値を返したらテストは失敗扱い。",
        f"- 追加するテスト数はちょうど **+1**（{_TEST_COUNT_GATE_NOTE_B}）。",
        "",
        "## 3. 制約（テンプレ B 逐語、本タスクで侵食してはならない）",
        "",
        f"- {_CONSTRAINT_B_COMMIT_REQUIRED}",
        f"- {_CONSTRAINT_B_NO_PUSH_BRANCH_MERGE}",
        f"- {_CONSTRAINT_B_GRACEFUL_FAIL_NOT_ACCEPT}",
        f"- {_CONSTRAINT_B_REPRODUCER_GATE}",
        f"- {_CONSTRAINT_B_EXISTING_TESTS_IMMUTABLE}",
        f"- {_CONSTRAINT_B_NO_SKIP_MARKERS}",
        f"- {_CONSTRAINT_B_ALLOWED_SET}",
        "",
        "## 4. 検証要件",
        "",
        "修正後、以下をすべて満たすこと:",
        "",
        f"- [ ] 再現テストは **修正前（本番未編集）の `main` で失敗** する "
        f"（`{finding.exception_type}` を再現）。",
        "- [ ] 再現テストは **修正後に成功** する（許可リスト例外を `pytest.raises` "
        "で受ける、`Exception` のような generic 型ではなく具体的な型 assert）。",
        f"- [ ] 修正後のパーサは当該壊れ入力 (`{finding.case_name}`) を **黙って "
        "受理しない**（値を返さず、必ず許可リスト例外を raise する）。",
        f"- [ ] `pytest -q` で **全スイート緑** "
        f"（{_TEST_COUNT_GATE_NOTE_B}）。",
        "- [ ] `ruff check .` クリーン。",
        f"- [ ] `git diff` を `ccd guard --template B --allowed {finding.file} "
        "tests/` で検査して **HALT しない** こと（R1/R2/R3 違反なし、R3＝本番 "
        "diff サイズ上限あり、spec_021 の静的ガード）。",
        "",
        "## 5. 許可ファイル集合（R1 ファイル許可リスト、逐語宣言）",
        "",
        f"**本タスクが触れてよいファイル** ＝ **`{finding.file}` ＋ `tests/`** の 2 つだけ。",
        "",
        "具体的に:",
        "",
        f"- `{finding.file}` — 当該パーサ `{finding.parser}` の修正のみ "
        "（無関係な関数のリファクタは scope creep、R3 で HALT）。",
        "- `tests/` 配下のすべての `.py` ファイルへの **追記** "
        "（既存ファイルへの新規テスト関数追加 or 新規テストファイル作成）。",
        "",
        "**本タスクが触れてはならないファイル**（diff 1 バイトでも HALT）:",
        "",
        f"- `{finding.file}` 以外の `ccd/` 配下のすべての本番コード",
        "- `_ai_workspace/` 以下のすべて（プロファイル / 発見レポート / blocklist 等）",
        "- `docs/` 以下のすべて",
        "- `pyproject.toml` / `ccd/__init__.py` / `CHANGELOG.md` "
        "（version bump も本タスクでは行わない）",
        "- `.github/` / `.pre-commit-config.yaml` / `setup.py` / `setup.cfg`",
        "",
        "この許可集合は `ccd guard` の "
        f"`--template B --allowed {finding.file} tests/` 引数と一致する "
        "（呼び出し側はこの宣言を直接読んで R1 を適用する）。",
        "",
        "## 6. 出力先",
        "",
        f"修正タスクの result は `{output_dest}` に書くこと "
        f"(spec_auto_NNN ↔ result_auto_NNN の対応、`ccd/protocol.py` の "
        "`_derive_result_id` と整合)。",
        "",
        "## 7. メタ情報（機械生成）",
        "",
        "- このタスクは `ccd translate`（spec_024 / v0.14.0）によって発見 1 件から "
        "**AI 不使用の機械的テンプレート穴埋め** で生成された "
        "(`docs/DESIGN.md §9.5` 論点5)。",
        f"- 翻訳元発見: `{finding.signature}` "
        f"(channel=`{finding.channel}`, parser=`{finding.parser}`, "
        f"case=`{finding.case_name}`, exception=`{finding.exception_type}`)",
    ]
    if finding.source_report:
        parts.append(f"- 翻訳元レポート: `{finding.source_report}`")
    parts += [
        "- このタスクは `spec_auto_*` **別名前空間** に属し、人間が grill-me で "
        "練った `spec_NNN` 連番とは別管理（git 履歴・朝レポートで「機械が書いた "
        "spec」と一目で判別できるように）。",
        "- 翻訳器は AI を呼ばない・決定的 ── 同じ発見 → 同じ spec_auto 本文。",
        "",
    ]
    return "\n".join(parts)


_TEST_COUNT_GATE_NOTE_B = (
    "ベースラインのテスト数 + 1 = 再現テスト 1 件のみ追加、"
    "既存テストの削除・変更は §3 で禁止"
)


# --------------------------------------------------------------------------- #
# Parser dotted-name → source file resolver (spec_024)
# --------------------------------------------------------------------------- #


def _parser_dotted_to_file(parser: str) -> str:
    """Convert a parser dotted-name to its source file path.

    ``"ccd.protocol.parse_spec"`` → ``"ccd/protocol.py"`` ── drops the last
    segment (the function name) and joins the module parts with ``/``. We do
    not attempt to import the module: the result is purely textual so this
    function is safe to call against unknown / stale parser names without
    side effects, and stays deterministic.

    Returns ``""`` when the parser cannot be cleanly split (no dots, empty
    segments anywhere, leading/trailing dot). That empty result then fails
    the template-B fit check with a clear "could not derive target file"
    reason rather than producing a bogus path.
    """

    if not parser or "." not in parser:
        return ""
    parts = parser.split(".")
    if len(parts) < 2:
        return ""
    # Reject any malformed dotted-name with empty segments (leading,
    # trailing, or doubled dots). A real parser name like
    # ``ccd.protocol.parse_spec`` has no empty parts; ``"trailing.dot."``
    # has a trailing empty function-name slot we cannot honour.
    if any(not p for p in parts):
        return ""
    module_parts = parts[:-1]
    return "/".join(module_parts) + ".py"


def _split_mutation(mutation: str) -> tuple[str | None, str | None]:
    """Try to split a mutation string of the form ``<old> → <new>``.

    mutmut always emits the unicode right-arrow ``→`` for survivor
    descriptions (see ``ccd/discover.py::_parse_mutmut_show``). Older or
    ad-hoc strings might use ``->``; we accept both as a small bit of
    polish. When the string is unsplittable (no arrow), return ``(None,
    None)`` and the renderer omits the before/after bullets — the full
    mutation string is still quoted verbatim in the §1 code block.
    """

    for sep in (" → ", " -> "):
        if sep in mutation:
            old, new = mutation.split(sep, 1)
            return old.strip(), new.strip()
    return None, None


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #


_SPEC_AUTO_RE = re.compile(r"^spec_auto_(\d+)\.md$")


def _next_spec_auto_seq(inbox_dir: Path) -> int:
    """Return ``max(existing spec_auto_NNN) + 1`` (or 1 if none).

    Scanning the inbox at write time means concurrent runs against the
    same inbox cannot reuse a number — but the autonomous-fix loop is
    single-threaded by design (论点7 tick-controller), so we treat this
    as best-effort with no file lock.
    """

    nums: list[int] = []
    if inbox_dir.exists():
        for p in inbox_dir.glob("spec_auto_*.md"):
            m = _SPEC_AUTO_RE.match(p.name)
            if m:
                nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def _relpath_or_str(target: Path, base: Path) -> str:
    """``target`` relative to ``base`` as a posix string; falls back to abs."""

    try:
        rel = target.resolve().relative_to(base.resolve())
    except ValueError:
        return str(target).replace("\\", "/")
    return str(rel).replace("\\", "/")
