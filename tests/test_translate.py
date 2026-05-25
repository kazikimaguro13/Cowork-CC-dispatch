"""Tests for ``ccd/translate.py`` (spec_022, v0.12.0, v2 Phase 2).

The translator is the deterministic, AI-free seam that hands a finding
to the autonomous-fix loop. These tests pin:

- One mutation survivor in → one ``spec_auto_NNN.md`` out.
- The generated spec contains every element template A demands: title,
  context, mutmut quote, verbatim constraints, verification gate, allowed
  file set declaration, output destination, and the §7 provenance block.
- The verbatim constraint phrases (test-only / existing tests immutable /
  no skip markers / deterministic / allowed-set) appear in the body
  unchanged — they are the "侵食不能な剛体" the spec calls out.
- Numbering increments across multiple translations into the same inbox.
- The translator is deterministic: same finding + same ``today`` produces
  byte-identical bodies into two fresh inboxes. No AI call.
- Findings that don't fit template A (wrong channel / wrong status /
  empty file or mutation / zero line) are halted with
  ``success=False`` + a halt reason; no file is written.
- The generated body parses as a valid CCD spec via ``parse_spec``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ccd.protocol import parse_spec
from ccd.translate import (
    Finding,
    TranslateResult,
    translate_finding,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty repo-shaped tmp directory."""
    (tmp_path / "_ai_workspace" / "bridge" / "inbox").mkdir(parents=True)
    (tmp_path / "_ai_workspace" / "bridge" / "outbox").mkdir(parents=True)
    return tmp_path


def _survivor_finding(
    *,
    file: str = "ccd/protocol.py",
    line: int = 46,
    mutation: str = "continue → break",
    signature: str | None = None,
    source_report: str = "discover_003.json",
) -> Finding:
    sig = signature if signature is not None else f"{file}:{line}:{mutation}"
    return Finding(
        channel="mutation",
        file=file,
        line=line,
        mutation=mutation,
        status="survived",
        signature=sig,
        source_report=source_report,
    )


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_mutation_survivor_produces_spec_auto_file(repo: Path) -> None:
    result = translate_finding(
        _survivor_finding(),
        repo=repo,
        today=date(2026, 5, 25),
    )

    assert result.success is True
    assert result.template == "A"
    assert result.halt_reason == ""
    assert result.spec_auto_id == "spec_auto_001"
    assert result.spec_auto_path is not None
    assert result.spec_auto_path.exists()
    assert result.spec_auto_path.name == "spec_auto_001.md"
    assert result.spec_auto_path.parent == repo / "_ai_workspace" / "bridge" / "inbox"


def test_spec_auto_body_includes_every_template_a_element(repo: Path) -> None:
    finding = _survivor_finding(
        file="ccd/protocol.py",
        line=46,
        mutation="continue → break",
        source_report="discover_003.json",
    )
    result = translate_finding(finding, repo=repo, today=date(2026, 5, 25))
    body = result.spec_auto_path.read_text(encoding="utf-8")

    # Title element — file:line is in the heading, with the template hint.
    assert body.startswith(
        "# spec_auto_001: `ccd/protocol.py:46` の生存改変を殺す（テンプレ A）"
    )

    # Header block has Created/Status/Type/Channel/Template/Source signature.
    assert "- **Created**: 2026-05-25" in body
    assert "- **Status**: pending" in body
    assert "- **Channel**: mutation" in body
    assert "- **Template**: A (test-only)" in body
    assert "- **Source signature**: `ccd/protocol.py:46:continue → break`" in body
    assert "- **Source report**: `discover_003.json`" in body
    assert "- **Author**: ccd translate" in body

    # § 1 文脈 — file:line, mutation quoted, mutmut evidence anchor.
    assert "## 1. 文脈" in body
    assert "`ccd/protocol.py:46`" in body
    assert "continue → break" in body
    assert "status=survived" in body
    assert "証拠アンカー" in body
    # The arrow split bullets when the mutation is "old → new".
    assert "**改変前**: `continue`" in body
    assert "**改変後**: `break`" in body

    # § 2 やってほしいこと — 1 test, fails under mutation, passes on main.
    assert "## 2. やってほしいこと" in body
    assert "**1 本だけ**" in body
    assert "特定アサーションで失敗" in body
    assert "現行 `main`" in body

    # § 3 制約 — the verbatim block (own test below pins exact strings).
    assert "## 3. 制約" in body
    assert "侵食してはならない" in body

    # § 4 検証要件 — pytest green, ruff clean, ccd guard HALT-free.
    assert "## 4. 検証要件" in body
    assert "`pytest -q`" in body
    assert "`ruff check .`" in body
    assert "`ccd guard --template A --allowed tests/`" in body

    # § 5 許可ファイル集合 — tests/ only, denylist of the rest.
    assert "## 5. 許可ファイル集合" in body
    assert "**`tests/` のみ**" in body
    # The target source file is explicitly forbidden so the agent can't
    # quietly edit ccd/protocol.py and claim to be writing a test.
    assert "ccd/protocol.py" in body
    assert "`ccd/` 以下のすべての本番コード" in body

    # § 6 出力先 — result_auto_NNN.md destination.
    assert "## 6. 出力先" in body
    assert (
        "_ai_workspace/bridge/outbox/result_auto_001.md" in body
    )

    # § 7 メタ情報 — provenance + namespace + AI-free statement.
    assert "## 7. メタ情報" in body
    assert "AI 不使用" in body
    assert "spec_auto_*` **別名前空間**" in body
    assert "翻訳元レポート: `discover_003.json`" in body


