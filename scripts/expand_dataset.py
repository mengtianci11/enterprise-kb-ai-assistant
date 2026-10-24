"""合并原有 24 题与新增题，生成 v3 评测集（eval/dataset_v3.json）。

用法：
    python scripts/expand_dataset.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from validate_dataset import load_json, summarize, validate  # noqa: E402

BASE_PATH = ROOT / "eval" / "dataset.json"
NEW_PATH = ROOT / "eval" / "new_questions.json"
OUT_PATH = ROOT / "eval" / "dataset_v3.json"


def main() -> None:
    base = load_json(BASE_PATH)["questions"]
    new = load_json(NEW_PATH)["questions"]

    # 原有题补充 type 标记，便于按类别统计与消融
    for q in base:
        q.setdefault("type", "single_hop")
        q.setdefault("note", "原有 24 题")

    merged = base + new
    for idx, q in enumerate(merged, 1):
        q["id"] = idx

    dataset = {
        "meta": {
            "name": "企业知识库检索评测集 v3",
            "version": "v3",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "base": "dataset.json（24 题）",
            "added": "new_questions.json（92 题）",
            "chunk_strategy": "recursive",
            "note": "evidence 为语料原文的精确子串，用于定位标准答案分块",
        },
        "questions": merged,
    }

    errors = validate(dataset, check_chunks=True)
    print(summarize(dataset))
    if errors:
        print(f"\n❌ 校验失败（{len(errors)} 项）：")
        for err in errors[:30]:
            print("  -", err)
        raise SystemExit(1)

    OUT_PATH.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 已生成：{OUT_PATH}")


if __name__ == "__main__":
    main()
