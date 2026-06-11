"""Tests for ``ccd/guard.py`` (spec_021) — guard-first proof.

The autonomous fix loop (Phase 2 next specs) will use the guard to refuse
fraudulent fixes — deleting a test, weakening an assert, marking a test
``skip``, sneaking past the allowed file set, or producing a sprawling
"narrow" fix. Before the loop ignites we prove the guard *alone* catches
those handcrafted cheats. Each test feeds ``inspect_diff`` a unified diff
text written by hand below — no real git, no real agent.

We also exercise the ``ccd guard`` CLI against a real (tiny) git repo to
prove the end-to-end wiring (``git diff`` -> parser -> rules -> exit
code).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ccd import cli
from ccd.guard import (
    DEFAULT_PROD_DIFF_LIMIT,
    DENYLIST_GLOBS,
    GuardResult,
    inspect_diff,
)

# --------------------------------------------------------------------------- #
# Diff text fixtures — handcrafted unified diffs that mimic the kinds of
# patches the autonomous fix loop will propose (or try to slip past us).
# --------------------------------------------------------------------------- #


def _diff_append_to_existing_test(file: str = "tests/test_widget.py") -> str:
    return f"""diff --git a/{file} b/{file}
index 1111111..2222222 100644
--- a/{file}
+++ b/{file}
@@ -10,3 +10,8 @@ def test_existing():
     assert widget(2) == 4
     assert widget(3) == 6
     assert widget(4) == 8
+
+
+def test_new_case():
+    # added by the agent, only additions to an existing test file
+    assert widget(5) == 10
"""


def _diff_new_test_file(file: str = "tests/test_brand_new.py") -> str:
    return f"""diff --git a/{file} b/{file}
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/{file}
@@ -0,0 +1,6 @@
+from widgets import widget
+
+
+def test_first():
+    assert widget(7) == 14
+    assert widget(8) == 16
"""


def _diff_delete_existing_test_lines(file: str = "tests/test_widget.py") -> str:
    return f"""diff --git a/{file} b/{file}
index 1111111..4444444 100644
--- a/{file}
+++ b/{file}
@@ -10,6 +10,3 @@ def test_existing():
     assert widget(2) == 4
     assert widget(3) == 6
-    assert widget(4) == 8
-
-
-def test_doomed():
-    assert widget(99) == 0
"""


def _diff_weaken_assert(file: str = "tests/test_widget.py") -> str:
    return f"""diff --git a/{file} b/{file}
index 1111111..5555555 100644
--- a/{file}
+++ b/{file}
@@ -10,3 +10,3 @@ def test_existing():
     assert widget(2) == 4
-    assert widget(3) == 6
+    assert widget(3) > 0
     assert widget(4) == 8
"""


def _diff_add_skip_marker(file: str = "tests/test_widget.py") -> str:
    return f"""diff --git a/{file} b/{file}
index 1111111..6666666 100644
--- a/{file}
+++ b/{file}
@@ -8,2 +8,3 @@
+@pytest.mark.skip(reason="flaky")
 def test_existing():
     assert widget(2) == 4
"""


def _diff_add_xfail_marker_to_new_file() -> str:
    file = "tests/test_brand_new.py"
    return f"""diff --git a/{file} b/{file}
new file mode 100644
index 0000000..7777777
--- /dev/null
+++ b/{file}
@@ -0,0 +1,5 @@
+import pytest
+
+@pytest.mark.xfail
+def test_first():
+    assert False
"""


def _diff_touch_outside_allowed(file: str = "ccd/integrate.py") -> str:
    return f"""diff --git a/{file} b/{file}
index 8888888..9999999 100644
--- a/{file}
+++ b/{file}
@@ -1,3 +1,4 @@
 def integrate():
-    return False
+    return True  # the agent decided integrate() should always pass
+
"""


def _diff_touch_denylist_file(file: str = "ccd/guard.py") -> str:
    return f"""diff --git a/{file} b/{file}
index aaaaaaa..bbbbbbb 100644
--- a/{file}
+++ b/{file}
@@ -10,1 +10,1 @@
-DEFAULT_PROD_DIFF_LIMIT = 60
+DEFAULT_PROD_DIFF_LIMIT = 999999
"""


def _diff_touch_pyproject() -> str:
    return """diff --git a/pyproject.toml b/pyproject.toml
