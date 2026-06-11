"""ccd brief — morning report renderer (spec_017, v2 Phase 1 + spec_025/028).

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

spec_028 — §B propose variant
-----------------------------
When the night included a successful **proposal** (``fix_mode="propose"``
+ R5 + R4 + guard all passed in the isolated clone), §B switches to a
third layout: same finding → action narrative, the same R-evidence,
the verified diff embedded inline, plus a ``git apply`` one-liner and
the patch file path. No merge happened — the operator decides whether
to adopt. When the propose loop ran but verification rejected the
candidate (R5/R4 fail or guard HALT), §B stays Phase 1 and the
rejection surfaces as a one-line note in §D — the propose mode
promise is "動くと確認済みの修正案だけを出す" (spec_028 §2-3), and
showing an unverified diff would break that.

To stay aligned with ``docs/DESIGN.md §9.6`` ("既定は簡潔・例外時のみ
伸びる"), the Phase 2 §B is only rendered when ``auto_fix`` is present
and reports either ``merged=True`` (auto) or ``proposed=True``
(propose). Skipped / halted nights still see the Phase 1 §B — those
nights have nothing additional worth surfacing in the brief's body
that isn't already in §D (halt / skip).

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

from ccd.guard import added_slow_markers

if TYPE_CHECKING:
    from ccd.nightly import AutoFixOutcome, ChannelOutcome

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
    auto_fix_extras: Sequence[AutoFixOutcome] = (),
    channel_outcomes: Sequence[ChannelOutcome] | None = None,
    # spec_041 — WorkerPool telemetry. P > 1 turns on the 夜サマリ line
    # under §B that surfaces 候補 K / 並列 P / 達成同時実行数 / merge数
    # / drop 数（理由別）. Default values keep the v2 / spec_023〜040
    # 外形 bit-for-bit identical.
    parallelism: int = 1,
    achieved_max_concurrency: int = 1,
    drop_reasons: Sequence[str] = (),
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
            auto_fix_extras=tuple(auto_fix_extras or ()),
            repo=repo,
            channel_outcomes=channel_outcomes,
            parallelism=parallelism,
            achieved_max_concurrency=achieved_max_concurrency,
            drop_reasons=tuple(drop_reasons or ()),
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
    auto_fix_extras: tuple[AutoFixOutcome, ...] = (),
    repo: Path | None = None,
    channel_outcomes: Sequence[ChannelOutcome] | None = None,
    parallelism: int = 1,
    achieved_max_concurrency: int = 1,
    drop_reasons: tuple[str, ...] = (),
) -> str:
    by_channel = {c.channel: c for c in channels}
    # spec_038 — collect all outcomes for multi-candidate brief sections.
    # At K=1 (extras empty) the renderer falls through to the v2 layout
    # bit-for-bit (no §B subsection enumeration). At K>1 the §B multi
    # variant kicks in.
    all_outcomes: tuple[AutoFixOutcome, ...] = (
        (auto_fix, *auto_fix_extras) if auto_fix is not None else ()
    )
    multi_candidate = len(all_outcomes) > 1
    phase2_merge_active = (
        auto_fix is not None
        and not auto_fix.skipped
        and auto_fix.merged
    )
    propose_active = (
        auto_fix is not None
        and not auto_fix.skipped
        and getattr(auto_fix, "proposed", False)
    )

    if phase2_merge_active:
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
    elif propose_active:
        header = (
            f"# 朝レポート {today.isoformat()} — "
            "ccd v2 Phase 3 (修正案あり — 提案モード)"
        )
        preamble = (
            "> Loop β の発見3チャンネル "
            "(`mutation` / `adversarial` / `ai`) に加え、"
            "**提案モード** (`fix_mode=\"propose\"`, spec_028) が "
            "1 件の **動くと確認済みの修正案**を生成しました。"
            "§B に修正案の diff と検証証拠 (R5/R4/ガード) と "
            "`git apply` ワンライナーを掲載 ── 採用するなら 1 コマンドで "
            "適用できる状態です。実 repo には何も変更を加えていません "
            "(隔離クローン内で生成、merge も commit も push もしません)。"
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

    # spec_030 — count channel-level halts / skips surfaced via the
    # nightly orchestrator's :class:`ChannelOutcome` list (mutation
    # 0-mutants HALT and adversarial未設定 skip both flow here). The
    # count anchors §A and §D so the operator sees the silent-failure
    # gates at a glance instead of having to scan the body.
    channel_halt_count = sum(
        1
        for co in (channel_outcomes or ())
        if not bool(getattr(co, "success", True))
        and (getattr(co, "halt_reason", "") or "")
    )

    parts: list[str] = [
        header,
        "",
        preamble,
        "",
    ]
    parts.extend(
        _render_section_a(
            summary,
            auto_fix=auto_fix,
            auto_fix_extras=auto_fix_extras,
            channel_halt_count=channel_halt_count,
        )
    )
    if multi_candidate:
        # spec_038 §2-4 — render §B as a per-candidate enumeration. The
        # mechanical-channel Phase 1 §B is still appended below so the
        # operator sees both the fix story AND the underlying findings.
        parts.extend(
            _render_section_b_multi(
                outcomes=all_outcomes,
                by_channel=by_channel,
                repo=repo,
                parallelism=parallelism,
                achieved_max_concurrency=achieved_max_concurrency,
                drop_reasons=drop_reasons,
            )
        )
    elif phase2_merge_active:
        assert auto_fix is not None  # narrowed by phase2_merge_active above
        parts.extend(_render_section_b_phase2(auto_fix=auto_fix, repo=repo))
    elif propose_active:
        assert auto_fix is not None  # narrowed by propose_active above
        parts.extend(_render_section_b_propose(auto_fix=auto_fix, repo=repo))
    else:
        parts.extend(_render_section_b(by_channel))
    parts.extend(_render_section_c(by_channel))
    parts.extend(
        _render_section_d(
            by_channel,
            summary,
            auto_fix=auto_fix,
            auto_fix_extras=auto_fix_extras,
            channel_outcomes=channel_outcomes,
        )
    )
    parts.extend(_render_section_e(summary, by_channel))
    parts.extend(
        _render_section_f(
            summary,
            auto_fix=auto_fix,
            auto_fix_extras=auto_fix_extras,
        )
    )

    return "\n".join(parts).rstrip() + "\n"


def _render_section_a(
    summary: BriefSummary,
    *,
    auto_fix: AutoFixOutcome | None = None,
    auto_fix_extras: tuple[AutoFixOutcome, ...] = (),
    channel_halt_count: int = 0,
) -> list[str]:
    bits: list[str] = []

    # spec_025/028 — surface the auto-fix / proposal outcome on the
    # front page so the operator gets the headline before scrolling.
    # States: merged (auto §B follows), proposed (propose §B follows),
    # skipped (no candidate / paused / un-pushed backlog),
    # halted (loop ran but did not merge / propose).
    #
    # spec_038 — when there are multiple candidates, summarise the
    # aggregate counts (merged / proposed / halted / skipped) instead
    # of the single-candidate phrasing. Default K=1 (extras empty) keeps
    # the v2 phrasing bit-for-bit.
    all_outcomes: tuple[AutoFixOutcome, ...] = (
        (auto_fix, *auto_fix_extras) if auto_fix is not None else ()
    )
    if len(all_outcomes) > 1:
        merged_n = sum(
            1 for o in all_outcomes if not o.skipped and o.merged
        )
        proposed_n = sum(
            1
            for o in all_outcomes
            if not o.skipped and getattr(o, "proposed", False)
        )
        halted_n = sum(
            1
            for o in all_outcomes
            if not o.skipped
            and not o.merged
            and not getattr(o, "proposed", False)
        )
        skipped_n = sum(1 for o in all_outcomes if o.skipped)
        mode = getattr(auto_fix, "mode", "auto") if auto_fix else "auto"
        prefix = "提案モード" if mode == "propose" else "自律修正"
        # Build the bullet so it's honest about the total K processed
        # serially this night, then per-outcome counts.
        parts_a: list[str] = [
            f"**{prefix} {len(all_outcomes)} 件直列処理**"
        ]
        sub: list[str] = []
        if merged_n:
            sub.append(f"merge {merged_n}")
        if proposed_n:
            sub.append(f"proposal {proposed_n}")
        if halted_n:
            sub.append(f"HALT {halted_n}")
        if skipped_n:
            sub.append(f"skip {skipped_n}")
        if sub:
            parts_a.append(" (" + " / ".join(sub) + ")")
        parts_a.append(" — §B に候補ごとの小節を掲載")
        bits.append("".join(parts_a))
    elif auto_fix is not None:
        mode = getattr(auto_fix, "mode", "auto")
        proposed = getattr(auto_fix, "proposed", False)
        if not auto_fix.skipped and auto_fix.merged:
            bits.append(
                f"**昨夜の自律修正 1 件をローカル merge** "
                f"(template {auto_fix.template}, "
                f"`{auto_fix.spec_auto_id}`) — §B に diff と push コマンド"
            )
        elif not auto_fix.skipped and proposed:
            bits.append(
                f"**昨夜の修正案 1 件を生成 (提案モード)** "
                f"(template {auto_fix.template}, "
                f"`{auto_fix.spec_auto_id}`) — §B に diff と "
                "`git apply` ワンライナー（実 repo は無変更）"
            )
        elif not auto_fix.skipped and not auto_fix.merged:
            label = "提案モード HALT" if mode == "propose" else "自律修正 HALT"
            bits.append(
                f"**{label}** ({auto_fix.spec_auto_id or 'no spec'}) — "
                f"{auto_fix.halt_reason or '理由不明'}"
            )
        elif auto_fix.skipped and auto_fix.skip_reason:
            label = "提案モード skip" if mode == "propose" else "自律修正 skip"
            bits.append(f"**{label}**: {auto_fix.skip_reason}")

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
    # spec_030 — surface the silent-failure HALT count alongside the
    # finding counts so the operator notices a 0-mutants HALT or an
    # adversarial-skip even when scrolling past §A in a hurry.
    if channel_halt_count > 0:
        bits.append(f"**HALT {channel_halt_count} 件** (§D 参照)")
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
    lines.append(_r5_evidence_line(auto_fix, label=r5_label))
    lines.append(_r4_evidence_line(auto_fix, in_clone=False))
    if auto_fix.guard_passed:
        lines.append("- ガード (R1〜R3): **pass**")
    else:
        reasons_text = "; ".join(auto_fix.guard_halt_reasons) or "理由不明"
        lines.append(f"- ガード: **HALT** — {reasons_text}")
    # spec_039 — surface the convergence loop's iteration count when
    # the profile raised ``safety.loop_max_iterations`` above 1. At the
    # default 1 the line is suppressed so the §B layout is bit-for-bit
    # identical to spec_025〜038.
    fix_loop_line = _format_fix_loop_summary(auto_fix)
    if fix_loop_line:
        lines.append(fix_loop_line)

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


def _render_section_b_propose(
    *,
    auto_fix: AutoFixOutcome,
    repo: Path | None,
) -> list[str]:
    """spec_028 §2-3 — render §B as the propose-mode story.

    Only invoked when ``auto_fix.proposed is True`` (R5 + R4 + guard
    all passed in the isolated clone and the diff was captured as a
    patch file). Surfaces the same four artifacts as the Phase 2
    auto §B, but **does not** suggest ``git push`` — propose mode
    never merged; the operator's action is ``git apply`` on the patch
    file (then commit / review as they see fit).

    Failure cases (R5/R4 fail, guard HALT, no diff) DO NOT render
    here — they surface in §D as a one-line note, per spec §2-3
    "弾かれた夜は §B 提案版にしない".
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
        "## B. 昨夜の修正案 (提案モード — `spec_028`)",
        "",
        "隔離クローン内で **R5 / R4 / ガードを通過した** 修正案を 1 件 "
        "生成しました。**実 repo には何も変更を加えていません** "
        "(merge / commit / push のいずれもしていません)。下記の "
        "`git apply` ワンライナーで採用できます。",
        "",
        "### 発見と修正案",
        "",
        f"- **テンプレ**: {template_desc}",
        f"- **signature**: `{auto_fix.finding_signature or '(不明)'}`",
        f"- **spec_auto**: `{auto_fix.spec_auto_id or '(不明)'}` "
        f"({auto_fix.candidate_count} 候補中 1 件を選択)",
        f"- **使い捨てブランチ (クローン内)**: "
        f"`{auto_fix.branch or '(不明)'}` (クローン破棄済み — "
        "実 repo には残らない)",
        "",
        "### 検証の証拠",
        "",
    ]
    if template == "A":
        r5_label = "R5 (target mutation killed in clone)"
    elif template == "B":
        r5_label = "R5 (parser now raises a graceful error in clone)"
    else:
        r5_label = "R5"
    lines.append(_r5_evidence_line(auto_fix, label=r5_label))
    lines.append(_r4_evidence_line(auto_fix, in_clone=True))
    if auto_fix.guard_passed:
        lines.append("- ガード (R1〜R3): **pass**")
    else:
        reasons_text = "; ".join(auto_fix.guard_halt_reasons) or "理由不明"
        lines.append(f"- ガード: **HALT** — {reasons_text}")
    fix_loop_line = _format_fix_loop_summary(auto_fix)
    if fix_loop_line:
        lines.append(fix_loop_line)

    lines.append("")
    lines.append("### 修正案の diff")
    lines.append("")
    diff = auto_fix.proposal_diff or ""
    if not diff:
        lines.append(
            "_(diff が記録されていません — loop の seam が "
            "`proposal_diff` を埋めなかった構造的ケース。)_"
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
                "全体はパッチファイルでご確認ください。)_"
            )

    lines.append("")
    lines.append("### 採用方法 (`git apply` ワンライナー)")
    lines.append("")
    lines.append(
        "レビューして問題なければコピーして実行してください "
        "(propose モードはここでは適用していません):"
    )
    lines.append("")
    lines.append("```bash")
    lines.append(_compose_apply_command(repo, auto_fix.proposal_patch_path))
    lines.append("```")
    if auto_fix.proposal_patch_path is not None:
        lines.append("")
        lines.append(
            f"パッチファイル: `{_rel_or_absolute(auto_fix.proposal_patch_path)}`"
        )
    lines.append("")
    return lines


