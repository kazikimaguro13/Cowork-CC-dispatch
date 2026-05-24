"""ccd discover --channel ai — AI-inference discovery channel (spec_016).

Discovery channel #3 of v2 Phase 1. The mutation channel (spec_013) finds
gaps in CCD's *tests*. The adversarial-input channel (spec_015) finds
crashes on realistic broken inputs. This channel finds something neither
of those can: *semantic* / *intent* concerns an agent surfaces by reading
the code — function names that don't match their bodies, invariants that
look fragile, error paths that look missing.

**Report-only.** Unlike the other two channels, AI-inference findings are
*claims*, not verified facts — there is no oracle that can confirm them
mechanically, and re-running the agent on the same code will not produce
identical output. So:

- This channel does NOT feed an autonomous fix loop.
- It writes a discovery report visually distinct from the mutation /
  adversarial reports so a human can see at a glance that these are
  proposals for human judgement, not findings ready to auto-fix.
- Nothing is auto-promoted to ``_ai_workspace/bridge/inbox/``; nothing
  is auto-dispatched (same human-in-the-loop discipline as
  ``ccd retrospect``).

Routing
-------
This channel dispatches the review-task to ``AgentRunner.run`` directly
(same pattern as ``run_retrospect``) rather than threading it through
``dispatch_one``. Dispatch classifies on result-file presence and commits;
a review task produces neither — its output is the per-finding markdown
files under ``_ai_workspace/discover/ai_review/findings_NNN/``.

Determinism
-----------
The factual summary (target files, file count) is deterministic. The
findings themselves are explicitly NOT — the report says so out loud
rather than pretending otherwise (spec_016 §2-4).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .agent import AgentRunner
from .discover import DEFAULT_DISCOVER_DIR_REL
from .models import Spec

AI_REVIEW_SUBDIR = "ai_review"
TARGET_PACKAGE = "ccd"

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AIReviewFinding:
    """One agent-surfaced concern.

    ``location`` follows the convention ``ccd/foo.py:123`` (file:line)
    when the agent supplies a line, ``ccd/foo.py`` when it doesn't, and
    ``(unspecified)`` as a last-resort sentinel — the spec requires an
    evidence anchor, but if the agent disobeys we still want to surface
    the leak rather than silently drop it.
    """

    slug: str
    location: str
    concern: str
    why_risky: str
    source_file: str  # path to the per-finding .md, relative to repo


@dataclass(frozen=True)
class AIReviewSummary:
    """Deterministic facts about the review run.

    The *findings count* is included because it is observable after the
    agent finishes, but it is NOT deterministic — re-running the channel
    on the same code may produce a different number. The report makes
    this explicit (spec_016 §2-4).
    """

    target_package: str
    files_reviewed: tuple[str, ...]
    files_total: int
    findings_total: int


@dataclass
class AIReviewResult:
    """``run_ai_review`` return value.

    Field names match :class:`ccd.discover.DiscoveryResult` and
    :class:`ccd.adversarial.AdversarialResult` so the CLI can treat all
    three discovery channels uniformly.
    """

    success: bool
    report_md_path: Path | None
    report_json_path: Path | None
    summary: AIReviewSummary
    findings: list[AIReviewFinding]
    review_spec_path: Path | None = None
    findings_dir: Path | None = None
    halt_reason: str = ""
    runner_invoked: bool = False
    raw_finding_paths: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_ai_review(
    runner: AgentRunner,
    *,
    repo: Path,
    discover_dir: Path | None = None,
) -> AIReviewResult:
    """Drive one AI-inference discovery run end-to-end.

    The flow mirrors :func:`ccd.retrospect.run_retrospect`:

    1. Enumerate the review target (``<repo>/ccd/*.py``) — this is the
       deterministic anchor.
    2. Allocate the next ``discover_NNN`` sequence number (shared with
       the mutation and adversarial channels).
    3. Write a self-contained review-task spec that hard-codes the
       findings directory and the §2-3 constraints (evidence anchor /
       no fabrication / report-only).
    4. Call ``runner.run`` once.
    5. Glob the findings directory, parse each per-finding markdown
       file, sort deterministically, and write the discovery report
       (``discover_NNN.md`` + ``.json``).

    A run with zero findings is graceful (``success=True``) — the agent
    saying "nothing to flag" is a legitimate outcome of a report-only
    channel.
    """

    repo = Path(repo).resolve()
    discover_root = (
        Path(discover_dir).resolve()
        if discover_dir is not None
        else repo / DEFAULT_DISCOVER_DIR_REL
    )
    discover_root.mkdir(parents=True, exist_ok=True)

    files_reviewed = _enumerate_target_files(repo)

    seq = _next_discover_seq(discover_root)
    findings_dir = discover_root / AI_REVIEW_SUBDIR / f"findings_{seq:03d}"
    findings_dir.mkdir(parents=True, exist_ok=True)

    review_spec_path = (
        discover_root / AI_REVIEW_SUBDIR / f"review_spec_{seq:03d}.md"
    )
    spec_id = f"spec_ai_review_{seq:03d}"
    review_body = _build_review_spec_body(
        repo=repo,
        files_reviewed=files_reviewed,
        findings_dir=findings_dir,
        seq=seq,
    )
    review_spec_path.write_text(
        f"# {spec_id}: ccd AI-inference discovery {seq:03d}\n\n{review_body}\n",
        encoding="utf-8",
    )

    if not files_reviewed:
        summary = _build_summary(
            files_reviewed=files_reviewed,
            findings=[],
        )
        return AIReviewResult(
            success=False,
            report_md_path=None,
            report_json_path=None,
            summary=summary,
            findings=[],
            review_spec_path=review_spec_path,
            findings_dir=findings_dir,
            halt_reason=(
                f"no review target: <repo>/{TARGET_PACKAGE}/*.py is empty — "
                "nothing for the agent to inspect"
            ),
            runner_invoked=False,
        )

    spec = Spec(
        id=spec_id,
        title=f"ccd AI-inference discovery {seq:03d}",
        body=review_body,
        path=review_spec_path,
    )

    runner.run(spec, workdir=repo)

    raw_paths = sorted(findings_dir.glob("*.md"))
    findings = sorted(
        (_parse_finding_file(p, repo=repo) for p in raw_paths),
        key=lambda f: (f.location, f.slug),
    )

    summary = _build_summary(
        files_reviewed=files_reviewed,
        findings=findings,
    )

    md_path = discover_root / f"discover_{seq:03d}.md"
    json_path = discover_root / f"discover_{seq:03d}.json"

    md_path.write_text(
        _render_md(
            seq=seq,
            summary=summary,
            findings=findings,
            findings_dir=findings_dir,
            repo=repo,
        ),
        encoding="utf-8",
    )
    json_path.write_text(
        _render_json(summary, findings),
        encoding="utf-8",
    )

    return AIReviewResult(
        success=True,
        report_md_path=md_path,
        report_json_path=json_path,
        summary=summary,
        findings=findings,
        review_spec_path=review_spec_path,
        findings_dir=findings_dir,
        runner_invoked=True,
        raw_finding_paths=raw_paths,
    )


# --------------------------------------------------------------------------- #
# Target enumeration (the deterministic anchor)
# --------------------------------------------------------------------------- #


def _enumerate_target_files(repo: Path) -> tuple[str, ...]:
    """Return the sorted ``ccd/`` Python files relative to ``repo``.

    ``__pycache__`` and tests / fixtures are excluded — the agent is asked
    to read shipping code, not generated bytecode or test scaffolding.
    """

    target_root = repo / TARGET_PACKAGE
    if not target_root.is_dir():
        return ()
    out: list[str] = []
    for path in target_root.rglob("*.py"):
        if any(part == "__pycache__" for part in path.parts):
            continue
        out.append(str(path.relative_to(repo)))
    return tuple(sorted(out))


def _build_summary(
    *,
    files_reviewed: tuple[str, ...],
    findings: list[AIReviewFinding],
) -> AIReviewSummary:
    return AIReviewSummary(
        target_package=TARGET_PACKAGE,
        files_reviewed=files_reviewed,
        files_total=len(files_reviewed),
        findings_total=len(findings),
    )


# --------------------------------------------------------------------------- #
# Numbering (shared with mutation + adversarial channels)
# --------------------------------------------------------------------------- #


def _next_discover_seq(discover_dir: Path) -> int:
    """Pick the next ``discover_NNN`` sequence across all channels.

    Same scheme as :func:`ccd.discover._next_discover_seq` and
    :func:`ccd.adversarial._next_discover_seq` — duplicated rather than
    imported because (a) the three discover-channel modules would
    otherwise risk circular imports, and (b) the logic is a five-line
    glob that costs nothing to repeat.
    """

    nums: list[int] = []
    for p in discover_dir.glob("discover_*.md"):
        m = re.match(r"discover_(\d+)\.md$", p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


# --------------------------------------------------------------------------- #
# Review-spec body — the prompt the agent receives
# --------------------------------------------------------------------------- #


def _build_review_spec_body(
    *,
    repo: Path,
    files_reviewed: tuple[str, ...],
    findings_dir: Path,
    seq: int,
) -> str:
    try:
        findings_rel = findings_dir.relative_to(repo)
    except ValueError:
        findings_rel = findings_dir

    file_list_lines = "\n".join(f"- `{p}`" for p in files_reviewed) or (
        f"(none — `{TARGET_PACKAGE}/*.py` was empty at scan time)"
    )

    body_parts = [
        "## 1. このタスクは何か",
        "",
        f"`{TARGET_PACKAGE}/` 配下のソースコードを **AI 推論で読み**、",
        "「ここ危なくない？」「このエラー処理抜けてない？」「関数名と実装が",
        "ズレていない？」といった意味的・意図的な懸念を所見として挙げる。",
        "",
        "これは v2 Phase 1 の**第三の発見チャンネル**（spec_016）であり、",
        "ミューテーション・チャンネル（spec_013）／敵対的入力チャンネル",
        "（spec_015）が出す **機械的・再現可能な事実** とは別物の、",
        "**主張（claim）**を出す。",
        "",
        "## 2. このチャンネルの位置づけ — 報告専用",
        "",
        "**この所見は主張であって、検証済みの事実ではない。** AI 推論は",
        "非決定的で、同じコードを 2 回読ませても違うことを言いうる。",
        "再現性のある検証オラクルを持たないので、機械的にバグと",
        "証明できる発見ではない。よって本チャンネルは:",
        "",
        "- **自律修正ループの引き金にしない** — 朝レポートで人間に提示し、",
        "  人間が個別に判断する。",
        "- **`_ai_workspace/bridge/inbox/` への自動投入をしない** —",
        "  自動 spec 化も自動 dispatch もしない（`ccd retrospect` の",
        "  human-in-the-loop 規律と同じ）。",
        "- **ソースコードを変更しない・テストを足さない** — 所見ファイルを",
        "  書くだけで止める。",
        "",
        "## 3. 評価対象 (決定的に算出した事実)",
        "",
        f"- 対象パッケージ: `{TARGET_PACKAGE}/`",
        f"- 対象ファイル数: **{len(files_reviewed)}** 件",
        "",
        "対象ファイル一覧:",
        "",
        file_list_lines,
        "",
        "**この数値は決定的に Python で算出済み — 正直さのアンカーとして",
        "使うこと。** 所見の数は決定的でないが、対象ファイル数は",
        "確定値なので、レポートの事実サマリに使われる。",
        "",
        "## 4. やってほしいこと",
        "",
        "1. 上記の対象ファイルを実際に読み込む（`Read` ツール等）。",
        "2. **読みながら推論で**、「ここ危なくない？」「このエラー処理抜けてない？」",
        "   「関数名と実装が乖離していない？」「不変条件が曖昧では？」といった",
        "   意味的な懸念を挙げる。",
        "3. 所見は **1 件 = 1 ファイル** として、",
        f"   `{findings_rel}/<short-kebab-case-slug>.md` に書く。",
        "   ファイル名 slug は所見の主題を表す短い識別子（例: ",
        "   `dispatch-unchecked-cwd`, `agent-runner-prompt-injection`）。",
        "4. 各所見ファイルは以下の構造とする:",
        "",
        "    ```markdown",
        "    # finding: <slug>",
        "",
        "    - **Location**: `ccd/<file>.py:<line>` （行が特定できない場合は ",
        "      `ccd/<file>.py` だけでも可。だが**ファイルは必須**。）",
        "    - **Concern**: <一行サマリ — 何が懸念か>",
        "    - **Why risky**: <なぜそれが危ういか — 数行の推論。",
        "      実コードの該当箇所を引用してよい>",
        "    ```",
        "",
        "5. 所見が**ゼロ件**だった場合（読んでみたが特に挙げるべき懸念がなかった）",
        f"   は、`{findings_rel}/` に何も書かずに終わってよい。",
        "   レポートには「所見ゼロ件 — AI 推論で挙げる懸念が見つからなかった」と",
        "   出る（捏造して埋めるな）。",
        "",
        "出力先まとめ:",
        f"- 所見群: `{findings_rel}/*.md` (1 所見 = 1 ファイル)",
        "- 本タスクは分析タスクなので `result_*.md` は書かない、",
        "  ソースコードに `commit` も作らない（出力は所見ファイルのみ）。",
        "",
        "## 5. 制約・ルール (厳守)",
        "",
        f"- **証拠アンカー必須** — すべての所見は `{TARGET_PACKAGE}/` の実在する",
        "  ファイル（できれば行）を引用する。「テストを増やそう」「もっと型を",
        "  つけよう」式の**汎用アドバイスは禁止**。具体的に「`ccd/X.py:N` の",
        "  `Y` という関数のこの分岐で…」のように一意に指せること。",
        "- **捏造しない** — 実在するコードだけを根拠にする。",
        "  「たぶんあるはず」「一般論として」では書かない。",
        "  読んでいないファイルについて推測しない。",
        "  該当箇所が見つからなければ書かない（ゼロ件で構わない）。",
        "- **報告のみ** — コードを修正しない、テストを足さない、",
        f"  `{TARGET_PACKAGE}/` 配下のファイルを変更しない。",
        f"  あなたが書いてよいのは `{findings_rel}/<slug>.md` だけ。",
        "- **触れてよい範囲** — 読むのは `ccd/` のソース（と関連ドキュメント）、",
        f"  書くのは `{findings_rel}/` の所見ファイルのみ。",
        "- **push / ブランチ操作・merge はしない** — `commit` も作らない。",
        "",
        "## 6. 出力先 (再掲)",
        "",
        f"- 所見ファイル群: `{findings_rel}/<slug>.md`",
        "- このレビューの結果レポート（事実サマリ + 所見集約）は",
        "  CCD 側 (`run_ai_review`) が `_ai_workspace/discover/"
        f"discover_{seq:03d}.md` に自動生成する — **あなたは触らない**。",
        "",
    ]
    return "\n".join(body_parts)


# --------------------------------------------------------------------------- #
# Per-finding markdown parsing
# --------------------------------------------------------------------------- #

_LOCATION_RE = re.compile(
    r"^-\s+\*\*Location\*\*:\s*(?P<value>.+?)\s*$", re.IGNORECASE
)
_CONCERN_RE = re.compile(
    r"^-\s+\*\*Concern\*\*:\s*(?P<value>.+?)\s*$", re.IGNORECASE
)
_WHY_RE = re.compile(
    r"^-\s+\*\*Why risky\*\*:\s*(?P<value>.*?)\s*$", re.IGNORECASE
)


def _parse_finding_file(path: Path, *, repo: Path) -> AIReviewFinding:
    """Parse one ``<slug>.md`` into an :class:`AIReviewFinding`.

    The parser is intentionally forgiving: a finding with no Location is
    not dropped, it surfaces with ``location="(unspecified)"`` so the
    discovery report can highlight that the agent broke the contract.
    Dropping silently would hide a leak; surfacing it lets a human see
    it.
    """

    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    location = ""
    concern = ""
    why_lines: list[str] = []
    in_why = False

    for line in lines:
        m = _LOCATION_RE.match(line)
        if m:
            location = _strip_backticks(m.group("value").strip())
            in_why = False
            continue
        m = _CONCERN_RE.match(line)
        if m:
            concern = m.group("value").strip()
            in_why = False
            continue
        m = _WHY_RE.match(line)
        if m:
            initial = m.group("value").strip()
            why_lines = [initial] if initial else []
            in_why = True
            continue
        if in_why:
            # Continuation: stop when we hit another `- **Key**:` bullet
            # or a heading; otherwise treat as why-risky body.
            if line.lstrip().startswith("- **") or line.startswith("#"):
                in_why = False
                continue
            why_lines.append(line)

    return AIReviewFinding(
        slug=path.stem,
        location=location or "(unspecified)",
        concern=concern or "(missing)",
        why_risky="\n".join(why_lines).strip() or "(missing)",
        source_file=str(path.relative_to(repo)) if _is_under(path, repo) else str(path),
    )


def _strip_backticks(s: str) -> str:
    s = s.strip()
    if s.startswith("`") and s.endswith("`") and len(s) >= 2:
        return s[1:-1].strip()
    return s


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #


def _render_md(
    *,
    seq: int,
    summary: AIReviewSummary,
    findings: list[AIReviewFinding],
    findings_dir: Path,
    repo: Path,
) -> str:
    try:
        findings_rel = findings_dir.relative_to(repo)
    except ValueError:
        findings_rel = findings_dir

    parts: list[str] = [
        f"# discover_{seq:03d} — ccd AI-inference discovery (報告専用)",
        "",
        "> ⚠️ **報告専用チャンネル** — 以下は AI 推論による**所見（主張）**で",
        "> あって、検証済みの事実ではない。再現性のある検証オラクルは",
        "> 存在せず、同じコードを 2 回読ませても内容は変わりうる",
        "> （**非決定的**）。人間の判断が必要であり、**自律修正の引き金には",
        "> しない**（ミューテーション／敵対的入力チャンネルの「事実→自律修正」",
        "> 経路とは別軸 — spec_016 §1 / §2-2）。",
        "",
        "## 1. 評価母数 (決定的に算出した事実)",
        "",
        "- チャンネル: `ai`",
        f"- 対象パッケージ: `{summary.target_package}/`",
        f"- 対象ファイル数: **{summary.files_total}** 件",
        f"- 所見格納先: `{findings_rel}/`",
        f"- 集約された所見数: **{summary.findings_total}** 件 "
        "(**非決定的** — 再実行で件数が変わりうる)",
        "",
        "対象ファイル一覧:",
        "",
        _render_files(summary.files_reviewed),
        "",
        "**対象ファイル数は決定的に Python で算出済み。** "
        "所見の件数だけは AI 推論の非決定的な出力なので "
        "再実行で増減しうる — レポートはその事実を偽装しない。",
        "",
        "## 2. AI 推論による所見 (主張 — 人間が判断する)",
        "",
        _render_findings(findings),
        "",
        "## 3. 他チャンネルとの違い (視覚的区別)",
        "",
        "- **ミューテーション・チャンネル** (`discover_NNN.md`, `channel: \"mutation\"`)",
        "  — 緑のテストが見ていない**事実**を mutmut が炙り出す。"
        "再現可能、検証オラクル付き、自律修正可能。",
        "- **敵対的入力チャンネル** (`channel: \"adversarial\"`) — "
        "壊れた入力でパーサが漏らす例外型を**事実**として記録する。"
        "再現可能、決定的分類、自律修正可能。",
        "- **本チャンネル** (`channel: \"ai\"`) — エージェントが読んで",
        "  「ここ危なくない？」と**推論で主張**する。再現性なし、",
        "  オラクル無し、**自律修正不可**（人間判断必須）。",
        "",
        "## 4. データから判断できなかったこと",
        "",
        _render_uncertain(summary, findings),
        "",
    ]
    return "\n".join(parts)


def _render_files(files: tuple[str, ...]) -> str:
    if not files:
        return "_(none)_"
    return "\n".join(f"- `{f}`" for f in files)


def _render_findings(findings: list[AIReviewFinding]) -> str:
    if not findings:
        return (
            "_(該当なし — AI 推論で挙げる懸念はゼロ件だった。"
            "ゼロ件は捏造で埋めない正直な結果であり、"
            "「コードに問題が無い」ことの証明ではない — "
            "非決定的なので別の実行では出るかもしれない。)_"
        )
    lines: list[str] = []
    for f in findings:
        lines.append(f"### {f.slug}")
        lines.append("")
        lines.append(f"- **Location**: `{f.location}`")
        lines.append(f"- **Concern**: {f.concern}")
        lines.append(f"- **Why risky**: {_indent_continuation(f.why_risky)}")
        lines.append(f"- _(source: `{f.source_file}`)_")
        lines.append("")
    return "\n".join(lines).rstrip()


def _indent_continuation(text: str) -> str:
    """Inline multi-line why-risky into a single bullet — first line on
    the bullet, subsequent lines indented two spaces under it."""

    parts = text.splitlines()
    if not parts:
        return ""
    head = parts[0]
    tail = "\n".join(f"  {ln}" for ln in parts[1:])
    return head if not tail else f"{head}\n{tail}"


def _render_uncertain(
    summary: AIReviewSummary, findings: list[AIReviewFinding]
) -> str:
    bullets: list[str] = [
        "- **本チャンネル全体が「判断できないこと」サイド** — 出力された",
        "  所見は AI 推論であり、機械的に「これがバグか」を証明できない。",
        "  各所見は人間レビュアが個別に検討する。",
    ]
    bad_anchors = [
        f for f in findings if f.location == "(unspecified)" or f.location == ""
    ]
    if bad_anchors:
        bullets.append(
            f"- **証拠アンカー欠落の所見が {len(bad_anchors)} 件** — レビュー用",
            )
        bullets.append(
            "  spec は「`ccd/<file>:<line>` を必須」と指示しているが、これらは",
        )
        bullets.append(
            "  Location 行が読み取れなかった。集約は落とさず surfacing する。",
        )
    missing_concern = [f for f in findings if f.concern == "(missing)"]
    if missing_concern:
        bullets.append(
            f"- **Concern 欠落の所見が {len(missing_concern)} 件** — "
            "所見ファイルに `- **Concern**:` 行が無い。"
        )
    if summary.files_total == 0:
        bullets.append(
            "- **対象ファイルがゼロ件だった** — `ccd/*.py` がスキャンで",
        )
        bullets.append(
            "  見つからなかった。レビューが実行されなかった可能性が高い。",
        )
    return "\n".join(bullets)


def _render_json(
    summary: AIReviewSummary,
    findings: Iterable[AIReviewFinding],
) -> str:
    payload = {
        "channel": "ai",
        "report_only": True,
        "non_deterministic": True,
        "summary": {
            "target_package": summary.target_package,
            "files_reviewed": list(summary.files_reviewed),
            "files_total": summary.files_total,
            "findings_total": summary.findings_total,
        },
        "findings": [
            {
                "slug": f.slug,
                "location": f.location,
                "concern": f.concern,
                "why_risky": f.why_risky,
                "source_file": f.source_file,
            }
            for f in findings
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "AIReviewFinding",
    "AIReviewResult",
    "AIReviewSummary",
    "AI_REVIEW_SUBDIR",
    "TARGET_PACKAGE",
    "run_ai_review",
]
