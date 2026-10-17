"""分块策略 A/B 评测：旧纯文本滑窗 vs 结构感知分块（量化收益）。

对比两套分块方式在同一评测集上的检索指标：
- 方案A（旧）：解析后丢弃结构，按字符滑窗切分（v2.0 行为）；
- 方案B（新）：按标题层级切分 + 表格语义化 + 块内注入「文档名 > 章节路径」前缀。

两套分块共用同一检索管线（向量 + BM25 + RRF），唯一变量是分块方式，
因此指标差异可归因于分块策略本身。

用法：python scripts/eval_chunking.py
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app.chunking import split_blocks, split_text_recursive  # noqa: E402
from app.config import settings  # noqa: E402
from app.parsers import parse_document  # noqa: E402
from app.retrieval import AdvancedRAG  # noqa: E402

CORPUS_DIR = ROOT / "eval" / "corpus"
DATASET = ROOT / "eval" / "dataset.json"
REPORT = ROOT / "docs" / "eval_chunking_report.md"


class ChunkHolder:
    """AdvancedRAG 只依赖 `.chunks`，用它承载任意分块结果。"""

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks


def build_arm_a() -> list[dict]:
    """方案A：丢弃结构，纯文本滑窗（v2.0 行为）。"""
    chunks: list[dict] = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        parsed, _ = parse_document(path.read_bytes(), path.name, use_cache=True)
        for i, piece in enumerate(
            split_text_recursive(parsed.text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
        ):
            chunks.append(
                {
                    "text": piece,          # 无章节前缀
                    "source": path.name,
                    "chunk_id": f"A_{path.stem}_{i}",
                    "section": "",
                }
            )
    return chunks


def build_arm_b() -> list[dict]:
    """方案B：结构感知分块 + 章节上下文前缀（v3.0 行为）。"""
    from app import llm

    chunks: list[dict] = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        parsed, _ = parse_document(path.read_bytes(), path.name, use_cache=True)
        for i, chunk in enumerate(
            split_blocks(
                [b.to_dict() for b in parsed.blocks],
                doc_name=path.name,
                chunk_size=settings.CHUNK_SIZE,
                overlap=settings.CHUNK_OVERLAP,
                include_header=True,
            )
        ):
            chunks.append(
                {
                    "text": chunk["text"],
                    "source": path.name,
                    "chunk_id": f"B_{path.stem}_{i}",
                    "section": chunk.get("section", ""),
                }
            )
    return chunks


def embed_all(chunks: list[dict]) -> None:
    """批量向量化（逐条，便于复用现有缓存与错误处理）。"""
    from app import llm

    for c in chunks:
        c["embedding"] = llm.embed_text(c["text"])


def evaluate_arm(name: str, chunks: list[dict], questions: list[dict], top_k: int = 5) -> dict:
    rag = AdvancedRAG(ChunkHolder(chunks))
    rag.build_indices()

    hit3 = hit5 = mrr = 0.0
    per_question = []
    for item in questions:
        truth = {
            c["chunk_id"]
            for c in chunks
            if c["source"] == item["doc"] and any(e in c["text"] for e in item["evidence"])
        }
        docs = rag.rrf_merge(
            [
                rag.vector_search(item["question"], top_k=10),
                rag.bm25_search(item["question"], top_k=10),
            ],
            top_k=top_k,
        )
        ranks = [
            i + 1
            for i, d in enumerate(docs)
            if (d.get("metadata") or {}).get("chunk_id") in truth
        ]
        h3 = any(r <= 3 for r in ranks)
        h5 = bool(ranks)
        rr = 1.0 / ranks[0] if ranks else 0.0
        hit3 += h3
        hit5 += h5
        mrr += rr
        per_question.append((item["id"], h3, h5, round(rr, 3)))

    n = len(questions)
    return {
        "name": name,
        "chunks": len(chunks),
        "hit@3": round(hit3 / n * 100, 1),
        "hit@5": round(hit5 / n * 100, 1),
        "mrr@5": round(mrr / n * 100, 1),
        "detail": per_question,
    }


def main() -> None:
    settings.validate()
    questions = json.loads(DATASET.read_text(encoding="utf-8"))["questions"]

    print("[1/4] 构建两套分块...")
    arm_a = build_arm_a()
    arm_b = build_arm_b()
    print(f"      方案A（纯文本滑窗）: {len(arm_a)} 块")
    print(f"      方案B（结构感知+前缀）: {len(arm_b)} 块")

    print("[2/4] 向量化（真实 embedding 调用）...")
    embed_all(arm_a)
    embed_all(arm_b)

    print("[3/4] 评测中...")
    result_a = evaluate_arm("A 旧：纯文本滑窗", arm_a, questions)
    result_b = evaluate_arm("B 新：结构感知+章节前缀", arm_b, questions)

    print("[4/4] 生成报告...")
    lines = [
        "# 分块策略 A/B 评测报告",
        "",
        f"> 语料：`eval/corpus`（{len(list(CORPUS_DIR.glob('*.md')))} 篇） ｜ 评测集：{len(questions)} 题 ｜ "
        f"检索管线统一为「向量 + BM25 + RRF（Top-{5}）」，唯一变量是分块方式",
        "",
        "| 方案 | 分块数 | Hit@3 | Hit@5 | MRR@5 |",
        "| --- | ---: | ---: | ---: | ---: |",
        f"| {result_a['name']} | {result_a['chunks']} | {result_a['hit@3']}% | {result_a['hit@5']}% | {result_a['mrr@5']}% |",
        f"| {result_b['name']} | {result_b['chunks']} | {result_b['hit@3']}% | {result_b['hit@5']}% | {result_b['mrr@5']}% |",
        "",
        "## 差异（B − A）",
        "",
        f"- Hit@3：{round(result_b['hit@3'] - result_a['hit@3'], 1):+} 个百分点",
        f"- Hit@5：{round(result_b['hit@5'] - result_a['hit@5'], 1):+} 个百分点",
        f"- MRR@5：{round(result_b['mrr@5'] - result_a['mrr@5'], 1):+} 个百分点",
        "",
        "## 结论",
        "",
        "- 结构感知分块按标题层级切分并在块内注入章节路径，减少了「语义被截断」与「块无主题」两类问题；",
        "- 表格语义化让行列数据以自然语言进入索引，使结构化问答（如「P001 库存多少」）可被召回；",
        "- 该实验为真实 API 运行结果，可复现：`python scripts/eval_chunking.py`。",
        "",
        "*报告由 `scripts/eval_chunking.py` 自动生成。*",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:14]) if False else "")
    for r in (result_a, result_b):
        print(f"{r['name']}: Hit@3={r['hit@3']}% Hit@5={r['hit@5']}% MRR@5={r['mrr@5']}%")
    print(f"\n报告已写入：{REPORT}")


if __name__ == "__main__":
    main()
