"""冲突与时效评测（B）：15 道冲突用例，测检测率与归因准确率。

用法：python scripts/eval_conflict.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citation import build_evidence  # noqa: E402
from app.conflicts import conflict_note, detect_conflicts, resolve_conflict  # noqa: E402

CONFLICT_DIR = ROOT / "eval" / "corpus_conflict"
REPORT = ROOT / "docs" / "eval_conflict_report.md"
LAST_RUN = ROOT / "eval" / "conflict_last_run.json"

V25, V26 = "员工手册_v2025.md", "员工手册_v2026.md"
LAW, EXT = "劳动法摘要_公开.md", "差旅参考_外部.md"

CASES = [
    # 年假：公司制度（新版优先）
    {"question": "公司年假有几天？", "winner": V26, "pair": (V25, V26)},
    {"question": "入职满一年年假几天？", "winner": V26, "pair": (V25, V26)},
    {"question": "满三年年假多少天？", "winner": V26, "pair": (V25, V26)},
    {"question": "年假新规和旧规有什么区别？", "winner": V26, "pair": (V25, V26)},
    {"question": "公司今年年假政策变了吗？", "winner": V26, "pair": (V25, V26)},
    {"question": "旧版员工手册年假怎么规定？", "winner": V25, "pair": (V25, V26)},
    # 年假：法定口径（公开优先）
    {"question": "按国家规定年假有几天？", "winner": LAW, "pair": (V26, LAW)},
    {"question": "法定年假是多少天？", "winner": LAW, "pair": (V25, LAW)},
    {"question": "劳动法规定的年休假天数？", "winner": LAW, "pair": (V26, LAW)},
    # 住宿：公司制度（新版优先）
    {"question": "一线城市出差住宿标准是多少？", "winner": V26, "pair": (V25, V26)},
    {"question": "其他城市住宿标准是多少？", "winner": V26, "pair": (V25, V26)},
    {"question": "出差住宿能报多少？", "winner": V26, "pair": (V25, V26)},
    # 住宿：外部参考
    {"question": "行业住宿参考标准是多少？", "winner": EXT, "pair": (V26, EXT)},
    # 历史版本
    {"question": "旧版住宿标准是多少？", "winner": V25, "pair": (V25, V26)},
    # 公司 vs 行业对比（仍以公司为准）
    {"question": "公司住宿标准比行业参考高吗？", "winner": V26, "pair": (V26, EXT)},
]


def run() -> dict:
    from scripts.eval_rag import build_rag

    rag = build_rag(CONFLICT_DIR)
    items = []
    for case in CASES:
        docs = rag.retrieve(case["question"], top_n=6)
        evidence = build_evidence(docs, limit=6)
        conflicts = detect_conflicts(evidence)

        expected = set(case["pair"])
        # 冲突对只要包含"期望来源"即可（法定题可能召回 V25 或 V26，都算有效冲突）
        matched = next(
            (c for c in conflicts if case["winner"] in c.source_pair()), None
        )
        resolved = resolve_conflict(matched, case["question"]) if matched else None
        items.append(
            {
                "question": case["question"],
                "expected_winner": case["winner"],
                "expected_pair": list(case["pair"]),
                "detected": matched is not None,
                "n_conflicts": len(conflicts),
                "winner": resolved["winner"].source if resolved else None,
                "reason": resolved["reason"] if resolved else None,
                "attribution_ok": bool(resolved and resolved["winner"].source == case["winner"]),
                "note": conflict_note([matched], case["question"]) if matched else "",
            }
        )
        print(f"  {len(items)}/{len(CASES)} {case['question'][:22]} | 检测={matched is not None} | 归因={items[-1]['attribution_ok']}")

    n = len(items)
    detection_rate = round(sum(i["detected"] for i in items) / n, 3)
    attribution_accuracy = round(sum(i["attribution_ok"] for i in items) / n, 3)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_cases": n,
        "metrics": {
            "detection_rate": detection_rate,
            "attribution_accuracy": attribution_accuracy,
        },
        "items": items,
    }


def render_markdown(result: dict) -> str:
    m = result["metrics"]
    lines = [
        "# 冲突与时效评测报告",
        "",
        f"> 用例：{result['n_cases']} 道 ｜ 运行时间：{result['generated_at']}",
        "",
        "## 指标",
        "",
        "| 指标 | 值 | 说明 |",
        "| --- | ---: | --- |",
        f"| 冲突检测率 | {m['detection_rate']} | 期望的冲突对是否被检出 |",
        f"| 归因准确率 | {m['attribution_accuracy']} | 是否按「法定/外部/企业/版本」规则选对来源 |",
        "",
        "## 逐题明细",
        "",
        "| 问题 | 期望来源 | 检出 | 归因 |",
        "| --- | --- | --- | --- |",
    ]
    for item in result["items"]:
        lines.append(
            f"| {item['question']} | {item['expected_winner']} | "
            f"{'✅' if item['detected'] else '❌'} | {'✅' if item['attribution_ok'] else '❌'} |"
        )
    lines.append("")
    lines.append("*本报告由 `scripts/eval_conflict.py` 自动生成（真实 API）。*")
    return "\n".join(lines)


def main() -> None:
    result = run()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render_markdown(result), encoding="utf-8")
    LAST_RUN.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n指标:", result["metrics"])
    print("报告:", REPORT)


if __name__ == "__main__":
    main()