def _compose_apply_command(
    repo: Path | None,
    patch_path: Path | None,
) -> str:
    """Build the ``git apply`` one-liner for the propose-§B body.

    Embeds an absolute patch path so the operator can paste from any
    cwd; uses ``git -C <repo>`` when the repo is known so they don't
    even have to be in the repo directory.
    """

    if patch_path is None:
        return "git apply <path-to-proposal.patch>"
    patch_str = str(patch_path)
    if repo is not None:
        try:
            repo_str = str(Path(repo).resolve())
        except OSError:
            repo_str = str(repo)
        return f"git -C {repo_str} apply {patch_str}"
    return f"git apply {patch_str}"


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


def _r5_evidence_line(outcome: AutoFixOutcome, *, label: str) -> str:
    """spec_045 §2-1 — the §B/§D R5 evidence line with the N-times
    determinism detail (RT-3).

    Renders ``- {label}: **pass** — killed (N/N 回安定)`` when
    ``outcome.r5_detail`` is non-empty (the profile raised
    ``safety.r5_recheck_times`` above 1), falling back to the bare
    ``**pass/fail**`` line otherwise so a default-profile (``=1``) night
    keeps the spec_023〜044 §B layout bit-for-bit. On an unstable fail
    the detail carries ``R5 不安定: killed N回中 M回のみ``."""

    verdict = "pass" if outcome.r5_killed else "fail"
    base = f"- {label}: **{verdict}**"
    detail = (outcome.r5_detail or "").strip()
    if detail:
        return f"{base} — {detail}"
    return base


