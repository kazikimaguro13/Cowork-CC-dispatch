"""ccd brief — morning report renderer (spec_017, v2 Phase 1 + spec_025).

The morning report is Loop β's single human-facing artifact
(``docs/DESIGN.md §9.6``). spec_017's job is the **renderer**: it reads
already-completed ``_ai_workspace/discover/discover_NNN.json`` files —
one *latest* per channel (``mutation`` / ``adversarial`` / ``ai``) — and
writes a one-page 6-section Markdown report to
``_ai_workspace/nightly/report_YYYY-MM-DD.md``.

spec_025 — §B upgrade
---------------------
When the night included a successful autonomous-fix merge, §B switches
to the **Phase 2** layout: it surfaces the fix's *finding → action*
narrative, the diff that landed on local ``main``, the verification
evidence (R5 / R4 / guard), and a ready-to-paste ``git push`` one-liner
the operator runs after reviewing the diff. Nights without an
autonomous-fix merge keep the Phase 1 §B (mechanical-channel
discoveries only).

To stay aligned with ``docs/DESIGN.md §9.6`` ("既定は簡潔・例外時のみ
伸びる"), the Phase 2 §B is only rendered when ``auto_fix`` is present
and reports ``merged=True``. Skipped / halted nights still see the
Phase 1 §B — those nights have nothing additional worth surfacing in
the brief's body that isn't already in §D (halt / skip).

What this module is NOT
-----------------------
- It does **not** run the discovery channels. Driving the channels and
  then rendering the brief is the scheduler's responsibility (spec_019).
- Phase 1 (and Phase 2 §B) describe what the loop did; rendering does
  not itself dispatch, merge, or push. spec_025 §3 keeps push as the
  operator's manual action — the Phase 2 §B's ``git push`` line is a
  *suggestion*, not an automation hook.

Channel attribution
-------------------
``discover_NNN.json`` produced by the adversarial / AI channels carries
an explicit top-level ``"channel"`` field. The mutation channel
(spec_013) does **not** — it predates the channel marker. We detect it
by shape (``summary.tool`` + ``actionable`` list) rather than by
modifying the producer, which spec_017 §3 forbids.

Determinism
-----------
The factual summary (``BriefSummary``) is computed in Python from the
JSON inputs; same inputs → same numbers. The findings the channels
themselves surfaced may individually be deterministic (mutation,
adversarial) or non-deterministic (ai); the brief inherits that
property and reports it honestly per channel. Phase 2 §B is purely
deterministic — diff + signature + R-results all come from the
:class:`ccd.nightly.AutoFixOutcome` recorded by the loop.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ccd.nightly import AutoFixOutcome

# Phase 2 §B diff cap — the brief shouldn't grow unbounded if a fix's
# diff is unexpectedly large. The guard's R3 already caps template-B
# production diffs at 60 ± lines, but template A test-only diffs are
# unbounded by R3 and a runaway agent could (in principle) generate a
# huge tests/ diff. 16 KB is plenty for any reasonable autonomous fix
# and prevents the morning report from ballooning if something is off.
_PHASE2_DIFF_CAP = 16 * 1024

DEFAULT_DISCOVER_DIR_REL = Path("_ai_workspace") / "discover"
DEFAULT_NIGHTLY_DIR_REL = Path("_ai_workspace") / "nightly"

CHANNEL_MUTATION = "mutation"
CHANNEL_ADVERSARIAL = "adversarial"
CHANNEL_AI = "ai"
KNOWN_CHANNELS: tuple[str, ...] = (
    CHANNEL_MUTATION,
    CHANNEL_ADVERSARIAL,
    CHANNEL_AI,
)

_CHANNEL_LABEL: dict[str, str] = {
    CHANNEL_MUTATION: "ミューテーション",
    CHANNEL_ADVERSARIAL: "敵対的入力",
    CHANNEL_AI: "AI推論",
}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChannelReport:
    """One discover_NNN.json the brief picked up.

    ``payload`` is the parsed JSON verbatim so the renderer can quote
    fields directly without re-deriving counts from the findings list.
    """

    channel: str
    seq: int
    json_path: Path
    md_path: Path | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class BriefSummary:
    """Deterministic, cross-channel facts about the morning brief.

    Same inputs → same numbers (spec_017 §2-1). ``mechanical_findings_total``
    is the sum of the two *fact*-producing channels (mutation actionable +
    adversarial ungraceful); the AI count is **report-only**, kept in a
    separate field so callers never accidentally fold a claim into a fact.
    """

    channels_picked: tuple[str, ...]
    channels_missing: tuple[str, ...]
    mutation_actionable: int
    adversarial_ungraceful: int
    ai_findings: int
    mechanical_findings_total: int


@dataclass
class BriefResult:
    """``run_brief`` return value."""

    success: bool
    report_path: Path | None
    summary: BriefSummary
    channels: list[ChannelReport] = field(default_factory=list)
    halt_reason: str = ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_brief(
    *,
    repo: Path,
    inputs: Sequence[Path] | None = None,
    brief_dir: Path | None = None,
    discover_dir: Path | None = None,
    today: date | None = None,
    auto_fix: AutoFixOutcome | None = None,
) -> BriefResult:
    """Render one morning report from already-completed discovery JSON.

    Picks the latest ``discover_NNN.json`` per channel under
    ``<repo>/_ai_workspace/discover/`` (or ``inputs`` if explicitly passed
    for tests), aggregates them into a :class:`BriefSummary`, and writes
    a 6-section markdown report to
    ``<repo>/_ai_workspace/nightly/report_YYYY-MM-DD.md``.

    Channels with no discovered report are recorded as "未実行" in
    sections D and F rather than crashing — the brief always renders
    something honest.

    spec_025: when ``auto_fix`` is supplied **and** describes a merged
    autonomous-fix (``auto_fix.merged is True``), §B switches to the
    Phase 2 layout — finding → action narrative, diff embed, R-result
    evidence, and a ready-to-paste ``git push`` line. All other cases
    (auto_fix omitted / skipped / halted) render the Phase 1 §B
    unchanged.
    """

    repo = Path(repo).resolve()
    discover_root = (
        Path(discover_dir).resolve()
        if discover_dir is not None
        else repo / DEFAULT_DISCOVER_DIR_REL
    )
    nightly_root = (
        Path(brief_dir).resolve()
        if brief_dir is not None
        else repo / DEFAULT_NIGHTLY_DIR_REL
    )
    nightly_root.mkdir(parents=True, exist_ok=True)

    if inputs is not None:
        channels = _load_explicit_inputs(inputs)
    else:
        channels = _collect_latest(discover_root)

    summary = _build_summary(channels)

    today_d = today if today is not None else _utc_today()
    report_path = nightly_root / f"report_{today_d.isoformat()}.md"

    report_path.write_text(
        _render_md(
            today=today_d,
            summary=summary,
            channels=channels,
            auto_fix=auto_fix,
            repo=repo,
        ),
        encoding="utf-8",
    )

    return BriefResult(
        success=True,
        report_path=report_path,
        summary=summary,
        channels=channels,
    )


# --------------------------------------------------------------------------- #
# Input collection
# --------------------------------------------------------------------------- #


def _collect_latest(discover_root: Path) -> list[ChannelReport]:
    """Return at most one ChannelReport per known channel — the latest seq."""

    if not discover_root.is_dir():
        return []

    by_channel: dict[str, ChannelReport] = {}
    for json_path in sorted(discover_root.glob("discover_*.json")):
        cr = _load_one(json_path)
        if cr is None:
            continue
        existing = by_channel.get(cr.channel)
        if existing is None or cr.seq > existing.seq:
            by_channel[cr.channel] = cr
    return [by_channel[c] for c in KNOWN_CHANNELS if c in by_channel]


def _load_explicit_inputs(inputs: Sequence[Path]) -> list[ChannelReport]:
    """Mirror of _collect_latest but driven by a caller-supplied list."""

    by_channel: dict[str, ChannelReport] = {}
    for raw in inputs:
        cr = _load_one(Path(raw))
        if cr is None:
            continue
        existing = by_channel.get(cr.channel)
        if existing is None or cr.seq > existing.seq:
            by_channel[cr.channel] = cr
    return [by_channel[c] for c in KNOWN_CHANNELS if c in by_channel]


def _load_one(json_path: Path) -> ChannelReport | None:
    json_path = json_path.resolve() if json_path.is_absolute() else json_path
    name = json_path.name
    m = re.match(r"discover_(\d+)\.json$", name)
    seq = int(m.group(1)) if m else 0
    try:
        text = json_path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    channel = _detect_channel(payload)
    if channel is None:
        return None
    md_path = json_path.with_suffix(".md")
    return ChannelReport(
        channel=channel,
        seq=seq,
        json_path=json_path,
        md_path=md_path if md_path.exists() else None,
        payload=payload,
    )


def _detect_channel(payload: dict[str, Any]) -> str | None:
    """Pick the channel name from a discover_NNN.json payload.

    Priority:
    1. The explicit top-level ``"channel"`` field (adversarial / ai both
       set it; future channels are expected to as well).
    2. Shape fallback for mutation, which predates the channel field —
       presence of ``summary.tool`` + an ``actionable`` list is uniquely
       mutation-channel output.
    """

    ch = payload.get("channel")
    if isinstance(ch, str) and ch in KNOWN_CHANNELS:
        return ch
    summary = payload.get("summary")
    if (
        isinstance(summary, dict)
        and "tool" in summary
        and isinstance(payload.get("actionable"), list)
    ):
        return CHANNEL_MUTATION
    return None


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


def _build_summary(channels: list[ChannelReport]) -> BriefSummary:
    picked: dict[str, ChannelReport] = {c.channel: c for c in channels}

    mutation_actionable = 0
    adversarial_ungraceful = 0
    ai_findings = 0

    if CHANNEL_MUTATION in picked:
        s = picked[CHANNEL_MUTATION].payload.get("summary", {})
        mutation_actionable = _safe_int(s.get("actionable_total"))
    if CHANNEL_ADVERSARIAL in picked:
        s = picked[CHANNEL_ADVERSARIAL].payload.get("summary", {})
        adversarial_ungraceful = _safe_int(s.get("ungraceful_total"))
    if CHANNEL_AI in picked:
        s = picked[CHANNEL_AI].payload.get("summary", {})
        ai_findings = _safe_int(s.get("findings_total"))

    return BriefSummary(
        channels_picked=tuple(c for c in KNOWN_CHANNELS if c in picked),
        channels_missing=tuple(c for c in KNOWN_CHANNELS if c not in picked),
        mutation_actionable=mutation_actionable,
        adversarial_ungraceful=adversarial_ungraceful,
        ai_findings=ai_findings,
        mechanical_findings_total=mutation_actionable + adversarial_ungraceful,
    )


def _safe_int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _utc_today() -> date:
    return datetime.now(UTC).date()


# --------------------------------------------------------------------------- #
# Report rendering — 6 sections (A〜F)
# --------------------------------------------------------------------------- #


def _render_md(
    *,
    today: date,
    summary: BriefSummary,
    channels: list[ChannelReport],
    auto_fix: AutoFixOutcome | None = None,
    repo: Path | None = None,
) -> str:
    by_channel = {c.channel: c for c in channels}
    phase2_active = (
        auto_fix is not None
        and not auto_fix.skipped
        and auto_fix.merged
    )

    if phase2_active:
        header = (
            f"# 朝レポート {today.isoformat()} — "
            "ccd v2 Phase 2 (昨夜の自律修正あり)"
        )
        preamble = (
            "> Loop β の発見3チャンネル "
            "(`mutation` / `adversarial` / `ai`) に加え、"
            "**昨夜の自律修正ループ** (`docs/DESIGN.md §9.5 / §9.7`) が "
            "ローカルに 1 件マージしました。§B に修正の diff と検証証拠と "
            "push コマンドを掲載 ── レビューしてから手動で push してください "
            "(spec_025 §2-2、安全境界レベル 2)。"
        )
    else:
        header = (
            f"# 朝レポート {today.isoformat()} — ccd v2 Phase 1 (発見のみ)"
        )
        preamble = (
            "> このレポートは Loop β の発見3チャンネル "
            "(`mutation` / `adversarial` / `ai`) の最新出力を集約した "
            "**朝レポート**です。Phase 1 は **発見のみ** — "
            "本レポートに基づく自律修正は行いません "
            "(`docs/DESIGN.md §9.6 / §9.7`)。"
        )

    parts: list[str] = [
        header,
        "",
        preamble,
        "",
    ]
    parts.extend(_render_section_a(summary, auto_fix=auto_fix))
    if phase2_active:
        assert auto_fix is not None  # narrowed by phase2_active above
        parts.extend(_render_section_b_phase2(auto_fix=auto_fix, repo=repo))
    else:
        parts.extend(_render_section_b(by_channel))
    parts.extend(_render_section_c(by_channel))
    parts.extend(_render_section_d(by_channel, summary, auto_fix=auto_fix))
    parts.extend(_render_section_e(summary, by_channel))
    parts.extend(_render_section_f(summary, auto_fix=auto_fix))

    return "\n".join(parts).rstrip() + "\n"


def _render_section_a(
    summary: BriefSummary,
    *,
    auto_fix: AutoFixOutcome | None = None,
) -> list[str]:
    bits: list[str] = []

    # spec_025 — surface the auto-fix outcome on the front page so the
    # operator gets the headline before scrolling. Three states matter:
    # merged (Phase 2 §B will follow), skipped (no candidate / paused /
    # un-pushed backlog), halted (loop ran but did not merge).
    if auto_fix is not None:
        if not auto_fix.skipped and auto_fix.merged:
            bits.append(
                f"**昨夜の自律修正 1 件をローカル merge** "
                f"(template {auto_fix.template}, "
                f"`{auto_fix.spec_auto_id}`) — §B に diff と push コマンド"
            )
        elif not auto_fix.skipped and not auto_fix.merged:
            bits.append(
                f"**自律修正 HALT** ({auto_fix.spec_auto_id or 'no spec'}) — "
                f"{auto_fix.halt_reason or '理由不明'}"
            )
        elif auto_fix.skipped and auto_fix.skip_reason:
            bits.append(f"**自律修正 skip**: {auto_fix.skip_reason}")

    if not summary.channels_picked:
        bits.append(
            "**発見なし** — 3チャンネルのいずれも出力が無く、本レポートには"
            "集約すべき discover_NNN が存在しなかった。"
            " (今夜は何もなし — エラーではない)"
        )
    else:
        bits.append(
            f"**機械的チャンネル: {summary.mechanical_findings_total} 件** "
            f"(ミューテーション actionable {summary.mutation_actionable} / "
            f"敵対的入力 ungraceful {summary.adversarial_ungraceful})"
        )
        bits.append(
            f"**AI推論 (報告専用): {summary.ai_findings} 件** "
            "(主張 — 人間判断)"
        )
        if summary.channels_missing:
            missing_label = " / ".join(
                _CHANNEL_LABEL.get(c, c) for c in summary.channels_missing
            )
            bits.append(f"**一部チャンネル未実行**: {missing_label}")
    headline = "; ".join(bits)
    return [
        "## A. 一行判定",
        "",
        headline,
        "",
    ]


def _render_section_b(by_channel: dict[str, ChannelReport]) -> list[str]:
    lines: list[str] = [
        "## B. 機械的チャンネルの発見 (事実)",
        "",
        "以下は機械的に検証可能な**事実**です — mutmut が生存させた改変、"
        "および敵対的入力で許可リスト外の例外を漏らしたパーサ。"
        "Phase 2 で自律修正ループの引き金になる候補。",
        "",
    ]
    lines.extend(_render_mutation_findings(by_channel.get(CHANNEL_MUTATION)))
    lines.append("")
    lines.extend(_render_adversarial_findings(by_channel.get(CHANNEL_ADVERSARIAL)))
    lines.append("")
    return lines


def _render_mutation_findings(report: ChannelReport | None) -> list[str]:
    lines: list[str] = ["### ミューテーション (`channel: mutation`)", ""]
    if report is None:
        lines.append("_(未実行 — `discover_NNN.json (channel=mutation)` が無い。)_")
        return lines
    summary = report.payload.get("summary", {})
    actionable = report.payload.get("actionable") or []
    lines.append(
        f"- 出典: `{_rel_or_absolute(report.json_path)}` "
        f"(seq={report.seq:03d}, tool=`{summary.get('tool', '(unknown)')}`)"
    )
    lines.append(
        f"- mutant 総数: **{_safe_int(summary.get('mutants_total'))}** / "
        f"survived: **{_safe_int(summary.get('survived_total'))}** / "
        f"actionable: **{_safe_int(summary.get('actionable_total'))}** "
        f"(blocklist 除外: {_safe_int(summary.get('blocklisted_total'))})"
    )
    if not actionable:
        lines.append("")
        lines.append("_(actionable 発見なし — テストの隙間はゼロ件。)_")
        return lines
    lines.append("")
    lines.append("actionable 発見 (生存改変 — テストの隙間):")
    lines.append("")
    for m in actionable:
        file_ = m.get("file", "?")
        line = m.get("line", "?")
        mutation = m.get("mutation", "(no description)")
        lines.append(f"- `{file_}:{line}` — {mutation}")
    return lines


def _render_adversarial_findings(report: ChannelReport | None) -> list[str]:
    lines: list[str] = ["### 敵対的入力 (`channel: adversarial`)", ""]
    if report is None:
        lines.append(
            "_(未実行 — `discover_NNN.json (channel=adversarial)` が無い。)_"
        )
        return lines
    summary = report.payload.get("summary", {})
    findings = report.payload.get("findings") or []
    lines.append(
        f"- 出典: `{_rel_or_absolute(report.json_path)}` "
        f"(seq={report.seq:03d})"
    )
    lines.append(
        f"- パーサ × ケース 評価母数: "
        f"**{_safe_int(summary.get('evaluations_total'))}** "
        f"(graceful: {_safe_int(summary.get('graceful_total'))} / "
        f"ungraceful: **{_safe_int(summary.get('ungraceful_total'))}**)"
    )
    if not findings:
        lines.append("")
        lines.append(
            "_(ungraceful 発見なし — 全評価が許可リスト例外または成功で graceful。)_"
        )
        return lines
    lines.append("")
    lines.append("ungraceful 発見 (パーサが許可リスト外の例外で漏れた箇所):")
    lines.append("")
    for f in findings:
        parser = f.get("parser", "?")
        case = f.get("case", "?")
        exc_type = f.get("exception_type", "?")
        exc_msg = f.get("exception_message", "")
        lines.append(
            f"- `{parser}` × `{case}` — **{exc_type}**: {exc_msg}"
        )
    return lines


def _render_section_b_phase2(
    *,
    auto_fix: AutoFixOutcome,
    repo: Path | None,
) -> list[str]:
    """spec_025 §2-2 — replace §B with the night's autonomous-fix story.

    Only invoked when ``auto_fix.merged is True``. Surfaces four
    artifacts the operator needs to decide whether to ``git push``:

    1. **Finding → action narrative** — what was discovered (mutation
       survivor / adversarial ungraceful crash) and what the loop did
       (added a test / fixed a parser).
    2. **Verification evidence** — R5 (template-specific) / R4 (full
       suite) / guard verdict, all from the :class:`AutoFixOutcome` the
       loop already recorded.
    3. **Diff embed** — the diff captured pre-merge by the loop. Guard's
       R3 keeps template-B diffs small; template A is test-only so the
       diff is usually a handful of lines.
    4. **Push command** — a copy-paste-ready ``git push origin main``
       the operator runs after reviewing the diff. The repo path is
       inlined when known so the operator can paste it as-is even when
       their shell is in a different directory.
    """

    template = auto_fix.template or "?"
    if template == "A":
        template_desc = "テンプレ A (ミューテーション生存 → test-only fix)"
    elif template == "B":
        template_desc = (
            "テンプレ B (敵対的入力 ungraceful → 本番修正 + 再現テスト)"
        )
    else:
        template_desc = f"テンプレ {template}"

    lines: list[str] = [
        "## B. 昨夜の自律修正 (Phase 2 — `docs/DESIGN.md §9.5/§9.7`)",
        "",
        "ローカル `main` に **1 件 merge 済み**。レビューしてから "
        "下記の `git push` を手動で実行してください "
        "(spec_025 §3、安全境界レベル 2 — ループは push しない)。",
        "",
        "### 発見と修正",
        "",
        f"- **テンプレ**: {template_desc}",
        f"- **signature**: `{auto_fix.finding_signature or '(不明)'}`",
        f"- **spec_auto**: `{auto_fix.spec_auto_id or '(不明)'}` "
        f"({auto_fix.candidate_count} 候補中 1 件を選択)",
        f"- **マージ済みブランチ**: `{auto_fix.branch or '(不明)'}` → "
        "`main` (local, no push)",
        "",
        "### 検証の証拠",
        "",
    ]
    if template == "A":
        r5_label = "R5 (target mutation killed)"
    elif template == "B":
        r5_label = "R5 (parser now raises a graceful error)"
    else:
        r5_label = "R5"
    lines.append(
        f"- {r5_label}: **{'pass' if auto_fix.r5_killed else 'fail'}**"
    )
    lines.append(
        f"- R4 (`pytest -q` 全件 green): "
        f"**{'pass' if auto_fix.r4_suite_passed else 'fail'}**"
    )
    if auto_fix.guard_passed:
        lines.append("- ガード (R1〜R3): **pass**")
    else:
        reasons_text = "; ".join(auto_fix.guard_halt_reasons) or "理由不明"
        lines.append(f"- ガード: **HALT** — {reasons_text}")

    lines.append("")
    lines.append("### 修正の diff")
    lines.append("")
    diff = auto_fix.merge_diff or ""
    if not diff:
        lines.append(
            "_(diff が記録されていません — loop の seam が `merge_diff` "
            "を埋めなかった構造的ケース。テスト dispatch などで起こりうる。)_"
        )
    else:
        truncated = len(diff) > _PHASE2_DIFF_CAP
        body = diff[:_PHASE2_DIFF_CAP] if truncated else diff
        lines.append("```diff")
        lines.extend(body.rstrip("\n").splitlines() or [""])
        lines.append("```")
        if truncated:
            lines.append("")
            lines.append(
                f"_(diff は {_PHASE2_DIFF_CAP} byte で切り詰めました — "
                "全体は `git show` でご確認ください。)_"
            )

    lines.append("")
    lines.append("### push コマンド")
    lines.append("")
    lines.append(
        "レビュー後にコピーして実行してください "
        "(論点 2 レベル 2 — push 判断は人間):"
    )
    lines.append("")
    lines.append("```bash")
    lines.append(_compose_push_command(repo))
    lines.append("```")
    lines.append("")
    return lines


def _compose_push_command(repo: Path | None) -> str:
    """Produce the ``git push origin main`` one-liner for the brief.

    When ``repo`` is known and absolute, embed it via ``git -C <repo>``
    so the operator can paste from any shell. Otherwise fall back to a
    relative-cwd version — still safe because the operator is normally
    sitting in the repo when reading the morning brief.
    """

    if repo is not None:
        try:
            repo_str = str(Path(repo).resolve())
        except OSError:
            repo_str = str(repo)
        return f"git -C {repo_str} push origin main"
    return "git push origin main"


def _render_section_c(by_channel: dict[str, ChannelReport]) -> list[str]:
    """AI-inference findings — visually distinct from §B (spec_017 §2-2)."""

    lines: list[str] = [
        "## C. AI推論の所見 (報告専用 — 主張)",
        "",
        "> ⚠️ **以下は AI 推論による「主張」であって、検証済みの事実ではない。**"
        " 再現性のある検証オラクルを持たず (**非決定的**)、再実行で内容が"
        " 変わりうる。**人間判断が必要**であり、**自律修正の引き金にはしない**"
        " (`spec_016 §1 / §2-2`)。§B の機械的発見 (事実) とは経路が別軸。",
        "",
    ]
    report = by_channel.get(CHANNEL_AI)
    if report is None:
        lines.append(
            "_(AI推論チャンネルは未実行 — `discover_NNN.json (channel=ai)` が無い。)_"
        )
        lines.append("")
        return lines
    summary = report.payload.get("summary", {})
    findings = report.payload.get("findings") or []
    lines.append(
        f"- 出典: `{_rel_or_absolute(report.json_path)}` "
        f"(seq={report.seq:03d})"
    )
    lines.append(
        f"- 対象パッケージ: `{summary.get('target_package', '?')}/` "
        f"({_safe_int(summary.get('files_total'))} ファイル) "
        f"／ 所見数: **{_safe_int(summary.get('findings_total'))}** 件 "
        "(**非決定的**)"
    )
    if not findings:
        lines.append("")
        lines.append(
            "_(所見ゼロ件 — AI 推論で挙げる懸念が見つからなかった。"
            "捏造で埋めない正直な結果。)_"
        )
        lines.append("")
        return lines
    lines.append("")
    lines.append("所見 (主張 — 人間が個別に判断する):")
    lines.append("")
    for f in findings:
        slug = f.get("slug", "?")
        location = f.get("location", "(unspecified)")
        concern = f.get("concern", "(missing)")
        lines.append(f"- **{slug}** — `{location}` — {concern}")
    lines.append("")
    return lines


def _render_section_d(
    by_channel: dict[str, ChannelReport],
    summary: BriefSummary,
    *,
    auto_fix: AutoFixOutcome | None = None,
) -> list[str]:
    """halt / skip section — appears only when there is something to say."""

    items: list[str] = []
    for channel in summary.channels_missing:
        label = _CHANNEL_LABEL.get(channel, channel)
        items.append(
            f"- **{label}** (`channel: {channel}`) — "
            "discover_NNN.json が見つからなかった (未実行)"
        )
    for channel in summary.channels_picked:
        report = by_channel.get(channel)
        if report is None:
            continue
        halt = report.payload.get("halt_reason")
        if isinstance(halt, str) and halt:
            label = _CHANNEL_LABEL.get(channel, channel)
            items.append(f"- **{label}** halt: {halt}")

    # spec_025 — surface autonomous-fix halts and structural skips so
    # the operator sees them in §D alongside channel halts. A *merged*
    # auto-fix isn't a halt; §B Phase 2 owns that story.
    if auto_fix is not None:
        if auto_fix.skipped and auto_fix.skip_reason:
            items.append(f"- **自律修正 skipped**: {auto_fix.skip_reason}")
        elif not auto_fix.skipped and not auto_fix.merged:
            items.append(
                f"- **自律修正 HALT** "
                f"(`{auto_fix.spec_auto_id or 'no spec'}`, "
                f"template {auto_fix.template or '?'}): "
                f"{auto_fix.halt_reason or '理由不明'}"
            )

    if not items:
        return []
    return [
        "## D. halt・スキップ項目",
        "",
        *items,
        "",
    ]


def _render_section_e(
    summary: BriefSummary,
    by_channel: dict[str, ChannelReport],
) -> list[str]:
    lines = [
        "## E. バックログ・推移",
        "",
        "機械的チャンネルの発見残数 (Phase 2 で自律修正ループに渡す候補):",
        "",
        f"- ミューテーション actionable: **{summary.mutation_actionable}** 件",
        f"- 敵対的入力 ungraceful: **{summary.adversarial_ungraceful}** 件",
        f"- 機械的発見の合計 (B 節の残数): "
        f"**{summary.mechanical_findings_total}** 件",
        "",
        "AI推論の所見数 (報告専用 — 人間判断、ループには渡さない):",
        "",
        f"- AI 所見: **{summary.ai_findings}** 件 (**非決定的**)",
        "",
    ]
    # Cross-reference the source discover_NNN per channel.
    refs: list[str] = []
    for channel in KNOWN_CHANNELS:
        report = by_channel.get(channel)
        label = _CHANNEL_LABEL.get(channel, channel)
        if report is None:
            refs.append(f"- {label}: (未実行)")
        else:
            refs.append(
                f"- {label}: `{_rel_or_absolute(report.json_path)}` "
                f"(seq={report.seq:03d})"
            )
    if refs:
        lines.append("採用した discover_NNN:")
        lines.append("")
        lines.extend(refs)
        lines.append("")
    return lines


def _render_section_f(
    summary: BriefSummary,
    *,
    auto_fix: AutoFixOutcome | None = None,
) -> list[str]:
    """Honesty section. Always present — anchors the Phase-1 invariant."""

    phase2_merge = (
        auto_fix is not None
        and not auto_fix.skipped
        and auto_fix.merged
    )

    lines = ["## F. 起きなかったこと (正直さの節)", ""]
    if phase2_merge:
        lines.append(
            "- **push は実行していない** — 昨夜の自律修正は "
            "**ローカル merge まで**。`origin/main` へは反映していない "
            "(spec_025 §3、安全境界レベル 2 — 論点 2: 朝に人間が diff を "
            "見て手動 push)。"
        )
        lines.append(
            "- **次の発見チャンネルは走らせていない** — spec_017 は純粋な"
            "レンダラ。チャンネル実行は別経路 (人手 / `ccd nightly`)。"
        )
    else:
        lines.extend(
            [
                "- **Phase 1 は自律修正していない** — これは発見のみのレポートであり、"
                "本レポート生成時に CCD はコードを変更していない。"
                "機械的発見の自律修正ループ (Phase 2) は別 spec の責務。",
                "- **AI推論の所見は引き金にしない** — §C の所見は主張であって"
                "事実ではない。`_ai_workspace/bridge/inbox/` への自動投入も、"
                "自動 spec 化も、自動 dispatch もしていない。",
                "- **発見チャンネル自体はこの brief 生成では走らせていない** — "
                "spec_017 は純粋なレンダラ。チャンネル実行は別経路 (人手 / spec_019)。",
            ]
        )
    if summary.channels_missing:
        missing_label = ", ".join(
            f"`{c}`" for c in summary.channels_missing
        )
        lines.append(
            f"- **未集約のチャンネル**: {missing_label} — 該当の "
            "discover_NNN.json が存在しなかった (本レポートのデータ欠落)。"
        )
    lines.append("")
    return lines


def _rel_or_absolute(path: Path) -> str:
    """Return a path the way it most usefully prints in the report.

    If the path lies under the current working directory we strip the
    prefix so the report doesn't bake in a developer's home directory.
    Otherwise we print the absolute path.
    """

    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


__all__ = [
    "BriefResult",
    "BriefSummary",
    "CHANNEL_ADVERSARIAL",
    "CHANNEL_AI",
    "CHANNEL_MUTATION",
    "ChannelReport",
    "DEFAULT_DISCOVER_DIR_REL",
    "DEFAULT_NIGHTLY_DIR_REL",
    "KNOWN_CHANNELS",
    "run_brief",
]
