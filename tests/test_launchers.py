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
    """spec_036 — wrapper の PROJECT 代入が relocation 耐性のある既知の 2 形のみであること。

    spec_035 は `PROJECT=/home/` の文字列不在を assert するだけで、`/Users/`・
    `/opt/`・`$HOME`・変数経由のハードコード復活を見逃していた（subagent 指摘 B）。
    本テストは whitelist 方式：PROJECT への代入行が
      (1) `PROJECT="${1:-...}"` （第 1 引数優先 + 相対解決フォールバック）
      (2) `PROJECT="$(readlink ...)"` （正規化）
    のいずれか以外なら fail させる。これで絶対パス・$HOME・任意変数のハードコードを
    形を問わず弾く（silent regression 防護網）。
    """
    real_wrapper = (
        Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    )
    content = real_wrapper.read_text(encoding="utf-8")
    assigns = [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("PROJECT=") and not line.strip().startswith("#")
    ]
    assert assigns, "PROJECT 代入が 1 つも無い（wrapper 構造が壊れている）"
    allowed = ('PROJECT="${1:-', 'PROJECT="$(readlink')
    for a in assigns:
        assert a.startswith(allowed), (
            f"想定外の PROJECT 代入（relocation 耐性を壊しうる）: {a!r}"
        )
    assert any(a.startswith('PROJECT="${1:-') for a in assigns), (
        "PROJECT の相対解決 idiom (${1:-...}) が失われている"
    )


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
    # 明示渡し → WARNING が出ない（既存）+ 渡した PROJECT が採用されている（spec_036 合流）
    assert str(tmp_repo) in content2, (
        f"明示渡しの PROJECT がログに採用されていない. log:\n{content2}"
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


def test_nightly_all_wrapper_unifies_ccd_and_activate_exit(tmp_path) -> None:
    """spec_036 — using ccd と venv activate exit が同一行（=同一 activate）で記録される。

    spec_035 は両者を別々のサブシェルで別々に activate していた（subagent 指摘 A）。
    spec_036 で 1 回の activate に統合したことの回帰防止：`venv activate exit:` を
    含む行は必ず `using ccd:` も含む（別行なら二重 activate の疑い）。
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
    exit_lines = [ln for ln in content.splitlines() if "venv activate exit:" in ln]
    assert exit_lines, f"venv activate exit 行が無い. log:\n{content}"
    assert all("using ccd:" in ln for ln in exit_lines), (
        f"using ccd と venv activate exit が別行（二重 activate の疑い）. log:\n{content}"
    )


def test_nightly_all_wrapper_detaches_without_disown() -> None:
    """spec_037 — wrapper が nohup+setsid で detach し、冗長な disown を持たないこと。

    実測（bash 5.2.21 / huponexit off / 非対話 job control off）で disown は
    プロセス生存に不要（冗長防御）と確認したため削除した。回帰防止：disown コマンドが
    復活していない（コメント言及は許容）& nohup setsid の detach idiom は維持。
    """
    real_wrapper = (
        Path(__file__).parent.parent / "scripts" / "launchers" / "nightly_all_wrapper.sh"
    )
    content = real_wrapper.read_text(encoding="utf-8")
    code_lines = [
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    ]
    assert not any("disown" in line for line in code_lines), (
        "冗長な disown コマンドが wrapper に復活している（spec_037 で削除済み）"
    )
    assert "nohup setsid" in content, "nohup setsid の detach idiom が失われている"