def _r4_evidence_line(auto_fix: AutoFixOutcome, *, in_clone: bool) -> str:
    """spec_043 §2-4 — the §B R4 evidence line with the dynamic count.

    Renders ``- R4 (`pytest -q` 全件 green[, baseline 比較]): **pass** —
    collected N, passed N, baseline N`` when the suite runner reported
    counts (``auto_fix.r4_detail`` non-empty), falling back to the plain
    ``**pass/fail**`` line otherwise so a fake-runner / no-baseline night
    keeps the spec_023〜042 §B layout bit-for-bit. On a count-driven R4
    fail, ``r4_detail`` carries the regression reason (実行テスト数が
    baseline を下回った …) and is surfaced verbatim after the verdict."""

    where = " in clone" if in_clone else ""
    verdict = "pass" if auto_fix.r4_suite_passed else "fail"
    base = f"- R4 (`pytest -q` 全件 green{where}): **{verdict}**"
    detail = (auto_fix.r4_detail or "").strip()
    if detail:
        return f"{base} — {detail}"
    return base


def _format_fix_loop_summary(auto_fix: AutoFixOutcome) -> str:
    """spec_039 — return a one-line iteration summary or "" to suppress.

    Format examples:

    - converged after 2 iterations  → ``- 収束: 2 iterations``
    - halted after 5 iterations on no-progress detection →
      ``- 未収束: 5 iterations, 無進捗検知で halt``

    Returns ``""`` (suppressed) when the candidate is skipped, when
    ``iterations`` is zero (no loop body ran), or when the loop ran
    exactly once and converged — at the default
    ``loop_max_iterations=1`` the single-iteration converged path is
    the spec_023〜038 happy path, so omitting the line keeps §B
    bit-for-bit identical to v2 for default profiles (spec_039 §3-1).
    """

    iterations = int(getattr(auto_fix, "iterations", 0) or 0)
    converged = bool(getattr(auto_fix, "converged", False))
    loop_halt = str(getattr(auto_fix, "loop_halt_reason", "") or "")

    if auto_fix.skipped:
        return ""
    if iterations <= 0:
        return ""
    if iterations == 1 and converged and not loop_halt:
        # v2 single-shot happy path — suppress the line so the K=1 /
        # iter=1 brief stays exactly as it was before spec_039.
        return ""

    if converged:
        return f"- 収束: {iterations} iterations"

    cause = _shorten_loop_halt_reason(loop_halt)
    suffix = f" ({cause})" if cause else ""
    return f"- 未収束: {iterations} iterations{suffix}"