index ccccccc..ddddddd 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,2 +1,2 @@
-version = "0.10.0"
+version = "0.10.1"
"""


def _diff_touch_github_workflow() -> str:
    file = ".github/workflows/ci.yml"
    return f"""diff --git a/{file} b/{file}
index eeeeeee..fffffff 100644
--- a/{file}
+++ b/{file}
@@ -1,2 +1,1 @@
-      run: pytest -q
       run: echo "skipping tests"
"""


def _diff_template_b_small(prod_file: str = "ccd/protocol.py") -> str:
    return f"""diff --git a/{prod_file} b/{prod_file}
index 1234567..2345678 100644
--- a/{prod_file}
+++ b/{prod_file}
@@ -42,1 +42,1 @@ def parse(text):
-    return text.strip().lower()
+    return text.strip()
diff --git a/tests/test_protocol.py b/tests/test_protocol.py
index 3456789..4567890 100644
--- a/tests/test_protocol.py
+++ b/tests/test_protocol.py
@@ -22,3 +22,6 @@ def test_existing():
     assert parse("X") == "x"
     assert parse(" Y ") == "y"
     assert parse("Z") == "z"
+
+def test_new_case():
+    assert parse(" mixedCase ") == "mixedCase"
"""


def _diff_template_b_huge(prod_file: str = "ccd/protocol.py") -> str:
    plus_lines = "\n".join(f"+    new_line_{i}()" for i in range(80))
    return f"""diff --git a/{prod_file} b/{prod_file}
index aaaa111..bbbb222 100644
--- a/{prod_file}
+++ b/{prod_file}
@@ -1,1 +1,81 @@
 def parse(text):
{plus_lines}
"""


def _diff_binary(file: str = "tests/fixtures/blob.png") -> str:
    return f"""diff --git a/{file} b/{file}
index 1010101..2020202 100644
Binary files a/{file} and b/{file} differ
"""


def _diff_rename(old: str = "tests/test_old.py", new: str = "tests/test_new.py") -> str:
    return f"""diff --git a/{old} b/{new}
similarity index 100%
rename from {old}
rename to {new}
"""


def _diff_mode_change(file: str = "tests/run.sh") -> str:
    return f"""diff --git a/{file} b/{file}
old mode 100644
new mode 100755
"""


def _diff_delete_test_file(file: str = "tests/test_doomed.py") -> str:
    return f"""diff --git a/{file} b/{file}
