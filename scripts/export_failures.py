"""C｜失败分类学：从评测产物导出真实失败样本 -> eval/failures.json。

数据来源（全部是真实运行产物，不编造）：
- eval/last_run_details.json        逐题检索明细（scripts/eval_rag.py 生成）
- eval/dataset.json                 问题原文与金标证据
- eval/quality_last_run.json        逐题 LLM-as-Judge 评分（可选）
- eval/quality_probes_last_run.json 拒答探针（可选）
- eval/incidents.json               开发过程中真实发生并修复的故障（人工维护）

用法：
    python scripts/export_failures.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
DETAILS_PATH = ROOT / "eval" / "last_run_details.json"
DATASET_PATH = ROOT / "eval" / "dataset.json"
QUALITY_PATH = ROOT / "eval" / "quality_last_run.json"
PROBES_PATH = ROOT / "eval" / "quality_probes_last_run.json"
INCIDENTS_PATH = ROOT / "eval" / "incidents.json"
OUT_PATH = ROOT / "eval" / "failures.json"

# 检索类失败的根因与建议修复（规则化，便于回归）
RETRIEVAL_RULES = {
    "doc_recall_miss": {
        "severity": "high",
        "root_cause": "正确文档都未进 Top-5：通用词命中错误文档，检索缺语义信号或候选池过窄",
        "suggested_fix": "扩大候选池 / 加领域词表与 IDF 调整 / 调整混合检索权重",
    },
    "chunk_recall_miss": {
        "severity": "high",
        "root_cause": "文档已召回但金标分块未进 Top-5：口语与书面语词面不匹配，或分块边界切断答案",
        "suggested_fix": "同义词/查询扩展；检查分块边界与章节前缀；提高 Top-k",
    },
    "ranking_noise": {
        "severity": "low",
        "root_cause": "金标分块已召回但未排第一：同文档相邻块/近重复块得分略高",
        "suggested_fix": "块级去重（MMR）/ 段落级回退 / 标题加权；Top-1 损失对下游影响有限",
    },
}


def _classify_retrieval(r: dict) -> Optional[str]:
    if not r.get("hit5"):
        return "chunk_recall_miss" if r.get("doc_hit") else "doc_recall_miss"
    if not r.get("hit3"):
        return "ranking_noise"
    if not r.get("top1"):
        return "ranking_noise"
    return None


def _rank_text(ranks: Iterable[int]) -> str:
    ranks = [r for r in ranks if r]
    return f"排名第 {ranks[0]}" if ranks else "未召回"


def collect_retrieval(details: dict, questions: dict) -> list[dict]:
    """把 (题 × 策略) 的检索失败按 (题, 类别) 聚合，避免同一问题重复计数。"""
    buckets: dict[tuple[str, str], dict] = {}
    for strategy, per_q in (details.get("results") or {}).items():
        for qid, r in (per_q or {}).items():
            # 不可答题不参与检索指标（它们考察拒答），不记为检索失败
            if questions.get(str(qid), {}).get("type") == "unanswerable":
                continue
            category = _classify_retrieval(r)
            if not category:
                continue
            key = (str(qid), category)
            bucket = buckets.setdefault(
                key,
                {
                    "question_id": int(qid) if str(qid).isdigit() else qid,
                    "question": questions.get(str(qid), {}).get("question", ""),
                    "doc": questions.get(str(qid), {}).get("doc", ""),
                    "category": category,
                    "strategies": [],
                    "ranks": [],
                },
            )
            bucket["strategies"].append(strategy)
            bucket["ranks"].append((r.get("ranks") or [None])[0])

    failures = []
    for bucket in buckets.values():
        rule = RETRIEVAL_RULES[bucket["category"]]
        best_rank = min([x for x in bucket["ranks"] if x] or [0])
        failures.append(
            {
                "category": bucket["category"],
                "severity": rule["severity"],
                "source": "retrieval",
                "question_id": bucket["question_id"],
                "question": bucket["question"],
                "doc": bucket["doc"],
                "expected": "金标分块出现在 Top-1",
                "actual": f"最佳 {_rank_text([best_rank])}；受影响策略：{', '.join(sorted(set(bucket['strategies'])))}",
                "root_cause": rule["root_cause"],
                "suggested_fix": rule["suggested_fix"],
                "status": "open",
            }
        )
    return failures


def collect_quality(quality: Optional[dict]) -> list[dict]:
    if not quality:
        return []
    failures = []
    for item in quality.get("items", []):
        judge = item.get("judge") or {}
        if not judge:
            continue
        bad = (judge.get("correctness") or 5) < 4 or (judge.get("faithfulness") or 5) < 4
        if not bad:
            continue
        failures.append(
            {
                "category": "answer_quality_fail",
                "severity": "high",
                "source": "quality_eval",
                "question_id": item.get("id"),
                "question": item.get("question", ""),
                "doc": item.get("doc", ""),
                "expected": "正确性 ≥ 4 且忠实度 ≥ 4",
                "actual": f"correctness={judge.get('correctness')}, faithfulness={judge.get('faithfulness')}",
                "root_cause": "生成层问题：证据不足仍作答，或引用与答案不一致",
                "suggested_fix": "引用校验 + 证据不足时拒答",
                "status": "open",
            }
        )
    return failures


def collect_probes(probes: Optional[dict]) -> list[dict]:
    if not probes:
        return []
    failures = []
    for item in probes.get("items", []):
        judge = item.get("judge") or {}
        if not judge:
            continue
        if judge.get("hallucination") or (judge.get("correctness") or 5) < 4:
            failures.append(
                {
                    "category": "refusal_fail",
                    "severity": "high",
                    "source": "probe",
                    "question_id": item.get("id"),
                    "question": item.get("question", ""),
                    "doc": "",
                    "expected": "知识库未收录时明确拒答",
                    "actual": f"hallucination={judge.get('hallucination')}, correctness={judge.get('correctness')}",
                    "root_cause": "检索未命中时仍生成答案（缺少拒答兜底或阈值过低）",
                    "suggested_fix": "加拒答阈值与提示词约束；对低分证据强制拒答",
                    "status": "open",
                }
            )
    return failures


def collect_incidents(incidents: Optional[dict]) -> list[dict]:
    if not incidents:
        return []
    failures = []
    for inc in incidents.get("incidents", []):
        failures.append(
            {
                "category": inc.get("category", "other"),
                "severity": inc.get("severity", "medium"),
                "source": "incident",
                "question_id": inc.get("id"),
                "question": inc.get("title", ""),
                "doc": "",
                "expected": "稳定行为",
                "actual": inc.get("symptom", ""),
                "root_cause": inc.get("root_cause", ""),
                "suggested_fix": inc.get("fix", ""),
                "status": "fixed",
                "evidence": inc.get("evidence", ""),
            }
        )
    return failures


def export(
    details: Optional[dict],
    dataset: Optional[dict],
    quality: Optional[dict] = None,
    probes: Optional[dict] = None,
    incidents: Optional[dict] = None,
) -> dict:
    questions = {str(q["id"]): q for q in (dataset or {}).get("questions", [])}
    failures = []
    if details:
        failures += collect_retrieval(details, questions)
    failures += collect_quality(quality)
    failures += collect_probes(probes)
    failures += collect_incidents(incidents)

    for idx, item in enumerate(failures, 1):
        item["id"] = f"F-{idx:03d}"

    counts: dict[str, int] = {}
    for item in failures:
        counts[item["category"]] = counts.get(item["category"], 0) + 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset_md5": (details or {}).get("dataset_md5"),
        "sources": {
            "retrieval_points": sum(len(v) for v in (details or {}).get("results", {}).values()),
            "quality_items": len((quality or {}).get("items", [])),
            "probe_items": len((probes or {}).get("items", [])),
            "incidents": len((incidents or {}).get("incidents", [])),
        },
        "total": len(failures),
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "failures": failures,
    }


def _load(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="导出失败样本（支持指定数据集产物）")
    parser.add_argument("--details", default=str(DETAILS_PATH))
    parser.add_argument("--dataset", default=str(DATASET_PATH))
    parser.add_argument("--quality", default=str(QUALITY_PATH))
    parser.add_argument("--probes", default=str(PROBES_PATH))
    parser.add_argument("--incidents", default=str(INCIDENTS_PATH))
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    details = _load(Path(args.details))
    if details is None:
        raise SystemExit(
            f"缺少 {args.details}，请先运行 python scripts/eval_rag.py 生成逐题明细"
        )
    result = export(
        details=details,
        dataset=_load(Path(args.dataset)),
        quality=_load(Path(args.quality)),
        probes=_load(Path(args.probes)),
        incidents=_load(Path(args.incidents)),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"失败样本已写入：{out}")
    print(f"共 {result['total']} 条：{result['counts']}")


if __name__ == "__main__":
    main()