def _shorten_loop_halt_reason(reason: str) -> str:
    """Map :data:`ccd.loop.LOOP_HALT_*` anchors to a 1-phrase
    operator-facing cause.

    The full anchors carry "fix-loop: " prefix that is helpful in
    machine logs but noisy in a one-line brief summary. Strip the
    prefix and map the well-known causes to compact phrasing.
    """

    if not reason:
        return ""
    body = reason.removeprefix("fix-loop: ")
    if "no-progress" in body:
        return "無進捗検知で halt"
    if "max_iterations" in body:
        return "max_iterations 到達"
    if "wall-clock budget" in body:
        return "wall-clock 予算 exhausted"
    if "immediate-halt" in body:
        return "immediate-halt カテゴリ"
    return body


def _render_section_b_multi(
    *,
    outcomes: tuple[AutoFixOutcome, ...],
    by_channel: dict[str, ChannelReport],
    repo: Path | None,
    parallelism: int = 1,
    achieved_max_concurrency: int = 1,
    drop_reasons: tuple[str, ...] = (),
) -> list[str]:
    """spec_038 §2-4 — render §B as per-candidate subsections when the
    profile raised ``safety.max_candidates_per_night`` above 1.

    Each outcome becomes one ``### 候補 i/N`` subsection that surfaces
    template, signature, status (merged / proposed / halted / skipped),
    the R-evidence, the diff (when merged or proposed), and the
    appropriate operator one-liner (``git push`` for merged auto,
    ``git apply`` for proposed). After the per-candidate enumeration
    the Phase 1 mechanical-channel §B is appended so the underlying
    findings remain visible alongside the loop's actions.

    spec_041 — when ``parallelism > 1`` the section opens with a
    one-line 夜サマリ listing K / P / 達成同時実行数 / merge数 / drop数
    so the operator sees the parallel telemetry at a glance.
    """

    mode = (
        getattr(outcomes[0], "mode", "auto") if outcomes else "auto"
    )
    heading = (
        "## B. 昨夜の修正案 (提案モード — 複数候補, `spec_038`)"
        if mode == "propose"
        else "## B. 昨夜の自律修正 (複数候補 — `spec_038`)"
    )
    # spec_041 — when parallelism > 1, switch the lede to say "並列処理"
    # instead of "直列処理"; either way the per-candidate enumeration
    # below carries the actual ordering (= integration完了順).
    process_word = "並列処理" if parallelism > 1 else "直列処理"
    lines: list[str] = [
        heading,
        "",
        f"本夜は **{len(outcomes)} 件の候補を{process_word}**しました "
        f"(`safety.max_candidates_per_night`)。候補ごとの結果は以下のとおり。",
        "",
    ]
    if parallelism > 1:
        # spec_041 §2-5 — 夜サマリ。"候補 K / 並列 P / 達成同時実行数 /
        # merge 数 / drop 数（理由別）".
        merged_n = sum(
            1 for o in outcomes if not o.skipped and o.merged
        )
        dropped_n = sum(
            1 for o in outcomes if o.skipped or (not o.merged and not getattr(o, "proposed", False))
        )
        bits = [
            f"候補 K={len(outcomes)}",
            f"並列 P={parallelism}",
            f"達成同時実行数={achieved_max_concurrency}",
            f"merge={merged_n}",
            f"drop={dropped_n}",
        ]
        lines.append("**夜サマリ (spec_041)**: " + " / ".join(bits) + "。")
        if drop_reasons:
            reasons_text = "; ".join(drop_reasons)
            lines.append(f"drop 理由: {reasons_text}。")
        lines.append("")

    n = len(outcomes)
    for i, outcome in enumerate(outcomes, start=1):
        lines.extend(
            _render_one_candidate_subsection(
                outcome=outcome,
                index=i,
                total=n,
                repo=repo,
            )
        )

    # After the per-candidate enumeration, surface the Phase 1
    # mechanical-channel §B so the operator still sees the underlying
    # findings (spec_038 §2-4 — multi-candidate brief includes both
    # the loop's actions AND the mechanical-channel context).
    lines.append("### 機械的チャンネルの発見 (Phase 1 — 参考)")
    lines.append("")
    lines.extend(_render_mutation_findings(by_channel.get(CHANNEL_MUTATION)))
    lines.append("")
    lines.extend(_render_adversarial_findings(by_channel.get(CHANNEL_ADVERSARIAL)))
    lines.append("")
    return lines


