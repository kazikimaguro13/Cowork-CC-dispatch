"""ccd guard — static inspector for the autonomous-fix loop's git diff (spec_021).

v2 Phase 2 first spec: **guard-first**. Phase 2 is the autonomous fix loop —
discoveries (mutation / adversarial / ai) feed an agent that proposes a fix,
runs the test suite, and (only if the suite is green) commits. The risk
established in ``docs/DESIGN.md §9.5 论点6`` is "fraudulent fixes": the agent
can make a failing test pass by deleting the test, weakening an ``assert``,
or marking it ``skip``/``xfail``. Phase 1's principle of isolating one risk
per spec ("discover-first" before discovery wiring) is mirrored here:
**the guard is built and proven alone — against crafted diffs — before any
loop is wired**. By the time the loop ignites, the inspector is already
shown to catch the cheats.

The guard is **enforcement, not instruction** (论点6's first principle): it
inspects the git diff and the git diff only. It never reads
``result_NNN.md`` or the agent's self-report — the diff is fact, the
self-report is claim.

Rules implemented here are the **statically checkable** ones from §9.5 论点6:

- **R1** — every file touched is in the caller's allowlist.
- **R2** — ``tests/`` is append-only: no removed/changed lines in existing
  test files, no new ``skip``/``xfail``/``disable`` markers, no deleted
  test files.
- **R3** (template B only) — the production-file diff is bounded; a
  narrow-scope fix that produces a sprawling diff is itself a signal of
  scope creep.

R4 ("existing suite is green") and R5 ("the target test is killed") are
**dynamic** — they need the test runner and ``mutmut`` — and live in the
loop-wiring spec (spec_023). Do not blend them into this module.

Self-protection
---------------

The guard refuses to allow modifications to its own machinery,
*regardless of the caller's allowlist*. ("Who guards the guard": the
guard cannot be weakened by the same loop it is supposed to police.)
Denylist hits are HALT even when the caller asks nicely. Two layers:

- **Non-``ccd/`` enumeration** (``DENYLIST_GLOBS``) — CI config under
  ``.github/``, ``pyproject.toml`` / ``setup.*`` / ``.pre-commit-config``,
  and the ``_ai_workspace/`` profile + discovery blocklist.
- **Inverted ``ccd/`` rule** (spec_044, RT-2 root-cause fix) — every
  ``.py`` under ``ccd/`` is **denied by default**; only modules on the
  explicit ``PRODUCT_FIXABLE`` allowlist (``protocol.py`` / ``models.py``)
  may be named by a template-B fix. An enumerated denylist protects only
  what someone remembered to list; the inverted rule protects a new module
  automatically, so a forgotten classification fails *safe* (denied), not
  *open*. ``CORE_CCD_MODULES`` enumerates the machinery so the §2-3 forced
  test can distinguish a deliberately-CORE module from an unclassified one.

False positives OK, false negatives not OK
------------------------------------------

The guard prefers to halt when it cannot interpret the diff confidently
(binary, rename, mode change, malformed). Stopping a legitimate fix is
cheap — it shows up in the morning brief and a human looks. Letting a
fraudulent fix through is what we must never do.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# --------------------------------------------------------------------------- #
# Denylist (self-protection)
# --------------------------------------------------------------------------- #

# Paths the guard refuses to allow modifications to, no matter what the caller
# put in ``allowed_files``. The intent: the autonomous-fix loop must never be
# able to soften its own gates. Concrete patterns:
#
# - ``ccd/guard.py`` — the inspector itself.
# - ``ccd/nightly.py`` plus future scheduler modules (glob picks up
#   any ``ccd/nightly*`` / ``ccd/scheduler*`` we add later in Phase 2).
# - ``.github/**`` — CI configuration; the loop must not edit how its own
#   PRs are verified.
# - ``pyproject.toml`` / ``setup.py`` / ``setup.cfg`` — packaging /
#   ``ruff`` / ``pytest`` config.
# - Discovery/profile config under ``_ai_workspace/`` — mutmut blocklist,
#   ccd profile (which channels are enabled, schedule, future cost
#   ceilings). The loop must not flip its own switches.
# - ``.pre-commit-config.yaml`` — same reason as CI.
#
# spec_048 (Fable 5 全レビュー 🟢-1): the *canonical* profile store the
# production ``nightly-all`` actually reads is the multi-policy directory
# ``_ai_workspace/profiles/*.toml`` (``ccd.profile.PROFILES_DIR_REL``;
# spec_029/046), NOT the legacy single ``ccd_profile.toml`` the original list
# named. That profile carries the **verification-strength knobs** (mutation
# runner, ``r5_recheck_times``, ``max_merges_per_night``, enabled templates) —
# the self-referential bug RT-2 warned about ("the system reproduces metrics
# that lie"). The legacy single-path entries are kept for backward
# compatibility; the ``profiles/**`` glob protects the whole canonical store so
# a forgotten enumeration cannot reopen the hole. The §2-2 forced-coverage test
# (:func:`uncovered_protected_configs`) pins that this list keeps covering the
# canonical config even if the path is migrated again.
DENYLIST_GLOBS: tuple[str, ...] = (
    "ccd/guard.py",
    "ccd/nightly.py",
    "ccd/nightly_*.py",
    "ccd/scheduler.py",
    "ccd/scheduler_*.py",
    ".github/**",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    ".pre-commit-config.yaml",
    "_ai_workspace/discover/blocklist.txt",
    "_ai_workspace/profiles/**",  # 多施策 profile 正本（spec_029/046 で移行）
    "_ai_workspace/ccd_profile.toml",  # 旧単一パス（後方互換で残置）
    "ccd_profile.toml",  # 旧裸パス（後方互換で残置）
)

# --------------------------------------------------------------------------- #
# Inverted self-protection for ``ccd/`` (spec_044 — RT-2 root-cause fix)
# --------------------------------------------------------------------------- #
#
# RT-2 (2026-06-10 red team, Fable 5): the *enumerated* ``DENYLIST_GLOBS``
# above only names ``guard`` / ``nightly`` / CI / packaging / profile config.
# After v2→v3 split the loop-and-guard machinery, ``loop.py`` / ``retry.py`` /
# ``translate.py`` / ``metrics.py`` / ``backfill.py`` / ``discover.py`` /
# ``adversarial.py`` / ``profile.py`` / Integrator were left **unprotected**.
# Today template A (tests-only) papers over it, but the moment template B
# (one named production file) ignites, a finding that names one of those core
# modules would sail through R1 and the loop could rewrite its own
# ``metrics.py`` — "the metrics lie" reproduced by the system itself.
#
# The root cause is that an *enumerated* denylist protects only what someone
# remembered to list: add a new ``ccd/`` module, forget to list it, and a hole
# opens. spec_044 **inverts** the rule for ``ccd/``: **default deny, explicit
# allow**. Every ``.py`` under ``ccd/`` is protected unless its path appears in
# the ``PRODUCT_FIXABLE`` allowlist below. A new ``ccd/`` module is protected
# automatically (safe default); forgetting to *protect* something is now
# structurally impossible — you can only forget to *un*protect, which fails
# safe.
#
# ``迷えば CORE`` (when in doubt, CORE): false positives (a legitimate
# ``protocol.py`` fix wrongly blocked) are cheap — they show up in the morning
# brief §D and a human adds the module to ``PRODUCT_FIXABLE``. A false negative
# (the loop edits its own machinery) is what we must never allow.

# Modules the loop must NEVER rewrite — its own machinery and the human-facing
# audit/report surfaces (tampering with a report *is* the cover-up). This set
# is enumerated explicitly (not "everything except PRODUCT_FIXABLE") so the
# §2-3 forced-classification test can tell a deliberately-CORE module apart
# from a brand-new *unclassified* one and fail on the latter.
CORE_CCD_MODULES: frozenset[str] = frozenset(
    {
        # loop / guard machinery
        "guard.py",  # the inspector itself — "who guards the guard"
        "nightly.py",  # orchestrator / Integrator
        "loop.py",  # FixLoop (convergence / no-progress detection)
        "retry.py",  # retryable-boundary classification
        "translate.py",  # template translation (must stay a rigid body)
        "metrics.py",  # honest-metrics computation
        "backfill.py",  # metrics backfill
        "discover.py",  # discovery oracle
        "adversarial.py",  # adversarial-input oracle
        "profile.py",  # safety-boundary validation
        "integrate.py",  # Integrator
        "agent.py",  # the loop's own fixer agent
        "chain.py",  # spec→dispatch chaining
        "cli.py",  # command surface
        "dispatch.py",  # worker dispatch
        "run_writer.py",  # run-record writer
        "sweep.py",  # sweep machinery
        # human-facing audit / report surfaces — tampering = cover-up
        "dashboard.py",
        "brief.py",
        "ai_review.py",
        "retrospect.py",
        # package plumbing
        "__init__.py",
        "__main__.py",
    }
)

# The ONLY ``ccd/`` modules a template-B fix may name: product logic that is
# under test and is NOT guard machinery. Kept deliberately tiny. Adding a
# module here to make a test pass defeats the purpose of this spec — when in
# doubt, leave it CORE.
PRODUCT_FIXABLE: frozenset[str] = frozenset(
    {
        "protocol.py",
        "models.py",
    }
)

# R3 default: a narrow scope-B fix that exceeds this many production-side
# +/- lines is treated as scope creep. Set deliberately loose for Phase 2
# bring-up; the morning brief will surface false-positive halts and we
# tighten the knob from operating experience.
DEFAULT_PROD_DIFF_LIMIT = 60


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FileDiff:
    """One file's contribution to a unified diff, normalized."""

    old_path: str | None
    new_path: str | None
    added_lines: int
    removed_lines: int
    added_text: tuple[str, ...]
    removed_text: tuple[str, ...]
    is_binary: bool
    is_rename: bool
    is_mode_change: bool
    is_new_file: bool
    is_deleted_file: bool

    @property
    def path(self) -> str:
        """Canonical path (new side preferred; falls back to old for deletes)."""
        return self.new_path or self.old_path or ""


