"""spec_033: wrapper script の構文整合性と shebang 妥当性を pin する。

タスクスケジューラ経由起動の integration test は Windows タスクスケジューラに
依存するため CI では走らせない（人間が手動 Start-ScheduledTask で確認する）。
本テストは wrapper script の bash 構文と shebang を unit レベルで担保する。
"""
import subprocess
from pathlib import Path


def test_nightly_all_wrapper_bash_syntax_valid() -> None:
    """`bash -n` で構文エラーが無いこと。"""
    wrapper = Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    assert wrapper.is_file(), f"wrapper script not found at {wrapper}"
    result = subprocess.run(
        ["bash", "-n", str(wrapper)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"bash syntax check failed: {result.stderr}"
    )


def test_nightly_all_wrapper_has_proper_shebang() -> None:
    """先頭が `#!/usr/bin/env bash` で始まること。"""
    wrapper = Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    first_line = wrapper.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env bash", (
        f"expected shebang '#!/usr/bin/env bash', got {first_line!r}"
    )


def test_nightly_all_wrapper_is_executable() -> None:
    """実行ビットが立っていること（git add --chmod=+x 経由）。"""
    wrapper = Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    import stat
    mode = wrapper.stat().st_mode
    assert mode & stat.S_IXUSR, "wrapper script is not user-executable"


def test_nightly_all_wrapper_resolves_project_relatively(tmp_path) -> None:
    """spec_034 — wrapper の場所から PROJECT を相対解決できる。

    tmp_path に repo root と scripts/launchers/wrapper を再現し、wrapper を
    bash で実行 → PROJECT の値（ログ書き込み）が repo root に解決されること
    を確認。
    """
    import shutil
    tmp_repo = tmp_path / "Cowork-CC-dispatch"
    (tmp_repo / "scripts" / "launchers").mkdir(parents=True)
    (tmp_repo / "_ai_workspace" / "logs").mkdir(parents=True)
    real_wrapper = Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    target = tmp_repo / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    shutil.copy(real_wrapper, target)
    target.chmod(0o755)
    # wrapper の cd と ccd nightly-all は失敗してよい (.venv も ccd も無い)
    # ログだけ確認する
    log = tmp_repo / "_ai_workspace" / "logs" / "nightly_task.log"
    subprocess.run(["bash", str(target)], capture_output=True, text=True, check=False)
    assert log.exists(), "log not created"
    content = log.read_text(encoding="utf-8")
    assert f"PROJECT: {tmp_repo}" in content or f"PROJECT: {tmp_repo.resolve()}" in content, (
        f"PROJECT not resolved to tmp repo root. log content:\n{content}"
    )


def test_nightly_all_wrapper_accepts_explicit_project_argument(tmp_path) -> None:
    """spec_034 — 第 1 引数で PROJECT を明示渡しできる。

    register_nightly.ps1 のテンプレが `bash $WrapperScript "$ProjectDir"` で
    呼ぶ運用パターンを想定。
    """
    import shutil
    explicit_project = tmp_path / "ExplicitProject"
    (explicit_project / "_ai_workspace" / "logs").mkdir(parents=True)
    real_wrapper = Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    target = tmp_path / "wrapper.sh"
    shutil.copy(real_wrapper, target)
    target.chmod(0o755)
    subprocess.run(
        ["bash", str(target), str(explicit_project)],
        capture_output=True, text=True, check=False,
    )
    log = explicit_project / "_ai_workspace" / "logs" / "nightly_task.log"
    assert log.exists(), "log not created in explicit project"
    content = log.read_text(encoding="utf-8")
    assert str(explicit_project) in content, (
        f"explicit PROJECT not used. log content:\n{content}"
    )


