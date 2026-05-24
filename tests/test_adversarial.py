"""Tests for `ccd.adversarial:run_adversarial` + `ccd discover --channel adversarial`.

The channel itself is in-process — no subprocess, no mutmut, no git — so
these tests run directly against the real CCD parsers. Fixtures live in a
``tempfile.TemporaryDirectory`` that ``run_adversarial`` owns; the only
artifact that lands in the test's ``repo`` fixture is the discovery
report (``_ai_workspace/discover/discover_NNN.{md,json}``).
"""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ccd import cli
from ccd.adversarial import (
    GRACEFUL_EXCEPTIONS,
    AdversarialCase,
    AdversarialFinding,
    AdversarialResult,
    AdversarialSummary,
    default_cases,
    default_parsers,
    run_adversarial,
)
from ccd.adversarial import _Parser as _AdvParser
from ccd.discover import (
    CHANNEL_ADVERSARIAL,
    CHANNEL_MUTATION,
    DEFAULT_DISCOVER_DIR_REL,
    FakeMutationRunner,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "_ai_workspace").mkdir(parents=True)
    return tmp_path


# --------------------------------------------------------------------------- #
# Default catalog — fixed, curated, deterministic
# --------------------------------------------------------------------------- #


def test_default_cases_are_a_fixed_curated_list() -> None:
    cases = default_cases()
    # spec_015 §2-3 — "おおむね15ケース", curated, deterministic.
    assert len(cases) >= 15
    # Names are unique (so tmp filenames don't collide) and ordered.
    names = [c.name for c in cases]
    assert len(set(names)) == len(names)
    assert names == sorted(names), "case list must be in stable lexicographic order"
    # Each case is non-trivial — has a name, a description, and bytes.
    for c in cases:
        assert c.name
        assert c.description
        assert isinstance(c.content, bytes)


def test_default_cases_cover_the_realistic_break_modes() -> None:
    """The catalog must include at least one of each modeled break mode.

    Spec_015 §2-3 enumerates the realistic ways a CCD input can be
    broken on disk; we verify the catalog actually carries them, not
    just that 15+ cases exist.
    """

    cases = {c.name: c for c in default_cases()}
    needles = [
        "empty",          # 01 — zero-byte file
        "whitespace",     # 02 — whitespace only
        "truncated_spec", # 03 — truncated mid-write (spec)
        "truncated_json", # 04 — truncated mid-write (JSON)
        "invalid_utf8",   # 05 — invalid UTF-8 bytes
        "bom",            # 06 — UTF-8 BOM prefix
        "null_bytes",     # 07 — embedded NUL bytes
        "missing_title",  # 08 — spec without title heading
        "missing_status", # 09 — result without **Status**
        "invalid_status", # 10 — result with bogus enum
        "trailing",       # 11 — JSON with trailing garbage
        "unclosed",       # 12 — JSON without closing brace
        "records_not_a_list",  # 13 — records non-list
        "type_mismatch",  # 14 — JSON record field type mismatch
        "yaml",           # 15 — broken YAML-like frontmatter
        "long",           # 16 — extremely long field value
        "png",            # 17 — PNG bytes as spec
        "future_schema",  # 18 — unknown future schema version
    ]
    matched = [n for n in needles if any(n in name for name in cases.keys())]
    # All 18 break modes must be covered (the list above is the curated set).
    missing = set(needles) - set(matched)
    assert not missing, f"missing break modes from catalog: {missing}"


def test_default_parsers_target_ccd_real_parsers() -> None:
    """The catalog is fed into CCD's actual parser functions."""

    names = {p.name for p in default_parsers()}
    assert "ccd.protocol.parse_spec" in names
    assert "ccd.protocol.parse_result" in names
    assert "ccd.run_writer.load_records" in names
    # `reconcile_run_file` included per spec_015 §6 CC judgement — it
    # exercises a read-then-write path that ``load_records`` does not.
    assert "ccd.run_writer.reconcile_run_file" in names


# --------------------------------------------------------------------------- #
# Classification — graceful vs ungraceful
# --------------------------------------------------------------------------- #