@dataclass(frozen=True)
class GuardResult:
    """``inspect_diff`` return value.

    ``passed`` is True only when no rule fired. ``halt_reasons`` is the
    full list (we don't short-circuit on the first hit — the operator
    wants to see every cheat the agent attempted in one go).
    ``files_touched`` mirrors the diff for the morning brief.
    """

    passed: bool
    halt_reasons: tuple[str, ...]
    files_touched: tuple[str, ...]
    template: str = ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def inspect_diff(
    *,
    diff: str,
    allowed_files: Iterable[str | Path],
    template: str,
    max_prod_diff_lines: int = DEFAULT_PROD_DIFF_LIMIT,
) -> GuardResult:
    """Inspect a unified diff against R1 / R2 / R3 + the denylist.

    Arguments
    ---------
    diff : str
        Unified diff text (e.g., the output of ``git diff main..HEAD``).
        Empty / whitespace-only diffs pass trivially.
    allowed_files : iterable of str or Path
        The caller's allowlist. Entries may be file paths (``"ccd/foo.py"``)
        or directory prefixes (``"tests/"`` / ``"tests"``). Glob characters
        (``*``, ``?``, ``[``) are honored.
    template : "A" or "B"
        Template A = tests-only. Template B = one named production file +
        ``tests/``. Unknown templates HALT.
    max_prod_diff_lines : int
        Threshold for R3 (template B only).
    """

    if template not in ("A", "B"):
        return GuardResult(
            passed=False,
            halt_reasons=(f"unknown template: {template!r} (expected 'A' or 'B')",),
            files_touched=(),
            template=str(template),
        )

    try:
        file_diffs = _parse_diff(diff)
    except _UnparseableDiff as exc:
        return GuardResult(
            passed=False,
            halt_reasons=(f"safe-halt: diff parse failed: {exc}",),
            files_touched=(),
            template=template,
        )

    allowed = _normalize_allowed(allowed_files)
    halt_reasons: list[str] = []
    files_touched: tuple[str, ...] = tuple(
        sorted({fd.path for fd in file_diffs if fd.path})
    )

    for fd in file_diffs:
        # Denylist always wins, even before R1. The same path on the old
        # side (renames) is also checked so an agent can't sneak a
        # protected file out via rename.
        deny_hit = _denylist_hit(fd)
        if deny_hit:
            halt_reasons.append(deny_hit)
            continue

        # Hard-to-interpret diffs → safe-halt. Binary / rename / mode change
        # all fall here: we cannot reason about content on these, and the
        # autonomous fix loop has no legitimate reason to produce them.
        safe = _safe_halt_reason(fd)
        if safe:
            halt_reasons.append(safe)
            continue

        # R1: file allowlist.
        if not _is_allowed(fd.path, allowed):
            halt_reasons.append(
                f"R1: {fd.path} is not in the allowed file set "
                f"(allowed={list(allowed) or '[]'})"
            )
            continue

        # R2: tests/ is append-only.
        r2 = _r2_violation(fd)
        if r2:
            halt_reasons.append(r2)
            continue

    # R3: template B production-diff bound.
    if template == "B":
        r3 = _r3_violation(file_diffs, max_prod_diff_lines)
        if r3:
            halt_reasons.append(r3)

    return GuardResult(
        passed=not halt_reasons,
        halt_reasons=tuple(halt_reasons),
        files_touched=files_touched,
        template=template,
    )


