"""Crash-safe, incremental persistence for run JSON files (spec_010).

`RunWriter` owns one ``--save`` path. Every commit (``write``) replaces the
file atomically via ``os.replace`` so a half-written JSON is never observed
on disk: readers see either the previous full state or the new full state.

`dispatch_one` / `run_chain` write the run file *before* each runner call as
an in-flight marker (`status=RUNNING`, `finished_at=None`) and again after
the runner returns or raises. That way, if the orchestrator dies mid-run,
the surviving record on disk is honest: completed records keep their final
classification, and the in-flight spec is left as a ``RUNNING`` marker that
``reconcile_run_file`` (or the auto-reconcile in ``cli.py``) will later
convert into ``HALTED + INTERRUPTED`` — the truthful "ccd started this
dispatch but never observed it finish".

Carry-forward: if the writer is pointed at a path that already has
``RUNNING`` records from a previous run (a single-orchestrator-v1
invariant: any pre-existing RUNNING is an orphan), they are reconciled in
place and pre-seeded as the first records of the new run's record list, so
re-using the default ``last_run.json`` path does not silently overwrite the
previous orphan.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .models import DispatchRecord, DispatchStatus, FailureCategory

if TYPE_CHECKING:
    from .chain import ChainResult


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Replace ``path`` with the JSON encoding of ``payload`` atomically.

    Writes to a same-directory tempfile then `os.replace`s into place. Same
    filesystem guarantees the rename is atomic; a crash during write leaves
    the previous file untouched (or the new one fully written).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class RunWriter:
    """Owns one run JSON path; supports incremental atomic writes.

    Usage from `dispatch` / `chain`:
        writer = RunWriter(path)
        writer.salvage_orphans()           # carry-forward + reconcile
        for spec in specs:
            writer.start(spec_id, started_at=now)
            try:
                record = dispatch_one(...)
            except Exception:
                record = halted_interrupted(spec_id, started_at=now)
            writer.finish(record)
        writer.attach_chain(chain_result)  # optional
        writer.flush()                     # ensures final write
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._records: list[DispatchRecord] = []
        self._inflight: DispatchRecord | None = None
        self._chain_meta: dict[str, object] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def records(self) -> list[DispatchRecord]:
        return list(self._records)

    def salvage_orphans(self) -> int:
        """Read existing file at ``path`` and carry-forward any RUNNING orphans.

        v1 is single-orchestrator (no parallel dispatch), so any ``RUNNING``
        record present in the file when a new run starts is by definition
        an orphan from a previous process death. We reconcile it to
        ``HALTED + INTERRUPTED`` (without inventing a ``finished_at``) and
        seed it into the new run's record list so re-using the same
        ``--save`` path does not silently overwrite the orphan.

        Returns the number of records carried forward (after reconcile).
        Prints a one-line notice to stderr if any were salvaged.
        """

        if not self._path.exists():
            return 0
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        raw = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return 0

        carry: list[DispatchRecord] = []
        salvaged = 0
        for item in raw:
            try:
                record = DispatchRecord.model_validate(item)
            except Exception:
                continue
            if record.status is DispatchStatus.RUNNING:
                record = record.model_copy(
                    update={
                        "status": DispatchStatus.HALTED,
                        "failure_category": FailureCategory.INTERRUPTED,
                    }
                )
                salvaged += 1
            carry.append(record)

        if salvaged > 0:
            self._records = carry
            self._write()
            print(
                f"salvaged {salvaged} interrupted dispatch(es) from a previous run",
                file=sys.stderr,
            )
        return salvaged

    def start(self, spec_id: str, *, started_at: datetime) -> None:
        """Write an in-flight ``RUNNING`` marker for ``spec_id``."""

        self._inflight = DispatchRecord(
            spec_id=spec_id,
            started_at=started_at,
            finished_at=None,
            status=DispatchStatus.RUNNING,
            attempts=1,
            failure_category=None,
            intervention=False,
        )
        self._write()

    def finish(self, record: DispatchRecord) -> None:
        """Commit the final ``record`` for the in-flight spec."""

        self._records.append(record)
        self._inflight = None
        self._write()

    def attach_chain(self, chain: ChainResult | None) -> None:
        if chain is None:
            self._chain_meta = None
        else:
            self._chain_meta = {
                "success": chain.success,
                "halted_at": chain.halted_at,
                "halt_reason": chain.halt_reason,
                "branches": [step.branch for step in chain.steps],
            }
        self._write()

    def flush(self) -> None:
        self._write()

    # --- internals --------------------------------------------------------

    def _write(self) -> None:
        records_payload = [r.model_dump(mode="json") for r in self._records]
        if self._inflight is not None:
            records_payload.append(self._inflight.model_dump(mode="json"))
        payload: dict[str, object] = {
            "version": 1,
            "saved_at": _now_iso(),
            "records": records_payload,
        }
        if self._chain_meta is not None:
            payload["chain"] = self._chain_meta
        _atomic_write_json(self._path, payload)


def halted_interrupted_record(
    spec_id: str,
    *,
    started_at: datetime,
) -> DispatchRecord:
    """Build a ``HALTED + INTERRUPTED`` record for a spec ccd never finished.

    ``finished_at`` is intentionally left ``None`` so the synthetic duration
    is not fed into ``_duration_stats``. The actual finish time is unknown —
    fabricating one would launder a guess into the metrics.
    """

    return DispatchRecord(
        spec_id=spec_id,
        started_at=started_at,
        finished_at=None,
        status=DispatchStatus.HALTED,
        attempts=1,
        failure_category=FailureCategory.INTERRUPTED,
        intervention=False,
    )


def reconcile_run_file(path: Path) -> int:
    """Reconcile ``RUNNING`` records in ``path`` to ``HALTED + INTERRUPTED``.

    Writes the result atomically. ``finished_at`` is not invented. Returns
    the number of records reconciled.
    """

    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("records", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'records' must be a list")

    new_records: list[dict] = []
    reconciled = 0
    for item in raw:
        try:
            record = DispatchRecord.model_validate(item)
        except Exception:
            new_records.append(item)
            continue
        if record.status is DispatchStatus.RUNNING:
            record = record.model_copy(
                update={
                    "status": DispatchStatus.HALTED,
                    "failure_category": FailureCategory.INTERRUPTED,
                }
            )
            reconciled += 1
        new_records.append(record.model_dump(mode="json"))

    if reconciled == 0:
        return 0

    payload["records"] = new_records
    payload["saved_at"] = _now_iso()
    _atomic_write_json(path, payload)
    return reconciled


def reconcile_path(target: Path) -> tuple[int, int]:
    """Reconcile a single file or every ``*.json`` under a directory.

    Returns ``(files_touched, records_reconciled)``.
    """

    target = Path(target)
    if target.is_file():
        n = reconcile_run_file(target)
        return (1 if n > 0 else 0, n)
    if target.is_dir():
        files_touched = 0
        total_reconciled = 0
        for path in sorted(target.glob("*.json")):
            n = reconcile_run_file(path)
            if n > 0:
                files_touched += 1
                total_reconciled += n
        return files_touched, total_reconciled
    raise FileNotFoundError(target)


def load_records(path: Path) -> Sequence[DispatchRecord]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = payload.get("records", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'records' must be a list")
    return [DispatchRecord.model_validate(item) for item in raw]


def is_running(records: Iterable[DispatchRecord]) -> bool:
    return any(r.status is DispatchStatus.RUNNING for r in records)