deleted file mode 100644
index 1234567..0000000
--- a/{file}
+++ /dev/null
@@ -1,3 +0,0 @@
-def test_doomed():
-    assert False
-
"""


# --------------------------------------------------------------------------- #
# R1 / R2 / R3 — direct `inspect_diff` tests against handcrafted diffs
# --------------------------------------------------------------------------- #


def test_legitimate_template_a_append_passes() -> None:
    """Adding a new test function to an existing test file is the typical
    legitimate template-A fix and must pass."""
    res = inspect_diff(
        diff=_diff_append_to_existing_test(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is True, res.halt_reasons
    assert res.halt_reasons == ()
    assert "tests/test_widget.py" in res.files_touched


def test_legitimate_template_a_new_test_file_passes() -> None:
    res = inspect_diff(
        diff=_diff_new_test_file(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is True, res.halt_reasons
    assert res.files_touched == ("tests/test_brand_new.py",)


def test_r2_deleted_lines_in_existing_test_halt() -> None:
    """The classic cheat: just remove the failing test lines."""
    res = inspect_diff(
        diff=_diff_delete_existing_test_lines(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("R2" in r for r in res.halt_reasons), res.halt_reasons
    assert any("removed/changed" in r for r in res.halt_reasons)


def test_r2_weakened_assert_halts() -> None:
    """Replacing ``assert x == 6`` with ``assert x > 0`` shows up as one
    removed line plus one added line — R2 catches the removal."""
    res = inspect_diff(
        diff=_diff_weaken_assert(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("R2" in r for r in res.halt_reasons)


def test_r2_skip_marker_added_to_existing_halts() -> None:
    res = inspect_diff(
        diff=_diff_add_skip_marker(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any(
        "skip" in r.lower() or "R2" in r
        for r in res.halt_reasons
    ), res.halt_reasons


def _diff_new_test_file_with_body(file: str, body: str) -> str:
    lines = body.split("\n")
    added = "\n".join(f"+{ln}" for ln in lines)
    return (
        f"diff --git a/{file} b/{file}\n"
        "new file mode 100644\n"
        "index 0000000..8888888\n"
        "--- /dev/null\n"
        f"+++ b/{file}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{added}\n"
    )


def test_r2_pytestmark_assignment_halts() -> None:
    """spec_043 §2-3(a) — the module-level ``pytestmark = pytest.mark.skip``
    assignment form (which the decorator regex misses) is caught by the
    secondary 保険 layer."""

    res = inspect_diff(
        diff=_diff_new_test_file_with_body(
            "tests/test_muted.py",
            "import pytest\npytestmark = pytest.mark.skip\n\ndef test_x():\n    assert True",
        ),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("R2" in r for r in res.halt_reasons), res.halt_reasons


def test_r2_collect_ignore_in_conftest_halts() -> None:
    """spec_043 §2-3(b) — a new ``tests/conftest.py`` with ``collect_ignore``
    is caught by the static layer."""

    res = inspect_diff(
        diff=_diff_new_test_file_with_body(
            "tests/conftest.py",
            'collect_ignore = ["test_slow.py"]',
        ),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("R2" in r for r in res.halt_reasons), res.halt_reasons


def test_r2_collection_hook_halts() -> None:
    """spec_043 §2-3(c) — a collection hook that deselects items is caught."""

    res = inspect_diff(
        diff=_diff_new_test_file_with_body(
            "tests/conftest.py",
            "def pytest_collection_modifyitems(config, items):\n    items[:] = []",
        ),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("R2" in r for r in res.halt_reasons), res.halt_reasons


def test_r2_xfail_marker_in_brand_new_test_still_halts() -> None:
    """Even in a brand-new test file, adding @xfail counts as muting and
    must halt — the cheat just hides as a "new" test that never enforces."""
    res = inspect_diff(
        diff=_diff_add_xfail_marker_to_new_file(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("xfail" in r.lower() for r in res.halt_reasons), res.halt_reasons


def test_r2_deleting_a_whole_test_file_halts() -> None:
    res = inspect_diff(
        diff=_diff_delete_test_file(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("R2" in r and "deleted" in r for r in res.halt_reasons), res.halt_reasons


def test_r1_outside_allowed_halts() -> None:
    """Template A allows only ``tests/`` — touching ccd/ must halt."""
    res = inspect_diff(
        diff=_diff_touch_outside_allowed(),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("R1" in r for r in res.halt_reasons), res.halt_reasons


def test_denylist_guard_file_halts_even_if_explicitly_allowed() -> None:
    """The denylist must override the caller's allowlist — otherwise the
    autonomous fix loop could simply allow the guard itself and weaken it."""
    res = inspect_diff(
        diff=_diff_touch_denylist_file("ccd/guard.py"),
        allowed_files=["ccd/guard.py", "tests/"],  # allowed by the caller
        template="B",
    )
    assert res.passed is False
    assert any("denylist" in r for r in res.halt_reasons), res.halt_reasons


def test_denylist_pyproject_halts() -> None:
    res = inspect_diff(
        diff=_diff_touch_pyproject(),
        allowed_files=["pyproject.toml"],
        template="B",
    )
    assert res.passed is False
    assert any("denylist" in r for r in res.halt_reasons)


def test_denylist_github_workflow_halts() -> None:
    res = inspect_diff(
        diff=_diff_touch_github_workflow(),
        allowed_files=[".github/"],
        template="B",
    )
    assert res.passed is False
    assert any("denylist" in r for r in res.halt_reasons)


def test_denylist_nightly_module_halts() -> None:
    diff = _diff_touch_denylist_file("ccd/nightly.py")
    res = inspect_diff(
        diff=diff,
        allowed_files=["ccd/nightly.py", "tests/"],
        template="B",
    )
    assert res.passed is False
    assert any("denylist" in r for r in res.halt_reasons), res.halt_reasons


def test_template_b_small_prod_diff_passes() -> None:
    res = inspect_diff(
        diff=_diff_template_b_small("ccd/protocol.py"),
        allowed_files=["ccd/protocol.py", "tests/"],
        template="B",
    )
    assert res.passed is True, res.halt_reasons


def test_template_b_huge_prod_diff_halts_r3() -> None:
    res = inspect_diff(
        diff=_diff_template_b_huge("ccd/protocol.py"),
        allowed_files=["ccd/protocol.py", "tests/"],
        template="B",
    )
    assert res.passed is False
    assert any("R3" in r for r in res.halt_reasons), res.halt_reasons


def test_template_a_does_not_apply_r3() -> None:
    """R3 is template-B only — template A can never legitimately touch a
    production file anyway (R1 already prevents it), but if somehow the
    diff is empty for prod and template is A we don't want to spuriously
    invoke R3 maths."""
    res = inspect_diff(
        diff=_diff_append_to_existing_test(),
        allowed_files=["tests/"],
        template="A",
        max_prod_diff_lines=1,  # absurdly tight — proves R3 isn't applied
    )
    assert res.passed is True, res.halt_reasons


def test_binary_diff_safe_halts() -> None:
    res = inspect_diff(
        diff=_diff_binary("tests/fixtures/blob.png"),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("safe-halt" in r and "binary" in r for r in res.halt_reasons), res.halt_reasons


def test_rename_safe_halts() -> None:
    res = inspect_diff(
        diff=_diff_rename("tests/test_old.py", "tests/test_new.py"),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("safe-halt" in r and "rename" in r for r in res.halt_reasons), res.halt_reasons


def test_mode_change_safe_halts() -> None:
    res = inspect_diff(
        diff=_diff_mode_change("tests/run.sh"),
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert any("safe-halt" in r and "mode" in r for r in res.halt_reasons), res.halt_reasons


def test_empty_diff_passes() -> None:
    """No diff = nothing to police = pass. The loop is expected to call
    the guard even on empty changes."""
    res = inspect_diff(
        diff="",
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is True
    assert res.files_touched == ()


def test_unknown_template_halts() -> None:
    res = inspect_diff(
        diff=_diff_append_to_existing_test(),
        allowed_files=["tests/"],
        template="C",  # type: ignore[arg-type]
    )
    assert res.passed is False
    assert any("unknown template" in r for r in res.halt_reasons)


def test_multiple_violations_all_surfaced() -> None:
    """The operator should see every cheat the agent attempted in one go —
    we don't short-circuit on the first hit."""
    diff = (
        _diff_delete_existing_test_lines()
        + _diff_touch_outside_allowed()
    )
    res = inspect_diff(
        diff=diff,
        allowed_files=["tests/"],
        template="A",
    )
    assert res.passed is False
    assert len(res.halt_reasons) >= 2
    assert any("R2" in r for r in res.halt_reasons)
    assert any("R1" in r for r in res.halt_reasons)