def test_value_error_is_graceful(repo: Path) -> None:
    """ValueError is in the allowlist (CCD parsers' canonical rejection)."""

    def raises_value(path: Path) -> object:
        raise ValueError("clean rejection")

    parser = _AdvParser("test.raises_value", raises_value)
    case = AdversarialCase(name="anything", description="x", content=b"x")

    result = run_adversarial(repo=repo, parsers=[parser], cases=[case])

    assert result.success
    assert result.summary.graceful_total == 1
    assert result.summary.ungraceful_total == 0
    assert result.findings == []


def test_json_decode_error_is_graceful(repo: Path) -> None:
    """json.JSONDecodeError (ValueError subclass) is graceful too."""

    def raises_json(path: Path) -> object:
        import json as _json

        _json.loads("not json")  # raises JSONDecodeError
        return None

    parser = _AdvParser("test.raises_json", raises_json)
    case = AdversarialCase(name="anything", description="x", content=b"x")

    result = run_adversarial(repo=repo, parsers=[parser], cases=[case])

    assert result.summary.ungraceful_total == 0
    assert result.summary.graceful_total == 1


def test_validation_error_is_graceful(repo: Path) -> None:
    """pydantic.ValidationError is in the allowlist — CCD's record parsers
    raise it on bad shapes."""

    from pydantic import BaseModel

    class M(BaseModel):
        x: int

    def raises_validation(path: Path) -> object:
        M(x="not_an_int")  # type: ignore[arg-type]
        return None

    parser = _AdvParser("test.raises_validation", raises_validation)
    case = AdversarialCase(name="anything", description="x", content=b"x")

    result = run_adversarial(repo=repo, parsers=[parser], cases=[case])

    assert result.summary.ungraceful_total == 0
    assert result.summary.graceful_total == 1


def test_attribute_error_is_ungraceful_and_becomes_a_finding(repo: Path) -> None:
    """AttributeError leaking from a parser is the canonical 'ungraceful crash'."""

    def raises_attr(path: Path) -> object:
        none = None
        return none.foo  # type: ignore[attr-defined]

    parser = _AdvParser("test.raises_attr", raises_attr)
    case = AdversarialCase(name="bad_case", description="x", content=b"x")

    result = run_adversarial(repo=repo, parsers=[parser], cases=[case])

    assert result.summary.ungraceful_total == 1
    assert result.summary.graceful_total == 0
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.parser == "test.raises_attr"
    assert finding.case_name == "bad_case"
    assert finding.exception_type == "AttributeError"


@pytest.mark.parametrize(
    "exc_cls,exc_name",
    [
        (KeyError, "KeyError"),
        (IndexError, "IndexError"),
        (TypeError, "TypeError"),
        (RecursionError, "RecursionError"),
        (UnicodeDecodeError, "UnicodeDecodeError"),
    ],
)
def test_non_allowlist_exceptions_are_all_ungraceful(
    repo: Path, exc_cls: type[Exception], exc_name: str
) -> None:
    """Every realistic 'leaked Python exception' is classified as ungraceful."""

    def raiser(path: Path) -> object:
        # UnicodeDecodeError needs a payload — easiest path is to actually
        # decode invalid bytes.
        if exc_cls is UnicodeDecodeError:
            b"\xff\xfe".decode("utf-8")
        raise exc_cls("oops")

    parser = _AdvParser("test.raiser", raiser)
    case = AdversarialCase(name="anything", description="x", content=b"x")

    result = run_adversarial(repo=repo, parsers=[parser], cases=[case])

    assert result.summary.ungraceful_total == 1
    assert result.summary.graceful_total == 0
    assert result.findings[0].exception_type == exc_name


def test_parser_success_is_graceful(repo: Path) -> None:
    """A parser that accepts the input cleanly (no exception) is graceful."""

    def accepts_anything(path: Path) -> object:
        return path.read_bytes()

    parser = _AdvParser("test.accepts", accepts_anything)
    case = AdversarialCase(name="anything", description="x", content=b"hello")

    result = run_adversarial(repo=repo, parsers=[parser], cases=[case])

    assert result.summary.ungraceful_total == 0
    assert result.summary.graceful_total == 1
    assert result.findings == []


