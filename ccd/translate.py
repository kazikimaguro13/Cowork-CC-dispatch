"""ccd translate — finding → fix-spec translator, template A (spec_022).

v2 Phase 2 の 2 本目。spec_021 で **インチキ修正ガード**（`ccd/guard.py`）が静的検査
単独で実証されたあと、次の階段が **翻訳** ── 発見（`discover_NNN.json` の生存改変）
を、CC に投げられる修正 spec（`spec_auto_NNN.md`）に変換する
（`docs/DESIGN.md §9.5` 論点5）。

論点5 の核心は **翻訳は AI を一切使わない機械的なテンプレート穴埋め** であること。
発見は Phase 1 の発見チャンネルで曖昧さゼロに絞り込まれているので、grill-me で
詰めるべき穴がない。翻訳は「（AI の）修正係に指示書を手渡す」ステップ ── その
指示そのものは侵食不能な剛体であるべきで、AI が書くとスコープを広げたり制約を
緩めたりしうる。だから純粋な機械的テンプレ穴埋めにする。

spec_022 はテンプレ A（ミューテーション生存改変 → test-only 修正）のみを実装する。
テンプレ B（敵対的入力 → 本番コード修正）は spec_024 の責務 ── 本モジュールに
事前の seam を仕込まず、template="B" を要求された場合は素直に halt させる
（将来テンプレ B 用エントリが増えたら同じ場所に追加する）。

`spec_auto_NNN` は **別名前空間**。本モジュールは `<repo>/_ai_workspace/bridge/
inbox/` 配下に `spec_auto_NNN.md` プレフィクスで書き出す ── 人間が grill-me で
練った `spec_NNN` 連番と git 履歴・朝レポートで一目で判別できるように。連番は
inbox にすでに存在する `spec_auto_*.md` の最大 +1（存在しなければ 001 から）。

報告専用降格
------------

発見がテンプレ A にきれいに収まらなければ（channel が mutation でない、status が
survived でない、file/line/mutation が空、等）、翻訳せず ``TranslateResult(
success=False, halt_reason="...")`` を返す。spec_auto は書き出されない。テンプレ A
（ミューテーション生存改変）は構造上常に収まるはずだが、原則として明文化・実装
する ── 将来チャンネルが増えたとき（敵対的・AI 推論）の保険。
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
    """

    channel: str
    file: str
    line: int
    mutation: str
    status: str
    signature: str = ""
    source_report: str = ""

    @classmethod
    def from_dict(
        cls,
        payload: dict,
        *,
        channel: str = "mutation",
        source_report: str = "",
    ) -> Finding:
        """Build a Finding from a `discover_NNN.json` actionable entry.

        Missing fields are tolerated and fall to safe defaults so the
        downstream "fits template A?" check (not this constructor) is the
        single place that rejects ill-shaped findings. The signature is
        recomputed from file/line/mutation when absent.
        """

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
    """Translate one finding into a ``spec_auto_NNN.md`` (template A only).

    Pure, deterministic, AI-free: same finding + same ``today`` → byte-identical
    spec body. The function never calls an LLM, never reads the agent's
    self-report, and (apart from filesystem I/O) has no side effects.

    Arguments
    ---------
    finding : Finding | dict
        The actionable finding. ``dict`` inputs are normalized via
        :meth:`Finding.from_dict` so the caller can pass items directly out
        of ``discover_NNN.json``'s ``actionable`` list.
    repo : Path
        Repo root. ``inbox_dir`` / ``outbox_dir`` resolve relative to it.
    inbox_dir, outbox_dir : Path | None
        Override the default ``<repo>/_ai_workspace/bridge/{inbox,outbox}/``
        locations. Used by tests; production leaves these ``None``.
    channel : str
        Channel the finding came from. Only ``"mutation"`` fits template A.
        Other channels (``"adversarial"`` / ``"ai"``) are halted with a
        report-only downgrade reason — they have no template here yet.
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
        )

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


# --------------------------------------------------------------------------- #
# Template A renderer
# --------------------------------------------------------------------------- #

# Verbatim constraint phrases. Lifted into module-level constants so tests
# can pin the exact wording — these strings ARE the "侵食不能な剛体" the
# spec calls out (论点5: instruction must not be softened by AI). Renaming
# any of these is a behaviour change, not a cleanup.
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