def test_constraint_phrases_are_verbatim(repo: Path) -> None:
    """The 论点5 'instruction must be a rigid body' clause: each constraint
    appears verbatim in the spec. If any of these strings change, the
    guard's R1/R2 enforcement loses the matching signal in the spec the
    agent is supposed to obey — so this test pins exact wording.
    """
    # Import the module-level constants here so a typo in either side
    # (test or source) trips the assertion rather than silently masking.
    from ccd import translate as t

    body = translate_finding(
        _survivor_finding(),
        repo=repo,
        today=date(2026, 5, 25),
    ).spec_auto_path.read_text(encoding="utf-8")

    for clause in (
        t._CONSTRAINT_TEST_ONLY,
        t._CONSTRAINT_EXISTING_TESTS_IMMUTABLE,
        t._CONSTRAINT_NO_SKIP_MARKERS,
        t._CONSTRAINT_DETERMINISTIC,
        t._CONSTRAINT_ALLOWED_SET,
    ):
        assert clause in body, f"missing verbatim constraint clause: {clause!r}"


def test_dict_input_is_accepted_directly_from_discover_json(repo: Path) -> None:
    """The discover_NNN.json ``actionable`` list yields dicts shaped like
    this. The translator must accept them without forcing the caller to
    wrap each one in a ``Finding`` by hand.
    """
    actionable_entry = {
        "file": "ccd/protocol.py",
        "line": 50,
        "mutation": (
            "f\"{path}: top-level heading must have the form '# <id>: <title>'\" "
            "→ f\"XX{path}: top-level heading must have the form '# <id>: <title>'XX\""
        ),
        "status": "survived",
        "signature": (
            "ccd/protocol.py:50:f\"{path}: top-level heading must have the form "
            "'# <id>: <title>'\" → "
            "f\"XX{path}: top-level heading must have the form '# <id>: <title>'XX\""
        ),
    }

    result = translate_finding(
        actionable_entry,
        repo=repo,
        channel="mutation",
        source_report="discover_003.json",
        today=date(2026, 5, 25),
    )

    assert result.success is True
    assert result.spec_auto_id == "spec_auto_001"
    assert result.finding.file == "ccd/protocol.py"
    assert result.finding.line == 50
    body = result.spec_auto_path.read_text(encoding="utf-8")
    assert "ccd/protocol.py:50" in body
    assert "top-level heading" in body


# --------------------------------------------------------------------------- #
# Numbering / namespace
# --------------------------------------------------------------------------- #