def test_allowlist_includes_documented_types() -> None:
    """The allowlist is exactly what the spec calls out (§2-2)."""

    from json import JSONDecodeError

    from pydantic import ValidationError as PydanticValidationError

    # JSONDecodeError is a ValueError subclass — handled by isinstance.
    assert issubclass(JSONDecodeError, ValueError)
    # ValidationError may or may not subclass ValueError depending on
    # pydantic minor version; the allowlist explicitly names it.
    assert PydanticValidationError in GRACEFUL_EXCEPTIONS or any(
        issubclass(PydanticValidationError, t) for t in GRACEFUL_EXCEPTIONS
    )
    assert ValueError in GRACEFUL_EXCEPTIONS
    assert FileNotFoundError in GRACEFUL_EXCEPTIONS


# --------------------------------------------------------------------------- #
# Integration — real CCD parsers, real catalog
# --------------------------------------------------------------------------- #


def test_full_catalog_runs_against_real_parsers_without_crashing(
    repo: Path,
) -> None:
    """The default catalog × default parsers smoke test.

    This is the spec_015 §2-5 integration test: the curated fixed list
    is fed to CCD's real parsers in-process. The runner itself must
    finish — even if individual (parser × case) pairs are ungraceful.
    """

    result = run_adversarial(repo=repo)

    assert isinstance(result, AdversarialResult)
    assert result.success
    assert isinstance(result.summary, AdversarialSummary)
    # parsers × cases is the full evaluation surface.
    assert result.summary.evaluations_total == (
        result.summary.cases_total * len(result.summary.parsers)
    )
    assert (
        result.summary.graceful_total + result.summary.ungraceful_total
        == result.summary.evaluations_total
    )
    # The PNG and invalid-UTF-8 cases hit ``path.read_text(encoding="utf-8")``
    # before any structural logic — that surfaces UnicodeDecodeError, which
    # is *not* in CCD's allowlist. So we should see at least one finding.
    finding_types = {f.exception_type for f in result.findings}
    assert "UnicodeDecodeError" in finding_types, (
        f"expected UnicodeDecodeError in findings, got {finding_types}"
    )


def test_full_catalog_finds_unicode_decode_for_all_real_parsers(repo: Path) -> None:
    """Every CCD parser starts with ``read_text(encoding='utf-8')`` and so
    leaks UnicodeDecodeError on the invalid-UTF-8 and PNG cases. The
    finding count should be at least 1 per parser per bad-byte case."""

    result = run_adversarial(repo=repo)

    # Group findings by (parser, case).
    by_pair = {(f.parser, f.case_name) for f in result.findings}
    parser_names = set(result.summary.parsers)

    # The PNG case is the cleanest invalid-UTF-8 fixture — it should
    # produce an ungraceful finding for every real parser.
    png_case = next(
        c.name for c in default_cases() if "png" in c.name
    )
    invalid_utf8_case = next(
        c.name for c in default_cases() if "invalid_utf8" in c.name
    )
    for parser_name in parser_names:
        assert (parser_name, png_case) in by_pair, (
            f"{parser_name} did not leak on PNG bytes — has the parser "
            f"started catching UnicodeDecodeError? Update the allowlist or "
            f"the catalog."
        )
        assert (parser_name, invalid_utf8_case) in by_pair


# --------------------------------------------------------------------------- #
# Determinism + summary
# --------------------------------------------------------------------------- #


def test_summary_is_deterministic_across_runs(repo: Path) -> None:
    """Same fixed catalog + same parsers → same counts, every time."""

    result_a = run_adversarial(repo=repo)
    result_b = run_adversarial(repo=repo)

    # The reports are written to discover_001 / discover_002 respectively,
    # but the *factual summary* must match exactly.
    assert result_a.summary == result_b.summary
    # And the ungraceful findings must match by (parser, case, type).
    fa = sorted((f.parser, f.case_name, f.exception_type) for f in result_a.findings)
    fb = sorted((f.parser, f.case_name, f.exception_type) for f in result_b.findings)
    assert fa == fb


def test_findings_are_sorted_deterministically(repo: Path) -> None:
    """The findings list is sorted (parser, case, exception type)."""

    result = run_adversarial(repo=repo)
    sorted_pairs = sorted(
        (f.parser, f.case_name, f.exception_type) for f in result.findings
    )
    actual_pairs = [(f.parser, f.case_name, f.exception_type) for f in result.findings]
    assert actual_pairs == sorted_pairs


