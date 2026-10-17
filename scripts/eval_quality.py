"""LLM-as-Judge 端到端质量评测（P2-⑩）。

评测对象：**生产检索管线（查询改写 + 混合召回 + RRF + 重排）→ 生成回答** 的完整链路。
评测方法：用另一个 LLM 作为裁判（LLM-as-Judge），对照「标准答案 + 检索资料」给回答打分。

三个维度（1–5 分）：
- correctness  ：与标准答案的事实一致性（5=完全正确，3=部分正确，1=错误）
- faithfulness ：回答是否完全能被检索资料支持（5=全部有依据，1=大量编造）→ 衡量幻觉
- relevance    ：是否直接回答用户问题（5=完全切题）

聚合指标：
- 平均分（1–5）与百分制换算
- 达标率：correctness ≥ 4 且 faithfulness ≥ 4 的比例
- 幻觉率：faithfulness ≤ 3 的比例

为什么需要（与检索指标互补）：
`eval_rag.py` 只衡量「能不能检索到」，不衡量「答得好不好、是否编造」。
端到端质量必须用 Judge 才能自动化，这是 RAG 评测的标准做法之一。

用法：
    python scripts/eval_quality.py                 # 全部题目
    python scripts/eval_quality.py --sample 6      # 抽查 6 题（快速验证）
    python scripts/eval_quality.py --skip-judge    # 只生成回答，不评分（省调用）
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app import llm  # noqa: E402
from app.config import settings  # noqa: E402
from scripts.eval_rag import build_rag, locate_ground_truth  # noqa: E402

EVAL_DIR = ROOT / "eval"
DATASET_PATH = EVAL_DIR / "dataset.json"
REPORT_PATH = ROOT / "docs" / "eval_quality_report.md"
LAST_RUN_PATH = EVAL_DIR / "quality_last_run.json"

JUDGE_PROMPT = """你是严格、客观的评审专家。请对照「标准答案」与「参考资料」评估「助手回答」。

只输出一个 JSON 对象，不要输出任何其他文字，格式：
{{"correctness": 1-5, "faithfulness": 1-5, "relevance": 1-5, "hallucination": true/false, "reason": "不超过80字的理由"}}

评分标准：
- correctness：回答与标准答案的事实一致性；5=完全正确，4=基本正确，3=部分正确，2=偏差较大，1=错误
- faithfulness：回答是否能被参考资料支持；5=全部有依据，4=基本有依据，3=存在无依据细节，2=较多编造，1=完全编造
- relevance：是否直接回答用户问题；5=完全切题，3=部分偏题，1=答非所问
- hallucination：回答是否包含参考资料与标准答案中都不存在的事实

问题：{question}
标准答案：{reference}
参考资料：
{contexts}
助手回答：{answer}"""


# ======================================================================
# 工具函数（可单测）
# ======================================================================
def parse_judge_json(text: str) -> dict | None:
    """从 Judge 输出中稳健地解析 JSON（容忍代码块、前后解释文字）。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    def score(key: str) -> int:
        """字段必须存在且为数值，超出 1-5 视为异常值夹紧；缺失/非数值则判整条无效。"""
        if key not in data:
            raise ValueError(f"缺少字段 {key}")
        try:
            value = float(data[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"字段 {key} 非数值") from exc
        return max(1, min(5, int(round(value))))

    try:
        result = {
            "correctness": score("correctness"),
            "faithfulness": score("faithfulness"),
            "relevance": score("relevance"),
            "hallucination": bool(data.get("hallucination", False)),
            "reason": str(data.get("reason", ""))[:200],
        }
    except ValueError:
        return None
    return result


def summarize(items: list[dict]) -> dict:
    """聚合指标（只统计评分成功的样本）。"""
    judged = [i for i in items if i.get("judge")]
    n = len(items)
    if not judged:
        return {
            "n": n,
            "judged": 0,
            "correctness": 0.0,
            "faithfulness": 0.0,
            "relevance": 0.0,
            "pass_rate": 0.0,
            "hallucination_rate": 0.0,
        }

    def avg(key: str) -> float:
        return round(sum(i["judge"][key] for i in judged) / len(judged), 2)

    pass_rate = sum(
        1 for i in judged if i["judge"]["correctness"] >= 4 and i["judge"]["faithfulness"] >= 4
    ) / len(judged)
    hallucination_rate = sum(1 for i in judged if i["judge"]["hallucination"]) / len(judged)
    return {
        "n": n,
        "judged": len(judged),
        "correctness": avg("correctness"),
        "faithfulness": avg("faithfulness"),
        "relevance": avg("relevance"),
        "pass_rate": round(pass_rate * 100, 1),
        "hallucination_rate": round(hallucination_rate * 100, 1),
    }


# ======================================================================
# 评测流程
# ======================================================================
def generate_answer(rag, question: str, top_n: int = 5) -> tuple[str, list[str]]:
    """生产检索管线 + 单轮生成（不含 Agent 编排，便于对照资料评忠实度）。"""
    docs = rag.retrieve(question, top_n=top_n)
    contexts = [d["content"] for d in docs]
    if not contexts:
        return "知识库中未检索到与问题相关的内容。", []

    prompt = (
        "你是企业知识库助手。仅根据下面提供的资料回答用户问题，不要编造资料之外的信息；"
        "如果资料不足以回答，请明确说明。回答结束后标注来源。\n\n"
        f"资料：\n{chr(10).join(f'[{i + 1}] {c}' for i, c in enumerate(contexts))}\n\n"
        f"问题：{question}"
    )
    answer = llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
    return answer.strip(), contexts


def judge_answer(question: str, reference: str, contexts: list[str], answer: str) -> dict | None:
    prompt = JUDGE_PROMPT.format(
        question=question,
        reference=reference or "（无标准答案）",
        contexts="\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts)) or "（无参考资料）",
        answer=answer,
    )
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.0)
    return parse_judge_json(raw)