def _render_one_candidate_subsection(
    *,
    outcome: AutoFixOutcome,
    index: int,
    total: int,
    repo: Path | None,
) -> list[str]:
    """spec_038 §2-4 — render one candidate's subsection for multi-candidate
    §B. Compact and honest: skipped/halted candidates get a one-line
    note plus reason; merged/proposed candidates get the same R-evidence
    + diff embed as the single-candidate Phase 2 / propose layouts.
    """

    template = outcome.template or "?"
    if template == "A":
        template_desc = "テンプレ A (mutation 生存 → test-only)"
    elif template == "B":
        template_desc = "テンプレ B (adversarial ungraceful → 本番修正 + 再現テスト)"
    else:
        template_desc = f"テンプレ {template}"

    head = f"### 候補 {index}/{total}"
    mode = getattr(outcome, "mode", "auto")

    if outcome.skipped:
        status_label = (
            "**skip** (提案モード)" if mode == "propose" else "**skip**"
        )
        return [
            head,
            "",
            f"- 状態: {status_label}",
            f"- 理由: {outcome.skip_reason or '理由不明'}",
            "",
        ]

    proposed = getattr(outcome, "proposed", False)
    if outcome.merged:
        status_label = "**ローカル merge 済み**"
    elif proposed:
        status_label = "**修正案を保存** (実 repo は無変更)"
    else:
        status_label = (
            "**HALT (提案モード)**" if mode == "propose" else "**HALT**"
        )

    lines: list[str] = [
        head,
        "",
        f"- 状態: {status_label}",
        f"- テンプレ: {template_desc}",
        f"- signature: `{outcome.finding_signature or '(不明)'}`",
        f"- spec_auto: `{outcome.spec_auto_id or '(不明)'}`"
        + (
            f" ({outcome.candidate_count} 候補中)"
            if outcome.candidate_count
            else ""
        ),
        f"- branch: `{outcome.branch or '(不明)'}`",
    ]
    # spec_041 — surface worker_id + start/finish timestamps when
    # populated (auto-mode WorkerPool path). Empty strings keep the
    # subsection identical to spec_038〜040 for outcomes that didn't
    # flow through a worker (skipped before dispatch, propose mode).
    worker_id = getattr(outcome, "worker_id", "")
    started_at = getattr(outcome, "worker_started_at", "")
    finished_at = getattr(outcome, "worker_finished_at", "")
    if worker_id:
        worker_bits = [f"id={worker_id}"]
        if started_at:
            worker_bits.append(f"start={started_at}")
        if finished_at:
            worker_bits.append(f"finish={finished_at}")
        lines.append(f"- worker: {', '.join(worker_bits)}")
    lines.append("")

    if template == "A":
        r5_label = "R5 (target mutation killed)"
    elif template == "B":
        r5_label = "R5 (parser now raises a graceful error)"
    else:
        r5_label = "R5"
    if outcome.dispatched:
        lines.append(_r5_evidence_line(outcome, label=r5_label))
        lines.append(_r4_evidence_line(outcome, in_clone=False))
        if outcome.guard_passed:
            lines.append("- ガード (R1〜R3): **pass**")
        else:
            reasons_text = (
                "; ".join(outcome.guard_halt_reasons) or "理由不明"
            )
            lines.append(f"- ガード: **HALT** — {reasons_text}")
        fix_loop_line = _format_fix_loop_summary(outcome)
        if fix_loop_line:
            lines.append(fix_loop_line)
        lines.append("")

    if not outcome.merged and not proposed:
        # HALT — record the halt_reason; no diff embed (the diff is not
        # a reviewable artifact for a halted attempt).
        if outcome.halt_reason:
            lines.append(f"halt_reason: {outcome.halt_reason}")
            lines.append("")
        return lines

    # Merged or proposed — embed the diff (truncated if huge) and the
    # operator one-liner.
    diff = (
        outcome.merge_diff
        if outcome.merged
        else (outcome.proposal_diff or "")
    )
    if diff:
        truncated = len(diff) > _PHASE2_DIFF_CAP
        body = diff[:_PHASE2_DIFF_CAP] if truncated else diff
        lines.append("```diff")
        lines.extend(body.rstrip("\n").splitlines() or [""])
        lines.append("```")
        if truncated:
            lines.append("")
            lines.append(
                f"_(diff は {_PHASE2_DIFF_CAP} byte で切り詰めました)_"
            )
        lines.append("")

    if outcome.merged:
        lines.append("操作: review してから")
        lines.append("```bash")
        lines.append(_compose_push_command(repo))
        lines.append("```")
        lines.append("")
    elif proposed:
        lines.append("採用するなら:")
        lines.append("```bash")
        lines.append(
            _compose_apply_command(repo, outcome.proposal_patch_path)
        )
        lines.append("```")
        if outcome.proposal_patch_path is not None:
            lines.append("")
            lines.append(
                "パッチファイル: "
                f"`{_rel_or_absolute(outcome.proposal_patch_path)}`"
            )
        lines.append("")
    return lines


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


