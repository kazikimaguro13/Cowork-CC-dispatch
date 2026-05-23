---
spec: spec_015
status: done
agent: Claude Code (dev-b)
created: 2026-05-21
---

# spec_015 — status pulled from YAML frontmatter outside the header block

The header parser stops at the first `#`. With no `- **Status**: ...` line in
the document the body fallback must scan for ``status:`` in the YAML
frontmatter (and the spec id from ``spec: spec_015``) so that early-spec
results that pre-date the current header convention still get counted.

SECRET_BODY_MARKER_015 — frontmatter recovery path.