def test_returns_guard_result_dataclass() -> None:
    res = inspect_diff(
        diff=_diff_append_to_existing_test(),
        allowed_files=["tests/"],
        template="A",
    )
    assert isinstance(res, GuardResult)
    # frozen → mutation must fail with FrozenInstanceError.
    from dataclasses import FrozenInstanceError  # noqa: PLC0415

    with pytest.raises(FrozenInstanceError):
        res.passed = False  # type: ignore[misc]


def test_denylist_globs_cover_expected_paths() -> None:
    """Lightweight smoke that the denylist names what we expect — if
    anyone "trims" it we want this test to scream."""
    must_be_denied = (
        "ccd/guard.py",
        "ccd/nightly.py",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "_ai_workspace/ccd_profile.toml",
        "_ai_workspace/discover/blocklist.txt",
    )
    from ccd.guard import _matches_any  # noqa: PLC0415 - internal helper under test

    for p in must_be_denied:
        assert _matches_any(p, DENYLIST_GLOBS), p


# --------------------------------------------------------------------------- #
# CLI end-to-end against a real (tiny) git repo
# --------------------------------------------------------------------------- #


def _init_repo(repo: Path) -> None:
    """Create a tiny git repo with one prod file and one test file."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "guard@test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Guard Test"],
        check=True,
    )
    (repo / "ccd").mkdir()
    (repo / "ccd" / "protocol.py").write_text(
        "def parse(text):\n    return text.strip().lower()\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_protocol.py").write_text(
        "from ccd.protocol import parse\n"
        "\n"
        "def test_existing():\n"
        "    assert parse('X') == 'x'\n"
        "    assert parse(' Y ') == 'y'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial"],
        check=True,
    )


def _commit_all(repo: Path, message: str) -> None:
    """Helper: stage everything and commit on whatever branch we're on."""
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message], check=True
    )


