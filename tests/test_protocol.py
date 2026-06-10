from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from ccd.models import DispatchStatus, FailureCategory, Result, Spec
from ccd.protocol import parse_result, parse_spec, write_result, write_spec


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_spec_extracts_id_title_and_body(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "spec_010.md",
        dedent(
            """\
            # spec_010: example spec

            - **Author**: Cowork
            - **Status**: pending

            ## 1. 目的

            do the thing
            """
        ),
    )

    spec = parse_spec(p)

    assert spec.id == "spec_010"
    assert spec.title == "example spec"
    assert spec.path == p
    assert spec.body.startswith("- **Author**: Cowork")
    assert "## 1. 目的" in spec.body
    assert spec.body.rstrip().endswith("do the thing")


def test_parse_spec_skips_lines_before_heading(tmp_path: Path) -> None:
    """Non-heading lines preceding the `# id: title` heading must be skipped.

    The scan loop uses `continue` to step past every line that does not start
    with `"# "` until it reaches the heading. If that `continue` became a
    `break`, the very first non-heading line (here a leading blank line) would
    abort the scan and `parse_spec` would wrongly raise "no top-level heading".
    """

    p = _write(
        tmp_path / "spec_777.md",
        dedent(
            """\

            # spec_777: leading blank line

            - **Author**: Cowork

            body here
            """
        ),
    )

    spec = parse_spec(p)

    assert spec.id == "spec_777"
    assert spec.title == "leading blank line"
    assert spec.body.startswith("- **Author**: Cowork")
    assert spec.body.rstrip().endswith("body here")


def test_parse_spec_raises_when_no_heading(tmp_path: Path) -> None:
    p = _write(tmp_path / "broken.md", "no heading here\njust text\n")
    with pytest.raises(ValueError, match="no top-level"):
        parse_spec(p)


def test_parse_spec_raises_when_heading_missing_colon(tmp_path: Path) -> None:
    p = _write(tmp_path / "broken.md", "# just-a-title-no-colon\n\nbody\n")
    with pytest.raises(ValueError, match="must have the form"):
        parse_spec(p)


def test_spec_round_trip(tmp_path: Path) -> None:
    original = _write(
        tmp_path / "spec_010.md",
        dedent(
            """\
            # spec_010: round trip

            - **Author**: Cowork
            - **Status**: pending

            ## 1. body

            line one
            line two
            """
        ),
    )
    first = parse_spec(original)

    rewritten = tmp_path / "spec_010_rewritten.md"
    write_spec(first, rewritten)
    second = parse_spec(rewritten)

    assert second.id == first.id
    assert second.title == first.title
    assert second.body == first.body


def test_write_result_minimal(tmp_path: Path) -> None:
    out = tmp_path / "result_010.md"
    result = Result(
        spec_id="spec_010",
        status=DispatchStatus.DONE,
        body="all good",
    )
    write_result(result, out)

    text = out.read_text(encoding="utf-8")
    assert text.startswith("# result_010\n")
    assert "- **Spec**: spec_010" in text
    assert "- **Status**: done" in text
    assert "all good" in text
    assert "Failure-Category" not in text
    assert "## Commits" not in text


def test_write_result_with_commits_and_failure_category(tmp_path: Path) -> None:
    out = tmp_path / "result_010.md"
    result = Result(
        spec_id="spec_010",
        status=DispatchStatus.FAILED,
        body="boom",
        commits=["abc1234", "def5678"],
        failure_category=FailureCategory.SMOKE_FAILED,
    )
    write_result(result, out)

    text = out.read_text(encoding="utf-8")
    assert "- **Failure-Category**: smoke_failed" in text
    assert "## Commits" in text
    assert "- abc1234" in text
    assert "- def5678" in text


def test_result_round_trip_minimal(tmp_path: Path) -> None:
    result = Result(
        spec_id="spec_010",
        status=DispatchStatus.DONE,
        body="hello\n\nworld",
    )
    out = tmp_path / "result.md"
    write_result(result, out)
    parsed = parse_result(out)

    assert parsed.spec_id == result.spec_id
    assert parsed.status == result.status
    assert parsed.body == result.body
    assert parsed.commits == []
    assert parsed.failure_category is None


def test_result_round_trip_full(tmp_path: Path) -> None:
    result = Result(
        spec_id="spec_010",
        status=DispatchStatus.BLOCKED,
        body=dedent(
            """\
            ## 1. やったこと

            things

            ## 2. 注意

            - point one
            - point two"""
        ),
        commits=["abc1234", "def5678"],
        failure_category=FailureCategory.SPEC_UNCLEAR,
    )
    out = tmp_path / "result.md"
    write_result(result, out)
    parsed = parse_result(out)

    assert parsed == result


def test_parse_result_rejects_missing_spec_header(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "bad.md",
        dedent(
            """\
            # result_010

            - **Status**: done

            body
            """
        ),
    )
    with pytest.raises(ValueError, match="Spec"):
        parse_result(p)


def test_parse_result_rejects_missing_status_header(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "bad.md",
        dedent(
            """\
            # result_010

            - **Spec**: spec_010

            body
            """
        ),
    )
    with pytest.raises(ValueError, match="Status"):
        parse_result(p)


def test_parse_result_handles_constructed_file(tmp_path: Path) -> None:
    """Parser must accept a hand-written file matching the documented format."""

    p = _write(
        tmp_path / "result_010.md",
        dedent(
            """\
            # result_010

            - **Spec**: spec_010
            - **Status**: done

            body text

            ## Commits

            - abc1234 first
            - def5678 second
            """
        ),
    )
    result = parse_result(p)
    assert result.spec_id == "spec_010"
    assert result.status is DispatchStatus.DONE
    assert "body text" in result.body
    assert result.commits == ["abc1234 first", "def5678 second"]


def test_spec_round_trip_starting_from_object(tmp_path: Path) -> None:
    """A Spec created in code must survive write → parse unchanged (sans path)."""

    spec = Spec(
        id="spec_042",
        title="constructed",
        body="- **Author**: Cowork\n\n## 1. やる\n\nbody.",
        path=tmp_path / "spec_042.md",
    )
    target = tmp_path / "spec_042.md"
    write_spec(spec, target)
    parsed = parse_spec(target)

    assert parsed.id == spec.id
    assert parsed.title == spec.title
    assert parsed.body == spec.body
    assert parsed.path == target