# --------------------------------------------------------------------------- #
# Report contents
# --------------------------------------------------------------------------- #


def test_report_md_carries_evaluation_count_and_findings(repo: Path) -> None:
    result = run_adversarial(repo=repo)

    assert result.report_md_path is not None
    md = result.report_md_path.read_text(encoding="utf-8")

    # §1 — 評価母数: count + breakdown surfaced verbatim.
    assert "評価母数" in md
    assert f"**{result.summary.evaluations_total}** 件" in md
    assert "graceful" in md
    assert "ungraceful" in md
    assert "許可リスト" in md

    # §2 — ungraceful 発見: each finding's (parser, case, exception type) appears.
    for f in result.findings:
        assert f.parser in md
        assert f.case_name in md
        assert f.exception_type in md

    # §4 — 判断できなかったこと: present even when empty.
    assert "判断できなかったこと" in md


def test_report_json_has_structured_findings(repo: Path) -> None:
    result = run_adversarial(repo=repo)

    assert result.report_json_path is not None
    payload = json.loads(result.report_json_path.read_text(encoding="utf-8"))
    assert payload["channel"] == "adversarial"
    assert payload["summary"]["evaluations_total"] == result.summary.evaluations_total
    assert payload["summary"]["graceful_total"] == result.summary.graceful_total
    assert payload["summary"]["ungraceful_total"] == result.summary.ungraceful_total
    assert payload["summary"]["parsers"] == list(result.summary.parsers)
    assert len(payload["findings"]) == len(result.findings)
    assert all(
        {"parser", "case", "exception_type", "exception_message"} <= set(f.keys())
        for f in payload["findings"]
    )
    # The case list is enumerated structurally — for downstream consumption.
    assert len(payload["cases"]) == result.summary.cases_total


def test_report_lands_in_live_repo_discover_dir(repo: Path) -> None:
    result = run_adversarial(repo=repo)

    assert result.report_md_path is not None
    assert result.report_json_path is not None
    assert result.report_md_path.parent == (repo / DEFAULT_DISCOVER_DIR_REL).resolve()
    assert result.report_md_path.name == "discover_001.md"
    assert result.report_json_path.name == "discover_001.json"


def test_discover_numbering_shared_with_mutation_channel(repo: Path) -> None:
    """Both channels share the discover_NNN counter."""

    discover_dir = repo / DEFAULT_DISCOVER_DIR_REL
    discover_dir.mkdir(parents=True, exist_ok=True)
    (discover_dir / "discover_001.md").write_text("# preexisting\n", encoding="utf-8")
    (discover_dir / "discover_001.json").write_text("{}\n", encoding="utf-8")

    result = run_adversarial(repo=repo)

    assert result.report_md_path is not None
    assert result.report_md_path.name == "discover_002.md"


# --------------------------------------------------------------------------- #
# Isolation — fixtures live in tmpdir, never in the live repo
# --------------------------------------------------------------------------- #


def test_fixtures_are_not_written_into_live_repo(repo: Path) -> None:
    """spec_015 §2-3 / §3 — the adversarial fixtures are written to a
    private ``tempfile.TemporaryDirectory``, never to the live repo.
    """

    # Snapshot the live repo before the run.
    discover_dir = repo / DEFAULT_DISCOVER_DIR_REL
    before = set(_walk_files(repo))

    run_adversarial(repo=repo)

    after = set(_walk_files(repo))
    added = after - before

    # The only artifacts added to the live repo are the discovery report
    # files (and the discover_dir itself, if it didn't exist).
    for path in added:
        # The path is relative to repo/.
        as_str = str(path)
        # Either a discover_NNN.{md,json} file under the discover dir, or
        # the discover dir itself.
        assert (
            as_str == str(DEFAULT_DISCOVER_DIR_REL)
            or as_str.startswith(str(DEFAULT_DISCOVER_DIR_REL) + os.sep)
        ), f"unexpected file added to live repo: {path}"
        if path.suffix in (".md", ".json"):
            assert path.name.startswith("discover_"), (
                f"unexpected file in discover dir: {path}"
            )
    # And critically — no fixture .bin / temporary files leaked through.
    leaked_fixtures = [p for p in added if str(p).endswith(".bin")]
    assert leaked_fixtures == [], f"fixture files leaked into live repo: {leaked_fixtures}"
    # discover_dir exists after the run.
    assert discover_dir.exists()