def _checkout_branch(repo: Path, name: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", name],
        check=True,
    )


def test_cli_guard_pass_on_legitimate_template_a(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    # Branch off main, append a new test function (legitimate template A),
    # and commit so `git diff main..HEAD` shows the change.
    _checkout_branch(repo, "feat/add-case")
    test_file = repo / "tests" / "test_protocol.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8")
        + "\n\ndef test_new_case():\n    assert parse('Q') == 'q'\n",
        encoding="utf-8",
    )
    _commit_all(repo, "test: add new case")

    rc = cli.main(
        [
            "guard",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--head",
            "HEAD",
            "--template",
            "A",
            "--allowed",
            "tests/",
        ]
    )
    out = capsys.readouterr()
    assert rc == 0, (out.out, out.err)
    assert "guard: pass" in out.out
    assert "tests/test_protocol.py" in out.out


def test_cli_guard_halts_on_test_deletion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _checkout_branch(repo, "feat/cheat")
    # The fraudulent fix: just delete the assertions.
    (repo / "tests" / "test_protocol.py").write_text(
        "from ccd.protocol import parse\n"
        "\n"
        "def test_existing():\n"
        "    pass\n",
        encoding="utf-8",
    )
    _commit_all(repo, "test: weaken assertions")

    rc = cli.main(
        [
            "guard",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--head",
            "HEAD",
            "--template",
            "A",
            "--allowed",
            "tests/",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "guard: HALT" in captured.err
    assert "R2" in captured.err


def test_cli_guard_halts_on_denylist_even_when_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The caller explicitly allows ``ccd/guard.py`` — but the denylist
    overrides every caller allowlist."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    # Commit an initial ccd/guard.py stub so the next commit produces a
    # modification diff against it.
    guard_clone = repo / "ccd" / "guard.py"
    guard_clone.write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "ccd/guard.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "add guard stub"],
        check=True,
    )
    # Now modify it and commit so HEAD~1..HEAD has the modification.
    guard_clone.write_text("X = 9999  # weakened by fix loop\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "ccd/guard.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "modify guard"],
        check=True,
    )

    rc = cli.main(
        [
            "guard",
            "--repo",
            str(repo),
            "--base",
            "HEAD~1",
            "--head",
            "HEAD",
            "--template",
            "B",
            "--allowed",
            "ccd/guard.py",
            "tests/",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1, (captured.out, captured.err)
    assert "denylist" in captured.err
    assert "ccd/guard.py" in captured.err


def test_cli_guard_template_b_huge_diff_halts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _checkout_branch(repo, "feat/scope-creep")
    # Bloat the prod file well past the default R3 limit.
    new_body = "def parse(text):\n" + "\n".join(
        f"    line_{i} = {i}" for i in range(DEFAULT_PROD_DIFF_LIMIT + 10)
    ) + "\n    return text.strip()\n"
    (repo / "ccd" / "protocol.py").write_text(new_body, encoding="utf-8")
    _commit_all(repo, "prod: huge rewrite")

    rc = cli.main(
        [
            "guard",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--head",
            "HEAD",
            "--template",
            "B",
            "--allowed",
            "ccd/protocol.py",
            "tests/",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "R3" in captured.err


def test_cli_guard_git_failure_surfaces_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bogus base ref → git errors → we surface it (non-zero) rather than
    silently returning pass on an empty diff."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    rc = cli.main(
        [
            "guard",
            "--repo",
            str(repo),
            "--base",
            "definitely-not-a-ref",
            "--head",
            "HEAD",
            "--template",
            "A",
            "--allowed",
            "tests/",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "guard halted" in captured.err
