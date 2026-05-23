"""Backfill anonymized `DispatchRecord` runs from historical `result_NNN.md`.

The v1.5 dashboard (spec_008) reads `DispatchRecord` envelopes under
`_ai_workspace/runs/`, but the bash-era and early-ccd pipelines only left
behind bridge `result_NNN.md` files. This module reads those, strips them
down to metric fields, anonymizes timestamps and spec ids, and writes one
run JSON per project.

Anonymization rules:

- Spec / Result body, commit hashes, and title text never enter the output.
- Timestamps are rounded to the date (00:00:00 UTC).
- Any `spec_id` that is not of the form ``spec_<digits>`` is renumbered
  to ``spec_NNN`` (sequential per project, continuing past the highest
  existing standard id so renumbered ids don't collide with kept ones).

The list of source projects (root path + display label + generation tag)
is supplied by the caller — either via a JSON config file (gitignored
under `_ai_workspace/`) or repeated `--source` CLI flags. The module never
encodes a real client path.

Invocation:

    python -m ccd.backfill --config _ai_workspace/backfill_sources.json
    python -m ccd.backfill --source PATH LABEL GENERATION [--source ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from .models import DispatchRecord, DispatchStatus, FailureCategory, RunFile

logger = logging.getLogger(__name__)


_STANDARD_SPEC_ID_RE = re.compile(r"^spec_(\d+)$")
_TITLE_RE = re.compile(r"^#\s+(?P<id>\S+?)\s*:\s*(?P<title>.+?)\s*$")
_HEADER_LINE_RE = re.compile(
    r"^-\s*\*\*(?P<key>[^*]+)\*\*\s*:\s*(?P<value>.*?)\s*$"
)
# Status lines anywhere in the document — covers Markdown bold (`**Status**: ...`),
# plain (`Status: ...`, `status: ...`), and YAML frontmatter (`status: done`).
_LOOSE_STATUS_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\*{0,2}status\*{0,2}\s*:\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
# Spec id mentioned anywhere (YAML frontmatter `spec: spec_054`, prose, branch names…).
_LOOSE_SPEC_ID_RE = re.compile(r"\bspec_(\d{3,})\b")
# `result_NNN.md` filename → `spec_NNN` fallback.
_FILENAME_NUM_RE = re.compile(r"result_(\d+)")

_DEFAULT_OUTBOX_REL = Path("_ai_workspace") / "bridge" / "outbox"
_DEFAULT_OUTPUT_REL = Path("_ai_workspace") / "runs"
_DEFAULT_CONFIG_REL = Path("_ai_workspace") / "backfill_sources.json"

# Normalized header keys (lowercased, hyphens → underscores).
_SPEC_ID_KEYS = ("spec", "spec_id")
_STATUS_KEYS = ("status",)
_STARTED_KEYS = ("started", "started_at", "start", "start_at", "begin", "begun")
_FINISHED_KEYS = (
    "finished",
    "finished_at",
    "completed",
    "completed_at",
    "ended",
    "ended_at",
)
_FAILURE_KEYS = ("failure_category", "failure")

_DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d")


@dataclass(frozen=True)
class BackfillSource:
    """A single project to ingest. Paths are caller-supplied; never baked in."""

    path: Path
    label: str
    generation: str
    results_dir: Path | None = None  # default: <path>/_ai_workspace/bridge/outbox


@dataclass
class _Header:
    title_id: str | None = None
    fields: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Single-file parsing                                                         #
# --------------------------------------------------------------------------- #


def parse_result_file(path: Path) -> DispatchRecord | None:
    """Parse one ``result_NNN.md`` into a `DispatchRecord`, or `None` to skip.

    Returns `None` (with a logged warning) when the required fields
    (``spec_id``, ``status``) are missing or unparseable. The function does
    not raise on malformed historical input — backfill must keep going.

    The parser is lenient: status values are normalized (emoji / parenthetical
    suffix / em-dash trail stripped, ``completed`` → ``done``, ``partial`` →
    ``DispatchStatus.PARTIAL``), and if neither the header block nor the title
    yields a spec_id / status, the entire document (and finally the filename)
    is searched as a fallback.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("backfill: cannot read %s: %s", path, exc)
        return None

    header = _parse_header(text)
    spec_id = (
        _pick(header.fields, _SPEC_ID_KEYS)
        or header.title_id
        or _find_spec_id_in_body(text)
        or _spec_id_from_filename(path)
    )
    status = _pick_status(header.fields) or _find_status_in_body(text)

    if not spec_id:
        logger.warning("backfill: skipping %s (no spec_id found)", path)
        return None
    if status is None:
        logger.warning("backfill: skipping %s (no parseable status)", path)
        return None

    started_date = _pick_date(header.fields, _STARTED_KEYS)
    finished_date = _pick_date(header.fields, _FINISHED_KEYS)

    # If only one side has a date, mirror it to started_at; if neither has one,
    # fall back to the file's mtime (already on disk, anonymized to day level).
    if started_date is None and finished_date is not None:
        started_date = finished_date
    if started_date is None:
        started_date = _file_mtime_date(path)

    failure_category = _pick_failure_category(header.fields)

    return DispatchRecord(
        spec_id=spec_id,
        started_at=_midnight_utc(started_date),
        finished_at=_midnight_utc(finished_date) if finished_date else None,
        status=status,
        attempts=1,
        failure_category=failure_category,
        intervention=False,
    )