def test_numbering_increments_and_does_not_overwrite_existing(
    repo: Path,
) -> None:
    inbox = repo / "_ai_workspace" / "bridge" / "inbox"

    # Pre-populate inbox with one machine spec and one human spec — the
    # next number must skip past the machine one and ignore the human one.
    (inbox / "spec_auto_001.md").write_text("# spec_auto_001: pinned\n", encoding="utf-8")
    (inbox / "spec_auto_005.md").write_text("# spec_auto_005: pinned\n", encoding="utf-8")
    (inbox / "spec_042.md").write_text("# spec_042: human spec\n", encoding="utf-8")

    result = translate_finding(
        _survivor_finding(),
        repo=repo,
        today=date(2026, 5, 25),
    )

    assert result.spec_auto_id == "spec_auto_006"
    assert result.spec_auto_path.name == "spec_auto_006.md"

    # Confirm we did NOT overwrite the pre-existing files.
    assert (inbox / "spec_auto_001.md").read_text(encoding="utf-8") == (
        "# spec_auto_001: pinned\n"
    )
    assert (inbox / "spec_auto_005.md").read_text(encoding="utf-8") == (
        "# spec_auto_005: pinned\n"
    )
    assert (inbox / "spec_042.md").read_text(encoding="utf-8") == (
        "# spec_042: human spec\n"
    )


def test_two_back_to_back_translations_increment(repo: Path) -> None:
    r1 = translate_finding(
        _survivor_finding(line=46),
        repo=repo,
        today=date(2026, 5, 25),
    )
    r2 = translate_finding(
        _survivor_finding(line=128, mutation="idx = 1 → idx = 2"),
        repo=repo,
        today=date(2026, 5, 25),
    )
    assert r1.spec_auto_id == "spec_auto_001"
    assert r2.spec_auto_id == "spec_auto_002"
    assert r1.spec_auto_path.exists()
    assert r2.spec_auto_path.exists()
    assert r1.spec_auto_path != r2.spec_auto_path


def test_inbox_dir_is_created_if_missing(tmp_path: Path) -> None:
    """Production callers may run on a fresh repo with no inbox yet."""
    # No mkdir for _ai_workspace/bridge/inbox.
    result = translate_finding(
        _survivor_finding(),
        repo=tmp_path,
        today=date(2026, 5, 25),
    )
    assert result.success is True
    assert result.spec_auto_path.parent.exists()


# --------------------------------------------------------------------------- #
# Determinism — translator is AI-free
# --------------------------------------------------------------------------- #


def test_translation_is_deterministic_same_finding_same_body(
    tmp_path: Path,
) -> None:
    """Same finding + same ``today`` → byte-identical spec_auto body, in
    two fresh inboxes. This is the 论点5 "no AI" guarantee made
    machine-checkable: an AI in the loop would inject nondeterminism.
    """
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    finding = _survivor_finding()

    r_a = translate_finding(finding, repo=repo_a, today=date(2026, 5, 25))
    r_b = translate_finding(finding, repo=repo_b, today=date(2026, 5, 25))

    assert r_a.success is True
    assert r_b.success is True

    body_a = r_a.spec_auto_path.read_text(encoding="utf-8")
    body_b = r_b.spec_auto_path.read_text(encoding="utf-8")

    assert body_a == body_b


def test_translator_does_not_import_any_agent_runner() -> None:
    """``ccd.translate`` must not touch the agent / dispatch / runner
    stack. Importing it must not pull in those modules either — a stray
    import is enough to leave room for an AI call. We verify the loaded
    module's symbol surface.
    """
    from ccd import translate as t

    forbidden_substrings = (
        "AgentRunner",
        "ClaudeCodeRunner",
        "dispatch_one",
        "dispatch_with_retry",
    )
    surface = dir(t)
    for needle in forbidden_substrings:
        assert needle not in surface, (
            f"ccd.translate must not expose {needle!r} — "
            "the translator is AI-free by 论点5"
        )


# --------------------------------------------------------------------------- #
# Report-only downgrade
# --------------------------------------------------------------------------- #


