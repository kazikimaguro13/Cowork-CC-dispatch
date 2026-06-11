"""spec_046 — mutation 発見 / R5 検証から重い統合テストを除外する。

2026-06-11 の auto 初実走で、protocol.py 1 ファイルの mutation 発見 + R5 検証が
1 時間半経っても抜けられなかった。根本原因: mutmut はミュータント 1 体ごとに
テストスイートを丸ごと回すが、CCD のスイートには ``ccd nightly-all`` / mutmut /
iso-venv プロビジョニングを実際に起動する統合テストが含まれ、それが mutmut の
subprocess の中で入れ子に再起動して所要時間が非線形に膨らんでいた。

対策 (方針A — マーカー除外): 重い統合テストに ``@pytest.mark.slow`` を付け、
CCD profile の mutation ランナーを ``-m "not slow"`` 付きにして発見/R5 検証から
除外する。最終ゲート (R4 live 再検証) は従来どおりフルスイート。

これらのテストが固定する不変条件:

1. 重い統合テスト (nightly-all / mutmut / iso-venv を起動) が ``slow`` マーカーで
   mutation サブセットから除外される (受け入れ基準 1)。
2. R4 のベースライン/修正後はサブセットに触れず常にフルスイートで測られ、基準が
   ズレないので正常系で偽陽性 halt しない (受け入れ基準 2)。
3. live 最終再検証 (``_default_suite_runner``) は ``-m`` を渡さずフルスイートのまま
   (受け入れ基準 3)。
4. R5 判定 (killed / survived / unknown) のロジックは不変。サブセット化は「回す
   テスト集合」だけで、判定基準は変えない (受け入れ基準 4)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccd.discover import Mutant, MutationRunOutcome, MutmutRunner
from ccd.nightly import (
    _build_default_mutation_rechecker,
    _default_suite_runner,
    _mutmut_runner_from_config,
)
from ccd.profile import MutationConfig, effective_mutation_config, load_profile

# Repo root (this file lives in tests/).
_REPO = Path(__file__).resolve().parent.parent

# The exact set of heavy integration tests spec_046 marks `slow`. Pinned by
# nodeid so that *removing* a marker (re-introducing the cost explosion) or
# silently widening the set is caught. Each entry actually spawns a real
# `ccd nightly-all`, a real iso-venv (`python -m venv` + `pip install -e`),
# or a 2s dispatch-timeout sleep — i.e. work that nests / bloats per-mutant.
_HEAVY_SLOW_TESTS: tuple[tuple[str, str], ...] = (
    ("tests.test_launchers", "test_nightly_all_wrapper_resolves_project_relatively"),
    ("tests.test_launchers", "test_nightly_all_wrapper_accepts_explicit_project_argument"),
    ("tests.test_launchers", "test_nightly_all_wrapper_warns_without_explicit_argument"),
    ("tests.test_launchers", "test_nightly_all_wrapper_logs_venv_activate_exit"),
    ("tests.test_launchers", "test_nightly_all_wrapper_unifies_ccd_and_activate_exit"),
    ("tests.test_discover", "test_provision_iso_venv_creates_clone_local_python"),
    ("tests.test_nightly", "test_dispatch_timeout_marks_candidate_failed"),
)


def _has_slow_marker(func) -> bool:
    """True iff ``func`` carries a decorator-form ``@pytest.mark.slow``.

    Decorators populate ``func.pytestmark`` (a list of ``Mark``). We read
    that list directly — deliberately NOT the module-level ``pytestmark =``
    assignment form, which guard R2 (spec_043) flags as a skip vector.
    """

    return any(
        m.name == "slow" for m in getattr(func, "pytestmark", [])
    )


def _load_test_func(module_name: str, func_name: str):
    import importlib

    mod = importlib.import_module(module_name)
    return getattr(mod, func_name)


# --------------------------------------------------------------------------- #
# 受け入れ基準 1 — 重い統合テストがサブセットから除外される
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module_name,func_name", _HEAVY_SLOW_TESTS)
def test_heavy_integration_test_carries_slow_marker(
    module_name: str, func_name: str
) -> None:
    """Each known-heavy test is marked ``slow`` (decorator form) so the
    mutation runner's ``-m "not slow"`` deselects it."""

    func = _load_test_func(module_name, func_name)
    assert _has_slow_marker(func), (
        f"{module_name}.{func_name} lost its @pytest.mark.slow marker — "
        "mutation testing would re-run this heavy integration test PER "
        "mutant (spec_046 cost explosion)."
    )


