"""ccd discover --channel adversarial — adversarial-input discovery (spec_015).

Discovery channel #2 of v2 Phase 1. The mutation channel (spec_013) finds
gaps in CCD's *tests*. This channel finds gaps in CCD's *input robustness*:
realistic broken inputs (spec / result / run JSON) that make a CCD parser
crash with a traceback instead of cleanly rejecting them.

The catalog is intentionally a *curated, deterministic fixed list* — not a
random fuzzer. Each entry models one way a real CCD input has plausibly
ended up broken on disk (truncated mid-write, mojibake, accidental binary,
malformed JSON, type-mismatched fields, …). Same set, same order, every
run.

graceful vs ungraceful
----------------------
For each (parser × case) the run records one outcome:

- **graceful** = the parser either *returned* without raising, or raised
  an exception from a small allowlist that means "I cleanly recognized
  this input as malformed" (``ValueError``, ``pydantic.ValidationError``,
  ``json.JSONDecodeError``, ``FileNotFoundError``). CCD's existing
  parsers already encode their structural errors with these.
- **ungraceful** = anything else (``AttributeError``, ``KeyError``,
  ``UnicodeDecodeError``, ``RecursionError``, …). These are the
  "findings" — the channel's reason for existing. Phase 1 surfaces them
  in the discovery report; Phase 2 (eventually) translates them into
  fix-spec seeds.

The classification is explicit and deterministic: same input → same
verdict, every time.

Isolation
---------
This channel is in-process — no subprocess, no mutmut, no git, no
isolated clone (spec_014 was solving the mutation channel's *write*
problem; CCD's parsers are pure reads, so the spec_014 machinery is
overkill here). Adversarial fixtures are written into a fresh
``tempfile.TemporaryDirectory`` and wiped on exit. The live repo only
ever receives the discovery report (same destination as the mutation
channel: ``_ai_workspace/discover/discover_NNN.{md,json}``).

Hang detection is *not* attempted here — see Open questions in result_015.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ccd.discover import DEFAULT_DISCOVER_DIR_REL
from ccd.protocol import parse_result, parse_spec
from ccd.run_writer import load_records, reconcile_run_file

# --------------------------------------------------------------------------- #
# Allowlist — what counts as graceful rejection
# --------------------------------------------------------------------------- #

# CCD's parsers encode their structural-error cases with these exact types:
#  - parse_spec / parse_result raise ValueError on bad heading / missing
#    header / unknown enum value.
#  - load_records / reconcile_run_file raise json.JSONDecodeError (a
#    ValueError subclass) on bad JSON, ValueError on wrong shape, and
#    pydantic.ValidationError on bad record fields.
#  - FileNotFoundError is here because pathlib.read_text raises it on a
#    missing path; an adversarial fixture never goes missing, but a future
#    parser that legitimately propagates this should still classify as
#    graceful.
GRACEFUL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ValueError,
    ValidationError,
    FileNotFoundError,
)

# UnicodeError + subclasses (UnicodeDecodeError / UnicodeEncodeError) are
# technically ValueError subclasses, so a naive isinstance check against
# the allowlist would let them slip through as "graceful". But the spec
# (§2-2) names "未処理の UnicodeDecodeError" as a canonical example of
# *ungraceful* — and intuitively it is: the codec layer is below CCD's
# parser, so a UnicodeDecodeError leak means the parser never got to
# inspect the input. We force these to ungraceful with a separate, more-
# specific override that's checked before the allowlist.
UNGRACEFUL_OVERRIDES: tuple[type[BaseException], ...] = (
    UnicodeError,
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdversarialCase:
    """One broken-input fixture.

    ``content`` is ``bytes`` (not ``str``) so the catalog can encode
    invalid-UTF-8 / PNG-header / null-byte cases without coercion.
    """

    name: str
    description: str
    content: bytes


@dataclass(frozen=True)
class AdversarialFinding:
    """An ungraceful (parser × case) outcome — the channel's reason for
    existing. ``exception_message`` is truncated so the report stays
    compact."""

    parser: str
    case_name: str
    exception_type: str
    exception_message: str


@dataclass(frozen=True)
class AdversarialSummary:
    """Deterministic facts about an adversarial run.

    Same input → same numbers. The markdown report quotes these counts
    directly rather than recomputing from the findings list.
    """

    parsers: tuple[str, ...]
    cases_total: int
    evaluations_total: int  # parsers × cases
    graceful_total: int
    ungraceful_total: int
    graceful_by_parser: dict[str, int]
    ungraceful_by_parser: dict[str, int]
    ungraceful_by_exception_type: dict[str, int]


@dataclass
class AdversarialResult:
    success: bool
    report_md_path: Path | None
    report_json_path: Path | None
    summary: AdversarialSummary
    findings: list[AdversarialFinding]
    halt_reason: str = ""


# --------------------------------------------------------------------------- #
# Target parsers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Parser:
    """One target callable plus its display name. Tests can substitute a
    dummy parser to exercise the classifier without touching CCD's real
    parsers."""

    name: str
    fn: Callable[[Path], object]


def default_parsers() -> tuple[_Parser, ...]:
    """The CCD parsers this channel observes. spec_015 §2-1.

    ``reconcile_run_file`` is included even though it *writes* on success
    — we feed it a tmp file, so its write happens inside the disposable
    fixture tree, never in the live repo.
    """

    return (
        _Parser("ccd.protocol.parse_spec", parse_spec),
        _Parser("ccd.protocol.parse_result", parse_result),
        _Parser("ccd.run_writer.load_records", load_records),
        _Parser("ccd.run_writer.reconcile_run_file", reconcile_run_file),
    )


# --------------------------------------------------------------------------- #
# Curated case catalog — spec_015 §2-3
# --------------------------------------------------------------------------- #


def default_cases() -> tuple[AdversarialCase, ...]:
    """Fixed catalog of realistic broken inputs.

    Each case names one way a CCD input file has plausibly ended up
    broken (truncated mid-write, mojibake, accidental binary blob,
    malformed JSON, missing required field, …). The catalog is *fixed*
    and *ordered*; the run is fully deterministic.
    """

    long_value = b"x" * 256_000  # ~256 KiB — realistic upper bound, not a memory bomb.

    return (
        AdversarialCase(
            name="01_empty_file",
            description="Zero-byte file — process never wrote anything before exiting.",
            content=b"",
        ),
        AdversarialCase(
            name="02_whitespace_only",
            description="Only spaces / tabs / blank lines — an empty edit saved.",
            content=b"   \n\n\t  \n",
        ),
        AdversarialCase(
            name="03_truncated_spec_mid_body",
            description="Spec file cut off mid-line — looks like a crashed editor save.",
            content=b"# spec_009: a partly-written title\n\nbody starts here then the file en",
        ),
        AdversarialCase(
            name="04_truncated_json_mid_value",
            description="Run JSON cut off in the middle of a string value.",
            content=b'{\n  "version": 1,\n  "saved_at": "2026-05-24T00:00:0',
        ),
        AdversarialCase(
            name="05_invalid_utf8_bytes",
            description="Bytes that are not valid UTF-8 anywhere in the file.",
            content=b"# spec_001: title\n\n\xff\xfe\x80\x81 garbage \xc3\x28 still garbage\n",
        ),
        AdversarialCase(
            name="06_utf8_bom_prefix",
            description="UTF-8 byte-order mark prepended to an otherwise valid spec.",
            content=b"\xef\xbb\xbf# spec_001: title\n\nbody after BOM\n",
        ),
        AdversarialCase(
            name="07_null_bytes_in_middle",
            description="Embedded NUL bytes in an otherwise text file.",
            content=b"# spec_001: title\n\nbody starts\x00\x00\x00 then ends\n",
        ),
        AdversarialCase(
            name="08_spec_missing_title_heading",
            description="No `# id: title` line — only body content.",
            content=b"This file has no top-level heading at all.\nJust prose.\n",
        ),
        AdversarialCase(
            name="09_result_missing_status_header",
            description="result_NNN.md shape missing the required `- **Status**:` line.",
            content=(
                b"# result_001\n\n- **Spec**: spec_001\n\nbody without status line\n"
            ),
        ),
        AdversarialCase(
            name="10_result_invalid_status_value",
            description="result file with a bogus DispatchStatus enum value.",
            content=(
                b"# result_001\n\n- **Spec**: spec_001\n"
                b"- **Status**: not_a_status\n\nbody\n"
            ),
        ),
        AdversarialCase(
            name="11_json_trailing_garbage",
            description="Valid JSON followed by extra non-JSON bytes after the closing brace.",
            content=b'{"version": 1, "records": []}\ngarbage after end\n',
        ),
        AdversarialCase(
            name="12_json_unclosed_brace",
            description="JSON object without the closing brace.",
            content=b'{"version": 1, "records": [',
        ),
        AdversarialCase(
            name="13_json_records_not_a_list",
            description="Run JSON whose `records` field is a string instead of a list.",
            content=b'{"version": 1, "records": "oops"}\n',
        ),
        AdversarialCase(
            name="14_json_record_field_type_mismatch",
            description="Run JSON whose record has a numeric `started_at` (expected ISO string).",
            content=(
                b'{"version": 1, "records": [{"spec_id": "spec_001", '
                b'"started_at": 12345, "status": "done", "attempts": 1, '
                b'"intervention": false}]}\n'
            ),
        ),
        AdversarialCase(
            name="15_yaml_like_frontmatter_garbage",
            description="A `---`-style frontmatter block with broken YAML before the heading.",
            content=(
                b"---\ntitle: [unclosed\nfoo: bar:\n---\n\n# spec_001: title\n\nbody\n"
            ),
        ),
        AdversarialCase(
            name="16_extremely_long_field_value",
            description="A spec/result heading containing ~256 KiB of one field value.",
            content=b"# spec_001: " + long_value + b"\n\nbody\n",
        ),
        AdversarialCase(
            name="17_png_bytes_as_spec",
            description="Raw PNG header bytes fed to a text parser.",
            content=(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10"
                b"\x00\x00\x00\x10\x08\x06\x00\x00\x00\x1f\xf3\xff\x61"
            ),
        ),
        AdversarialCase(
            name="18_unknown_future_schema_version",
            description="Run JSON with a schema `version` field from the future.",
            content=(
                b'{"version": 99, "saved_at": "2026-05-24T00:00:00Z", "records": []}\n'
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_adversarial(
    *,
    repo: Path,
    discover_dir: Path | None = None,
    parsers: Iterable[_Parser] | None = None,
    cases: Iterable[AdversarialCase] | None = None,
) -> AdversarialResult:
    """Drive one adversarial-input discovery batch end-to-end.

    For every (parser × case) the result is classified deterministically
    as graceful or ungraceful (per :data:`GRACEFUL_EXCEPTIONS`).
    Ungraceful pairs become entries in :attr:`AdversarialResult.findings`
    and in the discovery report. The summary is computed in Python and
    quoted by the report verbatim — same input, same numbers.

    Fixtures are written into a fresh ``tempfile.TemporaryDirectory`` and
    wiped on exit; the live repo only receives the discovery report.

    ``parsers`` / ``cases`` are injectable for tests (e.g. to verify the
    classifier with a dummy parser that raises ``AttributeError``).
    Production callers pass nothing and get the curated defaults.
    """

    repo = Path(repo).resolve()
    discover_root = (
        Path(discover_dir).resolve()
        if discover_dir is not None
        else repo / DEFAULT_DISCOVER_DIR_REL
    )
    discover_root.mkdir(parents=True, exist_ok=True)

    parser_list: tuple[_Parser, ...] = (
        tuple(parsers) if parsers is not None else default_parsers()
    )
    case_list: tuple[AdversarialCase, ...] = (
        tuple(cases) if cases is not None else default_cases()
    )

    findings: list[AdversarialFinding] = []
    graceful_by_parser: dict[str, int] = {p.name: 0 for p in parser_list}
    ungraceful_by_parser: dict[str, int] = {p.name: 0 for p in parser_list}
    ungraceful_by_exception_type: dict[str, int] = {}

    evaluations_total = 0

    with tempfile.TemporaryDirectory(prefix="ccd_adversarial_") as tmp_str:
        tmp = Path(tmp_str)
        # One fresh fixture file per (case × parser) pair so that
        # write-capable parsers (reconcile_run_file) cannot mutate the
        # fixture between parsers.
        for case in case_list:
            for parser in parser_list:
                evaluations_total += 1
                case_path = tmp / f"{case.name}__{_safe_filename(parser.name)}.bin"
                case_path.write_bytes(case.content)
                verdict = _classify(parser, case_path)
                if verdict is None:
                    graceful_by_parser[parser.name] += 1
                else:
                    exc_type, exc_msg = verdict
                    ungraceful_by_parser[parser.name] += 1
                    ungraceful_by_exception_type[exc_type] = (
                        ungraceful_by_exception_type.get(exc_type, 0) + 1
                    )
                    findings.append(
                        AdversarialFinding(
                            parser=parser.name,
                            case_name=case.name,
                            exception_type=exc_type,
                            exception_message=exc_msg,
                        )
                    )

    findings.sort(key=lambda f: (f.parser, f.case_name, f.exception_type))

    summary = AdversarialSummary(
        parsers=tuple(p.name for p in parser_list),
        cases_total=len(case_list),
        evaluations_total=evaluations_total,
        graceful_total=sum(graceful_by_parser.values()),
        ungraceful_total=sum(ungraceful_by_parser.values()),
        graceful_by_parser=dict(sorted(graceful_by_parser.items())),
        ungraceful_by_parser=dict(sorted(ungraceful_by_parser.items())),
        ungraceful_by_exception_type=dict(
            sorted(ungraceful_by_exception_type.items())
        ),
    )

    seq = _next_discover_seq(discover_root)
    md_path = discover_root / f"discover_{seq:03d}.md"
    json_path = discover_root / f"discover_{seq:03d}.json"

    md_path.write_text(
        _render_md(
            seq=seq,
            summary=summary,
            findings=findings,
            case_count=len(case_list),
        ),
        encoding="utf-8",
    )
    json_path.write_text(
        _render_json(summary, findings, case_list),
        encoding="utf-8",
    )

    return AdversarialResult(
        success=True,
        report_md_path=md_path,
        report_json_path=json_path,
        summary=summary,
        findings=findings,
    )


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def _classify(parser: _Parser, fixture: Path) -> tuple[str, str] | None:
    """Return ``None`` if the parser handled ``fixture`` gracefully.

    Otherwise return ``(exception_type_name, truncated_message)`` — an
    ungraceful finding.
    """

    try:
        parser.fn(fixture)
    except UNGRACEFUL_OVERRIDES as exc:
        # Caught BEFORE the graceful clause so UnicodeError (a ValueError
        # subclass) lands here despite the allowlist nominally including
        # ValueError.
        return (type(exc).__name__, _truncate_message(str(exc)))
    except GRACEFUL_EXCEPTIONS:
        return None
    except Exception as exc:
        # ``Exception`` only — KeyboardInterrupt / SystemExit pass through
        # so the operator can still abort an in-flight discovery batch.
        return (type(exc).__name__, _truncate_message(str(exc)))
    else:
        return None


def _truncate_message(msg: str, *, limit: int = 240) -> str:
    msg = msg.strip().replace("\n", " ")
    if len(msg) <= limit:
        return msg
    return msg[:limit] + "…"


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


# --------------------------------------------------------------------------- #
# Numbering (shared with the mutation channel)
# --------------------------------------------------------------------------- #


def _next_discover_seq(discover_dir: Path) -> int:
    """Pick the next ``discover_NNN`` number across all channels.

    The mutation and adversarial channels share the same
    ``_ai_workspace/discover/`` directory and the same numbering scheme,
    so a human reading the directory sees one chronological stream of
    discovery reports regardless of which channel produced them.
    """

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
    summary: AdversarialSummary,
    findings: list[AdversarialFinding],
    case_count: int,
) -> str:
    parts: list[str] = [
        f"# discover_{seq:03d} — ccd adversarial-input discovery",
        "",
        "## 1. 評価母数 (決定的に算出した事実)",
        "",
        "- チャンネル: `adversarial`",
        f"- 対象パーサ: {_render_parsers(summary.parsers)}",
        f"- ケース数: **{summary.cases_total}** 件 (= 固定リスト全件)",
        f"- 評価母数 (パーサ × ケース): **{summary.evaluations_total}** 件",
        f"- graceful (許可リスト例外 or 成功): **{summary.graceful_total}** 件",
        f"- ungraceful (発見): **{summary.ungraceful_total}** 件",
        f"- パーサ別 graceful: {_render_breakdown(summary.graceful_by_parser)}",
        f"- パーサ別 ungraceful: {_render_breakdown(summary.ungraceful_by_parser)}",
        f"- 例外型別 ungraceful: {_render_breakdown(summary.ungraceful_by_exception_type)}",
        "",
        "許可リスト = `ValueError`, `pydantic.ValidationError`, "
        "`json.JSONDecodeError` (ValueError 派生), `FileNotFoundError`。"
        "それ以外の例外漏洩は ungraceful = 発見扱い。"
        "ただし `UnicodeError` 系（`UnicodeDecodeError` 等、技術的には "
        "`ValueError` 派生）は明示的に ungraceful に上書きしている — "
        "spec_015 §2-2 が列挙する未処理 `UnicodeDecodeError` の例に従う。",
        "",
        "**この数値は決定的に Python で算出済み。** "
        "同じ固定リスト・同じパーサ集合なら毎回同じ。"
        "再集計で別の数値が出たら本節を疑うのではなく、"
        "「判断できなかった」と書く（捏造しない）。",
        "",
        "## 2. ungraceful 発見 — パーサが許可リスト外の例外で漏れた箇所",
        "",
        _render_findings(findings),
        "",
        "## 3. graceful (= 発見ではない) の概要",
        "",
        _render_graceful_summary(summary, case_count),
        "",
        "## 4. データから判断できなかったこと",
        "",
        _render_uncertain(summary),
        "",
    ]
    return "\n".join(parts)


def _render_parsers(parsers: tuple[str, ...]) -> str:
    if not parsers:
        return "(none)"
    return ", ".join(f"`{p}`" for p in parsers)


def _render_breakdown(d: dict[str, int]) -> str:
    if not d:
        return "(none)"
    return ", ".join(f"`{k}`={v}" for k, v in d.items())


def _render_findings(findings: list[AdversarialFinding]) -> str:
    if not findings:
        return (
            "_(該当なし — 全評価が許可リスト例外または成功で graceful。"
            "本チャンネルではテスト隙間は発見できなかった。)_"
        )
    by_parser: dict[str, list[AdversarialFinding]] = {}
    for f in findings:
        by_parser.setdefault(f.parser, []).append(f)
    lines: list[str] = []
    for parser_name in sorted(by_parser.keys()):
        items = by_parser[parser_name]
        lines.append(f"### `{parser_name}` ({len(items)})")
        lines.append("")
        for f in sorted(items, key=lambda x: (x.case_name, x.exception_type)):
            lines.append(
                f"- `{f.case_name}` — **{f.exception_type}**: {f.exception_message}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_graceful_summary(summary: AdversarialSummary, case_count: int) -> str:
    if summary.graceful_total == 0:
        return (
            "_(該当なし — graceful 件数 0。"
            "全パーサが全ケースで ungraceful だった可能性がある。)_"
        )
    lines = [
        f"- 全 {summary.evaluations_total} 評価のうち graceful は "
        f"{summary.graceful_total} 件。各パーサが固定リスト {case_count} 件のうち、"
        "許可リスト例外でクリーンに拒絶するか、無事に値を返した。",
        "- パーサ別 graceful: " + _render_breakdown(summary.graceful_by_parser),
    ]
    return "\n".join(lines)


def _render_uncertain(summary: AdversarialSummary) -> str:
    bullets: list[str] = []
    # If we ever extend the classifier with a "couldn't determine" bucket
    # (e.g. hang detection), it goes here. spec_015 §6 explicitly defers
    # hang detection — see Open questions in result_015.
    if not bullets:
        return (
            "_(該当なし — 全 (パーサ × ケース) 評価が graceful / ungraceful "
            "のいずれかに決定的に分類できた。ハング検出は本 spec の対象外で、"
            "in-process タイムアウトを設けない判断のもと、例外漏洩のみを観測する。)_"
        )
    return "\n".join(bullets)


def _render_json(
    summary: AdversarialSummary,
    findings: list[AdversarialFinding],
    cases: Iterable[AdversarialCase],
) -> str:
    payload = {
        "channel": "adversarial",
        "summary": {
            "parsers": list(summary.parsers),
            "cases_total": summary.cases_total,
            "evaluations_total": summary.evaluations_total,
            "graceful_total": summary.graceful_total,
            "ungraceful_total": summary.ungraceful_total,
            "graceful_by_parser": summary.graceful_by_parser,
            "ungraceful_by_parser": summary.ungraceful_by_parser,
            "ungraceful_by_exception_type": summary.ungraceful_by_exception_type,
        },
        "findings": [
            {
                "parser": f.parser,
                "case": f.case_name,
                "exception_type": f.exception_type,
                "exception_message": f.exception_message,
            }
            for f in findings
        ],
        "cases": [
            {"name": c.name, "description": c.description, "bytes": len(c.content)}
            for c in cases
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "AdversarialCase",
    "AdversarialFinding",
    "AdversarialResult",
    "AdversarialSummary",
    "GRACEFUL_EXCEPTIONS",
    "UNGRACEFUL_OVERRIDES",
    "default_cases",
    "default_parsers",
    "run_adversarial",
]