def test_non_mutation_channel_is_downgraded_to_report_only(repo: Path) -> None:
    finding = Finding(
        channel="adversarial",
        file="ccd/profile.py",
        line=10,
        mutation="(N/A — adversarial crash)",
        status="ungraceful",
        signature="ccd/profile.py:10:adversarial",
    )
    result = translate_finding(finding, repo=repo, today=date(2026, 5, 25))

    assert isinstance(result, TranslateResult)
    assert result.success is False
    assert result.spec_auto_id == ""
    assert result.spec_auto_path is None
    assert result.template == ""
    assert "report-only" in result.halt_reason
    assert "adversarial" in result.halt_reason

    # No file written.
    inbox = repo / "_ai_workspace" / "bridge" / "inbox"
    assert list(inbox.glob("spec_auto_*.md")) == []


def test_non_survived_status_is_downgraded(repo: Path) -> None:
    finding = _survivor_finding()
    finding = Finding(
        channel=finding.channel,
        file=finding.file,
        line=finding.line,
        mutation=finding.mutation,
        status="killed",
        signature=finding.signature,
        source_report=finding.source_report,
    )
    result = translate_finding(finding, repo=repo, today=date(2026, 5, 25))

    assert result.success is False
    assert "report-only" in result.halt_reason
    assert "killed" in result.halt_reason
    assert list(
        (repo / "_ai_workspace" / "bridge" / "inbox").glob("spec_auto_*.md")
    ) == []


def test_missing_required_fields_are_downgraded(repo: Path) -> None:
    cases = [
        (
            "empty file",
            Finding(channel="mutation", file="", line=1, mutation="a → b", status="survived"),
        ),
        (
            "zero line",
            Finding(
                channel="mutation", file="ccd/x.py", line=0,
                mutation="a → b", status="survived",
            ),
        ),
        (
            "empty mutation",
            Finding(
                channel="mutation", file="ccd/x.py", line=1,
                mutation="", status="survived",
            ),
        ),
    ]
    for label, finding in cases:
        result = translate_finding(finding, repo=repo, today=date(2026, 5, 25))
        assert result.success is False, f"{label}: should have halted"
        assert result.spec_auto_path is None
        assert "report-only" in result.halt_reason


# --------------------------------------------------------------------------- #
# Generated spec is a valid CCD spec
# --------------------------------------------------------------------------- #


def test_generated_spec_auto_is_parseable_by_parse_spec(repo: Path) -> None:
    result = translate_finding(
        _survivor_finding(),
        repo=repo,
        today=date(2026, 5, 25),
    )
    parsed = parse_spec(result.spec_auto_path)
    assert parsed.id == "spec_auto_001"
    assert "ccd/protocol.py:46" in parsed.title
    assert parsed.title.endswith("（テンプレ A）")
    # The body must contain the verbatim constraint phrases so dispatch's
    # prompt assembly carries the instruction to the agent.
    assert "テストの追加のみ" in parsed.body
    assert "既存テストの削除・改変は禁止" in parsed.body


def test_finding_from_dict_normalizes_missing_optional_fields() -> None:
    payload = {"file": "x.py", "line": "12", "mutation": "a → b", "status": "survived"}
    f = Finding.from_dict(payload, channel="mutation", source_report="d.json")
    assert f.file == "x.py"
    assert f.line == 12
    assert f.mutation == "a → b"
    assert f.status == "survived"
    assert f.signature == "x.py:12:a → b"
    assert f.channel == "mutation"
    assert f.source_report == "d.json"


def test_finding_from_dict_tolerates_bad_line_value() -> None:
    """A malformed line value (non-int string, None) must fall through to
    line=0 rather than raise — the downstream template-fit check then
    halts with a clear reason, instead of crashing the loop.
    """
    for bad in ("not-an-int", None, ""):
        f = Finding.from_dict(
            {"file": "x.py", "line": bad, "mutation": "a → b", "status": "survived"},
            channel="mutation",
        )
        assert f.line == 0


def test_translate_result_is_frozen_dataclass(repo: Path) -> None:
    """A loose ``TranslateResult`` would let downstream code mutate
    ``halt_reason`` or ``spec_auto_path`` after the translator returned
    — defeating the whole "translator is the source of truth" contract.
    """
    import dataclasses

    result = translate_finding(
        _survivor_finding(), repo=repo, today=date(2026, 5, 25)
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.success = False  # type: ignore[misc]
