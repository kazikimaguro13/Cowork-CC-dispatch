from __future__ import annotations

import re
import subprocess
import sys

import ccd
from ccd.cli import build_parser


def test_version_string_is_semver_like() -> None:
    assert isinstance(ccd.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", ccd.__version__)


def test_version_is_020() -> None:
    assert ccd.__version__ == "0.2.0"


def test_parser_version_flag_exits_zero(capsys) -> None:
    parser = build_parser()
    try:
        parser.parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("--version should trigger SystemExit")
    captured = capsys.readouterr()
    assert ccd.__version__ in captured.out


def test_module_invocation_prints_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ccd", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ccd.__version__ in result.stdout
