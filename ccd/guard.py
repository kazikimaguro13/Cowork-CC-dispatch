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

A hardcoded **denylist** holds the files the guard must refuse to allow
modifications to, *regardless of the caller's allowlist*: the guard
itself, the nightly scheduler modules, CI config, mutation/discovery
config, ``pyproject.toml``. ("Who guards the guard": the guard cannot be
weakened by the same loop it is supposed to police.) Denylist hits are
HALT even when the caller asks nicely.

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
    "_ai_workspace/ccd_profile.toml",
    "ccd_profile.toml",
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


def _denylist_hit(fd: FileDiff) -> str | None:
    """Return halt reason iff this FileDiff touches the denylist."""
    for candidate in (fd.new_path, fd.old_path):
        if candidate and _matches_any(candidate, DENYLIST_GLOBS):
            return (
                f"denylist: {candidate} is self-protected "
                f"(guard / scheduler / CI / packaging / discovery config)"
            )
    return None


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
    "DEFAULT_PROD_DIFF_LIMIT",
    "DENYLIST_GLOBS",
    "FileDiff",
    "GuardResult",
    "fetch_diff",
    "inspect_diff",
]
