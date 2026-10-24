"""校验评测集：文档存在、evidence 是原文精确子串、问题唯一，并可选检查分块可检索性。

用法：
    python scripts/validate_dataset.py eval/dataset_v3.json
    python scripts/validate_dataset.py eval/dataset_v3.json --no-chunks
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "eval" / "corpus"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_chunks(corpus_dir: Path = CORPUS) -> dict[str, list[str]]:
    """按当前分块配置重建每篇文档的分块文本，用于检查 evidence 是否可被检索到。"""
    from app.chunking import split_blocks
    from app.config import settings
    from app.parsers import parse_document

    chunks: dict[str, list[str]] = {}
    for path in sorted(corpus_dir.glob("*.md")):
        # 校验必须反映当前解析器行为，不能命中历史解析缓存
        parsed, _ = parse_document(path.read_bytes(), path.name, use_cache=False)
        texts = [
            c["text"]
            for c in split_blocks(
                [b.to_dict() for b in parsed.blocks],
                doc_name=path.name,
                chunk_size=settings.CHUNK_SIZE,
                overlap=settings.CHUNK_OVERLAP,
                include_header=True,
            )
        ]
        chunks[path.name] = texts
    return chunks


def validate(ds: dict, check_chunks: bool = True, corpus_dir: Path = CORPUS) -> list[str]:
    errors: list[str] = []
    questions = ds.get("questions", [])
    if not questions:
        return ["评测集为空"]

    seen_questions: dict[str, str] = {}
    seen_ids: set = set()
    for q in questions:
        qid = q.get("id")
        if qid in seen_ids:
            errors.append(f"重复 id：{qid}")
        seen_ids.add(qid)

        text = (q.get("question") or "").strip()
        if not text:
            errors.append(f"{qid}: 问题为空")
        elif text in seen_questions:
            errors.append(f"{qid}: 与 {seen_questions[text]} 问题重复")
        else:
            seen_questions[text] = qid

        qtype = q.get("type", "")
        if qtype == "unanswerable":
            if q.get("evidence"):
                errors.append(f"{qid}: 不可答题不应有 evidence")
            continue

        doc = q.get("doc") or ""
        if not doc:
            errors.append(f"{qid}: 缺少 doc")
            continue
        path = corpus_dir / doc
        if not path.exists():
            errors.append(f"{qid}: 文档不存在 {doc}")
            continue

        evidence = q.get("evidence") or []
        if not evidence:
            errors.append(f"{qid}: 缺少 evidence")
            continue
        content = path.read_text(encoding="utf-8")
        for ev in evidence:
            if ev not in content:
                errors.append(f"{qid}: evidence 不是 {doc} 的原文子串 -> {ev[:40]}")

    if check_chunks and not errors:
        chunks = build_chunks(corpus_dir)
        for q in questions:
            if q.get("type") == "unanswerable":
                continue
            doc = q.get("doc")
            for ev in q.get("evidence") or []:
                if not any(ev in c for c in chunks.get(doc, [])):
                    errors.append(f"{q['id']}: evidence 分块后不可检索（跨块）-> {ev[:40]}")
    return errors


def summarize(ds: dict) -> str:
    questions = ds.get("questions", [])
    by_type = Counter(q.get("type", "?") for q in questions)
    by_doc = Counter(q.get("doc") or "(不可答)" for q in questions)
    lines = [
        f"题数：{len(questions)}",
        "类型分布：" + "，".join(f"{k}={v}" for k, v in by_type.most_common()),
        "文档分布：" + "，".join(f"{k}={v}" for k, v in by_doc.most_common()),
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    parser.add_argument("--no-chunks", action="store_true", help="跳过分块可检索性检查")
    args = parser.parse_args()

    ds = load_json(Path(args.dataset))
    errors = validate(ds, check_chunks=not args.no_chunks)
    print(summarize(ds))
    if errors:
        print(f"\n❌ 发现 {len(errors)} 个问题：")
        for err in errors[:30]:
            print("  -", err)
        raise SystemExit(1)
    print("\n✅ 校验通过")


if __name__ == "__main__":
    main()