# --------------------------------------------------------------------------- #
# Rule helpers
# --------------------------------------------------------------------------- #


def _ccd_module_relpath(path: str | None) -> str | None:
    """Return the path *relative to* ``ccd/`` iff ``path`` is a ``.py`` file
    under the ``ccd/`` package, else ``None``.

    ``"ccd/metrics.py"`` → ``"metrics.py"``; ``"ccd/sub/x.py"`` →
    ``"sub/x.py"``; ``"tests/test_x.py"`` / ``"ccd/data.json"`` → ``None``.
    """
    if not path:
        return None
    pp = PurePosixPath(path)
    if len(pp.parts) >= 2 and pp.parts[0] == "ccd" and pp.suffix == ".py":
        return str(PurePosixPath(*pp.parts[1:]))
    return None


def classify_ccd_module(relpath: str) -> str:
    """Classify a ``ccd/``-relative module path.

    Returns ``"product_fixable"`` (template B may name it), ``"core"`` (the
    loop's own machinery — never touchable), or ``"unclassified"`` (a module
    nobody has triaged yet — treated as deny, and flagged by the §2-3 forced
    test so a human classifies it). PRODUCT_FIXABLE wins ties, but the two
    sets are kept disjoint.
    """
    if relpath in PRODUCT_FIXABLE:
        return "product_fixable"
    if relpath in CORE_CCD_MODULES:
        return "core"
    return "unclassified"


