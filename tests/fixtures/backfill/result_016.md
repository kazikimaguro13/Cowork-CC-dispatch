# result_016 — em-dash title, spec_id derived from filename

- **Status**: ✅ done
- **Branch**: `feat/spec_016-something`
- **Date**: 2026-05-14

## Body

SECRET_BODY_MARKER_016 — title uses em-dash so `_TITLE_RE` cannot pick up the
id; the parser falls through to the filename ``result_016.md`` and finds
``spec_016`` in the body (Branch line).