def test_a_fast_unit_test_is_not_marked_slow() -> None:
    """A representative pure-unit test must NOT be ``slow`` — otherwise the
    marker is being over-applied and the mutation subset would shrink past
    the tests that actually kill mutants."""

    func = _load_test_func("tests.test_smoke", "test_version_is_0270")
    assert not _has_slow_marker(func)


def test_slow_marker_is_registered_in_pyproject() -> None:
    """The ``slow`` marker is declared under ``[tool.pytest.ini_options]``
    so ``-m "not slow"`` is a recognised selector (and ``--strict-markers``
    deployments don't error)."""

    body = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "markers = [" in body
    assert '"slow:' in body


@pytest.mark.parametrize(
    "profile_path",
    ["_ai_workspace/profiles/ccd.toml", "_ai_workspace/ccd_profile.toml"],
)
def test_ccd_profile_mutation_runner_excludes_slow(profile_path: str) -> None:
    """The CCD profile drives mutmut with a ``--runner`` that excludes the
    ``slow`` subset. This is what actually wires the marker to the mutation
    discovery / R5 recheck (criterion 1, profile side).

    spec_046 honest note: the mutation isolation clone EXCLUDES
    ``_ai_workspace/`` (``_ISOLATION_IGNORE`` in ccd/discover.py), so when
    mutmut re-runs this very suite inside its clone the profile file is
    absent. We skip rather than read a default-empty profile and fail —
    otherwise this test would poison mutmut's own baseline run (a
    self-inflicted 0-mutants HALT). In the live repo / CI the file is
    present and the assertion runs for real."""

    path = _REPO / profile_path
    if not path.exists():
        pytest.skip(
            f"{profile_path} absent (mutation clone excludes _ai_workspace/)"
        )
    prof = load_profile(_REPO, path)
    cfg = effective_mutation_config(prof.discovery)
    assert cfg.mutation_paths == ["ccd/protocol.py"]
    # `--runner <cmd>` appears as two consecutive extra_args.
    assert "--runner" in cfg.extra_args
    runner_idx = cfg.extra_args.index("--runner")
    runner_cmd = cfg.extra_args[runner_idx + 1]
    # The runner is pytest-based and filters out the slow marker.
    assert "pytest" in runner_cmd
    assert "-m" in runner_cmd
    assert "not slow" in runner_cmd


# --------------------------------------------------------------------------- #
# 受け入れ基準 2 / 3 — R4 (baseline/post-fix & live) は常にフルスイート
# --------------------------------------------------------------------------- #


