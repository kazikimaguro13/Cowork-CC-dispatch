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
echo "[$(date -Is)] using ccd: $(. .venv/bin/activate 2>/dev/null; command -v ccd 2>&1 || echo 'NOT FOUND')" >> "$LOG"
( . .venv/bin/activate 2>/dev/null )
echo "[$(date -Is)] venv activate exit: $?" >> "$LOG"
nohup setsid bash -c ". .venv/bin/activate 2>/dev/null; ccd nightly-all --repo $PROJECT >> $LOG 2>&1" </dev/null >/dev/null 2>&1 &
PID=$!
disown 2>/dev/null || true
echo "[$(date -Is)] nightly-all detached (PID $PID)" >> "$LOG"
