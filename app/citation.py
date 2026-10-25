"""引用级可验证（Citation Verification）。

把答案按句切分，逐句对齐到检索证据 [E1][E2]…，量化：
- Citation Precision：被引用的证据里，有多少真的支持该句；
- Citation Recall：有引用的句子里，有多少句至少被一条证据支持；
- Uncited Rate：完全没有引用的句子占比（用于发现"无据陈述"）。

设计取舍：
- 判定用 LLM（可注入 judge，便于离线测试），而不是纯向量相似度——
  相似度高不代表"支持"，事实句需要蕴含判断；
- 判定失败（异常/无 JSON）保守视为"不支持"，宁可误删也不放过幻觉；
- 校验失败时支持"删句/拒答"两种策略（见 sanitize_answer）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

_SENT_SPLIT = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_CITE_RE = re.compile(r"\[E(\d+)\]")

CITATION_INSTRUCTION = (
    "回答时必须基于参考资料，并在每句末尾标注引用编号，例如 [E1]；"
    "证据不足时明确说明「资料中未找到」，不要编造。"
)

JudgeFn = Callable[[str, str], Optional[bool]]


@dataclass(frozen=True)
class Evidence:
    eid: str
    text: str
    source: str = ""
    chunk_id: str = ""


def build_evidence(docs: list, limit: int = 5) -> list[Evidence]:
    """把检索结果转成带编号的证据列表（按 chunk 去重）。"""
    seen: set = set()
    items: list[Evidence] = []
    for doc in docs or []:
        meta = doc.get("metadata") or {}
        key = meta.get("chunk_id") or meta.get("id") or doc.get("content", "")[:60]
        if key in seen:
            continue
        seen.add(key)
        items.append(
            Evidence(
                eid=f"E{len(items) + 1}",
                text=(doc.get("content") or "").strip(),
                source=meta.get("source", ""),
                chunk_id=str(key),
            )
        )
        if len(items) >= limit:
            break
    return items


def format_evidence(evidence: list[Evidence]) -> str:
    """把证据拼成给模型看的参考资料块。"""
    return "\n\n".join(
        f"[{e.eid}] (来源: {e.source or '未知'})\n{e.text}" for e in evidence
    )


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.findall(text or "") if s.strip()]


def cited_ids(sentence: str) -> list[int]:
    return [int(x) for x in _CITE_RE.findall(sentence)]


def strip_citations(sentence: str) -> str:
    return _CITE_RE.sub("", sentence).strip()


def llm_judge(sentence: str, evidence_text: str) -> Optional[bool]:
    """默认判定器：用 LLM 判断证据是否支持句子；异常时返回 None（视为不支持）。"""
    import app.llm as llm

    prompt = (
        "判断【证据】是否足以支持【句子】中的陈述。只输出 JSON："
        '{"supported": true 或 false, "reason": "简要理由"}\n\n'
        f"【句子】{sentence}\n\n【证据】{evidence_text[:1500]}"
    )
    try:
        raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.0)
        match = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(match.group(0)) if match else {}
        if "supported" not in data:
            return None
        return bool(data["supported"])
    except Exception:  # noqa: BLE001
        return None


def verify_answer(
    answer: str, evidence: list[Evidence], judge: Optional[JudgeFn] = None
) -> dict:
    """逐句校验引用，返回明细与聚合指标。"""
    judge = judge or llm_judge
    by_id = {int(e.eid[1:]): e for e in evidence}
    sentences = split_sentences(answer)

    details = []
    pairs_total = pairs_supported = cited_sentences = supported_sentences = 0
    for sentence in sentences:
        ids = cited_ids(sentence)
        if not ids:
            details.append({"sentence": sentence, "citations": [], "supported": None})
            continue
        cited_sentences += 1
        ok_any = False
        for idx in ids:
            ev = by_id.get(idx)
            if ev is None:
                continue
            pairs_total += 1
            supported = judge(strip_citations(sentence), ev.text)
            if supported:
                pairs_supported += 1
                ok_any = True
        if ok_any:
            supported_sentences += 1
        details.append(
            {
                "sentence": sentence,
                "citations": [f"E{i}" for i in ids],
                "supported": ok_any,
            }
        )

    return {
        "sentences": details,
        "citation_precision": round(pairs_supported / pairs_total, 3) if pairs_total else None,
        "citation_recall": round(supported_sentences / cited_sentences, 3) if cited_sentences else None,
        "uncited_rate": round(
            (len(sentences) - cited_sentences) / len(sentences), 3
        ) if sentences else 0.0,
        "n_sentences": len(sentences),
        "n_cited_sentences": cited_sentences,
        "n_citation_pairs": pairs_total,
        "n_supported_pairs": pairs_supported,
    }


def sanitize_answer(
    answer: str,
    verification: dict,
    fallback: str = "根据现有资料无法确认该问题，建议转人工核实。",
) -> tuple[str, int]:
    """删掉「引用了证据但证据不支持」的句子；如果全被删光则返回兜底话术。"""
    kept: list[str] = []
    removed = 0
    for item in verification.get("sentences", []):
        if item.get("supported") is False:
            removed += 1
            continue
        kept.append(item["sentence"])
    if not kept:
        return fallback, removed
    return "".join(kept), removed


__all__ = [
    "Evidence",
    "CITATION_INSTRUCTION",
    "build_evidence",
    "format_evidence",
    "split_sentences",
    "cited_ids",
    "strip_citations",
    "llm_judge",
    "verify_answer",
    "sanitize_answer",
]