def test_r4_suite_runner_runs_full_suite_without_subset_filter(monkeypatch) -> None:
    """``_default_suite_runner`` — the R4 gate (both the pre-fix baseline and
    the post-fix / live re-check) — must invoke pytest WITHOUT any ``-m "not
    slow"`` filter.

    Why this pins both criteria 2 and 3: R4 compares executed test counts
    pre- vs post-fix (spec_043). Both measurements go through THIS one
    runner, so they are subset-identical by construction → no false-positive
    halt (criterion 2). And because no marker filter is applied, the final /
    live R4 re-check sees the FULL suite — the lightweight subset is a
    mutmut-only concern and never weakens the merge gate (criterion 3)."""

    import ccd.nightly as nightly

    captured: dict[str, list[str]] = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "713 passed in 1.00s\ncollected 713 items\n"
        stderr = ""

    def _fake_run(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = list(argv)
        return _FakeCompleted()

    monkeypatch.setattr(nightly.subprocess, "run", _fake_run)
    _default_suite_runner(repo=_REPO)

    argv = captured["argv"]
    assert argv[0] == "pytest"
    # The R4 suite is the full suite — no marker selection of any kind.
    assert "-m" not in argv
    assert "not slow" not in argv
    assert "slow" not in " ".join(argv)


# --------------------------------------------------------------------------- #
# 受け入れ基準 4 — R5 recheck はサブセットを使うが判定基準は不変
# --------------------------------------------------------------------------- #


def test_rechecker_default_runner_inherits_profile_subset() -> None:
    """When no runner is injected, the R5 recheck's default ``MutmutRunner``
    carries the profile's ``cwd`` / ``tests_dir`` / ``extra_args`` — so the
    recheck runs over the SAME lightweight subset as discovery instead of
    silently falling back to the full suite."""

    cfg = MutationConfig(
        mutation_paths=["ccd/protocol.py"],
        cwd="sub",
        tests_dir="tests",
        extra_args=["--runner", "python -m pytest -m 'not slow'"],
    )
    runner = _mutmut_runner_from_config(None, cfg)
    assert isinstance(runner, MutmutRunner)
    assert runner._cwd == "sub"
    assert runner._tests_dir == "tests"
    assert runner._extra_args == ["--runner", "python -m pytest -m 'not slow'"]


def test_rechecker_honours_explicit_injected_runner() -> None:
    """An explicitly injected runner (test double) is returned verbatim —
    config only builds the *default*, it never overrides an injection."""

    sentinel = object()
    assert _mutmut_runner_from_config(sentinel, MutationConfig(mutation_paths=["x"])) is sentinel  # type: ignore[arg-type]


def test_rechecker_default_runner_without_config_is_bare() -> None:
    """No config → a bare ``MutmutRunner`` (backward compatible with the
    pre-spec_046 default)."""

    runner = _mutmut_runner_from_config(None, None)
    assert isinstance(runner, MutmutRunner)
    assert runner._cwd is None
    assert runner._tests_dir is None
    assert runner._extra_args == []


class _FakeMutationRunner:
    """Returns a fixed ``MutationRunOutcome`` so the R5 recheck's
    *judgment* (not the subset) can be exercised deterministically."""

    def __init__(self, outcome: MutationRunOutcome) -> None:
        self._outcome = outcome

    def run(self, *, repo, paths=None):  # noqa: ANN001, ARG002
        return self._outcome


_TARGET_SIG = "ccd/protocol.py:10:a → b"


def test_r5_judgment_unchanged_target_survived() -> None:
    """Target signature still present as a survivor → R5 says ``survived``
    (the fix did NOT kill it). Unchanged from pre-spec_046 behaviour."""

    outcome = MutationRunOutcome(
        mutants=[Mutant(file="ccd/protocol.py", line=10, mutation="a → b", status="survived")],
        tool="mutmut",
    )
    recheck = _build_default_mutation_rechecker(_FakeMutationRunner(outcome))
    verdict = recheck(
        repo=_REPO, file="ccd/protocol.py", line=10, mutation="a → b", signature=_TARGET_SIG
    )
    assert verdict == "survived"


def test_r5_judgment_unchanged_target_killed() -> None:
    """Target signature absent from the survivor lists → R5 says ``killed``
    (the fix closed the gap)."""

    outcome = MutationRunOutcome(
        mutants=[Mutant(file="ccd/protocol.py", line=99, mutation="c → d", status="survived")],
        tool="mutmut",
    )
    recheck = _build_default_mutation_rechecker(_FakeMutationRunner(outcome))
    verdict = recheck(
        repo=_REPO, file="ccd/protocol.py", line=10, mutation="a → b", signature=_TARGET_SIG
    )
    assert verdict == "killed"


def test_r5_judgment_unchanged_unknown_on_failed_run() -> None:
    """A run that fails to produce any mutants → R5 says ``unknown`` so the
    loop halts conservatively rather than merging on a false positive."""

    outcome = MutationRunOutcome(mutants=[], tool="mutmut", error="boom")
    recheck = _build_default_mutation_rechecker(_FakeMutationRunner(outcome))
    verdict = recheck(
        repo=_REPO, file="ccd/protocol.py", line=10, mutation="a → b", signature=_TARGET_SIG
    )
    assert verdict == "unknown"