def unclassified_ccd_modules(relpaths: Iterable[str]) -> list[str]:
    """Return the subset of ``relpaths`` that is neither CORE nor
    PRODUCT_FIXABLE, sorted. The §2-3 forced-classification test walks the
    real ``ccd/`` tree through this and fails if it is non-empty.
    """
    return sorted(r for r in relpaths if classify_ccd_module(r) == "unclassified")


def uncovered_protected_configs(paths: Iterable[str]) -> list[str]:
    """Return the protected-config ``paths`` NOT covered by any
    ``DENYLIST_GLOBS`` glob, sorted (spec_048 §2-2).

    The §2-1 inversion for ``ccd/*.py`` made "forgetting to protect a *module*"
    structurally impossible. The analogous hole for **config** is "the loop's
    verification-strength knobs live in ``_ai_workspace/`` and someone migrated
    the canonical path but forgot to update ``DENYLIST_GLOBS``" — exactly the
    Fable 5 🟢-1 finding (profile正本が ``ccd_profile.toml`` → ``profiles/*.toml``
    に移ったのに denylist が旧パスのままだった). This helper is the forced check:
    the §2-2 test enumerates the *actual* canonical config on disk (deriving the
    profile directory from :data:`ccd.profile.PROFILES_DIR_REL`, the same source
    of truth production reads) and fails if any of it is uncovered. False
    positives are fine — the protected set is kept deliberately broad.
    """
    return sorted(p for p in paths if not _matches_any(p, DENYLIST_GLOBS))


# spec_048 §2-3 — markers that, when *added* to an existing test by a fix,
# permanently drop that test from the mutation subset runner
# (``-m "not slow"``; CCD profile, spec_046). This is **safe-side**: a test
# pulled out of the subset means more surviving mutants → more discovery, never
# a worse merge — so it must NOT halt. But the silent, permanent subset shrink
# is worth one warning line in the morning brief §D so a human can notice it.
# Mirrors the decorator / module-level (``pytestmark = ...``) forms the R2
# skip-marker scan already understands.
_SLOW_MARKER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@\s*pytest\.mark\.slow\b"),
    re.compile(r"^\s*pytestmark\s*=.*\bslow\b"),
)