def _halt_artifacts_link(outcome: AutoFixOutcome | None) -> str:
    """spec_047 §2-1 — relative markdown link to a HALT's persisted
    artifacts, or ``""`` when none were captured.

    The artifact dir (``…/nightly[/<policy>]/halts/<night>_<spec>/``) is a
    sibling sub-tree of the per-night report, so a link relative to the
    report is just ``halts/<dirname>/`` — robust whether the report is the
    flat single-policy one or a per-policy sweep report."""

    art = getattr(outcome, "halt_artifacts_dir", None)
    if art is None:
        return ""
    name = Path(art).name
    return f"[halts/{name}/](halts/{name}/)"


def _render_section_d(
    by_channel: dict[str, ChannelReport],
    summary: BriefSummary,
    *,
    auto_fix: AutoFixOutcome | None = None,
    auto_fix_extras: tuple[AutoFixOutcome, ...] = (),
    channel_outcomes: Sequence[ChannelOutcome] | None = None,
) -> list[str]:
    """halt / skip section — appears only when there is something to say.

    spec_030: also surfaces halt / skip reasons recorded by the nightly
    orchestrator's :class:`ChannelOutcome` list. Mutation 0-mutants HALT
    (silent failure: 0 mutants for non-empty targets) and adversarial
    skip ("[discovery.adversarial.parsers] 未設定") both flow here —
    without the explicit ``channel_outcomes`` plumbing they would
    appear in §D as the indistinguishable "未実行" line because
    halted channels never write a JSON file.
    """

    items: list[str] = []
    # spec_030 — channels that ran (or were deliberately skipped) but
    # did NOT write a JSON payload still surface their halt_reason via
    # ``channel_outcomes``. Index by channel name so missing-channel
    # rendering below can defer to the explicit reason when available.
    outcomes_by_channel: dict[str, str] = {}
    for co in (channel_outcomes or ()):
        if bool(getattr(co, "success", True)):
            continue
        reason = (getattr(co, "halt_reason", "") or "").strip()
        if not reason:
            continue
        outcomes_by_channel[getattr(co, "channel", "")] = reason

    for channel in summary.channels_missing:
        label = _CHANNEL_LABEL.get(channel, channel)
        reason = outcomes_by_channel.pop(channel, "")
        if reason:
            # The HALT / skip wording is whatever the orchestrator
            # supplied verbatim — discover.py emits the 0-mutants
            # HALT phrasing, sweep.py emits the adversarial-skipped
            # phrasing (spec_030 §2-2 / §2-4).
            items.append(f"- **{label}** halt: {reason}")
        else:
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
    # Any remaining halt reasons (e.g. an unfamiliar channel name) get
    # surfaced without a friendly label — better to expose them than
    # drop them silently (spec_030 §1 "正直な計測").
    for channel, reason in outcomes_by_channel.items():
        label = _CHANNEL_LABEL.get(channel, channel)
        items.append(f"- **{label}** halt: {reason}")

    # spec_025/028 — surface autonomous-fix / propose halts and
    # structural skips so the operator sees them in §D alongside
    # channel halts. A *merged* auto-fix and a *proposed* propose
    # outcome aren't halts; §B owns those stories.
    #
    # spec_038 — when extras are present (K > 1), the multi-candidate
    # §B already enumerates EVERY candidate's HALT/skip subsection.
    # Suppressing the per-candidate §D lines here avoids the duplicate
    # reporting that would otherwise repeat each HALT/skip in two
    # sections of the same brief.
    if auto_fix is not None and not auto_fix_extras:
        mode = getattr(auto_fix, "mode", "auto")
        proposed = getattr(auto_fix, "proposed", False)
        if auto_fix.skipped and auto_fix.skip_reason:
            label = "提案モード skipped" if mode == "propose" else "自律修正 skipped"
            items.append(f"- **{label}**: {auto_fix.skip_reason}")
        elif not auto_fix.skipped and mode == "propose" and not proposed:
            # spec_028 §2-3 — propose generated a candidate but
            # verification or guard rejected it. One-line note only;
            # §B stays Phase 1 (no unverified diff in the body).
            items.append(
                f"- **提案モード rejected** "
                f"(`{auto_fix.spec_auto_id or 'no spec'}`, "
                f"template {auto_fix.template or '?'}): "
                f"提案を生成したが検証/ガードで弾いた — "
                f"{auto_fix.halt_reason or '理由不明'}"
            )
        elif not auto_fix.skipped and not auto_fix.merged:
            line = (
                f"- **自律修正 HALT** "
                f"(`{auto_fix.spec_auto_id or 'no spec'}`, "
                f"template {auto_fix.template or '?'}): "
                f"{auto_fix.halt_reason or '理由不明'}"
            )
            link = _halt_artifacts_link(auto_fix)
            if link:
                line += f" — 診断: {link}"
            items.append(line)

    # spec_047 — additive §D lines that surface regardless of the K>1
    # suppression above (they are NOT per-candidate verdicts §B re-tells):
    #   §2-3 stale-candidate skips, §2-2 inbox supersessions. Collected over
    # every outcome (primary + extras) and de-duplicated so a multi-candidate
    # night still shows them exactly once.
    all_outcomes = (
        (auto_fix, *auto_fix_extras) if auto_fix is not None else ()
    )
    stale_lines: list[str] = []
    supersede_lines: list[str] = []
    for o in all_outcomes:
        skip_reason = getattr(o, "skip_reason", "") or ""
        if skip_reason.startswith("stale candidate skipped"):
            entry = f"- **{skip_reason}**"
            if entry not in stale_lines:
                stale_lines.append(entry)
        for old_id in getattr(o, "superseded_ids", ()) or ():
            new_id = getattr(o, "spec_auto_id", "") or "?"
            entry = f"- **inbox supersede**: `{old_id}` → `{new_id}` が置換 (同一 signature)"
            if entry not in supersede_lines:
                supersede_lines.append(entry)
    items.extend(stale_lines)
    items.extend(supersede_lines)

    # spec_048 §2-3 (🟢-2 観測強化) — a fix that purely ADDS @pytest.mark.slow
    # to an existing test silently drops it from the mutation subset runner
    # (``-m "not slow"``). This is safe-side (more surviving mutants → more
    # discovery; it never reaches a worse merge) so it does NOT halt — but the
    # permanent subset shrink deserves one warning line so a human notices.
    # Scanned from the diff already on each outcome (merge_diff / proposal_diff);
    # no extra plumbing through the loop. False positives are acceptable.
    slow_lines: list[str] = []
    for o in all_outcomes:
        diff = (getattr(o, "merge_diff", "") or "") or (
            getattr(o, "proposal_diff", "") or ""
        )
        if not diff:
            continue
        for marker in added_slow_markers(diff):
            entry = (
                f"- ⚠️ **mutation サブセット縮小** (`{getattr(o, 'spec_auto_id', '') or '?'}`): "
                f"fix が `@pytest.mark.slow` を純追加 ({marker}) — "
                f"当該テストは `-m \"not slow\"` 発見サブセットから恒久的に外れる "
                f"(安全側: ミュータント生存↑＝発見↑。HALT 不要・要確認のみ)"
            )
            if entry not in slow_lines:
                slow_lines.append(entry)
    items.extend(slow_lines)

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
    auto_fix_extras: tuple[AutoFixOutcome, ...] = (),
) -> list[str]:
    """Honesty section. Always present — anchors the Phase-1 invariant.

    spec_038 — when extras are present (K > 1), the merge / proposal
    presence is evaluated across the full outcome list so the honesty
    section still tells the truth on multi-candidate nights (e.g. one
    candidate merged and two halted still warrants the auto-mode
    honesty bullets).
    """

    all_outcomes: tuple[AutoFixOutcome, ...] = (
        (auto_fix, *auto_fix_extras) if auto_fix is not None else ()
    )
    phase2_merge = any(
        not o.skipped and o.merged for o in all_outcomes
    )
    proposed = any(
        not o.skipped and getattr(o, "proposed", False)
        for o in all_outcomes
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
    elif proposed:
        lines.append(
            "- **merge / commit / push のいずれも実行していない** — "
            "提案モード (spec_028) は修正案を **使い捨ての隔離クローン内**で"
            "生成・検証し、diff をパッチファイルに保存しただけ。"
            "実 repo の作業ツリー・ブランチ・main の HEAD は無変更。"
        )
        lines.append(
            "- **採用判断は人間** — `git apply` で適用するかどうか、"
            "適用するならどの粒度で commit するかは中島さん判断。"
            "本レポートは「動くと確認済みの修正案」を提示しただけで、"
            "勝手に適用はしません。"
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
