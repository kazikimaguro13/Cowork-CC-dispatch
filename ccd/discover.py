"""ccd discover — mutation-testing discovery channel (spec_013, v2 Phase 1).

`run_discovery` invokes a `MutationRunner` (mutmut in production, a fake in
tests), normalizes its raw output into a stable list of `Mutant` records,
computes a deterministic factual summary in Python, splits survivors against
`_ai_workspace/discover/blocklist.txt`, and emits a discovery report —
`discover_NNN.md` (human-readable) plus `discover_NNN.json` (structured,
consumed by the future morning-report / Phase 2 auto-fix loop).

The split between `discover.py` (tool-agnostic) and `MutmutRunner` (the only
piece that depends on mutmut's CLI shape) mirrors the
`dispatch.py` ↔ `ClaudeCodeRunner` split: tests stay fast and offline by
swapping in `FakeMutationRunner`; production wires in the real mutmut.

Phase 1 limits — by spec_013 design — are tight: no scheduler, no other
discovery channels (adversarial-input / AI-inference), no auto-fix. The whole
point is to verify that mutation discovery produces useful gaps for CCD's own
code, surfaced manually via `ccd discover`. Triage / auto-blocklist
maintenance are Phase 2.

spec_014: `MutmutRunner` runs mutmut inside a disposable isolated copy of
the live repo (`_isolated_clone`), not against the live working tree. The
live `ccd/` is therefore never in-place-mutated, and any runaway git write
that a mutation might trigger inside CCD's own test suite hits the isolated
copy's `.git` (with all remotes stripped) instead of the real repo. The
discovery report itself is still written to the live repo's
`_ai_workspace/discover/` — only mutation *execution* is isolated.

spec_019: the parent venv carries a PEP 660 editable-install MetaPathFinder
(``__editable___cowork_cc_dispatch_*_finder.py``) hard-coded to the LIVE
repo's ``ccd/``. Under that finder, even with ``cwd=<clone>`` /
``PYTHONPATH=<clone>``, the mutmut-spawned pytest subprocess silently
resolves ``import ccd`` to the live (un-mutated) source — so every mutmut
mutation lands in the clone but the tests never see it, and 100% of
mutants get reported as ``survived``. The fix: provision a dedicated venv
*inside the clone* and ``pip install -e <clone>`` it. mutmut then runs
against that venv's binaries, whose import machinery points at the clone
(not the live repo). A "canary" guard (``_detect_broken_mutation_setup``)
also halts the run if the mutation tool reports a structurally impossible
result (mutants > 0 but killed == 0) — so a setup regression like the
spec_014 one can never again ship a misleading 1273-survivor report.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_DISCOVER_DIR_REL = Path("_ai_workspace") / "discover"
DEFAULT_BLOCKLIST_FILENAME = "blocklist.txt"
DEFAULT_MUTATION_TARGETS: tuple[str, ...] = ("ccd",)

# spec_019 — canary threshold. If mutmut reports at least this many mutants
# but kills literally zero, treat the setup as broken (tests aren't seeing
# the mutations) and refuse to emit a misleading report. Tuned conservatively:
# a real test suite finding 0/N>=5 killed would be near-impossible.
CANARY_MIN_MUTANTS_FOR_HALT = 5

CHANNEL_MUTATION = "mutation"
CHANNEL_ADVERSARIAL = "adversarial"
CHANNEL_AI = "ai"
DEFAULT_CHANNEL = CHANNEL_MUTATION
SUPPORTED_CHANNELS: tuple[str, ...] = (
    CHANNEL_MUTATION,
    CHANNEL_ADVERSARIAL,
    CHANNEL_AI,
)

STATUS_SURVIVED = "survived"
STATUS_KILLED = "killed"
STATUS_TIMEOUT = "timeout"
STATUS_INCOMPETENT = "incompetent"
STATUS_SUSPICIOUS = "suspicious"
STATUS_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Mutant:
    """One mutation produced by the mutation tool.

    The triple ``(file, line, mutation)`` is the "stable signature" the
    blocklist matches on. mutmut's internal numeric IDs are explicitly *not*
    used — they shift across runs as the codebase changes.
    """

    file: str
    line: int
    mutation: str
    status: str

    @property
    def signature(self) -> str:
        return f"{self.file}:{self.line}:{self.mutation}"


@dataclass
class MutationRunOutcome:
    """What the `MutationRunner` observed.

    ``error`` non-empty means the tool itself failed (not "no survivors" —
    that's a successful run with an empty list). ``raw_output`` is kept for
    debugging / appending to the discovery report so a human can see what the
    tool printed.
    """

    mutants: list[Mutant]
    tool: str = ""
    raw_output: str = ""
    error: str = ""


class MutationRunner(Protocol):
    def run(
        self,
        *,
        repo: Path,
        paths: list[str] | None = None,
    ) -> MutationRunOutcome: ...


@dataclass(frozen=True)
class DiscoverySummary:
    """Deterministic facts about a mutation run.

    Same input → same numbers. This is the honesty anchor for the discovery
    report: the markdown body quotes these counts directly rather than
    re-estimating from the mutant list.
    """

    tool: str
    target_paths: tuple[str, ...]
    mutants_total: int
    status_breakdown: dict[str, int]
    survived_total: int
    survived_by_file: dict[str, int]
    blocklisted_total: int
    actionable_total: int


@dataclass
class DiscoveryResult:
    success: bool
    report_md_path: Path | None
    report_json_path: Path | None
    summary: DiscoverySummary
    actionable_mutants: list[Mutant]
    blocklisted_mutants: list[Mutant]
    halt_reason: str = ""
    raw_output: str = ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_discovery(
    runner: MutationRunner,
    *,
    repo: Path,
    paths: list[str] | None = None,
    discover_dir: Path | None = None,
) -> DiscoveryResult:
    """Drive one mutation discovery batch end-to-end.

    The discovery halts gracefully (``success=False``, ``halt_reason`` set,
    no report files) when the mutation tool itself fails to run. A
    successful run that finds zero mutants — or zero survivors — still emits
    a report; "nothing survived" is information too.
    """

    repo = Path(repo).resolve()
    discover_root = (
        Path(discover_dir).resolve()
        if discover_dir is not None
        else repo / DEFAULT_DISCOVER_DIR_REL
    )
    discover_root.mkdir(parents=True, exist_ok=True)

    target_paths: tuple[str, ...] = (
        tuple(paths) if paths else DEFAULT_MUTATION_TARGETS
    )

    outcome = runner.run(repo=repo, paths=list(target_paths))

    if outcome.error:
        return DiscoveryResult(
            success=False,
            report_md_path=None,
            report_json_path=None,
            summary=_empty_summary(outcome.tool, target_paths),
            actionable_mutants=[],
            blocklisted_mutants=[],
            halt_reason=f"mutation tool failed: {outcome.error}",
            raw_output=outcome.raw_output,
        )

    # spec_030 §2-2 — 0-mutants silent-failure HALT. mutmut returning zero
    # mutants for a non-empty target list is structurally implausible (any
    # real Python file contains arithmetic / comparisons / defaults mutmut
    # can mutate). The Phase 2.5 incident: an iso-venv whose pip install
    # silently failed left mutmut unable to instrument the target, but
    # ``outcome.error`` stayed empty and the existing canary fires only on
    # ``mutants > threshold && killed == 0`` — leaving a "0 mutants"
    # ``success=True`` report indistinguishable from "no findings". The
    # honest read is "we don't know whether there were findings": halt
    # without writing a misleading report. False positives (genuinely
    # trivial files like ``__init__.py``) are accepted — operators
    # suppress them by removing the file from ``mutation_paths`` (YAGNI:
    # no opt-out knob until a real false positive shows up).
    if not outcome.mutants and target_paths:
        return DiscoveryResult(
            success=False,
            report_md_path=None,
            report_json_path=None,
            summary=_empty_summary(outcome.tool, target_paths),
            actionable_mutants=[],
            blocklisted_mutants=[],
            halt_reason=(
                f"mutation setup likely failed: 0 mutants generated for "
                f"non-empty targets {list(target_paths)}. "
                "Possible causes: iso-venv dependency install error, "
                "mutmut path mismatch, test discovery failure, "
                "or genuinely trivial Python file "
                "(suppress via profile.mutation_paths if intended)."
            ),
            raw_output=outcome.raw_output,
        )

    blocklist = _load_blocklist(discover_root / DEFAULT_BLOCKLIST_FILENAME)
    survived = [m for m in outcome.mutants if m.status == STATUS_SURVIVED]
    actionable: list[Mutant] = []
    blocklisted: list[Mutant] = []
    for m in survived:
        (blocklisted if m.signature in blocklist else actionable).append(m)

    summary = _build_summary(
        tool=outcome.tool,
        target_paths=target_paths,
        mutants=outcome.mutants,
        survived=survived,
        actionable=actionable,
        blocklisted=blocklisted,
    )

    # spec_019 canary — refuse to ship a report when the run looks
    # structurally broken (mutmut is not exercising the test suite). The
    # incident this guards against: a 1273-mutant run reporting 0 killed
    # because PEP 660 import hooks redirected `import ccd` away from the
    # mutated clone. We bail BEFORE writing any report files so no
    # downstream consumer (brief / dashboard / future auto-fix) can mistake
    # a setup failure for "1273 actionable test gaps".
    canary_reason = _detect_broken_mutation_setup(summary)
    if canary_reason:
        return DiscoveryResult(
            success=False,
            report_md_path=None,
            report_json_path=None,
            summary=summary,
            actionable_mutants=[],
            blocklisted_mutants=[],
            halt_reason=canary_reason,
            raw_output=outcome.raw_output,
        )

    seq = _next_discover_seq(discover_root)
    md_path = discover_root / f"discover_{seq:03d}.md"
    json_path = discover_root / f"discover_{seq:03d}.json"

    md_path.write_text(
        _render_md(
            seq=seq,
            summary=summary,
            actionable=actionable,
            blocklisted=blocklisted,
            other_mutants=outcome.mutants,
            blocklist_path=discover_root / DEFAULT_BLOCKLIST_FILENAME,
            target_paths=target_paths,
        ),
        encoding="utf-8",
    )
    json_path.write_text(
        _render_json(summary, actionable, blocklisted),
        encoding="utf-8",
    )

    return DiscoveryResult(
        success=True,
        report_md_path=md_path,
        report_json_path=json_path,
        summary=summary,
        actionable_mutants=actionable,
        blocklisted_mutants=blocklisted,
        raw_output=outcome.raw_output,
    )


# --------------------------------------------------------------------------- #
# Channel dispatch (spec_015)
# --------------------------------------------------------------------------- #


def run_channel(
    channel: str,
    *,
    repo: Path,
    paths: list[str] | None = None,
    mutation_runner: MutationRunner | None = None,
    agent_runner=None,
    discover_dir: Path | None = None,
    adversarial_parsers: Any = None,
    mutation_config: Any = None,
):
    """Dispatch one ``ccd discover --channel <channel>`` invocation.

    The three channels surface *different classes of bug*:

    - ``mutation`` (spec_013, default) — gaps in CCD's own tests, via
      mutmut. Returns a :class:`DiscoveryResult`.
    - ``adversarial`` (spec_015) — places where CCD's parsers crash on
      realistic broken inputs. Returns an :class:`AdversarialResult`.
    - ``ai`` (spec_016) — semantic / intent concerns surfaced by an
      agent reading the code. **Report-only** — does not feed an
      autonomous fix loop. Returns an :class:`AIReviewResult`.

    Each result type is shaped to its own channel. cli.py picks the
    display path from the channel name. ``paths`` / ``mutation_runner``
    are mutation-specific tuning knobs and silently ignored for the
    other channels; ``agent_runner`` is the AI channel's seam (the same
    ``AgentRunner`` abstraction ``dispatch`` / ``retrospect`` use) and
    is ignored for the mutation / adversarial channels.

    spec_030: ``adversarial_parsers`` is the profile-driven parser
    injection — a tuple of resolved ``_Parser`` objects from
    :func:`ccd.adversarial.resolve_parser_targets`. ``None`` means the
    caller did not supply parsers; the adversarial channel then falls
    back to :func:`ccd.adversarial.default_parsers` (the CCD-only
    hard-coded set) so single-invocation ``ccd discover --channel
    adversarial`` keeps its spec_015 behavior. The sweep entry point
    deliberately decides BEFORE calling ``run_channel`` whether to pass
    a profile-driven list or skip the channel — it never falls back
    here.
    """

    if channel == CHANNEL_MUTATION:
        if mutation_runner is not None:
            runner = mutation_runner
        else:
            # spec_032 — forward profile-driven mutmut parameters
            # (cwd / tests_dir / extra_args) when supplied. The test
            # double `FakeMutationRunner` and existing single-CLI
            # callers continue to work because ``mutation_config`` is
            # optional and the runner constructor's new kwargs all
            # default to None/empty.
            runner_kwargs: dict[str, Any] = {}
            if mutation_config is not None:
                runner_kwargs["cwd"] = getattr(mutation_config, "cwd", None)
                runner_kwargs["tests_dir"] = getattr(
                    mutation_config, "tests_dir", None
                )
                runner_kwargs["extra_args"] = list(
                    getattr(mutation_config, "extra_args", []) or []
                )
            runner = MutmutRunner(**runner_kwargs)
        return run_discovery(
            runner,
            repo=repo,
            paths=paths,
            discover_dir=discover_dir,
        )
    if channel == CHANNEL_ADVERSARIAL:
        # Lazy import: adversarial.py imports ``DEFAULT_DISCOVER_DIR_REL``
        # from this module, so a top-level import here would create a
        # circular load order on first use.
        from ccd.adversarial import run_adversarial

        return run_adversarial(
            repo=repo,
            discover_dir=discover_dir,
            parsers=adversarial_parsers,
        )
    if channel == CHANNEL_AI:
        # Same lazy-import rationale as the adversarial channel.
        from ccd.agent import ClaudeCodeRunner
        from ccd.ai_review import run_ai_review

        runner = agent_runner if agent_runner is not None else ClaudeCodeRunner()
        return run_ai_review(runner, repo=repo, discover_dir=discover_dir)
    raise ValueError(
        f"unknown discover channel: {channel!r} "
        f"(supported: {', '.join(SUPPORTED_CHANNELS)})"
    )


# --------------------------------------------------------------------------- #
# Summary + blocklist
# --------------------------------------------------------------------------- #


def _empty_summary(tool: str, target_paths: tuple[str, ...]) -> DiscoverySummary:
    return DiscoverySummary(
        tool=tool or "(unknown)",
        target_paths=target_paths,
        mutants_total=0,
        status_breakdown={},
        survived_total=0,
        survived_by_file={},
        blocklisted_total=0,
        actionable_total=0,
    )


def _build_summary(
    *,
    tool: str,
    target_paths: tuple[str, ...],
    mutants: Iterable[Mutant],
    survived: list[Mutant],
    actionable: list[Mutant],
    blocklisted: list[Mutant],
) -> DiscoverySummary:
    status_breakdown: dict[str, int] = {}
    for m in mutants:
        status_breakdown[m.status] = status_breakdown.get(m.status, 0) + 1

    survived_by_file: dict[str, int] = {}
    for m in survived:
        survived_by_file[m.file] = survived_by_file.get(m.file, 0) + 1

    return DiscoverySummary(
        tool=tool or "(unknown)",
        target_paths=target_paths,
        mutants_total=sum(status_breakdown.values()),
        status_breakdown=dict(sorted(status_breakdown.items())),
        survived_total=len(survived),
        survived_by_file=dict(sorted(survived_by_file.items())),
        blocklisted_total=len(blocklisted),
        actionable_total=len(actionable),
    )


def _detect_broken_mutation_setup(summary: DiscoverySummary) -> str:
    """Spec_019 canary. Return a non-empty halt reason iff the mutation run
    looks structurally broken — i.e. there are enough mutants to draw a
    conclusion but literally zero were killed.

    The 1273/0/1273 incident: the editable-install MetaPathFinder routed
    ``import ccd`` to the live (un-mutated) source, so every mutmut mutation
    was invisible to the test suite. Empty `status_breakdown` (no run at all)
    is handled separately by the ``outcome.error`` branch above; this helper
    only fires on a "ran, but the tests couldn't see any mutation" pattern.
    """

    killed = summary.status_breakdown.get(STATUS_KILLED, 0)
    if summary.mutants_total < CANARY_MIN_MUTANTS_FOR_HALT:
        return ""
    if killed > 0:
        return ""
    return (
        "mutation setup is broken: canary mutant survived — "
        f"0 killed out of {summary.mutants_total} mutants "
        f"(threshold ≥ {CANARY_MIN_MUTANTS_FOR_HALT}). "
        "mutmut likely is not exercising the test suite "
        "(e.g. tests are importing an editable-installed copy of the "
        "source rather than the mutated clone)."
    )


def _load_blocklist(path: Path) -> set[str]:
    """Read blocklist signatures. Missing file → empty set (graceful)."""

    if not path.exists():
        return set()
    sigs: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sigs.add(line)
    return sigs


def _next_discover_seq(discover_dir: Path) -> int:
    nums: list[int] = []
    for p in discover_dir.glob("discover_*.md"):
        m = re.match(r"discover_(\d+)\.md$", p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #


def _render_md(
    *,
    seq: int,
    summary: DiscoverySummary,
    actionable: list[Mutant],
    blocklisted: list[Mutant],
    other_mutants: Iterable[Mutant],
    blocklist_path: Path,
    target_paths: tuple[str, ...],
) -> str:
    timeouts = [m for m in other_mutants if m.status == STATUS_TIMEOUT]
    incompetent = [m for m in other_mutants if m.status == STATUS_INCOMPETENT]
    suspicious = [m for m in other_mutants if m.status == STATUS_SUSPICIOUS]
    unknown = [m for m in other_mutants if m.status == STATUS_UNKNOWN]

    parts: list[str] = [
        f"# discover_{seq:03d} — ccd mutation discovery",
        "",
        "## 1. 評価母数 (決定的に算出した事実)",
        "",
        f"- ツール: `{summary.tool}`",
        f"- 対象パス: {_render_paths(target_paths)}",
        f"- mutant 総数: **{summary.mutants_total}** 件",
        f"- status 内訳: {_render_breakdown(summary.status_breakdown)}",
        f"- 生存 mutant: **{summary.survived_total}** 件 "
        f"(blocklist 除外 **{summary.blocklisted_total}** / "
        f"actionable **{summary.actionable_total}**)",
        f"- ファイル別生存数: {_render_breakdown(summary.survived_by_file)}",
        "",
        "**この数値は決定的に Python で算出済み。** "
        "再集計で別の数値が出たら本節を疑うのではなく、"
        "「判断できなかった」と書く（捏造しない）。",
        "",
        "## 2. 生き残った改変 (actionable) — テストの隙間",
        "",
        _render_actionable(actionable),
        "",
        "## 3. blocklist で除外した件数",
        "",
        _render_blocklisted(blocklisted, blocklist_path),
        "",
        "## 4. データから判断できなかったこと",
        "",
        _render_uncertain(
            timeouts=timeouts,
            incompetent=incompetent,
            suspicious=suspicious,
            unknown=unknown,
        ),
        "",
    ]
    return "\n".join(parts)


def _render_paths(paths: tuple[str, ...]) -> str:
    if not paths:
        return "(none)"
    return ", ".join(f"`{p}`" for p in paths)


def _render_breakdown(d: dict[str, int]) -> str:
    if not d:
        return "(none)"
    return ", ".join(f"`{k}`={v}" for k, v in d.items())


def _render_actionable(actionable: list[Mutant]) -> str:
    if not actionable:
        return "_(該当なし — 生存した actionable mutant はゼロ件。)_"
    by_file: dict[str, list[Mutant]] = {}
    for m in actionable:
        by_file.setdefault(m.file, []).append(m)
    lines: list[str] = []
    for fname in sorted(by_file.keys()):
        items = sorted(by_file[fname], key=lambda x: (x.line, x.mutation))
        lines.append(f"### `{fname}` ({len(items)})")
        lines.append("")
        for m in items:
            lines.append(f"- `{m.file}:{m.line}` — {m.mutation}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_blocklisted(blocklisted: list[Mutant], blocklist_path: Path) -> str:
    if not blocklisted:
        return (
            f"_(0 件 — blocklist (`{blocklist_path.name}`) による除外はゼロ。)_"
        )
    lines = [
        f"**{len(blocklisted)}** 件を blocklist (`{blocklist_path.name}`) "
        "により非表示。",
        "",
    ]
    for m in sorted(blocklisted, key=lambda x: (x.file, x.line, x.mutation)):
        lines.append(f"- `{m.signature}`")
    return "\n".join(lines)


def _render_uncertain(
    *,
    timeouts: list[Mutant],
    incompetent: list[Mutant],
    suspicious: list[Mutant],
    unknown: list[Mutant],
) -> str:
    bullets: list[str] = []
    if timeouts:
        bullets.append(
            f"- **timeout**: {len(timeouts)} 件 — mutmut のタイムアウトに当たった。"
            "テストの隙間ではなく実行時間の問題かもしれない（判定保留）。"
        )
    if incompetent:
        bullets.append(
            f"- **incompetent**: {len(incompetent)} 件 — 改変がコンパイル/import"
            "に失敗した。「テストが捕まえたか」とは別軸の事象（判定保留）。"
        )
    if suspicious:
        bullets.append(
            f"- **suspicious**: {len(suspicious)} 件 — mutmut が判定を保留した。"
        )
    if unknown:
        bullets.append(
            f"- **unknown**: {len(unknown)} 件 — ステータス文字列が認識外。"
        )
    if not bullets:
        return "_(該当なし — すべての mutant が決定的に分類できた。)_"
    return "\n".join(bullets)


def _render_json(
    summary: DiscoverySummary,
    actionable: list[Mutant],
    blocklisted: list[Mutant],
) -> str:
    payload = {
        "summary": {
            "tool": summary.tool,
            "target_paths": list(summary.target_paths),
            "mutants_total": summary.mutants_total,
            "status_breakdown": summary.status_breakdown,
            "survived_total": summary.survived_total,
            "survived_by_file": summary.survived_by_file,
            "blocklisted_total": summary.blocklisted_total,
            "actionable_total": summary.actionable_total,
        },
        "actionable": [_mutant_to_dict(m) for m in actionable],
        "blocklisted": [_mutant_to_dict(m) for m in blocklisted],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _mutant_to_dict(m: Mutant) -> dict:
    return {
        "file": m.file,
        "line": m.line,
        "mutation": m.mutation,
        "status": m.status,
        "signature": m.signature,
    }


# --------------------------------------------------------------------------- #
# Isolation (spec_014) — mutmut runs in a disposable copy of the live repo
# --------------------------------------------------------------------------- #

# spec_019: name of the dedicated venv directory provisioned inside each
# clone. Declared up here so `_ISOLATION_IGNORE` can reference it.
_ISO_VENV_DIR_NAME = ".ccd-iso-venv"

# Directories/files excluded from the isolated copy. Two reasons:
# (1) safety — `_ai_workspace/` belongs to the live repo (discovery reports go
#     there, not the clone), and copying it back would create a hall of mirrors
#     plus risk overwriting accumulated logs;
# (2) speed — caches, build artifacts, vendored deps are huge and irrelevant
#     to mutation testing of `ccd/`.
_ISOLATION_IGNORE: tuple[str, ...] = (
    "_ai_workspace",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mutmut-cache",
    ".venv",
    "venv",
    _ISO_VENV_DIR_NAME,  # spec_019 — defense in depth; clones live in their
    # own tmp roots so this would not normally clash, but excluding the
    # iso-venv name from copytree means a nested re-clone (if ever wired)
    # never picks up a stale iso-venv from a prior run.
    "build",
    "dist",
    "*.egg-info",
    "node_modules",
)


@contextmanager
def _isolated_clone(src: Path) -> Iterator[Path]:
    """Yield a disposable, fully isolated copy of ``src``.

    Guarantees (spec_014 §2-1):

    (a) The live source tree is never touched — the clone is a separate
        directory under a fresh ``tempfile.mkdtemp`` root, so mutmut's
        in-place rewrites land in the copy.
    (b) Any git write inside the clone (commit / branch / checkout / push)
        cannot reach the real repo — the clone has its own ``.git`` (copied,
        not hardlinked) and *every* git remote is stripped so push has no
        target.
    (c) The temporary tree is removed on success, failure, *and* exception
        via try/finally.

    We use ``shutil.copytree`` (not ``git clone --local``) deliberately:
    mutation testing should reflect what's actually on disk now, including
    uncommitted edits — ``git clone --local`` would silently skip them.
    """

    src = Path(src).resolve()
    tmp_root = Path(tempfile.mkdtemp(prefix="ccd_discover_iso_"))
    clone = tmp_root / src.name
    try:
        shutil.copytree(
            src,
            clone,
            ignore=shutil.ignore_patterns(*_ISOLATION_IGNORE),
            symlinks=False,
            ignore_dangling_symlinks=True,
        )
        _strip_git_remotes(clone)
        yield clone
    finally:
        # Use the temp ROOT (not `clone`) so we wipe partial copies too.
        shutil.rmtree(tmp_root, ignore_errors=True)


def _strip_git_remotes(clone: Path) -> None:
    """Remove every git remote from the clone so push has no target.

    No-ops if the clone has no ``.git`` directory or if ``git`` isn't on
    PATH. The clone's ``.git`` is already a copied, independent directory
    (no hardlinks), so commits/branches/refs are isolated by construction;
    stripping remotes closes the one remaining escape hatch (a stray
    ``git push origin``).
    """

    if not (clone / ".git").is_dir():
        return
    try:
        listed = subprocess.run(
            ["git", "-C", str(clone), "remote"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return
    for name in (listed.stdout or "").split():
        name = name.strip()
        if not name:
            continue
        try:
            subprocess.run(
                ["git", "-C", str(clone), "remote", "remove", name],
                capture_output=True,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return


def _workspace_env(
    workspace: Path,
    *,
    iso_venv_bin: Path | None = None,
) -> dict[str, str]:
    """Build the env mutmut (and its pytest subprocess) run with.

    Prepends the isolated workspace to ``PYTHONPATH`` so that when mutmut
    invokes pytest, ``import ccd`` resolves to the *isolated copy's* ``ccd/``
    — not whatever an editable install in the parent venv points at. Without
    this, mutmut would mutate the copy but tests would import the live
    source, making mutation testing silently no-op (spec_014 §2-1 (d)).

    spec_019: ``PYTHONPATH`` alone is not enough — a PEP 660 editable-install
    MetaPathFinder in the parent venv beats ``PYTHONPATH``-based resolution.
    The real fix lives in :func:`_provision_iso_venv`, which gives mutmut /
    pytest a Python whose own import machinery points at the clone. This
    helper still prepends the workspace to ``PYTHONPATH`` as belt-and-braces,
    and (when given) puts the iso-venv's ``bin/`` first on ``PATH`` so
    mutmut's default ``python -m pytest`` runner resolves to the iso-venv's
    Python rather than the parent venv's.
    """

    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(workspace) + (os.pathsep + existing if existing else "")
    )
    if iso_venv_bin is not None:
        existing_path = env.get("PATH", "")
        env["PATH"] = (
            str(iso_venv_bin)
            + (os.pathsep + existing_path if existing_path else "")
        )
        # Pin which Python mutmut's default ``python -m pytest`` invokes.
        # mutmut spawns its runner via ``shlex.split`` of the test_command
        # string and relies on ``$PATH`` for the binary; making this explicit
        # via the iso-venv's bin/python avoids "wrong python" surprises.
        env["VIRTUAL_ENV"] = str(iso_venv_bin.parent)
        # ``PYTHONHOME`` from a possibly-active parent venv would confuse the
        # iso-venv's Python startup; clear it so site.py picks up the
        # iso-venv's pyvenv.cfg cleanly.
        env.pop("PYTHONHOME", None)
    return env


# --------------------------------------------------------------------------- #
# spec_019: provision an iso-venv inside the clone so mutmut/pytest run
# against a Python whose import machinery points at the CLONE, not the live
# repo. See module docstring for the root-cause story.
# --------------------------------------------------------------------------- #


class IsoVenvProvisioningError(RuntimeError):
    """Raised when the per-clone isolated venv cannot be built or populated.

    The MutmutRunner converts this into a graceful
    :class:`MutationRunOutcome.error` so `run_discovery` halts cleanly
    instead of crashing — same contract as a missing ``mutmut`` binary.
    """


def _provision_iso_venv(
    workspace: Path,
    *,
    parent_python: str | None = None,
    timeout: float | None = None,
) -> Path:
    """Create a fresh venv inside ``workspace`` and install the clone editable.

    Returns the path to the iso-venv's ``bin/`` directory.

    spec_019: ``--system-site-packages`` is NOT used. That flag inherits the
    *system* Python's packages — not the parent venv's — so mutmut / pytest
    installed in the parent venv would be absent from the iso-venv. We
    build a clean venv and explicitly install (a) the clone editable,
    (b) mutmut, (c) pytest. The editable install of the clone registers
    a fresh PEP 660 finder whose MAPPING points at the clone's ``ccd/``;
    since the iso-venv has no competing editable finders for ``ccd``,
    ``import ccd`` under iso-Python resolves to the clone unambiguously.

    Cost: roughly 2-3s for venv creation + 5-10s for the install (pip's
    wheel cache absorbs the deps after the first run). For a full
    ``ccd discover`` batch that already takes hours, this overhead is
    negligible compared to the value of a trustworthy kill rate.
    """

    parent_python = parent_python or sys.executable
    venv_dir = workspace / _ISO_VENV_DIR_NAME
    try:
        subprocess.run(
            [parent_python, "-m", "venv", str(venv_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise IsoVenvProvisioningError(
            f"python venv module unavailable ({exc})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise IsoVenvProvisioningError("venv creation timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise IsoVenvProvisioningError(
            f"venv creation failed (rc={exc.returncode}): "
            f"{(exc.stderr or exc.stdout or '').strip()}"
        ) from exc

    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        raise IsoVenvProvisioningError(
            f"iso-venv python not found at {venv_python}"
        )

    # Editable install of the CLONE into the iso-venv, plus mutmut and
    # pytest (mutmut's default runner is ``python -m pytest``). The clone
    # is installed via PEP 660 editable so its finder MAPPING points at
    # the clone's ``ccd/`` — that's the whole reason this venv exists.
    # The iso-venv has no parent editable finder, so this clone-pointing
    # finder is the only candidate ``import ccd`` resolves to.
    install_args = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--quiet",
        "--no-input",
        "--disable-pip-version-check",
        "-e",
        str(workspace),
        "mutmut>=2.4,<3",
        "pytest>=8",
    ]
    try:
        subprocess.run(
            install_args,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise IsoVenvProvisioningError(
            f"pip unavailable in iso-venv ({exc})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise IsoVenvProvisioningError(
            "iso-venv pip install timed out"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise IsoVenvProvisioningError(
            f"iso-venv pip install failed (rc={exc.returncode}): "
            f"{(exc.stderr or exc.stdout or '').strip()}"
        ) from exc

    # spec_031 §2-1 — post-install validation. ``pip install`` can exit 0 yet
    # leave the iso-venv missing the binaries / package the mutation channel
    # depends on (sweep #3 / #4 caught this with axis-knowledge-rag: mutmut
    # ran "successfully" against a venv where the target package wasn't
    # actually importable, producing ``mutants_total=0`` — the silent failure
    # spec_030 surfaces as a HALT, but only after the misleading exit-0
    # install. spec_031 makes the install itself the failure boundary.
    iso_venv_bin = venv_dir / "bin"
    _validate_iso_venv_post_install(
        iso_venv_bin=iso_venv_bin,
        workspace=workspace,
        timeout=timeout,
    )
    return iso_venv_bin


# Max stderr bytes appended to a single post-install validation error line.
# Real pip / importlib stderr can be many KB; halt_reason eventually lands in
# the morning brief §D where multi-KB blobs hurt readability. 2 KB keeps the
# diagnostic informative (full ImportError chain, missing-module name) without
# overwhelming the report.
_POST_INSTALL_STDERR_MAX = 2048


def _extract_pyproject_project_name(workspace: Path) -> str | None:
    """Return the dist name from ``<workspace>/pyproject.toml``'s
    ``[project] name``, or None if pyproject.toml is missing, malformed, or
    lacks ``[project] name``.

    Used by spec_031 post-install validation to verify the target package is
    importable in the iso-venv; non-fatal if absent (古典 setuptools 等の
    repo を排除しない、過剰実装を避ける)。
    """

    pyproject_path = workspace / "pyproject.toml"
    if not pyproject_path.is_file():
        return None
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project") or {}
    name = project.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _validate_iso_venv_post_install(
    *,
    iso_venv_bin: Path,
    workspace: Path,
    timeout: float | None,
) -> None:
    """spec_031 §2-1 — collect all post-install failures into one exception.

    Three checks, all run unconditionally so the operator sees every missing
    piece in one go (spec_021 ガード 5 と同じ「全部チェックする」流儀):

    1. ``mutmut`` binary present in ``iso_venv_bin``
    2. ``pytest`` binary present in ``iso_venv_bin``
    3. the workspace's ``[project] name`` is importable inside the iso-venv
       (only when pyproject.toml advertises one — 古典 setuptools 等の repo
       はスキップして過剰実装を避ける)
    """

    errors: list[str] = []

    mutmut_bin = iso_venv_bin / "mutmut"
    if not mutmut_bin.is_file():
        errors.append(
            f"mutmut binary not found at {mutmut_bin} "
            "(pip install of mutmut likely failed silently)"
        )

    pytest_bin = iso_venv_bin / "pytest"
    if not pytest_bin.is_file():
        errors.append(
            f"pytest binary not found at {pytest_bin} "
            "(pip install of pytest likely failed silently)"
        )

    dist_name = _extract_pyproject_project_name(workspace)
    if dist_name:
        iso_python = iso_venv_bin / "python"
        # importlib.metadata.version raises PackageNotFoundError when the
        # dist isn't visible to the iso-venv — much cheaper than ``import``
        # since it doesn't execute the package's __init__ (which could fail
        # for unrelated reasons like a missing optional dependency).
        probe = (
            "from importlib.metadata import version, PackageNotFoundError\n"
            f"try:\n"
            f"    version({dist_name!r})\n"
            "except PackageNotFoundError as exc:\n"
            "    raise SystemExit(f'PackageNotFoundError: {exc}')\n"
        )
        try:
            check_proc = subprocess.run(
                [str(iso_python), "-c", probe],
                capture_output=True,
                timeout=timeout if timeout is not None else 30,
            )
        except FileNotFoundError as exc:
            errors.append(
                f"iso-venv python missing at {iso_python} ({exc}); "
                f"cannot verify package {dist_name!r} is importable"
            )
        except subprocess.TimeoutExpired:
            errors.append(
                f"importlib.metadata probe for {dist_name!r} timed out "
                f"in iso-venv"
            )
        else:
            if check_proc.returncode != 0:
                stderr_text = check_proc.stderr.decode(errors="replace").strip()
                if len(stderr_text) > _POST_INSTALL_STDERR_MAX:
                    stderr_text = (
                        stderr_text[:_POST_INSTALL_STDERR_MAX] + "...[truncated]"
                    )
                errors.append(
                    f"package {dist_name!r} not importable in iso-venv "
                    f"(pip install -e . likely failed silently); "
                    f"stderr: {stderr_text}"
                )

    if errors:
        raise IsoVenvProvisioningError(
            "post-install validation failed:\n  - " + "\n  - ".join(errors)
        )


# --------------------------------------------------------------------------- #
# MutmutRunner — subprocess wrapper (production)
# --------------------------------------------------------------------------- #


def _build_mutmut_run_argv(
    *,
    binary: str,
    paths_arg: str,
    tests_dir: str | None,
    extra_args: list[str],
) -> list[str]:
    """spec_032 §2-2 — assemble the ``mutmut run`` command line.

    Order matters for the test suite that pins the exact argv shape:

    1. ``<binary> run``
    2. ``--paths-to-mutate <comma-separated paths>``
    3. ``--tests-dir <tests_dir>`` when set
    4. profile-supplied ``extra_args`` verbatim, last

    Extracted to a pure helper so the assembly logic is unit-testable
    without spinning up an iso-venv. CCD does NOT whitelist
    ``extra_args`` — the existing防護網 (mutmut non-zero exit, the
    spec_030 0-mutants HALT) absorbs bad inputs (spec §3
    「既存防護網に委譲」).
    """

    cmd: list[str] = [binary, "run", "--paths-to-mutate", paths_arg]
    if tests_dir:
        cmd += ["--tests-dir", tests_dir]
    cmd += list(extra_args)
    return cmd


class MutmutRunner:
    """Production `MutationRunner` that shells out to ``mutmut``.

    Mutmut's CLI / output shape varies across versions; this wrapper takes a
    best-effort approach: run mutations, enumerate results, then ``mutmut
    show <id>`` each mutant to extract a stable ``(file, line, change)``
    triple. If any step fails it returns an ``error``-bearing
    ``MutationRunOutcome`` — ``run_discovery`` then halts gracefully without
    a crash or traceback.

    spec_014 — every mutmut subprocess is invoked inside ``_isolated_clone``
    of the live repo, never against the live working tree. mutmut's in-place
    file rewrites land in the disposable copy; any runaway git write a broken
    mutation might trigger inside CCD's own test suite hits the copy's
    remoteless ``.git`` instead of the real repo / origin.

    spec_019 — additionally provisions a dedicated venv *inside* the clone
    via :func:`_provision_iso_venv` and runs ``mutmut`` from that venv's
    binaries. This is what actually makes ``import ccd`` (inside the
    mutmut-spawned pytest subprocess) resolve to the *mutated* clone rather
    than the live source — bypassing the parent venv's PEP 660 editable
    finder that caused the 0-killed regression.

    spec_032 — accepts ``cwd`` / ``tests_dir`` / ``extra_args`` so a
    profile can target nested-structure repos (``backend/src/...``).
    When ``cwd`` is set, mutmut is invoked from ``<clone>/<cwd>`` and
    ``.mutmut-cache`` is read from that same subdirectory; the iso-venv
    itself still lives at the clone root, which avoids any need to
    change how ``_provision_iso_venv`` installs the editable target.
    """

    DEFAULT_BINARY = "mutmut"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout: float | None = None,
        cwd: str | None = None,
        tests_dir: str | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self._binary = binary or self.DEFAULT_BINARY
        self._timeout = timeout
        self._cwd = cwd
        self._tests_dir = tests_dir
        self._extra_args = list(extra_args or [])

    def run(
        self,
        *,
        repo: Path,
        paths: list[str] | None = None,
    ) -> MutationRunOutcome:
        paths_arg = ",".join(paths) if paths else "."

        with _isolated_clone(Path(repo)) as workspace:
            try:
                iso_venv_bin = _provision_iso_venv(
                    workspace, timeout=self._timeout
                )
            except IsoVenvProvisioningError as exc:
                return MutationRunOutcome(
                    mutants=[],
                    tool="mutmut",
                    error=f"iso-venv provisioning failed: {exc}",
                )

            binary = self._resolve_binary(iso_venv_bin)
            env = _workspace_env(workspace, iso_venv_bin=iso_venv_bin)
            # spec_032 — mutmut's working directory inside the clone.
            # ``None`` keeps the spec_014 behavior (clone root); a
            # non-None ``cwd`` is resolved relative to the clone, so
            # ``<clone>/backend`` is what mutmut sees as its CWD and
            # ``.mutmut-cache`` is read from that subdirectory.
            run_cwd = workspace / self._cwd if self._cwd else workspace

            argv = _build_mutmut_run_argv(
                binary=binary,
                paths_arg=paths_arg,
                tests_dir=self._tests_dir,
                extra_args=self._extra_args,
            )
            try:
                run_proc = subprocess.run(
                    argv,
                    cwd=str(run_cwd),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                return MutationRunOutcome(
                    mutants=[],
                    tool="mutmut",
                    error=f"mutmut binary not found ({exc.filename or binary})",
                )
            except subprocess.TimeoutExpired:
                return MutationRunOutcome(
                    mutants=[],
                    tool="mutmut",
                    error=f"mutmut run timed out after {self._timeout}s",
                )

            # mutmut returns non-zero when any mutants survived — that is the
            # *successful* path here (we want survivors). Only treat the run as
            # failed when it explicitly says it couldn't start.
            run_raw = (run_proc.stdout or "") + (run_proc.stderr or "")
            if run_proc.returncode not in (0, 1, 2) and not run_raw.strip():
                return MutationRunOutcome(
                    mutants=[],
                    tool="mutmut",
                    raw_output=run_raw,
                    error=(
                        f"mutmut run exited {run_proc.returncode} with no output"
                    ),
                )

            try:
                results_proc = subprocess.run(
                    [binary, "results"],
                    cwd=str(run_cwd),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                return MutationRunOutcome(
                    mutants=[],
                    tool="mutmut",
                    raw_output=run_raw,
                    error=f"mutmut results failed: {exc}",
                )

            raw_output = (run_raw + "\n" + (results_proc.stdout or "")).strip()
            groups = _parse_mutmut_results(results_proc.stdout or "")
            mutants: list[Mutant] = []
            for status, by_file in groups.items():
                for file_path, ids in by_file.items():
                    for mid in ids:
                        file_from_show, line, desc = self._show(
                            binary, run_cwd, env, mid
                        )
                        mutants.append(
                            Mutant(
                                file=file_from_show or file_path,
                                line=line,
                                mutation=desc or f"mutmut:{mid}",
                                status=status,
                            )
                        )

            # spec_019: ``mutmut results`` deliberately omits killed mutants
            # (it's optimised for surfacing actionable survivors). Without
            # the killed count, our summary always reports ``killed=0`` and
            # the canary check would fire on every successful run. Read the
            # killed mutants straight from ``.mutmut-cache`` (mutmut's own
            # SQLite store) to get the authoritative count.
            # spec_032 — when ``cwd`` is set the cache lives in
            # ``<clone>/<cwd>/.mutmut-cache``, not at the clone root.
            mutants.extend(_collect_killed_mutants_from_cache(run_cwd))

            return MutationRunOutcome(
                mutants=mutants,
                tool="mutmut",
                raw_output=raw_output,
            )

    def _resolve_binary(self, iso_venv_bin: Path) -> str:
        """Prefer the iso-venv's own ``mutmut`` script over $PATH lookup.

        Falls back to ``shutil.which`` so a user override via constructor
        still works. The iso-venv path is preferred because it pairs with
        the iso-venv's Python (whose import machinery points at the clone) —
        running the parent venv's ``mutmut`` would silently route back to
        the parent's Python and re-introduce the spec_019 0-killed regression.
        """

        if self._binary == self.DEFAULT_BINARY:
            iso_mutmut = iso_venv_bin / self._binary
            if iso_mutmut.exists():
                return str(iso_mutmut)
        return shutil.which(self._binary) or self._binary

    def _show(
        self,
        binary: str,
        run_cwd: Path,
        env: dict[str, str],
        mid: str,
    ) -> tuple[str | None, int, str]:
        try:
            proc = subprocess.run(
                [binary, "show", mid],
                cwd=str(run_cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return (None, 0, "")
        if proc.returncode != 0:
            return (None, 0, "")
        return _parse_mutmut_show(proc.stdout or "")


# --------------------------------------------------------------------------- #
# spec_019: read killed mutants from mutmut's SQLite cache. ``mutmut results``
# only prints actionable (non-killed) mutants; without this, the killed count
# stays at zero and the canary trips on every successful run.
# --------------------------------------------------------------------------- #


_MUTMUT_CACHE_REL = ".mutmut-cache"

# mutmut's status values → CCD's canonical labels. Only listed here:
# ``ok_killed`` (the one ``mutmut results`` swallows). The other statuses
# are already covered by the text-parsing path above; duplicating them here
# would double-count and confuse the canary detector.
_MUTMUT_CACHE_KILLED_STATUS = "ok_killed"


def _collect_killed_mutants_from_cache(workspace: Path) -> list[Mutant]:
    """Return a Mutant record per killed mutation, sourced from the cache.

    The cache schema (as of mutmut 2.5.x):

      SourceFile(id, filename, hash)
      Line(id, sourcefile, line, line_number)
      Mutant(id, line, index, tested_against_hash, status)

    We don't need the mutation description for killed mutants — they're
    not actionable by construction (the tests caught them). The signature
    string just has to be unique-enough to keep ``status_breakdown``
    accurate and to render in the "killed" section if a future report
    ever lists them.

    Failures here are best-effort: a missing cache, schema drift, or a
    read error all return an empty list. The canary will then correctly
    flag the run as broken — silently swallowing the killed count would
    re-introduce the very bug spec_019 set out to fix.
    """

    cache_path = workspace / _MUTMUT_CACHE_REL
    if not cache_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        cur = con.execute(
            """
            SELECT s.filename, l.line_number, m.id
            FROM Mutant m
            JOIN Line l ON m.line = l.id
            JOIN SourceFile s ON l.sourcefile = s.id
            WHERE m.status = ?
            """,
            (_MUTMUT_CACHE_KILLED_STATUS,),
        )
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    out: list[Mutant] = []
    for filename, line_number, mutant_id in rows:
        out.append(
            Mutant(
                file=str(filename),
                line=int(line_number) if line_number is not None else 0,
                mutation=f"mutmut:killed:{mutant_id}",
                status=STATUS_KILLED,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Test double
# --------------------------------------------------------------------------- #


@dataclass
class FakeMutationRunner:
    """Test double for `MutationRunner`. Returns canned mutants verbatim.

    Mirrors `FakeAgentRunner` in shape: ``calls`` records each ``run()`` for
    test assertions, and the canned ``mutants`` / ``error`` / ``raw_output``
    are returned without ever invoking real ``mutmut``.
    """

    mutants: list[Mutant] = field(default_factory=list)
    tool: str = "fake"
    raw_output: str = ""
    error: str = ""
    calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)

    def run(
        self,
        *,
        repo: Path,
        paths: list[str] | None = None,
    ) -> MutationRunOutcome:
        self.calls.append((repo, tuple(paths) if paths else ()))
        return MutationRunOutcome(
            mutants=list(self.mutants),
            tool=self.tool,
            raw_output=self.raw_output,
            error=self.error,
        )


# --------------------------------------------------------------------------- #
# mutmut output parsing
# --------------------------------------------------------------------------- #

_RESULTS_STATUS_HEADERS: dict[str, str] = {
    # Section title → canonical status. mutmut variants emit different
    # decorations (emoji, parentheses with counts) so the matcher is
    # substring-based and case-insensitive.
    "survived": STATUS_SURVIVED,
    "killed": STATUS_KILLED,
    "timeout": STATUS_TIMEOUT,
    "incompetent": STATUS_INCOMPETENT,
    "suspicious": STATUS_SUSPICIOUS,
}


def _parse_mutmut_results(text: str) -> dict[str, dict[str, list[str]]]:
    """Parse ``mutmut results`` text into ``{status: {file: [ids]}}``.

    Best-effort: mutmut's output varies a bit across versions but the shape
    is consistent — a status header, then per-file blocks (``---- file (n)
    ----``), then comma- or range-separated IDs ("1, 3, 5-7").
    """

    groups: dict[str, dict[str, list[str]]] = {}
    current_status: str | None = None
    current_file: str | None = None

    file_header = re.compile(r"^----\s+(.+?)\s+\((\d+)\)\s+----")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        status = _match_status_header(line)
        if status is not None:
            current_status = status
            current_file = None
            groups.setdefault(current_status, {})
            continue
        m = file_header.match(line)
        if m and current_status is not None:
            current_file = m.group(1).strip()
            groups[current_status].setdefault(current_file, [])
            continue
        if current_status and current_file:
            ids = _parse_id_list(line)
            if ids:
                groups[current_status][current_file].extend(ids)
    return groups


def _match_status_header(line: str) -> str | None:
    lowered = line.lower()
    for keyword, canonical in _RESULTS_STATUS_HEADERS.items():
        if lowered.startswith(keyword):
            return canonical
    return None


def _parse_id_list(line: str) -> list[str]:
    out: list[str] = []
    for chunk in re.split(r"[,\s]+", line):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            parts = chunk.split("-", 1)
            try:
                lo, hi = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if lo <= hi:
                out.extend(str(n) for n in range(lo, hi + 1))
        else:
            try:
                int(chunk)
            except ValueError:
                continue
            out.append(chunk)
    return out


def _parse_mutmut_show(text: str) -> tuple[str | None, int, str]:
    """Parse a ``mutmut show <id>`` unified-diff into ``(file, line, desc)``.

    Returns empty values when parsing fails — the caller falls back to a
    placeholder description, which still yields a stable signature provided
    the file path / line are recovered.
    """

    file_name: str | None = None
    hunk_offset_base: int = 0
    in_hunk = False
    offset = 0
    minus_line: str | None = None
    plus_line: str | None = None
    minus_line_no: int = 0
    hunk_re = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")

    for raw in text.splitlines():
        if raw.startswith("+++ "):
            candidate = raw[4:].strip()
            file_name = _clean_diff_path(candidate)
            continue
        if raw.startswith("--- ") and file_name is None:
            # If +++ is absent we'll fall back to the --- side.
            file_name = _clean_diff_path(raw[4:].strip())
            continue
        m = hunk_re.match(raw)
        if m:
            hunk_offset_base = int(m.group(2))
            in_hunk = True
            offset = 0
            continue
        if not in_hunk:
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            if minus_line is None:
                minus_line = raw[1:]
                minus_line_no = hunk_offset_base + offset
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            if plus_line is None:
                plus_line = raw[1:]
            offset += 1
            continue
        # Context line.
        offset += 1

    if file_name is None:
        return (None, 0, "")
    desc = ""
    if minus_line is not None and plus_line is not None:
        desc = f"{minus_line.strip()} → {plus_line.strip()}"
    elif minus_line is not None:
        desc = f"{minus_line.strip()} → (removed)"
    elif plus_line is not None:
        desc = f"(added) → {plus_line.strip()}"
    return (file_name, minus_line_no, desc)


def _clean_diff_path(p: str) -> str:
    # Strip optional `a/` / `b/` prefixes and surrounding whitespace.
    p = p.strip()
    for prefix in ("a/", "b/", "./"):
        if p.startswith(prefix):
            return p[len(prefix) :]
    return p
