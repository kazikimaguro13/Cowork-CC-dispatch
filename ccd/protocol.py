"""Bridge protocol: read Spec / write Result Markdown files.

Format (per the spec_NNN.md and result_NNN.md files used by the bash bridge):

    # <id>: <title>

    - **Key**: value
    - **Key**: value

    <body sections...>

`parse_spec` extracts `id` and `title` from the top-level `# id: title` heading;
everything after that line (including the `- **Key**: value` header block) is
preserved verbatim as `body`. `write_result` produces the symmetric format with
`Spec` / `Status` / (optional) `Failure-Category` header lines and an optional
trailing `## Commits` section.

`write_spec` / `parse_result` are the symmetric duals — provided so that the
round-trip `parse → write → parse` invariant can be checked in tests.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import DispatchStatus, FailureCategory, Result, Spec

_TITLE_RE = re.compile(r"^#\s+(?P<id>\S+?)\s*:\s*(?P<title>.+?)\s*$")
_HEADER_LINE_RE = re.compile(r"^-\s+\*\*(?P<key>[^*]+)\*\*:\s*(?P<value>.*?)\s*$")
_COMMITS_HEADING = "## Commits"


def parse_spec(path: Path | str) -> Spec:
    """Parse a `spec_NNN.md` file into a `Spec`.

    The body is preserved verbatim (modulo leading/trailing blank lines).
    """

    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    for idx, line in enumerate(lines):
        if not line.startswith("# "):
            continue
        match = _TITLE_RE.match(line)
        if match is None:
            raise ValueError(
                f"{path}: top-level heading must have the form '# <id>: <title>'"
            )
        body = "\n".join(lines[idx + 1 :]).strip("\n")
        return Spec(
            id=match.group("id").strip(),
            title=match.group("title").strip(),
            body=body,
            path=path,
        )

    raise ValueError(f"{path}: no top-level '# <id>: <title>' heading found")


def write_spec(spec: Spec, path: Path | str) -> Path:
    """Write a `Spec` to disk in the canonical `# id: title` + body format."""

    path = Path(path)
    parts = [f"# {spec.id}: {spec.title}"]
    if spec.body:
        parts.append("")
        parts.append(spec.body.rstrip("\n"))
    text = "\n".join(parts) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def write_result(result: Result, path: Path | str) -> Path:
    """Serialize a `Result` to `result_NNN.md` Markdown.

    Layout:
        # <result-id>

        - **Spec**: <spec_id>
        - **Status**: <status>
        - **Failure-Category**: <category>   (omitted when None)

        <body>

        ## Commits                            (omitted when commits is empty)

        - <commit1>
        - <commit2>
    """

    path = Path(path)
    result_id = _derive_result_id(result.spec_id)

    header_lines = [
        f"- **Spec**: {result.spec_id}",
        f"- **Status**: {result.status.value}",
    ]
    if result.failure_category is not None:
        header_lines.append(f"- **Failure-Category**: {result.failure_category.value}")

    sections: list[str] = [f"# {result_id}", "", "\n".join(header_lines)]
    if result.body:
        sections.append("")
        sections.append(result.body.strip("\n"))
    if result.commits:
        sections.append("")
        commits_block = [_COMMITS_HEADING, ""] + [f"- {c}" for c in result.commits]
        sections.append("\n".join(commits_block))

    text = "\n".join(sections) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def parse_result(path: Path | str) -> Result:
    """Parse a `result_NNN.md` file produced by `write_result` back into a `Result`."""

    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    if not lines or not lines[0].startswith("# "):
        raise ValueError(f"{path}: missing top-level '# ' heading")

    idx = 1
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1

    header: dict[str, str] = {}
    while idx < len(lines):
        match = _HEADER_LINE_RE.match(lines[idx])
        if match is None:
            break
        header[match.group("key").strip()] = match.group("value").strip()
        idx += 1

    if "Spec" not in header:
        raise ValueError(f"{path}: missing required '- **Spec**:' header line")
    if "Status" not in header:
        raise ValueError(f"{path}: missing required '- **Status**:' header line")

    rest = lines[idx:]
    commits: list[str] = []
    body_lines = rest

    commits_idx = -1
    for k in range(len(rest) - 1, -1, -1):
        if rest[k].strip() == _COMMITS_HEADING:
            commits_idx = k
            break

    if commits_idx >= 0:
        body_lines = rest[:commits_idx]
        for entry in rest[commits_idx + 1 :]:
            stripped = entry.strip()
            if not stripped:
                continue
            if stripped.startswith("- "):
                commits.append(stripped[2:].strip())
            else:
                raise ValueError(
                    f"{path}: unexpected non-list line under '## Commits': {entry!r}"
                )

    body = "\n".join(body_lines).strip("\n")

    failure_category_raw = header.get("Failure-Category")
    failure_category = (
        FailureCategory(failure_category_raw) if failure_category_raw else None
    )

    return Result(
        spec_id=header["Spec"],
        status=DispatchStatus(header["Status"]),
        body=body,
        commits=commits,
        failure_category=failure_category,
    )


def _derive_result_id(spec_id: str) -> str:
    """`spec_001` → `result_001`. Other ids → `result_<spec_id>`."""

    if spec_id.startswith("spec_"):
        return "result_" + spec_id[len("spec_") :]
    return f"result_{spec_id}"
