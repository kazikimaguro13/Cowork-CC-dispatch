"""ccd sweep — multi-policy nightly orchestration (spec_029).

spec_018 (v2 Phase 1) introduced **one profile per CCD instance** — a
single ``_ai_workspace/ccd_profile.toml`` configured the repo, the
channels, the schedule, and (from spec_023〜028) the fix mode. That was
enough to run CCD against itself, but only one client repo could
benefit from autonomous fix / propose without "fork the codebase".

spec_029 lifts that 1-policy ceiling. Operators drop one TOML per
policy into ``_ai_workspace/profiles/`` and a single weekly task —
``ccd nightly-all`` — sweeps every policy in turn. Each policy keeps
its own ``fix_mode`` (e.g. CCD itself = ``"auto"``, a client = "propose"),
its own discovery channels, its own ``repo`` target. The sweep:

1. Loads the registry via :func:`ccd.profile.load_profile_registry`.
   When the ``profiles/`` directory is absent the loader falls back to
   the legacy single-profile path so existing single-policy operation
   is bit-for-bit unchanged (the sweep then has exactly one entry
   named ``"ccd"`` and writes the report to the spec_020 flat path).
2. Runs :func:`ccd.nightly.run_nightly` for each policy, redirecting
   every artifact (discover JSON, morning report, proposal patches)
   into a per-policy subdirectory under **CCD's own** ``_ai_workspace/``
   ── client repos with ``fix_mode in {"propose","off"}`` therefore
   receive ZERO writes from CCD (论点3 — privacy / isolation). The only
   exception is the ``"auto"`` mode merge into the target repo's local
   ``main``, which is the auto-mode contract (spec_023〜028).
3. Isolates per-policy failures: any exception raised while processing
   policy N is captured into a :class:`PolicyOutcome` and policies
   ``N+1..end`` continue (论点4 — 1 施策の事故が他施策を止めない). The
   sweep ALWAYS returns ``success=True`` after attempting every
   policy — individual failures are surfaced in the cross-policy
   index, not bubbled up to the scheduler as "the run failed".
4. Renders a one-line-per-policy index
   (``_ai_workspace/nightly/index_YYYY-MM-DD.md``) so the operator
   reads ONE file the morning after and dives into per-policy reports
   only when something interesting happened (``docs/DESIGN.md §9.6``
   "既定は簡潔・例外時のみ伸びる" applied to the index, spec §2-3).

Per-policy artifact layout (spec §2-3)
--------------------------------------

For each policy ``<name>``, all writes go to CCD-side paths:

- discover: ``<ccd_repo>/_ai_workspace/discover/<name>/discover_NNN.{md,json}``
- morning report:
  ``<ccd_repo>/_ai_workspace/nightly/<name>/report_YYYY-MM-DD.md``
- proposal patches (propose mode only):
  ``<ccd_repo>/_ai_workspace/nightly/<name>/proposals/proposal_*.patch``
- cross-policy index:
  ``<ccd_repo>/_ai_workspace/nightly/index_YYYY-MM-DD.md`` (flat —
  one index per night, not per policy)

When the registry falls back to single-policy mode (legacy operation,
``profiles/`` absent), the sweep instead invokes ``run_nightly`` with
no path overrides, so the report still lands at
``<repo>/_ai_workspace/nightly/report_YYYY-MM-DD.md`` (spec_020 flat
layout). Existing tests that pin those paths therefore continue to
pass without modification.

What the sweep does NOT do
--------------------------

- It does not change channel / loop / guard / translate / brief
  semantics ── per spec §3 (touch list) only this module + ``cli.py``
  + ``profile.py`` (registry loader) + ``brief.py`` (index helper if
  any) + the scheduler template + tests + version files are touched.
  The body of ``run_nightly`` is unchanged ── ``nightly-all`` is just
  "call ``run_nightly`` per policy + write an index".
- It does not retry a failed policy. A policy that raised is
  recorded as failed for this night; next sweep, the same policy
  runs again from scratch.
- It does not parallelize. Direct series is enough: even on a 5-
  policy weekly run each policy takes minutes-to-hours so a 5×
  serial run completes overnight; cost / contention beats wall-
  clock at this scale (spec §2-2 "直列で十分").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ccd.nightly import NightlyResult, run_nightly
from ccd.profile import (
    PROFILES_DIR_REL,
    PolicyEntry,
    load_profile_registry,
)

# Where the cross-policy index lands. One file per night, parallel to
# the per-policy ``nightly/<name>/`` subdirectories. The morning ritual
# is "open this file first, drill into a policy only when its summary
# is interesting" (spec §2-3).
INDEX_DIR_REL: Path = Path("_ai_workspace") / "nightly"


# Type alias for the seam tests use to substitute ``run_nightly`` —
# accepts the full ``run_nightly`` kwargs surface and returns a
# ``NightlyResult``. Production wiring uses :func:`ccd.nightly.run_nightly`
# directly; tests pass a fake that records its calls and returns canned
# results so failure-isolation can be exercised without any real
# discovery / dispatch.
NightlyRunner = Callable[..., NightlyResult]


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PolicyOutcome:
    """One policy's contribution to a multi-policy sweep (spec_029 §2-2).

    Exactly one of ``result`` / ``error`` is populated:

    - ``result is not None`` and ``error is None`` — the policy's
      ``run_nightly`` returned (it may have halted internally, e.g. a
      channel canary halt or a propose-mode rejection — those are
      surfaced in the per-policy morning report, not as a sweep-level
      failure).
    - ``result is None`` and ``error`` is set — the policy raised an
      exception *outside* of ``run_nightly``'s own halt-reason machinery
      (e.g. the target repo path does not exist, the dispatcher seam
      crashed in a way ``run_nightly`` could not catch). The index
      surfaces this as a sweep-level failure (论点4).

    ``report_path`` is the absolute path of the morning report that
    ``run_nightly`` wrote (or ``None`` if it never got that far). The
    cross-policy index uses it to render the per-policy link.
    """

    name: str
    success: bool
    error: str = ""
    result: NightlyResult | None = None
    report_path: Path | None = None
    source: Path | None = None


@dataclass
class SweepResult:
    """``run_nightly_all`` return value (spec_029 §2-2).

    ``success`` is ``True`` whenever the sweep *attempted* every policy
    — individual policy failures do NOT flip it (论点4 "1 施策の事故が
    他施策を止めない"). The scheduler treats the sweep as "successful"
    as long as it completed the round; the operator reads the index for
    the real story.

    ``policies`` lists outcomes in the order the sweep processed them
    (registry order = alphabetical by name). ``index_path`` is the
    absolute path of the morning index ``run_nightly_all`` wrote, or
    ``None`` when the sweep had zero policies (empty ``profiles/``
    directory — still considered a successful sweep, just nothing to
    do).
    """

    success: bool
    today: date
    policies: list[PolicyOutcome] = field(default_factory=list)
    index_path: Path | None = None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_nightly_all(
    *,
    repo: Path,
    profiles_dir: Path | None = None,
    today: date | None = None,
    nightly_runner: NightlyRunner | None = None,
    **nightly_kwargs: Any,
) -> SweepResult:
    """Sweep every policy in the registry through ``run_nightly``.

    ``repo`` is **CCD's own working directory** — the location that
    holds ``_ai_workspace/profiles/`` and the destination of every
    per-policy artifact. The *target* repo each policy inspects comes
    from that policy's ``profile.repo`` field, resolved relative to
    ``repo`` when not absolute.

    ``profiles_dir`` is an override for the registry location (used by
    tests; production reads ``<repo>/_ai_workspace/profiles/``). When
    omitted, the loader applies its standard fallback to the legacy
    single-profile path if the directory does not exist (spec_029 §2-1).

    Remaining ``nightly_kwargs`` are forwarded to each per-policy
    :func:`run_nightly` call — tests pass channel / brief / git / fix
    seams through this kwargs bag so a sweep can be exercised end-to-end
    without invoking real ``mutmut`` / ``claude`` / git. Per-policy
    output overrides (``discover_dir`` / ``brief_dir`` /
    ``proposal_dir``) are computed by the sweep and override any
    same-named entry the caller supplied — that override hierarchy
    keeps tests honest (a test cannot accidentally fix the output
    path of every policy to the same directory).

    Failure isolation contract (spec §2-2 论点4):

    - Per-policy exceptions raised by ``run_nightly`` (or seam loaders)
      are caught into a :class:`PolicyOutcome` with ``success=False``
      and the next policy continues.
    - Loader-level errors (TOML parse / pydantic schema / invalid
      policy name) bubble up; they happen BEFORE any policy runs and
      indicate a registry that needs operator attention. The sweep is
      not "successful" in this case because there is nothing to sweep.

    spec_019 §2-3 / spec_026 §3 caveat: this entry point does NOT
    invoke a real ``ccd nightly-all`` end-to-end. The sweep itself is
    structurally tested by injecting fake nightly runners; the real
    seams are tested by ``ccd nightly`` (single policy). The sweep is
    a thin loop that adds failure isolation + path namespacing — no
    new dispatch / discovery / guard surface (spec §3).
    """

    repo = Path(repo).resolve()
    today_d = today if today is not None else _utc_today()
    run = nightly_runner if nightly_runner is not None else run_nightly

    registry = load_profile_registry(repo, profiles_dir=profiles_dir)

    # Single-policy fallback (``profiles/`` directory absent) — preserve
    # the spec_020 flat layout so existing operation is unchanged. The
    # registry loader returns one entry named ``"ccd"`` whose source is
    # the legacy ``ccd_profile.toml`` (or ``None`` when even that was
    # missing). We detect "fallback mode" structurally: the registry has
    # exactly one entry AND ``profiles_dir`` does not exist. Any other
    # case (real registry, even empty) uses per-policy paths.
    resolved_profiles_dir = (
        Path(profiles_dir).resolve()
        if profiles_dir is not None
        else (repo / PROFILES_DIR_REL).resolve()
    )
    fallback_mode = (
        not resolved_profiles_dir.is_dir()
        and len(registry) == 1
        and registry[0].name == "ccd"
    )

    policies: list[PolicyOutcome] = []
    for entry in registry:
        outcome = _process_policy(
            entry=entry,
            ccd_repo=repo,
            today=today_d,
            run=run,
            fallback_mode=fallback_mode,
            nightly_kwargs=nightly_kwargs,
        )
        policies.append(outcome)

    # Cross-policy index — even an empty sweep writes one so the
    # operator gets a "ran but had no policies" trail and not a
    # silently-missing file. In fallback mode the per-policy report is
    # already at the spec_020 flat path (``nightly/report_*.md``) so
    # the index sits alongside it under the same flat directory.
    index_dir = (repo / INDEX_DIR_REL).resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / f"index_{today_d.isoformat()}.md"
    index_path.write_text(
        render_index(
            today=today_d,
            policies=policies,
            fallback_mode=fallback_mode,
            ccd_repo=repo,
        ),
        encoding="utf-8",
    )

    return SweepResult(
        success=True,
        today=today_d,
        policies=policies,
        index_path=index_path,
    )


# --------------------------------------------------------------------------- #
# Per-policy execution
# --------------------------------------------------------------------------- #


def _process_policy(
    *,
    entry: PolicyEntry,
    ccd_repo: Path,
    today: date,
    run: NightlyRunner,
    fallback_mode: bool,
    nightly_kwargs: dict[str, Any],
) -> PolicyOutcome:
    """Run one policy and capture either its result or the exception.

    The "raised exception" path is where 论点4 (failure isolation) lives:
    no matter what blows up here, the sweep records it as a failed
    :class:`PolicyOutcome` and returns control to ``run_nightly_all``
    so the next policy runs.
    """

    target_repo = _resolve_target_repo(entry=entry, ccd_repo=ccd_repo)

    # Per-policy path overrides ── ALWAYS computed for sweep mode so
    # client repos (propose / off) never receive a write. In fallback
    # mode the overrides are intentionally None: the legacy
    # single-policy flat layout under ``<repo>/_ai_workspace/`` is
    # preserved so existing tests + existing operation work unchanged.
    if fallback_mode:
        discover_dir: Path | None = None
        brief_dir: Path | None = None
        proposal_dir: Path | None = None
        record_dir: Path | None = None
    else:
        discover_dir = (
            ccd_repo / "_ai_workspace" / "discover" / entry.name
        ).resolve()
        brief_dir = (
            ccd_repo / "_ai_workspace" / "nightly" / entry.name
        ).resolve()
        proposal_dir = (brief_dir / "proposals").resolve()
        record_dir = (brief_dir / "records").resolve()

    # spec_030 — profile-driven adversarial parser injection. In
    # genuine registry mode (NOT fallback) a施策 that lists
    # ``"adversarial"`` in ``discovery.channels`` MUST also configure
    # ``[discovery.adversarial.parsers]`` — otherwise the sweep skips
    # the channel rather than silently routing it to CCD's hard-coded
    # parsers (the Phase 2.5 misfire that motivated this spec).
    # Fallback mode preserves the spec_015 behavior bit-for-bit: no
    # parser injection, no skip — adversarial uses ``default_parsers``.
    adversarial_parsers: Any = None
    channel_skips: dict[str, str] = {}
    profile_disc = getattr(entry.profile, "discovery", None)
    profile_channels = list(getattr(profile_disc, "channels", ()) or ())
    if not fallback_mode and "adversarial" in profile_channels:
        adv_cfg = getattr(profile_disc, "adversarial", None)
        if adv_cfg is None:
            channel_skips["adversarial"] = (
                "adversarial channel skipped: profile に "
                "[discovery.adversarial.parsers] が未設定 "
                "(CCD のパーサは走らせない — spec_030 §2-3)"
            )
        else:
            from ccd.adversarial import resolve_parser_targets

            try:
                adversarial_parsers = resolve_parser_targets(adv_cfg.parsers)
            except ValueError as exc:
                # Bad import / not callable etc. ── do not silently
                # fall back to CCD parsers; record as a skip so the
                # operator sees the misconfiguration in §D.
                channel_skips["adversarial"] = (
                    "adversarial channel skipped: cannot resolve "
                    f"[discovery.adversarial.parsers] — {exc}"
                )
                adversarial_parsers = None

    # The caller's nightly_kwargs are layered first; the sweep's
    # per-policy overrides win (a test cannot accidentally pin every
    # policy's output to the same path via the kwargs bag).
    call_kwargs: dict[str, Any] = dict(nightly_kwargs)
    call_kwargs["repo"] = target_repo
    call_kwargs["profile"] = entry.profile
    call_kwargs["today"] = today
    call_kwargs["discover_dir"] = discover_dir
    call_kwargs["brief_dir"] = brief_dir
    call_kwargs["proposal_dir"] = proposal_dir
    call_kwargs["record_dir"] = record_dir
    if adversarial_parsers is not None:
        call_kwargs["adversarial_parsers"] = adversarial_parsers
    if channel_skips:
        call_kwargs["channel_skips"] = channel_skips

    try:
        result = run(**call_kwargs)
    except Exception as exc:
        return PolicyOutcome(
            name=entry.name,
            success=False,
            error=f"{type(exc).__name__}: {exc}".strip() or type(exc).__name__,
            result=None,
            report_path=None,
            source=entry.source,
        )

    # ``run_nightly`` may internally halt (channel canary, brief
    # rendering failure, etc.) — those are surfaced as
    # ``result.success=False`` but NOT a sweep-level failure: the
    # per-policy morning report (or its absence) carries the detail
    # and the next policy still gets to run. The sweep is "did we
    # finish trying every policy?", not "did every policy go green?".
    return PolicyOutcome(
        name=entry.name,
        success=bool(result.success),
        error=result.halt_reason if not result.success else "",
        result=result,
        report_path=result.brief_report_wsl,
        source=entry.source,
    )


def _resolve_target_repo(*, entry: PolicyEntry, ccd_repo: Path) -> Path:
    """Resolve the policy's ``profile.repo`` field to an absolute path.

    ``"."`` (the spec_018 default) → CCD's own repo. A relative path is
    resolved against the CCD repo (matches the spec_018 / spec_019
    convention). An absolute path is taken as-is so client policies
    can point CCD at any repo on disk.
    """

    raw = entry.profile.repo or "."
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return (ccd_repo / p).resolve()


# --------------------------------------------------------------------------- #
# Cross-policy index renderer (spec §2-3)
# --------------------------------------------------------------------------- #


def render_index(
    *,
    today: date,
    policies: list[PolicyOutcome],
    fallback_mode: bool = False,
    ccd_repo: Path | None = None,
) -> str:
    """Render the cross-policy morning index.

    Format follows spec §2-3 "施策ごと 1 行のサマリ＋各施策レポートへ
    のリンクだけ" — no re-rendering, no embedded findings, just a
    headline per policy and a relative link to the per-policy report.
    The operator reads this one file every morning and drills into a
    policy's full report only when its headline is interesting.

    ``fallback_mode`` adds a one-line note that single-policy operation
    is active (``profiles/`` directory missing) — when ON,
    ``policies`` contains exactly one entry named ``"ccd"`` and its
    report sits at the legacy flat path.

    ``ccd_repo`` is used to render report paths relative to the CCD
    workspace when known (otherwise the absolute path is printed).
    """

    lines: list[str] = [
        f"# 朝レポート横断インデックス {today.isoformat()} (ccd v2 Phase 3)",
        "",
    ]
    if fallback_mode:
        lines.append(
            "> **単一プロファイル運用** (`_ai_workspace/profiles/` 未配置) — "
            "従来どおり `_ai_workspace/ccd_profile.toml` を 1 施策として処理しました。"
            "複数施策に展開するなら `_ai_workspace/profiles/<施策名>.toml` を配置してください "
            "(spec_029 §2-1)。"
        )
    else:
        lines.append(
            "> 複数施策の巡回運用 ── `_ai_workspace/profiles/*.toml` を上から順に処理しました "
            "(spec_029)。施策ごと 1 行のサマリと詳細リンクのみ。"
            "気になる施策は詳細レポートを開いてください。"
        )
    lines.append("")
    lines.append(f"## 施策別サマリ ({len(policies)} 件)")
    lines.append("")

    if not policies:
        lines.append(
            "_(処理対象の施策がありません — `_ai_workspace/profiles/` ディレクトリは"
            "存在しますが TOML が 1 つも配置されていません。)_"
        )
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for outcome in policies:
        lines.append(_render_policy_line(outcome=outcome, ccd_repo=ccd_repo))

    lines.append("")
    lines.append("## メモ")
    lines.append("")
    lines.append(
        "- 各施策の詳細レポートには発見・修正案・HALT 理由などが入っています。"
        "**`fix_mode=\"propose\"`** の施策は対象 repo に書き込みません "
        "(隔離クローン内で生成、パッチは CCD 側に保存)。"
        "**`fix_mode=\"auto\"`** の施策のみ対象 repo の **ローカル `main`** に merge します "
        "(spec_028 §2-3、安全境界レベル 2)。"
    )
    lines.append(
        "- 失敗した施策があっても他施策の処理は続行しています "
        "(spec_029 §2-2 论点4 — 1 施策の事故が他施策を止めない)。"
    )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_policy_line(
    *,
    outcome: PolicyOutcome,
    ccd_repo: Path | None,
) -> str:
    """One Markdown bullet per policy — the entire "横断インデックス" body."""

    name_md = f"`{outcome.name}`"
    if not outcome.success:
        # Sweep-level failure path. ``error`` carries the exception text
        # (or, when ``run_nightly`` halted internally without raising,
        # the halt reason from :class:`NightlyResult.halt_reason`).
        reason = outcome.error or "理由不明"
        return f"- {name_md}: **失敗** — {reason}"

    summary = _summarize_nightly(outcome.result)
    link = _format_report_link(
        report_path=outcome.report_path, ccd_repo=ccd_repo
    )
    if link:
        return f"- {name_md}: {summary} → [詳細]({link})"
    return f"- {name_md}: {summary}"


def _summarize_nightly(result: NightlyResult | None) -> str:
    """Compose the 1-line summary from a per-policy ``NightlyResult``.

    Picks the most operator-relevant fact in this order:

    1. PAUSE — operator-set kill switch, surface verbatim.
    2. auto-fix merged (auto mode) — a fix landed locally.
    3. propose proposed (propose mode) — a patch was saved.
    4. auto-fix / propose HALT — verification rejected.
    5. auto-fix / propose skipped — no candidate / un-pushed backlog.
    6. discover-only summary — mechanical findings count.

    The full per-policy report carries detail; this string is what the
    operator scans first.
    """

    if result is None:
        return "結果なし (run_nightly returned None)"
    if result.paused:
        return "PAUSE 中 — `_ai_workspace/PAUSE` が在ったので何もしませんでした"

    # spec_030 — count channel-level halts / skips so the index makes
    # silent failures visible at a glance (the Phase 2.5 misfire's root
    # cause was that 0-mutants / wrong-parser silently looked like a
    # successful run in the index). The summary still leads with the
    # most operator-relevant fact below; the HALT count is appended.
    halt_count = sum(
        1
        for co in (result.channels_run or ())
        if not bool(co.success) and (co.halt_reason or "")
    )
    halt_suffix = f" — HALT {halt_count} 件 (§D 参照)" if halt_count else ""

    af = result.auto_fix
    if af is not None:
        mode = getattr(af, "mode", "auto")
        if not af.skipped and af.merged:
            return (
                f"自律修正 1 件を merge "
                f"(template {af.template}, `{af.spec_auto_id}`)"
            )
        if not af.skipped and getattr(af, "proposed", False):
            return (
                f"修正案 1 件を生成 (提案モード, template {af.template}, "
                f"`{af.spec_auto_id}`)"
            )
        if not af.skipped:
            label = "提案モード HALT" if mode == "propose" else "自律修正 HALT"
            return f"{label} — {af.halt_reason or '理由不明'}"
        if af.skipped and af.skip_reason:
            label = "提案モード skip" if mode == "propose" else "自律修正 skip"
            return f"{label} ({af.skip_reason})"

    # No auto-fix (fix_mode="off") or auto_fix is None. Surface the
    # mechanical-channel finding count if we have any channels.
    if not result.channels_run:
        return "発見なし (チャンネル未実行)" + halt_suffix
    channel_names = ", ".join(co.channel for co in result.channels_run)
    return f"発見のみ ({channel_names}){halt_suffix}"


def _format_report_link(
    *,
    report_path: Path | None,
    ccd_repo: Path | None,
) -> str:
    """Render the per-policy report path relative to the index location.

    The index sits at ``<ccd_repo>/_ai_workspace/nightly/index_*.md``;
    per-policy reports sit at
    ``<ccd_repo>/_ai_workspace/nightly/<name>/report_*.md`` (sweep
    mode) or ``<ccd_repo>/_ai_workspace/nightly/report_*.md`` (fallback
    mode). A relative link is cleaner in the rendered Markdown — the
    operator can click straight from the index in a viewer.
    """

    if report_path is None:
        return ""
    if ccd_repo is None:
        return str(report_path)
    try:
        nightly_root = (ccd_repo / INDEX_DIR_REL).resolve()
        rel = Path(report_path).resolve().relative_to(nightly_root)
        return str(rel)
    except ValueError:
        return str(report_path)


def _utc_today() -> date:
    """Today's date in UTC. Matches the convention in nightly / brief."""
    return datetime.now(UTC).date()


__all__ = [
    "INDEX_DIR_REL",
    "NightlyRunner",
    "PolicyOutcome",
    "SweepResult",
    "render_index",
    "run_nightly_all",
]