def added_slow_markers(diff: str) -> list[str]:
    """Return ``"<path>: <marker>"`` for every ``@pytest.mark.slow`` (or
    module-level ``pytestmark = ... slow``) marker **added** by ``diff``
    (spec_048 §2-3).

    Best-effort, non-halting observation only (see :data:`_SLOW_MARKER_PATTERNS`).
    Scans added (``+``) hunk lines of every parsed file; unparseable diffs
    yield ``[]`` (the dynamic R4 count gate remains the主防御 against any real
    test muting). Duplicates within a file are collapsed.
    """
    try:
        file_diffs = _parse_diff(diff)
    except _UnparseableDiff:
        return []
    out: list[str] = []
    for fd in file_diffs:
        seen: set[str] = set()
        for line in fd.added_text:
            for pat in _SLOW_MARKER_PATTERNS:
                m = pat.search(line)
                if m and m.group(0) not in seen:
                    seen.add(m.group(0))
                    out.append(f"{fd.path}: {m.group(0).strip()}")
                    break
    return out


def _ccd_default_deny_hit(fd: FileDiff) -> str | None:
    """Inverted self-protection (spec_044): any ``ccd/*.py`` that is not on
    the ``PRODUCT_FIXABLE`` allowlist is denied — CORE and unclassified alike.
    Both old and new paths are checked so a rename can't smuggle a core module
    out.
    """
    for candidate in (fd.new_path, fd.old_path):
        rel = _ccd_module_relpath(candidate)
        if rel is None or rel in PRODUCT_FIXABLE:
            continue
        kind = "core" if rel in CORE_CCD_MODULES else "unclassified"
        return (
            f"denylist: {candidate} は core 機構のため修正対象にできない"
            f"（テンプレB は PRODUCT_FIXABLE のみ: {sorted(PRODUCT_FIXABLE)}"
            f"; 分類={kind}）"
        )
    return None


def _denylist_hit(fd: FileDiff) -> str | None:
    """Return halt reason iff this FileDiff touches the denylist.

    Two layers, denylist always wins over the caller's allowlist:
    1. the enumerated non-``ccd/`` protections (CI / packaging / profile);
    2. the inverted ``ccd/`` default-deny (spec_044) — everything under
       ``ccd/`` except ``PRODUCT_FIXABLE``.
    """
    for candidate in (fd.new_path, fd.old_path):
        if candidate and _matches_any(candidate, DENYLIST_GLOBS):
            return (
                f"denylist: {candidate} is self-protected "
                f"(guard / scheduler / CI / packaging / discovery config)"
            )
    return _ccd_default_deny_hit(fd)


def _safe_halt_reason(fd: FileDiff) -> str | None:
    if fd.is_binary:
        return f"safe-halt: binary diff in {fd.path}"
    if fd.is_rename:
        return (
            f"safe-halt: rename detected ({fd.old_path} -> {fd.new_path}); "
            f"the guard does not interpret renames"
        )
    if fd.is_mode_change:
        return f"safe-halt: mode change on {fd.path}"
    return None


# A test file under R2 = a .py file directly under tests/ (recursive). We
# intentionally exclude tests/fixtures/*.json etc. for now — those are data,
# not assertion code, and false-positive cost would be high.
def _is_test_file(path: str) -> bool:
    if not path:
        return False
    pp = PurePosixPath(path)
    return len(pp.parts) >= 1 and pp.parts[0] == "tests" and pp.suffix == ".py"


