"""ccd self-retrospective (spec_012).

`run_retrospect` is the first feedback path that turns ccd's accumulated
dispatch history into a structured analysis. It:

  1. Gathers evidence — run JSON files, `result_*.md` notes, recent git
     history.
  2. Computes a deterministic factual summary in Python (dispatch counts /
     status breakdown / result-file count / recent commit count). This is
     the "honesty anchor" — the agent should not invent different numbers.
  3. Generates a self-contained review-task spec under
     `_ai_workspace/retro/retro_spec.md`.
  4. Dispatches that spec through the `AgentRunner` abstraction — the same
     `ClaudeCodeRunner` / `FakeAgentRunner` seam the rest of ccd uses, so
     `tests/test_retrospect.py` can inject a fake runner.
  5. Verifies the agent wrote `_ai_workspace/retro/retro_NNN.md` plus one
     or more `_ai_workspace/retro/proposals/<slug>.md` files.

The proposals are *seeds*, not full specs. Retrospect deliberately does
not auto-promote them into `_ai_workspace/bridge/inbox/` and does not
auto-dispatch — that would short-circuit the human-in-the-loop /
grill-me discipline this codebase relies on for spec quality.

Routing note: retrospect calls `AgentRunner.run` directly rather than
threading through `dispatch_one`. Dispatch classifies on result-file
presence and commit count, which would force the agent into an
implementation-task shape (write `result_retro_NNN.md`, make a commit).
The retrospective is an analysis task — no commits expected, output goes
to `_ai_workspace/retro/`. The pass/fail signal is "did the retro files
appear?", computed inside `run_retrospect`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .agent import AgentRunner
from .models import Spec

DEFAULT_RUNS_DIR_REL = Path("_ai_workspace") / "runs"
DEFAULT_LEGACY_LOGS_REL = Path("_ai_workspace") / "logs"
DEFAULT_OUTBOX_REL = Path("_ai_workspace") / "bridge" / "outbox"
DEFAULT_RETRO_DIR_REL = Path("_ai_workspace") / "retro"
DEFAULT_LIMIT = 50

_GIT_STAT_RECENT = 10


@dataclass(frozen=True)
class FactualSummary:
    """Deterministic facts about the evidence corpus.

    Same input → same numbers. The retrospective agent must quote these
    numbers verbatim rather than re-estimating them from the raw files.
    """

    runs_scanned: int
    records_total: int
    status_breakdown: dict[str, int]
    failure_category_breakdown: dict[str, int]
    result_files: int
    recent_commits: int


@dataclass(frozen=True)
class Evidence:
    run_files: list[Path]
    result_files: list[Path]
    git_log: str
    summary: FactualSummary


@dataclass
class RetrospectResult:
    success: bool
    review_spec_path: Path
    retro_path: Path | None
    proposal_paths: list[Path]
    summary: FactualSummary
    halt_reason: str = ""
    runner_invoked: bool = False


def run_retrospect(
    runner: AgentRunner,
    *,
    repo: Path,
    runs_dir: Path | None = None,
    limit: int = DEFAULT_LIMIT,
    retro_dir: Path | None = None,
) -> RetrospectResult:
    """Drive one retrospective end-to-end.

    The retrospective fails gracefully (success=False, halt_reason set,
    runner not invoked) when there is no evidence to analyze. Otherwise
    it dispatches the generated review spec to ``runner`` and checks
    that the agent wrote the expected output files.
    """

    repo = Path(repo).resolve()
    evidence = collect_evidence(repo=repo, runs_dir=runs_dir, limit=limit)

    retro_root = (
        Path(retro_dir).resolve()
        if retro_dir is not None
        else repo / DEFAULT_RETRO_DIR_REL
    )
    proposals_dir = retro_root / "proposals"
    retro_root.mkdir(parents=True, exist_ok=True)
    proposals_dir.mkdir(parents=True, exist_ok=True)

    seq = _next_retro_seq(retro_root)
    retro_filename = f"retro_{seq:03d}.md"
    spec_id = f"spec_retro_{seq:03d}"

    review_spec_body = _build_review_spec_body(
        repo=repo,
        evidence=evidence,
        retro_filename=retro_filename,
    )
    review_spec_path = retro_root / "retro_spec.md"
    review_spec_path.write_text(
        f"# {spec_id}: ccd self-retrospective {seq:03d}\n\n{review_spec_body}\n",
        encoding="utf-8",
    )

    if evidence.summary.records_total == 0 and evidence.summary.result_files == 0:
        return RetrospectResult(
            success=False,
            review_spec_path=review_spec_path,
            retro_path=None,
            proposal_paths=[],
            summary=evidence.summary,
            halt_reason=(
                "no evidence: 0 run records and 0 result files — "
                "nothing meaningful to analyze"
            ),
            runner_invoked=False,
        )

    proposals_before = set(proposals_dir.glob("*.md"))

    spec = Spec(
        id=spec_id,
        title=f"ccd self-retrospective {seq:03d}",
        body=review_spec_body,
        path=review_spec_path,
    )

    runner.run(spec, workdir=repo)

    retro_path = retro_root / retro_filename
    proposals_after = set(proposals_dir.glob("*.md"))
    new_proposals = sorted(proposals_after - proposals_before)

    if not retro_path.exists():
        return RetrospectResult(
            success=False,
            review_spec_path=review_spec_path,
            retro_path=None,
            proposal_paths=new_proposals,
            summary=evidence.summary,
            halt_reason=f"agent did not write {retro_path}",
            runner_invoked=True,
        )

    if not new_proposals:
        return RetrospectResult(
            success=False,
            review_spec_path=review_spec_path,
            retro_path=retro_path,
            proposal_paths=[],
            summary=evidence.summary,
            halt_reason=(
                f"agent wrote {retro_path.name} but produced no proposals "
                f"under {proposals_dir}"
            ),
            runner_invoked=True,
        )

    return RetrospectResult(
        success=True,
        review_spec_path=review_spec_path,
        retro_path=retro_path,
        proposal_paths=new_proposals,
        summary=evidence.summary,
        runner_invoked=True,
    )


# --------------------------------------------------------------------------- #
# Evidence collection
# --------------------------------------------------------------------------- #


def collect_evidence(
    *,
    repo: Path,
    runs_dir: Path | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Evidence:
    """Gather run JSON / result files / git log into one bundle.

    ``runs_dir`` defaults to ``<repo>/_ai_workspace/runs``. If that
    directory is empty or missing, legacy ``<repo>/_ai_workspace/logs/
    *_run.json`` files are also pulled in (the bash-bridge era wrote run
    JSONs there).
    """

    repo = Path(repo).resolve()
    runs_root = (
        Path(runs_dir).resolve()
        if runs_dir is not None
        else repo / DEFAULT_RUNS_DIR_REL
    )

    run_files: list[Path] = []
    if runs_root.exists():
        run_files.extend(sorted(runs_root.glob("*.json")))

    legacy_dir = repo / DEFAULT_LEGACY_LOGS_REL
    if legacy_dir.exists():
        # Restrict to *_run.json so we don't slurp `last_run.json` /
        # `chain.done` / per-spec dispatch logs.
        run_files.extend(sorted(legacy_dir.glob("*_run.json")))

    records = _load_records(run_files)

    outbox = repo / DEFAULT_OUTBOX_REL
    result_files = (
        sorted(outbox.glob("result_*.md")) if outbox.exists() else []
    )

    git_log = _git_log(repo, limit=limit)
    recent_commits = _count_oneline_commits(git_log)

    status_breakdown: dict[str, int] = {}
    fc_breakdown: dict[str, int] = {}
    for r in records:
        status = str(r.get("status") or "unknown")
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        fc = r.get("failure_category")
        if fc:
            fc_breakdown[str(fc)] = fc_breakdown.get(str(fc), 0) + 1

    summary = FactualSummary(
        runs_scanned=len(run_files),
        records_total=len(records),
        status_breakdown=dict(sorted(status_breakdown.items())),
        failure_category_breakdown=dict(sorted(fc_breakdown.items())),
        result_files=len(result_files),
        recent_commits=recent_commits,
    )
    return Evidence(
        run_files=run_files,
        result_files=result_files,
        git_log=git_log,
        summary=summary,
    )


def _load_records(run_files: list[Path]) -> list[dict]:
    records: list[dict] = []
    for rf in run_files:
        try:
            payload = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw = payload.get("records", [])
        if not isinstance(raw, list):
            continue
        for r in raw:
            if isinstance(r, dict):
                records.append(r)
    return records


def _git_log(repo: Path, *, limit: int) -> str:
    if not (repo / ".git").exists():
        return ""
    try:
        oneline = subprocess.run(
            ["git", "log", "--oneline", f"-{limit}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        stat = subprocess.run(
            ["git", "log", "--stat", f"-{min(limit, _GIT_STAT_RECENT)}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if oneline.returncode != 0:
        return ""
    parts = [
        "## git log --oneline",
        oneline.stdout.rstrip(),
        "",
        f"## git log --stat (latest {min(limit, _GIT_STAT_RECENT)})",
        stat.stdout.rstrip() if stat.returncode == 0 else "(unavailable)",
    ]
    return "\n".join(parts).rstrip() + "\n"


def _count_oneline_commits(git_log: str) -> int:
    if not git_log:
        return 0
    n = 0
    in_oneline = False
    for line in git_log.splitlines():
        stripped = line.strip()
        if stripped.startswith("## git log --oneline"):
            in_oneline = True
            continue
        if stripped.startswith("## "):
            in_oneline = False
            continue
        if in_oneline and stripped:
            n += 1
    return n


def _next_retro_seq(retro_dir: Path) -> int:
    nums: list[int] = []
    for p in retro_dir.glob("retro_*.md"):
        m = re.match(r"retro_(\d+)\.md$", p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


# --------------------------------------------------------------------------- #
# Review-spec body
# --------------------------------------------------------------------------- #


def _build_review_spec_body(
    *,
    repo: Path,
    evidence: Evidence,
    retro_filename: str,
) -> str:
    summary = evidence.summary

    run_section = _path_list(evidence.run_files, repo)
    result_section = _path_list(evidence.result_files, repo)

    body_parts = [
        "## 1. このタスクは何か",
        "",
        "`ccd` 自身の dispatch 履歴・result メモ・直近 git 履歴を読み、",
        "「spec → dispatch → fix ループのどこが非効率か」を定性的に分析する",
        "レトロスペクティブタスク。出力は人間が読んで grill-me で正式 spec に",
        "育てるための **改善提案の \"種\"** であり、フル spec ではない。",
        "",
        "## 2. 評価母数 (決定的に算出した事実)",
        "",
        f"- 調べた run JSON: **{summary.runs_scanned}** 件",
        f"- 集計対象 `DispatchRecord`: **{summary.records_total}** 件",
        f"- status 内訳: {_render_breakdown(summary.status_breakdown)}",
        (
            "- failure_category 内訳: "
            f"{_render_breakdown(summary.failure_category_breakdown)}"
        ),
        f"- `result_*.md` ファイル: **{summary.result_files}** 件",
        f"- 直近の commit (`git log --oneline`): **{summary.recent_commits}** 件",
        "",
        "**この数値は決定的に Python で算出済み — レトロスペクティブの",
        "「正直さのアンカー」として使うこと。** 自分で別の数値を推測しない。",
        "判断できないことは「判断できなかった」と書く。",
        "",
        "## 3. 証拠ファイル (実際に読むこと)",
        "",
        "### 3-1. run JSON",
        "",
        run_section,
        "",
        "### 3-2. result_*.md",
        "",
        result_section,
        "",
        "### 3-3. git log (`--oneline` + 直近の `--stat`)",
        "",
        "```",
        (evidence.git_log.strip() or "(no commits available)"),
        "```",
        "",
        "## 4. やってほしいこと",
        "",
        "1. 上記の証拠ファイルを実際に読み込む (run JSON / result_*.md / git log)。",
        f"2. `_ai_workspace/retro/{retro_filename}` に **レトロスペクティブ本体**",
        "   を書く。次の構造で:",
        "    - **評価母数** — §2 の数値を引き写し、レトロスペクティブの土台として明示",
        "    - **観測した摩擦点 / 非効率** — 各項目に **具体的な証拠**",
        "      (run id / `result_NNN.md` ファイル名 / コミットハッシュ) を引用",
        "    - **改善提案** — 各提案は `_ai_workspace/retro/proposals/<短いslug>.md`",
        "      に **1 ファイルずつ** 書き、本体からリンクする",
        "    - **データから判断できなかったこと** — 明示する正直さの節",
        "3. 各 proposal ファイルは = 問題 / 根拠 (証拠引用) / 提案の方向 /",
        "   概算スコープ / 優先度。**フル spec ではなく \"種\"**。",
        "",
        "出力先まとめ:",
        f"- 本体: `_ai_workspace/retro/{retro_filename}`",
        "- 提案群: `_ai_workspace/retro/proposals/*.md` (1 提案 = 1 ファイル)",
        "",
        "## 5. 制約・ルール (厳守)",
        "",
        "- **証拠アンカー必須**: すべての指摘は特定の run/result/commit を引用する。",
        "  「テストを増やそう」式の汎用アドバイスを出さない。",
        "- **捏造しない**: 実在するファイルだけを根拠にする。事実サマリの数値は",
        "  §2 をそのまま引き写す (再集計の数値が違ったら §2 を疑うのではなく",
        "  「判断できなかった」と書く)。",
        "- **human-in-the-loop**: 提案を出すだけで止める。",
        "  `_ai_workspace/bridge/inbox/` への自動投入も自動 dispatch も**しない**。",
        "- 提案は spec の \"種\"。**フル spec を生成しない** (grill-me 規律を保つ)。",
        "- 本タスクは分析タスクなので `result_*.md` を書く必要はない。",
        "  出力は §4 の retro ファイル群のみ。",
        "- push / ブランチ操作・merge は**一切しない**。",
        "  コミットも必須ではない (作っても 1 論理単位で OK)。",
        "",
    ]
    return "\n".join(body_parts)


def _path_list(paths: list[Path], repo: Path) -> str:
    if not paths:
        return "(none — no evidence of this type)"
    lines: list[str] = []
    for p in paths:
        rel = _rel_to_repo(p, repo)
        lines.append(f"- `{rel}`")
    return "\n".join(lines)


def _rel_to_repo(p: Path, repo: Path) -> Path | str:
    try:
        return p.relative_to(repo)
    except ValueError:
        return p


def _render_breakdown(d: dict[str, int]) -> str:
    if not d:
        return "(none)"
    return ", ".join(f"`{k}`={v}" for k, v in d.items())
