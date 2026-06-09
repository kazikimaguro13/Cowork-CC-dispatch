#!/usr/bin/env bash
# nightly_all_wrapper.sh ── タスクスケジューラから呼ばれる wrapper。
#
# spec_033 で導入。タスクスケジューラ → wsl.exe → bash -c の経路で
# 複数行 here-string を直接渡すと改行が ';' 解釈されず、`& ; echo` の
# 構文衝突を起こす（手動 bash では動くがタスクスケジューラ経由だと
# LastTaskResult=2 で失敗する 7 件目の欠陥）。
#
# 複数行 bash 処理は本スクリプトに集約。タスクスケジューラからは
#   wsl.exe -d Ubuntu-24.04 -- bash <この .sh>
# の 1 行で呼べる。
#
# 設定値（PROJECT パス）は本スクリプト先頭でハードコード。複数環境で
# 使うなら fork / 環境変数化など個別運用。

set -u
# spec_034 — PROJECT を相対解決して relocation 耐性を持たせる。
# 第 1 引数があれば明示渡し優先（register_nightly.ps1 のテンプレが ProjectDir を
# 渡せる）、なければ wrapper の場所（scripts/launchers/）から repo root を導出。
# これにより、repo を別パスに clone しても wrapper が動く（test_launchers.py の
# 相対パス前提と整合する）。
# set -u 安全: ${1:-...} は $1 が unset でも default が効くため $1 を直接参照しない。
# $# 判定も同様に unset 変数を踏まない（spec_036 — set -u 相性を明文化）。
PROJECT="${1:-$(dirname "$(readlink -f "$0")")/../..}"
PROJECT="$(readlink -f "$PROJECT")"
LOG=$PROJECT/_ai_workspace/logs/nightly_task.log
mkdir -p "$(dirname "$LOG")"
echo "[$(date -Is)] nightly-all trigger fired (wrapper)" >> "$LOG"
if [ "$#" -eq 0 ]; then
  echo "[$(date -Is)] WARNING: called without explicit PROJECT argument, using relative resolution" >> "$LOG"
fi
echo "[$(date -Is)] PROJECT: $PROJECT" >> "$LOG"
cd "$PROJECT" || { echo "[$(date -Is)] cd failed" >> "$LOG"; exit 1; }
# spec_036 — activate を 1 回だけ実行し、その exit code と ccd path を
# 同一サブシェルで確定する。spec_035 では `using ccd:` 行と `venv activate exit:`
# 行が別々のサブシェルで別々に activate しており、報告する ccd と測定する exit が
# 別プロセスだった（honest 診断の自己矛盾）。1 回の activate に統合して解消する。
CCD_INFO=$(. .venv/bin/activate 2>/dev/null; ac=$?; printf '%s|%s' "$ac" "$(command -v ccd 2>/dev/null || echo 'NOT FOUND')")
echo "[$(date -Is)] using ccd: ${CCD_INFO#*|} (venv activate exit: ${CCD_INFO%%|*})" >> "$LOG"
# spec_037 — 旧実装は detach（nohup setsid）でトリガー本体を即時返していた。
# 2026-06-07 hotfix — タスクスケジューラ → wsl.exe → 非対話 bash 経路では、
# トリガー即時返却で wsl.exe が終了すると WSL VM がスピンダウンし、setsid 子プロセス
# ごと刈られて nightly-all が完走しないことを実機で確認（5/28 以降 index 未生成、
# 6/2 は detached PID 631 まで出るも完了行・index なし）。spec_037 の「setsid は親
# exit 後も生存」は対話 WSL 前提で、本番経路では成立しなかった。
# → タスク内で同期実行し、タスク完了まで wsl を保持する。ExecutionTimeLimit=PT6H が上限ガード。
echo "[$(date -Is)] nightly-all starting (synchronous hotfix 2026-06-07)" >> "$LOG"
. .venv/bin/activate 2>/dev/null
ccd nightly-all --repo "$PROJECT" >> "$LOG" 2>&1
rc=$?
echo "[$(date -Is)] nightly-all finished (rc=$rc)" >> "$LOG"
exit $rc