def evaluate(sample: int | None = None, skip_judge: bool = False) -> dict:
    questions = json.loads(DATASET_PATH.read_text(encoding="utf-8"))["questions"]
    if sample:
        questions = questions[:sample]

    print(f"[1/3] 构建评测索引（{len(questions)} 题）...")
    rag = build_rag()

    items: list[dict] = []
    t0 = time.time()
    for idx, item in enumerate(questions, start=1):
        truth_chunks = locate_ground_truth(rag, item)
        reference = "\n".join(c["text"] for c in rag.dp.chunks if c["chunk_id"] in truth_chunks)

        answer, contexts = generate_answer(rag, item["question"])
        judge = None
        if not skip_judge:
            judge = judge_answer(item["question"], reference, contexts, answer)

        items.append(
            {
                "id": item["id"],
                "question": item["question"],
                "doc": item["doc"],
                "answer": answer,
                "answer_len": len(answer),
                "context_count": len(contexts),
                "judge": judge,
            }
        )
        status = "ok" if judge else ("skipped" if skip_judge else "judge-failed")
        print(f"      [{idx}/{len(questions)}] 题 {item['id']} 评分={status}  ({time.time() - t0:.0f}s)")

    metrics = summarize(items)
    print("\n===== 端到端质量指标 =====")
    print(
        f"样本 {metrics['n']} 题（有效评分 {metrics['judged']}）｜"
        f"正确性 {metrics['correctness']}/5 ｜忠实度 {metrics['faithfulness']}/5 ｜"
        f"相关性 {metrics['relevance']}/5 ｜达标率 {metrics['pass_rate']}% ｜幻觉率 {metrics['hallucination_rate']}%"
    )
    return {"metrics": metrics, "items": items, "elapsed_s": round(time.time() - t0, 1)}


def render_report(result: dict) -> str:
    m = result["metrics"]
    lines = [
        "# 端到端质量评测报告（LLM-as-Judge）",
        "",
        f"> 评测对象：生产检索管线（改写 + 混合召回 + RRF + 重排）→ 生成回答 ｜ "
        f"样本 {m['n']} 题（有效评分 {m['judged']}） ｜ 耗时 {result['elapsed_s']}s",
        "",
        "## 评分说明",
        "",
        "| 维度 | 含义 | 5 分 | 1 分 |",
        "| --- | --- | --- | --- |",
        "| 正确性 | 与标准答案的事实一致性 | 完全正确 | 错误 |",
        "| 忠实度 | 回答能否被检索资料支持（幻觉检测） | 全部有依据 | 完全编造 |",
        "| 相关性 | 是否直接回答用户问题 | 完全切题 | 答非所问 |",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 平均正确性 | {m['correctness']}/5（{round(m['correctness'] / 5 * 100, 1)}%） |",
        f"| 平均忠实度 | {m['faithfulness']}/5（{round(m['faithfulness'] / 5 * 100, 1)}%） |",
        f"| 平均相关性 | {m['relevance']}/5（{round(m['relevance'] / 5 * 100, 1)}%） |",
        f"| 达标率（正确≥4 且忠实≥4） | {m['pass_rate']}% |",
        f"| 幻觉率（忠实≤3） | {m['hallucination_rate']}% |",
        "",
        "## 逐题明细",
        "",
        "| # | 问题 | 正确 | 忠实 | 相关 | 幻觉 | 评审理由 |",
        "| ---: | --- | ---: | ---: | ---: | :-: | --- |",
    ]
    for item in result["items"]:
        judge = item.get("judge")
        if judge:
            lines.append(
                f"| {item['id']} | {item['question'][:26]} | {judge['correctness']} | "
                f"{judge['faithfulness']} | {judge['relevance']} | "
                f"{'是' if judge['hallucination'] else '否'} | {judge['reason']} |"
            )
        else:
            lines.append(f"| {item['id']} | {item['question'][:26]} | - | - | - | - | 评分失败 |")
    lines += [
        "",
        "*本报告由 `scripts/eval_quality.py` 生成，评分为真实 LLM Judge 输出，可复现。*",
    ]
    return "\n".join(lines)


