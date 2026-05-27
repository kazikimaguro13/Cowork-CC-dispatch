"""Tests for spec_032 — profile-driven mutmut invocation parameters.

The mutation channel grew three new optional profile fields in spec_032:

- ``discovery.mutation.cwd`` — subdirectory of the iso-clone mutmut runs in.
- ``discovery.mutation.tests_dir`` — value passed to ``--tests-dir``.
- ``discovery.mutation.extra_args`` — appended verbatim to the ``mutmut run``
  command line.

These tests cover three layers:

1. **Profile schema parse** — TOML with ``[discovery.mutation]`` round-trips
   into a :class:`MutationConfig` with the right field types / defaults.
2. **mutmut argv assembly** — :class:`MutmutRunner` builds the correct
   command line, with the right ``cwd``, for the supplied parameters.
3. **Profile fail-fast** — ``cwd`` / ``mutation_paths`` / ``tests_dir`` that
   do not exist on disk raise :class:`ProfileError` at load time with the
   missing absolute path embedded in the message.

No real ``mutmut`` is invoked — subprocess.run is monkeypatched, and the
iso-venv provisioner is stubbed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from ccd.discover import MutmutRunner, _build_mutmut_run_argv
from ccd.profile import (
    MutationConfig,
    ProfileError,
    effective_mutation_config,
    load_profile,
)

# --------------------------------------------------------------------------- #
# unit test #1 — profile schema parse
# --------------------------------------------------------------------------- #


def _write_axis_style_profile(
    tmp_path: Path,
    *,
    repo: str,
    cwd: str | None = "backend",
    paths: tuple[str, ...] = ("src/normalizer.py",),
    tests_dir: str | None = "tests",
    extra_args: tuple[str, ...] = (),
) -> Path:
    """Write a profile that uses the spec_032 ``[discovery.mutation]`` form."""

    lines = [
        f'repo = "{repo}"',
        "",
        "[discovery]",
        'channels = ["mutation"]',
        "",
        "[discovery.mutation]",
    ]
    paths_list = ", ".join(f'"{p}"' for p in paths)
    lines.append(f"mutation_paths = [{paths_list}]")
    if cwd is not None:
        lines.append(f'cwd = "{cwd}"')
    if tests_dir is not None:
        lines.append(f'tests_dir = "{tests_dir}"')
    extra_list = ", ".join(f'"{x}"' for x in extra_args)
    lines.append(f"extra_args = [{extra_list}]")
    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return profile_path


def test_profile_parses_discovery_mutation_block(tmp_path: Path) -> None:
    """spec_032 §2-1 — [discovery.mutation] TOML round-trips into a
    :class:`MutationConfig` with the right types and values."""

    target_repo = tmp_path / "target_repo"
    (target_repo / "backend" / "src").mkdir(parents=True)
    (target_repo / "backend" / "src" / "normalizer.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (target_repo / "backend" / "tests").mkdir()

    _write_axis_style_profile(
        tmp_path,
        repo=str(target_repo),
        cwd="backend",
        paths=("src/normalizer.py",),
        tests_dir="tests",
        extra_args=("--use-coverage",),
    )

    profile = load_profile(tmp_path)

    assert profile.discovery.mutation is not None
    cfg = profile.discovery.mutation
    assert isinstance(cfg, MutationConfig)
    assert cfg.cwd == "backend"
    assert cfg.mutation_paths == ["src/normalizer.py"]
    assert cfg.tests_dir == "tests"
    assert cfg.extra_args == ["--use-coverage"]


def test_profile_mutation_block_defaults_when_optional_keys_omitted(
    tmp_path: Path,
) -> None:
    """Only ``mutation_paths`` is required inside [discovery.mutation];
    ``cwd`` / ``tests_dir`` default to ``None`` and ``extra_args`` to ``[]``."""

    target_repo = tmp_path / "target_repo"
    (target_repo / "ccd").mkdir(parents=True)
    (target_repo / "ccd" / "__init__.py").write_text("", encoding="utf-8")

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            f"""
            repo = "{target_repo}"

            [discovery]
            channels = ["mutation"]

            [discovery.mutation]
            mutation_paths = ["ccd"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    cfg = profile.discovery.mutation
    assert cfg is not None
    assert cfg.cwd is None
    assert cfg.tests_dir is None
    assert cfg.extra_args == []
    assert cfg.mutation_paths == ["ccd"]


def test_effective_mutation_config_wraps_legacy_mutation_paths(
    tmp_path: Path,
) -> None:
    """A profile that uses the legacy top-level ``mutation_paths`` (the
    spec_018 form, ``discovery.mutation is None``) is wrapped into a
    :class:`MutationConfig` by :func:`effective_mutation_config` so call
    sites have one shape to read."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "."

            [discovery]
            channels = ["mutation"]
            mutation_paths = ["ccd/protocol.py"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)

    assert profile.discovery.mutation is None  # legacy form preserved
    cfg = effective_mutation_config(profile.discovery)
    assert isinstance(cfg, MutationConfig)
    assert cfg.mutation_paths == ["ccd/protocol.py"]
    assert cfg.cwd is None
    assert cfg.tests_dir is None
    assert cfg.extra_args == []


def test_profile_rejects_both_legacy_and_new_mutation_forms(
    tmp_path: Path,
) -> None:
    """A profile that mixes top-level ``mutation_paths`` with the new
    ``[discovery.mutation]`` block is ambiguous → load-time error."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "."

            [discovery]
            channels = ["mutation"]
            mutation_paths = ["ccd"]

            [discovery.mutation]
            mutation_paths = ["ccd/protocol.py"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_profile(tmp_path)
    assert "mutually exclusive" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# unit test #2 — mutmut argv + cwd assembly
# --------------------------------------------------------------------------- #


def _fake_subprocess_run(captures: list[dict]):
    """Return a fake ``subprocess.run`` that records argv / cwd / env per
    call and returns an empty CompletedProcess for ``mutmut`` invocations.

    Non-mutmut calls (e.g. ``git remote``) fall through to the real
    ``subprocess.run`` so the isolation helper keeps working.
    """

    real_run = subprocess.run

    def _fake(*args, **kwargs):  # type: ignore[no-untyped-def]
        argv = args[0] if args else kwargs.get("args", [])
        if (
            isinstance(argv, (list, tuple))
            and argv
            and "mutmut" in str(argv[0])
        ):
            captures.append(
                {
                    "argv": list(argv),
                    "cwd": kwargs.get("cwd", ""),
                }
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )
        return real_run(*args, **kwargs)

    return _fake


def _stub_iso_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_provision(workspace, *, parent_python=None, timeout=None):  # type: ignore[no-untyped-def]
        return workspace / ".ccd-iso-venv" / "bin"

    monkeypatch.setattr("ccd.discover._provision_iso_venv", fake_provision)


def test_build_mutmut_argv_minimal_uses_only_paths_to_mutate() -> None:
    """``cwd=None, tests_dir=None, extra_args=[]`` → only the bare
    ``mutmut run --paths-to-mutate <paths>`` command."""

    argv = _build_mutmut_run_argv(
        binary="/bin/mutmut",
        paths_arg="ccd/protocol.py",
        tests_dir=None,
        extra_args=[],
    )

    assert argv == ["/bin/mutmut", "run", "--paths-to-mutate", "ccd/protocol.py"]


def test_build_mutmut_argv_appends_tests_dir_when_set() -> None:
    """``tests_dir="tests"`` appends ``--tests-dir tests`` AFTER the
    paths-to-mutate pair (spec_032 §2-2 ordering)."""

    argv = _build_mutmut_run_argv(
        binary="/bin/mutmut",
        paths_arg="src/normalizer.py",
        tests_dir="tests",
        extra_args=[],
    )

    assert argv == [
        "/bin/mutmut",
        "run",
        "--paths-to-mutate",
        "src/normalizer.py",
        "--tests-dir",
        "tests",
    ]


def test_build_mutmut_argv_appends_extra_args_last() -> None:
    """``extra_args=["--use-coverage"]`` lands at the very end of the
    command line (after any ``--tests-dir`` pair)."""

    argv = _build_mutmut_run_argv(
        binary="/bin/mutmut",
        paths_arg="src",
        tests_dir="tests",
        extra_args=["--use-coverage", "--runner=pytest"],
    )

    assert argv == [
        "/bin/mutmut",
        "run",
        "--paths-to-mutate",
        "src",
        "--tests-dir",
        "tests",
        "--use-coverage",
        "--runner=pytest",
    ]


def test_mutmut_runner_default_uses_clone_root_as_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spec_032 — :class:`MutmutRunner` constructed with no overrides
    (the legacy shape) invokes mutmut from the clone root, exactly as
    spec_014 specified. The argv carries no ``--tests-dir`` and no
    extra args."""

    src = tmp_path / "src"
    (src / "ccd").mkdir(parents=True)
    (src / "ccd" / "__init__.py").write_text("# x\n", encoding="utf-8")

    captures: list[dict] = []
    monkeypatch.setattr(
        "ccd.discover.subprocess.run", _fake_subprocess_run(captures)
    )
    _stub_iso_venv(monkeypatch)

    runner = MutmutRunner()
    runner.run(repo=src, paths=["ccd"])

    # First mutmut call is the ``run`` invocation.
    assert captures
    first = captures[0]
    assert first["argv"][1] == "run"
    assert first["argv"][2] == "--paths-to-mutate"
    assert first["argv"][3] == "ccd"
    # No --tests-dir, no extra args.
    assert "--tests-dir" not in first["argv"]
    assert "--use-coverage" not in first["argv"]
    # cwd is the iso-clone root (not a subdirectory).
    assert "ccd_discover_iso_" in first["cwd"]
    assert not first["cwd"].endswith("/backend")


def test_mutmut_runner_with_cwd_runs_from_subdirectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spec_032 §2-2 — ``cwd="backend"`` makes the mutmut subprocess
    cwd land at ``<clone>/backend``."""

    src = tmp_path / "src"
    (src / "backend" / "src").mkdir(parents=True)
    (src / "backend" / "src" / "normalizer.py").write_text(
        "x = 1\n", encoding="utf-8"
    )

    captures: list[dict] = []
    monkeypatch.setattr(
        "ccd.discover.subprocess.run", _fake_subprocess_run(captures)
    )
    _stub_iso_venv(monkeypatch)

    runner = MutmutRunner(cwd="backend")
    runner.run(repo=src, paths=["src/normalizer.py"])

    assert captures
    first = captures[0]
    # cwd is the *subdirectory* of the iso-clone.
    assert "ccd_discover_iso_" in first["cwd"]
    assert first["cwd"].endswith("/backend")
    # Argv carries the paths-to-mutate verbatim.
    assert first["argv"][2:4] == ["--paths-to-mutate", "src/normalizer.py"]


def test_mutmut_runner_with_tests_dir_appends_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spec_032 §2-2 — ``tests_dir="tests"`` injects
    ``--tests-dir tests`` into the mutmut argv."""

    src = tmp_path / "src"
    (src / "ccd").mkdir(parents=True)
    (src / "ccd" / "__init__.py").write_text("", encoding="utf-8")

    captures: list[dict] = []
    monkeypatch.setattr(
        "ccd.discover.subprocess.run", _fake_subprocess_run(captures)
    )
    _stub_iso_venv(monkeypatch)

    runner = MutmutRunner(tests_dir="tests")
    runner.run(repo=src, paths=["ccd"])

    assert captures
    first = captures[0]
    assert "--tests-dir" in first["argv"]
    idx = first["argv"].index("--tests-dir")
    assert first["argv"][idx + 1] == "tests"


def test_mutmut_runner_extra_args_appear_at_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spec_032 §2-2 — ``extra_args=["--use-coverage"]`` lands at the
    end of the mutmut argv (after ``--paths-to-mutate <paths>`` and any
    ``--tests-dir <tests_dir>``)."""

    src = tmp_path / "src"
    (src / "ccd").mkdir(parents=True)
    (src / "ccd" / "__init__.py").write_text("", encoding="utf-8")

    captures: list[dict] = []
    monkeypatch.setattr(
        "ccd.discover.subprocess.run", _fake_subprocess_run(captures)
    )
    _stub_iso_venv(monkeypatch)

    runner = MutmutRunner(extra_args=["--use-coverage"])
    runner.run(repo=src, paths=["ccd"])

    assert captures
    first = captures[0]
    assert first["argv"][-1] == "--use-coverage"


# --------------------------------------------------------------------------- #
# unit test #3 — profile validation fail-fast (cwd / mutation_paths /
# tests_dir existence)
# --------------------------------------------------------------------------- #


def test_profile_rejects_nonexistent_cwd(tmp_path: Path) -> None:
    """spec_032 §2-1 — ``cwd`` that does not resolve to a directory
    under the target repo raises :class:`ProfileError`, and the
    message includes the full path that was missing."""

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir()
    # Note: no ``target_repo/nonexistent`` directory.

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            f"""
            repo = "{target_repo}"

            [discovery]
            channels = ["mutation"]

            [discovery.mutation]
            cwd = "nonexistent"
            mutation_paths = ["src"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "discovery.mutation.cwd directory not found" in msg
    assert str(target_repo / "nonexistent") in msg


def test_profile_rejects_nonexistent_mutation_paths_entry(
    tmp_path: Path,
) -> None:
    """spec_032 §2-1 — every entry in ``mutation_paths`` must exist
    under ``repo_root / cwd`` (or ``repo_root`` when ``cwd`` is None).
    Missing entries raise :class:`ProfileError` with the missing full
    path in the message."""

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir()

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            f"""
            repo = "{target_repo}"

            [discovery]
            channels = ["mutation"]

            [discovery.mutation]
            mutation_paths = ["does_not_exist.py"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "mutation_paths entry not found" in msg
    assert str(target_repo / "does_not_exist.py") in msg


def test_profile_rejects_nonexistent_tests_dir(tmp_path: Path) -> None:
    """spec_032 §2-1 — ``tests_dir`` must exist under the resolved cwd
    base. Missing → :class:`ProfileError` with the full path in the
    message."""

    target_repo = tmp_path / "target_repo"
    (target_repo / "backend" / "src").mkdir(parents=True)
    (target_repo / "backend" / "src" / "normalizer.py").write_text(
        "", encoding="utf-8"
    )
    # Note: no ``target_repo/backend/tests``.

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            f"""
            repo = "{target_repo}"

            [discovery]
            channels = ["mutation"]

            [discovery.mutation]
            cwd = "backend"
            mutation_paths = ["src/normalizer.py"]
            tests_dir = "tests"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "discovery.mutation.tests_dir not found" in msg
    assert str(target_repo / "backend" / "tests") in msg


def test_profile_collects_multiple_failures_into_one_error(
    tmp_path: Path,
) -> None:
    """Multiple existence failures are collected into one ProfileError
    (same "全部チェックする" flavour as spec_031 post-install validator)."""

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir()  # cwd / paths / tests_dir all missing.

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            f"""
            repo = "{target_repo}"

            [discovery]
            channels = ["mutation"]

            [discovery.mutation]
            cwd = "no_backend"
            mutation_paths = ["src/nope.py"]
            tests_dir = "no_tests"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError) as excinfo:
        load_profile(tmp_path)

    msg = str(excinfo.value)
    assert "cwd directory not found" in msg
    assert "mutation_paths entry not found" in msg
    assert "tests_dir not found" in msg


def test_profile_validation_skipped_for_legacy_mutation_paths(
    tmp_path: Path,
) -> None:
    """The new path-existence check applies ONLY to
    ``[discovery.mutation]``. Legacy ``mutation_paths`` at the top of
    ``[discovery]`` is NOT validated for existence (backward compat
    with spec_018 profiles where the target repo might not be checked
    out where the profile expects)."""

    profile_path = tmp_path / "_ai_workspace" / "ccd_profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        dedent(
            """
            repo = "./nowhere_at_all"

            [discovery]
            channels = ["mutation"]
            mutation_paths = ["definitely/nonexistent.py"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    # Loads without raising — legacy form is exempt from spec_032
    # existence validation.
    profile = load_profile(tmp_path)
    assert profile.discovery.mutation_paths == ["definitely/nonexistent.py"]