# Patterns that indicate a test was muted. We match the prefix on a stripped
# line so leading whitespace doesn't matter; and we look for both decorator
# and runtime forms ("pytest.skip(...)").
#
# spec_043 §2-3 — IMPORTANT: this is the **secondary, best-effort 保険
# layer**, NOT the主防御. String matching against a diff is an arms race
# the guard loses on principle: it can only catch the muting *spellings*
# it knows. RT-7 (import-alias muting — ``from pytest import mark`` →
# ``@mark.skip``; ``from unittest import skip`` → ``@skip``) is a worked
# example of a spelling this list deliberately does NOT chase, because the
# 主防御 catches it for free: the dynamic R4 count gate in
# :func:`ccd.nightly._r4_verdict` rejects any fix whose post-fix suite
# runs *fewer* tests than the pre-fix baseline — skip, deselect,
# collection-hook removal, and import-alias muting all collapse into the
# same single observable fact ("実行数が減った") regardless of how the
# muting was spelled. The patterns below exist only to fail *early and
# cheaply* on the common, statically-obvious cases; never treat their
# absence as proof a diff is safe.
#
# The spec_043 additions over the spec_022 decorator/runtime set:
#   (a) ``pytestmark = …`` — the module-level assignment form
#       (``pytestmark = pytest.mark.skip``) the decorator regex misses.
#   (b) ``collect_ignore`` — a new ``tests/conftest.py`` excluding files
#       from collection.
#   (c) ``pytest_collection_modifyitems`` / ``pytest_ignore_collect`` —
#       collection hooks that deselect / drop items before they run.
_SKIP_MARKER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@\s*pytest\.mark\.(?:skip|skipif|xfail)\b"),
    re.compile(r"@\s*unittest\.skip\b"),
    re.compile(r"\bpytest\.skip\s*\("),
    re.compile(r"\bpytest\.xfail\s*\("),
    # spec_043 §2-3 (a) — module-level ``pytestmark`` assignment.
    re.compile(r"^\s*pytestmark\s*="),
    # spec_043 §2-3 (b) — collection exclusion list (conftest).
    re.compile(r"\bcollect_ignore\b"),
    # spec_043 §2-3 (c) — collection hooks that deselect / drop items.
    re.compile(r"\bpytest_collection_modifyitems\b"),
    re.compile(r"\bpytest_ignore_collect\b"),
)


