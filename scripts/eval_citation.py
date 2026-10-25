"""引用级可验证评测（A）：检索 → 带引用生成 → 逐句校验 → Citation P/R。

用法：
    python scripts/eval_citation.py                 # 默认 30 道可答 + 10 道不可答
    python scripts/eval_citation.py --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citation import (  # noqa: E402
    build_evidence,
    format_evidence,
    sanitize_answer,
    verify_answer,
)
from app.config import settings  # noqa: E402

DATASET = ROOT / "eval" / "dataset_v3.json"
REPORT = ROOT / "docs" / "eval_citation_report.md"
LAST_RUN = ROOT / "eval" / "citation_last_run.json"

SYSTEM_PROMPT = (
    "你是企业知识库问答助手。只依据【参考资料】回答问题，"
    "每句末尾必须标注证据编号（如 [E1]）；"
    "如果参考资料不足以回答，直接说明「资料中未找到相关内容」，不要编造。"
)


def _answer(question: str, evidence) -> str:
    import app.llm as llm

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"【参考资料】\n{format_evidence(evidence)}\n\n【问题】{question}",
        },
    ]
    return llm.chat(messages, temperature=0.0)


def _is_refusal(answer: str) -> bool:
    keywords = ("未找到", "无法", "未收录", "没有相关", "资料中未", "无法确认")
    return any(k in (answer or "") for k in keywords)


def run(dataset_path: Path, limit: int) -> dict:
    from scripts.eval_rag import build_rag

    rag = build_rag()
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    answerable = [q for q in dataset["questions"] if q.get("type") != "unanswerable"]
    unanswerable = [q for q in dataset["questions"] if q.get("type") == "unanswerable"]
    # 固定抽样，保证可复现
    picked = answerable[:limit] + unanswerable

    items = []
    t0 = time.time()
    for idx, q in enumerate(picked, 1):
        docs = rag.retrieve(q["question"], top_n=5)
        evidence = build_evidence(docs, limit=5)
        if not evidence:
            answer = "资料中未找到相关内容。"
        else:
            try:
                answer = _answer(q["question"], evidence)
            except Exception as exc:  # noqa: BLE001
                answer = f"[错误] {exc}"
        verification = verify_answer(answer, evidence) if evidence else {}
        sanitized, removed = (
            sanitize_answer(answer, verification) if verification else (answer, 0)
        )
        items.append(
            {
                "id": q["id"],
                "type": q.get("type"),
                "question": q["question"],
                "answer": answer,
                "sanitized": sanitized,
                "removed_sentences": removed,
                "n_evidence": len(evidence),
                "refused": _is_refusal(answer),
                "verification": verification,
            }
        )
        print(f"  {idx}/{len(picked)} 完成：{q['question'][:24]}")

    def _mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 3) if values else None

    ans_items = [i for i in items if i["type"] != "unanswerable"]
    unas_items = [i for i in items if i["type"] == "unanswerable"]
    metrics = {
        "n_answerable": len(ans_items),
        "n_unanswerable": len(unas_items),
        "citation_precision": _mean(
            [i["verification"].get("citation_precision") for i in ans_items]
        ),
        "citation_recall": _mean(
            [i["verification"].get("citation_recall") for i in ans_items]
        ),
        "uncited_rate": _mean([i["verification"].get("uncited_rate") for i in ans_items]),
        "removed_sentences": sum(i["removed_sentences"] for i in ans_items),
        "refusal_accuracy": round(
            sum(1 for i in unas_items if i["refused"]) / len(unas_items), 3
        ) if unas_items else None,
        "elapsed_s": round(time.time() - t0, 1),
    }
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "dataset": str(dataset_path.relative_to(ROOT)), "metrics": metrics, "items": items}


def render_markdown(result: dict) -> str:
    m = result["metrics"]
    lines = [
        "# 引用级可验证评测报告",
        "",
        f"> 数据集：`{result['dataset']}` ｜ 运行时间：{result['generated_at']} ｜ 耗时：{m['elapsed_s']}s",
        "",
        "## 指标",
        "",
        "| 指标 | 值 | 说明 |",
        "| --- | ---: | --- |",
        f"| Citation Precision | {m['citation_precision']} | 被引用证据中真正支持该句的比例 |",
        f"| Citation Recall | {m['citation_recall']} | 有引用的句子中被支持的比例 |",
        f"| Uncited Rate | {m['uncited_rate']} | 完全没有引用的句子占比 |",
        f"| 删除句子数 | {m['removed_sentences']} | 引用不被支持而被删掉的句子 |",
        f"| 不可答拒答率 | {m['refusal_accuracy']} | 10 道不可答题正确拒答比例 |",
        "",
        "## 样例",
        "",
    ]
    for item in result["items"][:5]:
        v = item.get("verification") or {}
        lines += [
            f"**Q{item['id']}**：{item['question']}",
            "",
            f"- 回答：{item['answer'][:160]}",
            f"- 证据数：{item['n_evidence']}｜Precision：{v.get('citation_precision')}｜Recall：{v.get('citation_recall')}",
            "",
        ]
    lines.append("*本报告由 `scripts/eval_citation.py` 自动生成（真实 API）。*")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    result = run(Path(args.dataset), args.limit)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render_markdown(result), encoding="utf-8")
    LAST_RUN.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n指标:", result["metrics"])
    print("报告:", REPORT)
    print("结果:", LAST_RUN)


if __name__ == "__main__":
    main()