def judge_probe_answer(question: str, contexts: list[str], answer: str) -> dict | None:
    """拒答测试的裁判：知识库本就没有答案，应当明确说明无法回答。"""
    prompt = (
        "你是严格、客观的评审专家。下面这个问题在「企业知识库」中**没有答案**。\n"
        "正确行为是明确说明资料不足/无法回答，或建议转人工；错误行为是编造具体事实。\n\n"
        "只输出一个 JSON 对象，格式："
        '{"correctness": 1-5, "faithfulness": 1-5, "relevance": 1-5, "hallucination": true/false, "reason": "不超过80字"}\n'
        "评分标准：correctness 5=正确拒答且没有编造；3=含糊其辞但没有具体编造；1=给出了具体但无依据的事实。\n\n"
        f"问题：{question}\n"
        f"检索到的资料（可能无关）：\n" + ("\n".join(contexts) if contexts else "（无）") + "\n"
        f"助手回答：{answer}"
    )
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.0)
    return parse_judge_json(raw)


def run_probes() -> dict:
    """拒答测试：问知识库里没有的问题，衡量「不硬编」的能力。"""
    probes_path = EVAL_DIR / "quality_probes.json"
    probes = json.loads(probes_path.read_text(encoding="utf-8"))["probes"]
    rag = build_rag()
    items = []
    for idx, probe in enumerate(probes, start=1):
        answer, contexts = generate_answer(rag, probe["question"])
        judge = judge_probe_answer(probe["question"], contexts, answer)
        items.append(
            {
                "id": probe["id"],
                "question": probe["question"],
                "note": probe.get("note", ""),
                "answer": answer,
                "context_count": len(contexts),
                "judge": judge,
            }
        )
        status = "ok" if judge else "judge-failed"
        print(f"      [{idx}/{len(probes)}] 探针 {probe['id']} 评分={status}")

    judged = [i for i in items if i.get("judge")]
    refuse_rate = (
        sum(1 for i in judged if i["judge"]["correctness"] >= 4) / len(judged) * 100
        if judged
        else 0.0
    )
    hallu_rate = (
        sum(1 for i in judged if i["judge"]["hallucination"]) / len(judged) * 100
        if judged
        else 0.0
    )
    metrics = {
        "n": len(items),
        "judged": len(judged),
        "refuse_rate": round(refuse_rate, 1),
        "hallucination_rate": round(hallu_rate, 1),
    }
    print(
        f"\n===== 拒答测试 =====\n样本 {metrics['n']} 题（有效 {metrics['judged']}）｜"
        f"正确拒答率 {metrics['refuse_rate']}% ｜ 编造率 {metrics['hallucination_rate']}%"
    )
    return {"metrics": metrics, "items": items}


def render_probe_report(result: dict) -> str:
    m = result["metrics"]
    lines = [
        "# 拒答测试报告（对抗样本）",
        "",
        f"> 问题均无法从知识库回答；正确行为是明确说明资料不足。样本 {m['n']} 题（有效 {m['judged']}）",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 正确拒答率 | {m['refuse_rate']}% |",
        f"| 编造率 | {m['hallucination_rate']}% |",
        "",
        "## 逐题明细",
        "",
        "| # | 问题 | 说明 | 回答是否恰当 | 评审理由 |",
        "| ---: | --- | --- | :-: | --- |",
    ]
    for item in result["items"]:
        judge = item.get("judge")
        if judge:
            ok = "✅ 拒答" if judge["correctness"] >= 4 else "❌ 编造/含糊"
            lines.append(
                f"| {item['id']} | {item['question']} | {item['note']} | {ok} | {judge['reason']} |"
            )
        else:
            lines.append(f"| {item['id']} | {item['question']} | {item['note']} | - | 评分失败 |")
    lines += ["", "*本报告由 `scripts/eval_quality.py --probes-only` 生成。*"]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM-as-Judge 端到端质量评测")
    parser.add_argument("--sample", type=int, default=None, help="只评测前 N 题（快速验证）")
    parser.add_argument("--skip-judge", action="store_true", help="只生成回答，不调用 Judge")
    parser.add_argument("--probes-only", action="store_true", help="只跑拒答测试（对抗样本）")
    args = parser.parse_args()

    settings.validate()

    if args.probes_only:
        probe_result = run_probes()
        probe_report = ROOT / "docs" / "eval_quality_probes.md"
        probe_report.write_text(render_probe_report(probe_result), encoding="utf-8")
        print(f"\n报告已写入：{probe_report}")
        probe_json = EVAL_DIR / "quality_probes_last_run.json"
        probe_json.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "metrics": probe_result["metrics"],
                    "items": [{k: v for k, v in i.items() if k != "answer"} for i in probe_result["items"]],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"机器可读结果已写入：{probe_json}")
        raise SystemExit(0)

    result = evaluate(sample=args.sample, skip_judge=args.skip_judge)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(result), encoding="utf-8")
    print(f"\n报告已写入：{REPORT_PATH}")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset_md5": hashlib.md5(DATASET_PATH.read_bytes()).hexdigest(),
        "metrics": result["metrics"],
        "items": [
            {k: v for k, v in item.items() if k != "answer"}  # 省体积：不含完整回答
            for item in result["items"]
        ],
    }
    LAST_RUN_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"机器可读结果已写入：{LAST_RUN_PATH}")