def _parse_header(text: str) -> _Header:
    """Scan only the header block (title line + ``- **Key**: value`` lines).

    Stops at the second top-level heading (typically a ``## section``) so that
    inline ``- **foo**:`` patterns inside the prose body never bleed in.
    """

    out = _Header()
    heading_count = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading_count += 1
            if heading_count == 1:
                match = _TITLE_RE.match(line)
                if match:
                    out.title_id = match.group("id").strip()
                continue
            break
        match = _HEADER_LINE_RE.match(line)
        if match:
            key = _normalize_key(match.group("key"))
            out.fields[key] = match.group("value").strip()
    return out


def _normalize_key(raw: str) -> str:
    return raw.strip().lower().replace("-", "_").replace(" ", "_")


def _pick(fields: dict[str, str], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = fields.get(key)
        if value:
            return value.strip()
    return None


def _pick_status(fields: dict[str, str]) -> DispatchStatus | None:
    return _coerce_status(_pick(fields, _STATUS_KEYS))


def _coerce_status(raw: str | None) -> DispatchStatus | None:
    """Normalize a free-form status value and map it to a `DispatchStatus`.

    Handles emoji prefixes (``✅ done``), parenthetical suffixes
    (``done (clasp push のみ認証待ち)``, including full-width ``（）``),
    em-dash / dash / comma trails (``done — push 済み``), and the
    ``completed`` / ``complete`` synonyms (mapped to ``done``).
    Unknown values return ``None`` so the caller can keep skipping
    truly opaque files instead of fabricating data.
    """

    if raw is None:
        return None
    normalized = _normalize_status_value(raw)
    if not normalized:
        return None
    try:
        return DispatchStatus(normalized)
    except ValueError:
        return None


_STATUS_SYNONYMS = {
    "completed": "done",
    "complete": "done",
    "完了": "done",
}


def _normalize_status_value(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    # Drop everything after a parenthetical suffix (`(...)` / `（...）`).
    s = re.split(r"[(（]", s, maxsplit=1)[0]
    # Drop trailing prose after an em-dash / en-dash (e.g. `done — push 済み`).
    # Stick to those wide dashes so plausible status values containing a
    # hyphen-minus (`in-progress`) still normalize to a single token.
    s = re.split(r"[—–]", s, maxsplit=1)[0]
    # Strip non-letter decoration from the edges (emoji ✅, punctuation, etc.).
    s = re.sub(r"^[^\w]+", "", s)
    s = re.sub(r"[^\w]+$", "", s)
    s = s.strip().lower()
    return _STATUS_SYNONYMS.get(s, s)


def _find_status_in_body(text: str) -> DispatchStatus | None:
    """Last-resort status scan over the whole document, used when the header
    block has none. Picks the first status-shaped line that normalizes to a
    known `DispatchStatus`; later occurrences (often inside checklists or
    sample blocks) are ignored.
    """

    for line in text.splitlines():
        m = _LOOSE_STATUS_RE.match(line)
        if not m:
            continue
        status = _coerce_status(m.group("value"))
        if status is not None:
            return status
    return None


def _find_spec_id_in_body(text: str) -> str | None:
    """Find a ``spec_NNN`` mention anywhere in the document.

    Used when neither the header field nor the title carries one (e.g. YAML
    frontmatter, em-dash titles like ``# result_034 — ...``). The first hit
    wins; downstream anonymization renumbers anything weird.
    """

    m = _LOOSE_SPEC_ID_RE.search(text)
    if m is None:
        return None
    return f"spec_{m.group(1)}"


def _spec_id_from_filename(path: Path) -> str | None:
    """Fallback ``spec_NNN`` derived from ``result_NNN.md`` filename."""

    m = _FILENAME_NUM_RE.search(path.stem)
    if m is None:
        return None
    return f"spec_{m.group(1)}"


def _pick_failure_category(fields: dict[str, str]) -> FailureCategory | None:
    raw = _pick(fields, _FAILURE_KEYS)
    if raw is None:
        return None
    try:
        return FailureCategory(raw.lower())
    except ValueError:
        logger.warning("backfill: unknown failure_category %r — dropping", raw)
        return None


def _pick_date(fields: dict[str, str], keys: Iterable[str]) -> date | None:
    raw = _pick(fields, keys)
    if raw is None:
        return None
    return _coerce_date(raw)


def _coerce_date(raw: str) -> date | None:
    s = raw.strip()
    if not s:
        return None
    # Strip any trailing time, timezone, or parenthetical annotation.
    head = re.split(r"[T\s(]", s, maxsplit=1)[0]
    for pat in _DATE_PATTERNS:
        try:
            return datetime.strptime(head, pat).date()
        except ValueError:
            continue
    return None


def _midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _file_mtime_date(path: Path) -> date:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()


# --------------------------------------------------------------------------- #
# Per-project ingest + anonymization                                          #
# --------------------------------------------------------------------------- #


def backfill_project(
    project_root: Path,
    *,
    label: str,
    generation: str,
    results_dir: Path | None = None,
) -> RunFile:
    """Walk a project's result files and return an anonymized `RunFile`."""

    project_root = Path(project_root)
    src = (
        Path(results_dir)
        if results_dir is not None
        else project_root / _DEFAULT_OUTBOX_REL
    )

    records: list[DispatchRecord] = []
    if src.is_dir():
        for path in sorted(src.glob("result_*.md")):
            record = parse_result_file(path)
            if record is not None:
                records.append(record)
    else:
        logger.warning("backfill: results directory %s does not exist", src)

    records = _anonymize_spec_ids(records)

    return RunFile(
        version=1,
        saved_at=datetime.now(UTC).isoformat(),
        project=label,
        generation=generation,
        records=records,
    )


def _anonymize_spec_ids(records: Sequence[DispatchRecord]) -> list[DispatchRecord]:
    """Renumber any non-standard spec_id to ``spec_NNN`` so client names don't leak."""

    used: set[int] = set()
    for r in records:
        m = _STANDARD_SPEC_ID_RE.match(r.spec_id)
        if m:
            used.add(int(m.group(1)))

    next_n = (max(used) + 1) if used else 1
    out: list[DispatchRecord] = []
    for r in records:
        if _STANDARD_SPEC_ID_RE.match(r.spec_id):
            out.append(r)
            continue
        while next_n in used:
            next_n += 1
        new_id = f"spec_{next_n:03d}"
        used.add(next_n)
        next_n += 1
        out.append(r.model_copy(update={"spec_id": new_id}))
    return out


# --------------------------------------------------------------------------- #
# Output                                                                       #
# --------------------------------------------------------------------------- #


def write_run_file(
    run: RunFile,
    output_dir: Path,
    *,
    filename: str | None = None,
) -> Path:
    """Serialize `run` to ``output_dir/<slug>.json`` (slug derived from label)."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = f"{_slugify_label(run.project or 'run')}.json"
    path = output_dir / filename
    path.write_text(
        json.dumps(
            run.model_dump(mode="json"),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _slugify_label(label: str) -> str:
    s = label.strip()
    # Replace path separators and shell-unsafe chars; preserve unicode letters.
    s = re.sub(r"[/\\:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s or "run"


# --------------------------------------------------------------------------- #
# Source list loading                                                          #
# --------------------------------------------------------------------------- #


def load_sources_config(path: Path) -> list[BackfillSource]:
    """Load a JSON array of source entries (path / label / generation)."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON array of source entries")

    sources: list[BackfillSource] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}[{index}]: entry must be a JSON object")
        try:
            sources.append(
                BackfillSource(
                    path=Path(entry["path"]),
                    label=str(entry["label"]),
                    generation=str(entry.get("generation", "")),
                    results_dir=(
                        Path(entry["results_dir"])
                        if entry.get("results_dir")
                        else None
                    ),
                )
            )
        except KeyError as exc:
            raise ValueError(
                f"{path}[{index}]: missing required key {exc!s}"
            ) from None
    return sources


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        prog="ccd.backfill",
        description=(
            "Backfill anonymized DispatchRecord runs from historical "
            "result_NNN.md files."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "JSON config file (array of {path,label,generation,results_dir?}). "
            f"If omitted, ./{_DEFAULT_CONFIG_REL} is used if it exists."
        ),
    )
    parser.add_argument(
        "--source",
        action="append",
        nargs=3,
        default=[],
        metavar=("PATH", "LABEL", "GENERATION"),
        help="Add an inline source (may be repeated).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(_DEFAULT_OUTPUT_REL),
        help=f"Where to write run JSON files (default: {_DEFAULT_OUTPUT_REL}/).",
    )

    args = parser.parse_args(argv)
    sources = _collect_sources(args)
    if not sources:
        print(
            "no sources provided — pass --config or --source",
            file=sys.stderr,
        )
        return 2

    for src in sources:
        run = backfill_project(
            src.path,
            label=src.label,
            generation=src.generation,
            results_dir=src.results_dir,
        )
        out = write_run_file(run, args.output_dir)
        print(f"wrote {out} ({len(run.records)} records, label={src.label!r})")
    return 0


def _collect_sources(args: argparse.Namespace) -> list[BackfillSource]:
    sources: list[BackfillSource] = []
    config_path = args.config
    if config_path is None and not args.source and _DEFAULT_CONFIG_REL.exists():
        config_path = _DEFAULT_CONFIG_REL
    if config_path is not None:
        sources.extend(load_sources_config(config_path))
    for path_s, label, generation in args.source:
        sources.append(
            BackfillSource(path=Path(path_s), label=label, generation=generation)
        )
    return sources


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
