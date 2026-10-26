"""D｜一键复现：检索（v2 门禁 + v3 扩展）+ 引用校验 + 冲突归因 + 质量门禁。

用法：
    python scripts/eval_all.py                 # 全量（约 25–30 分钟，消耗 API 额度）
    python scripts/eval_all.py --skip-v3 --skip-citation   # 只跑 v2 + 冲突 + 门禁
    python scripts/eval_all.py --only-gate                 # 只跑门禁（用已有 last_run）

输出：
- eval/eval_all_summary.json   机器可读汇总
- docs/eval_all_report.md      人读汇总
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"
DOCS = ROOT / "docs"
SUMMARY_JSON = EVAL / "eval_all_summary.json"
SUMMARY_MD = DOCS / "eval_all_report.md"


def _load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def run_step(name: str, args: list[str]) -> dict:
    print(f"\n===== [{name}] {' '.join(args[1:])} =====")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = (proc.stdout or "")[-1200:] + (proc.stderr or "")[-400:]
    print(tail)
    return {
        "name": name,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "seconds": round(time.time() - t0, 1),
        "tail": tail.strip().splitlines()[-6:],
    }


def collect_metrics() -> dict:
    v2 = _load(EVAL / "last_run.json") or {}
    v3 = _load(EVAL / "last_run_v3.json") or {}
    citation = _load(EVAL / "citation_last_run.json") or {}
    conflict = _load(EVAL / "conflict_last_run.json") or {}

    def full(d):
        return (d.get("metrics") or {}).get("full") or {}

    return {
        "retrieval_v2": {
            "questions": v2.get("questions"),
            "hit@3": full(v2).get("hit@3"),
            "mrr@5": full(v2).get("mrr@5"),
            "top1": full(v2).get("top1"),
        },
        "retrieval_v3": {
            "questions": v3.get("questions"),
            "scored": v3.get("scored_questions"),
            "hit@3": full(v3).get("hit@3"),
            "mrr@5": full(v3).get("mrr@5"),
            "top1": full(v3).get("top1"),
        },
        "citation": citation.get("metrics") or {},
        "conflict": conflict.get("metrics") or {},
    }


def render_markdown(steps: list[dict], metrics: dict) -> str:
    ran = {s["name"] for s in steps}
    lines = [
        "# 一键复现报告（eval_all）",
        "",
        f"> 生成时间：{datetime.now().isoformat(timespec='seconds')} ｜ "
        f"环境：Python {sys.version.split()[0]}",
        "",
        "## 步骤",
        "",
        "| 步骤 | 结果 | 耗时(s) |",
        "| --- | --- | ---: |",
    ]
    for s in steps:
        lines.append(f"| {s['name']} | {'✅' if s['ok'] else '❌'} | {s['seconds']} |")

    lines += ["", "## 指标汇总", ""]
    v2, v3 = metrics["retrieval_v2"], metrics["retrieval_v3"]
    cit, con = metrics["citation"], metrics["conflict"]
    rows = [
        (
            "检索 v2（24 题，CI 口径）",
            f"Hit@3 {v2.get('hit@3')}% / MRR@5 {v2.get('mrr@5')}% / Top-1 {v2.get('top1')}%",
            "检索 v2（24 题）" in ran,
        ),
        (
            "检索 v3（115 题，计分 105）",
            f"Hit@3 {v3.get('hit@3')}% / MRR@5 {v3.get('mrr@5')}% / Top-1 {v3.get('top1')}%",
            "检索 v3（115 题）" in ran,
        ),
        (
            "引用校验",
            f"P {cit.get('citation_precision')} / R {cit.get('citation_recall')} / 拒答 {cit.get('refusal_accuracy')}",
            "引用校验" in ran,
        ),
        (
            "冲突与时效",
            f"检测 {con.get('detection_rate')} / 归因 {con.get('attribution_accuracy')}",
            "冲突与时效" in ran,
        ),
    ]
    lines += ["| 指标 | 值 |", "| --- | --- |"]
    for name, value, executed in rows:
        suffix = "" if executed else "（本次跳过，沿用上次结果）"
        lines.append(f"| {name} | {value}{suffix} |")
    lines += [
        "",
        "## 复现",
        "",
        "```bash",
        "python scripts/eval_all.py",
        "```",
        "",
        "*本报告由 `scripts/eval_all.py` 自动生成；详细命令见 `docs/reproduce.md`。*",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-v2", action="store_true", help="跳过 v2 检索（CI 门禁口径）")
    parser.add_argument("--skip-v3", action="store_true", help="跳过 v3 检索（115 题，约 15 分钟）")
    parser.add_argument("--skip-citation", action="store_true", help="跳过引用校验（约 6 分钟）")
    parser.add_argument("--skip-conflict", action="store_true", help="跳过冲突评测（约 4 分钟）")
    parser.add_argument("--only-gate", action="store_true", help="只跑质量门禁（用已有 last_run）")
    args = parser.parse_args()

    steps: list[dict] = []
    py = sys.executable

    if not args.only_gate:
        if not args.skip_v2:
            steps.append(run_step("检索 v2（24 题）", ["scripts/eval_rag.py"]))
        if not args.skip_v3:
            steps.append(
                run_step(
                    "检索 v3（115 题）",
                    [
                        "scripts/eval_rag.py",
                        "--dataset", "eval/dataset_v3.json",
                        "--report", "docs/eval_report_v3.md",
                        "--last-run", "eval/last_run_v3.json",
                        "--details", "eval/last_run_details_v3.json",
                    ],
                )
            )
        if not args.skip_citation:
            steps.append(run_step("引用校验", ["scripts/eval_citation.py", "--limit", "30"]))
        if not args.skip_conflict:
            steps.append(run_step("冲突与时效", ["scripts/eval_conflict.py"]))

    steps.append(run_step("质量门禁", ["scripts/check_metrics.py", "--from-last-run"]))

    metrics = collect_metrics()
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "steps": steps,
        "metrics": metrics,
        "all_ok": all(s["ok"] for s in steps),
    }
    EVAL.mkdir(parents=True, exist_ok=True)
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    DOCS.mkdir(parents=True, exist_ok=True)
    SUMMARY_MD.write_text(render_markdown(steps, metrics), encoding="utf-8")

    print("\n===== 汇总 =====")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"\n汇总已写入：{SUMMARY_JSON}")
    print(f"报告已写入：{SUMMARY_MD}")
    if not summary["all_ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
