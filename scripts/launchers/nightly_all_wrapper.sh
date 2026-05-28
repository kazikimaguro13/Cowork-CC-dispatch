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
PROJECT=/home/nakashima/projects/Cowork-CC-dispatch
LOG=$PROJECT/_ai_workspace/logs/nightly_task.log
mkdir -p "$(dirname "$LOG")"
echo "[$(date -Is)] nightly-all trigger fired (wrapper)" >> "$LOG"
cd "$PROJECT" || { echo "[$(date -Is)] cd failed" >> "$LOG"; exit 1; }
nohup setsid bash -c ". .venv/bin/activate 2>/dev/null; ccd nightly-all --repo $PROJECT >> $LOG 2>&1" </dev/null >/dev/null 2>&1 &
PID=$!
disown 2>/dev/null || true
echo "[$(date -Is)] nightly-all detached (PID $PID)" >> "$LOG"