def test_nightly_all_wrapper_has_no_hardcoded_project_path() -> None:
    """spec_035 — wrapper に PROJECT のハードコード絶対パスが復活していないこと。

    spec_034 で PROJECT を相対解決にしたが、positive test だけでは「うっかり
    ハードコードに戻す」regression を検出できない。本テストは wrapper 本文に
    `PROJECT=/home/...` のような絶対パス直接代入が無いことを assert する
    guardrail（silent regression 防護網）。相対解決 idiom が残っていることも確認。
    """
    real_wrapper = (
        Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    )
    content = real_wrapper.read_text(encoding="utf-8")
    assert "PROJECT=/home/" not in content, (
        "PROJECT がハードコード絶対パスに戻っている（relocation 耐性が壊れる）"
    )
    assert 'PROJECT="/home/' not in content, (
        "PROJECT がハードコード絶対パス（quote 付き）に戻っている"
    )
    assert 'PROJECT="${1:-' in content, "PROJECT の相対解決 idiom が失われている"


def test_register_nightly_template_exists() -> None:
    """spec_035 — examples/register_nightly.ps1.template が repo に存在する。

    register_nightly.ps1 は _ai_workspace/ 配下で git 管理外のため、運用切替時の
    参照点として examples/ に明示渡し版テンプレを置く（§2-i）。
    """
    template = (
        Path(__file__).parent.parent / "examples" / "register_nightly.ps1.template"
    )
    assert template.exists(), "examples/register_nightly.ps1.template が無い"
    body = template.read_text(encoding="utf-8")
    assert "nightly_all_wrapper.sh" in body, "wrapper 呼び出しが template に無い"
    assert "$ProjectDir" in body, "ProjectDir プレースホルダが template に無い"


def test_nightly_all_wrapper_warns_without_explicit_argument(tmp_path) -> None:
    """spec_035 — wrapper が引数なしで呼ばれたらログに WARNING を残す（§2-ii）。

    明示渡し（第 1 引数あり）では WARNING が出ないことも確認。
    """
    import shutil

    tmp_repo = tmp_path / "Cowork-CC-dispatch"
    (tmp_repo / "scripts" / "launchers").mkdir(parents=True)
    (tmp_repo / "_ai_workspace" / "logs").mkdir(parents=True)
    real_wrapper = (
        Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    )
    target = tmp_repo / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    shutil.copy(real_wrapper, target)
    target.chmod(0o755)
    log = tmp_repo / "_ai_workspace" / "logs" / "nightly_task.log"

    # 引数なし → WARNING が出る
    subprocess.run(["bash", str(target)], capture_output=True, text=True, check=False)
    content = log.read_text(encoding="utf-8")
    assert "WARNING: called without explicit PROJECT argument" in content, (
        f"引数なし呼び出しの WARNING が無い. log:\n{content}"
    )

    # 明示渡し → WARNING が出ない
    log.unlink()
    subprocess.run(
        ["bash", str(target), str(tmp_repo)], capture_output=True, text=True, check=False
    )
    content2 = log.read_text(encoding="utf-8")
    assert "WARNING: called without explicit PROJECT argument" not in content2, (
        f"明示渡しなのに WARNING が出ている. log:\n{content2}"
    )


def test_nightly_all_wrapper_logs_venv_activate_exit(tmp_path) -> None:
    """spec_035 — wrapper が venv activate の exit code をログに明示記録する（§3-b）。

    `using ccd:` 行だけでは venv の ccd か system の ccd か判別できないため、
    activate の exit code を別行で記録する。.venv 不在の tmp_repo では activate が
    失敗（非 0）するが、行自体が記録されることを確認。
    """
    import shutil

    tmp_repo = tmp_path / "Cowork-CC-dispatch"
    (tmp_repo / "scripts" / "launchers").mkdir(parents=True)
    (tmp_repo / "_ai_workspace" / "logs").mkdir(parents=True)
    real_wrapper = (
        Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    )
    target = tmp_repo / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    shutil.copy(real_wrapper, target)
    target.chmod(0o755)
    subprocess.run(["bash", str(target)], capture_output=True, text=True, check=False)
    log = tmp_repo / "_ai_workspace" / "logs" / "nightly_task.log"
    content = log.read_text(encoding="utf-8")
    assert "venv activate exit:" in content, (
        f"venv activate exit code がログに記録されていない. log:\n{content}"
    )
