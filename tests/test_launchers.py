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
