"""Synthetic resolution targets for ``test_adversarial.py`` (spec_030).

The spec_030 ``resolve_parser_targets`` resolver looks up dotted names
via ``importlib`` + ``getattr``. Tests need targets that live at known
importable paths so the resolver can find them; this module supplies a
handful of stable callables that record what they were called with so
the tests can assert the ``input_kind`` wrapper is doing the right
thing (path / bytes / str dispatch).

Production code does NOT import this module — it exists only for the
test suite, and its content is intentionally trivial.
"""

from __future__ import annotations

received: list[tuple[object, str]] = []


def record_bytes(payload: bytes) -> None:
    """Target for ``input_kind="bytes"`` round-trip tests."""

    received.append((payload, "bytes"))


def record_str(payload: str) -> None:
    """Target for ``input_kind="str"`` round-trip tests."""

    received.append((payload, "str"))
