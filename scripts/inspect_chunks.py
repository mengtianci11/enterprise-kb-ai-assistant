"""开发工具：查看已入库文档的解析结果与分块内容。

用法：
    python scripts/inspect_chunks.py             # 查看全部文档
    python scripts/inspect_chunks.py 员工手册    # 按名称过滤
"""
import pathlib
import sqlite3
import sys

# Windows 控制台默认可能是 GBK，强制 UTF-8 输出，避免中文与特殊符号报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "kb.db"


def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    docs = conn.execute(
        "SELECT id, name, format, pages, blocks, chunk_count, status, warnings FROM documents ORDER BY id"
    ).fetchall()
    print(f"共 {len(docs)} 份文档\n")
    for d in docs:
        if keyword and keyword not in d["name"]:
            continue
        warn = f"  告警: {d['warnings'][:80]}" if d["warnings"] else ""
        print("=" * 78)
        print(
            f"[{d['id']}] {d['name']} | 格式={d['format'] or '?'} | 结构块={d['blocks']} | "
            f"分块={d['chunk_count']} | 状态={d['status']}{warn}"
        )
        chunks = conn.execute(
            "SELECT chunk_index, section, text FROM chunks WHERE doc_id=? ORDER BY chunk_index",
            (d["id"],),
        ).fetchall()
        for c in chunks:
            section = c["section"] or "(无章节)"
            text = c["text"].replace("\n", " / ")
            print(f"  ── 块{c['chunk_index']} [{section}]")
            print(f"     {text[:300]}{'...' if len(text) > 300 else ''}")
        print()
    conn.close()


if __name__ == "__main__":
    main()