def test_tmp_fixture_dir_is_cleaned_up_on_exit(repo: Path) -> None:
    """After ``run_adversarial`` returns, the tmp fixture tree is gone."""

    import tempfile as _tempfile

    before = set(Path(_tempfile.gettempdir()).glob("ccd_adversarial_*"))
    run_adversarial(repo=repo)
    after = set(Path(_tempfile.gettempdir()).glob("ccd_adversarial_*"))

    # No new ccd_adversarial_* leftovers.
    assert after - before == set()


# --------------------------------------------------------------------------- #
# CLI integration
# --------------------------------------------------------------------------- #


def test_cli_discover_default_channel_is_mutation(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """spec_013 挙動 不変 — no ``--channel`` flag means mutation channel."""

    runner = FakeMutationRunner(mutants=[])
    rc = cli.main(["discover", "--repo", str(repo)], mutation_runner=runner)

    assert rc == 0
    # The mutation runner was invoked (i.e. the mutation channel ran).
    assert len(runner.calls) == 1
    out = capsys.readouterr().out
    assert "factual summary" in out
    # The mutation summary uses mutmut-specific keys.
    assert "mutants=" in out


def test_cli_discover_explicit_channel_mutation(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Explicit ``--channel mutation`` matches the default behavior."""

    runner = FakeMutationRunner(mutants=[])
    rc = cli.main(
        ["discover", "--repo", str(repo), "--channel", CHANNEL_MUTATION],
        mutation_runner=runner,
    )

    assert rc == 0
    assert len(runner.calls) == 1
    out = capsys.readouterr().out
    assert "mutants=" in out


def test_cli_discover_channel_adversarial_end_to_end(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ccd discover --channel adversarial`` writes a discover_NNN report."""

    rc = cli.main(
        ["discover", "--repo", str(repo), "--channel", CHANNEL_ADVERSARIAL]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "discovery report" in out
    assert "discover_001.md" in out
    assert "factual summary" in out
    # Adversarial summary uses adversarial-specific keys.
    assert "parsers=" in out
    assert "cases=" in out
    assert "evaluations=" in out

    md_path = repo / DEFAULT_DISCOVER_DIR_REL / "discover_001.md"
    json_path = repo / DEFAULT_DISCOVER_DIR_REL / "discover_001.json"
    assert md_path.exists()
    assert json_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["channel"] == "adversarial"


def test_cli_discover_rejects_unknown_channel(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse ``choices`` rejects garbage at the CLI boundary."""

    with pytest.raises(SystemExit):
        cli.main(["discover", "--repo", str(repo), "--channel", "bogus"])


def test_cli_discover_paths_flag_is_ignored_for_adversarial(
    repo: Path,
) -> None:
    """``--paths`` is mutation-only; it must not break the adversarial run."""

    rc = cli.main(
        [
            "discover",
            "--repo",
            str(repo),
            "--channel",
            CHANNEL_ADVERSARIAL,
            "--paths",
            "ccd/dispatch.py",
        ]
    )

    assert rc == 0
    assert (repo / DEFAULT_DISCOVER_DIR_REL / "discover_001.md").exists()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _walk_files(root: Path) -> list[Path]:
    """List every file/dir under ``root`` as a path relative to ``root``."""

    out: list[Path] = []
    for child in root.rglob("*"):
        out.append(child.relative_to(root))
    return out


def test_finding_dataclass_is_frozen() -> None:
    f = AdversarialFinding(
        parser="x",
        case_name="y",
        exception_type="Z",
        exception_message="m",
    )
    with pytest.raises(FrozenInstanceError):
        f.parser = "other"  # type: ignore[misc]


def test_summary_dataclass_is_frozen() -> None:
    s = AdversarialSummary(
        parsers=(),
        cases_total=0,
        evaluations_total=0,
        graceful_total=0,
        ungraceful_total=0,
        graceful_by_parser={},
        ungraceful_by_parser={},
        ungraceful_by_exception_type={},
    )
    with pytest.raises(FrozenInstanceError):
        s.cases_total = 5  # type: ignore[misc]