def _find_skip_markers(lines: Iterable[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        for pat in _SKIP_MARKER_PATTERNS:
            m = pat.search(line)
            if m:
                out.append(m.group(0))
                break
    return out


def _r2_violation(fd: FileDiff) -> str | None:
    if not _is_test_file(fd.path):
        return None
    if fd.is_deleted_file:
        return f"R2: existing test file deleted: {fd.path}"
    if not fd.is_new_file and fd.removed_lines > 0:
        return (
            f"R2: existing test file has {fd.removed_lines} removed/changed "
            f"line(s): {fd.path} — tests/ is append-only"
        )
    markers = _find_skip_markers(fd.added_text)
    if markers:
        return (
            f"R2: test diff introduces a skip/xfail/disable marker "
            f"({markers[0]}) in {fd.path}"
        )
    return None


def _r3_violation(
    file_diffs: Iterable[FileDiff], limit: int
) -> str | None:
    prod_total = 0
    prod_files: list[str] = []
    for fd in file_diffs:
        if not fd.path or _is_test_file(fd.path):
            continue
        if fd.is_binary or fd.is_rename or fd.is_mode_change:
            continue
        prod_total += fd.added_lines + fd.removed_lines
        prod_files.append(fd.path)
    if prod_total > limit:
        return (
            f"R3: production diff is {prod_total} +/- lines across "
            f"{len(prod_files)} file(s) (limit {limit}); narrow-scope "
            f"fixes should not produce large diffs — likely scope creep"
        )
    return None


# --------------------------------------------------------------------------- #
# Allowlist helpers
# --------------------------------------------------------------------------- #


def _normalize_allowed(allowed: Iterable[str | Path]) -> tuple[str, ...]:
    out: list[str] = []
    for a in allowed:
        s = str(a).replace("\\", "/").rstrip("/")
        if s:
            out.append(s)
    return tuple(out)


def _is_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    if not path:
        return False
    for a in allowed:
        if path == a:
            return True
        # Directory-prefix match (caller can pass "tests" or "tests/" — both work).
        if path.startswith(a + "/"):
            return True
        # Glob style — fnmatch's `*` is greedy across `/` which is fine for
        # how the guard is used (concrete file lists, not deep wildcards).
        if any(c in a for c in "*?["):
            if fnmatch.fnmatch(path, a):
                return True
    return False


def _matches_any(path: str, globs: Iterable[str]) -> bool:
    for pat in globs:
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if path == prefix or path.startswith(prefix + "/"):
                return True
            continue
        if fnmatch.fnmatch(path, pat):
            return True
    return False


# --------------------------------------------------------------------------- #
# Unified-diff parser
# --------------------------------------------------------------------------- #


class _UnparseableDiff(Exception):
    """Raised when the diff text cannot be parsed safely."""


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ .* @@")


def _parse_diff(diff: str) -> list[FileDiff]:
    """Split a unified diff into per-file FileDiff records.

    The parser is intentionally minimal: only the fields the rules need
    (path/+/-/binary/rename/mode/new/deleted). Anything that doesn't
    cleanly start with ``diff --git`` is skipped at the top level; per-file
    parsing handles ``Binary files`` / ``rename`` / mode lines explicitly.
    """
    if not diff or not diff.strip():
        return []
    lines = diff.splitlines()
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for ln in lines:
        if _DIFF_HEADER_RE.match(ln):
            if cur is not None:
                blocks.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
        # Lines before the first "diff --git" are tolerated (e.g., commit
        # message prefix), but ignored.
    if cur is not None:
        blocks.append(cur)
    return [_parse_block(b) for b in blocks]


def _parse_block(block: list[str]) -> FileDiff:
    header = block[0]
    m = _DIFF_HEADER_RE.match(header)
    if not m:
        raise _UnparseableDiff(f"missing diff --git header: {header[:80]!r}")
    a_path, b_path = m.group(1), m.group(2)

    old_path: str | None = a_path
    new_path: str | None = b_path
    is_binary = False
    is_rename = False
    is_mode_change = False
    is_new_file = False
    is_deleted_file = False
    added_lines = 0
    removed_lines = 0
    added_text: list[str] = []
    removed_text: list[str] = []
    in_hunk = False

    for ln in block[1:]:
        if ln.startswith("Binary files"):
            is_binary = True
            in_hunk = False
            continue
        if ln.startswith("new file mode"):
            is_new_file = True
            continue
        if ln.startswith("deleted file mode"):
            is_deleted_file = True
            continue
        if ln.startswith("rename from "):
            is_rename = True
            old_path = ln[len("rename from "):].strip()
            continue
        if ln.startswith("rename to "):
            is_rename = True
            new_path = ln[len("rename to "):].strip()
            continue
        if ln.startswith("copy from ") or ln.startswith("copy to "):
            is_rename = True  # treat copies the same way (safe-halt)
            continue
        if ln.startswith("old mode") or ln.startswith("new mode"):
            is_mode_change = True
            continue
        if ln.startswith("index ") or ln.startswith("similarity index") \
                or ln.startswith("dissimilarity index"):
            continue
        if ln.startswith("--- "):
            in_hunk = False
            if "/dev/null" in ln:
                old_path = None
            continue
        if ln.startswith("+++ "):
            in_hunk = False
            if "/dev/null" in ln:
                new_path = None
            continue
        if _HUNK_RE.match(ln):
            in_hunk = True
            continue
        if in_hunk:
            if ln.startswith("+"):
                added_lines += 1
                added_text.append(ln[1:])
            elif ln.startswith("-"):
                removed_lines += 1
                removed_text.append(ln[1:])
            # Context " ..." and "\ No newline at end of file" lines: ignored.
            continue
        # Anything else (e.g., blank line outside a hunk): tolerated.

    return FileDiff(
        old_path=old_path,
        new_path=new_path,
        added_lines=added_lines,
        removed_lines=removed_lines,
        added_text=tuple(added_text),
        removed_text=tuple(removed_text),
        is_binary=is_binary,
        is_rename=is_rename,
        is_mode_change=is_mode_change,
        is_new_file=is_new_file,
        is_deleted_file=is_deleted_file,
    )


# --------------------------------------------------------------------------- #
# git interaction (for the CLI; tests pass diffs in directly)
# --------------------------------------------------------------------------- #


def fetch_diff(repo: Path, base: str, head: str) -> str:
    """Run ``git diff <base>..<head>`` inside ``repo`` and return stdout.

    Raises ``subprocess.CalledProcessError`` if git itself errors (unknown
    ref, not a repo). The CLI surfaces that as a non-zero exit so the
    operator knows the diff scope was wrong, rather than getting a silent
    pass on an empty diff.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", f"{base}..{head}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


__all__ = [
    "CORE_CCD_MODULES",
    "DEFAULT_PROD_DIFF_LIMIT",
    "DENYLIST_GLOBS",
    "PRODUCT_FIXABLE",
    "FileDiff",
    "GuardResult",
    "added_slow_markers",
    "classify_ccd_module",
    "fetch_diff",
    "inspect_diff",
    "unclassified_ccd_modules",
    "uncovered_protected_configs",
]
