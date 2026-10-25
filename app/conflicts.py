"""冲突与时效（B）：检测证据之间的矛盾/版本差异，并给出显式归因。

为什么需要：
企业知识库经常同时存在「旧版制度 / 新版制度 / 外部法规 / 行业参考」，
检索会把它们一起召回。若模型直接拼答案，就会出现"同一问题两个数"却不说依据。

设计：
1. 候选对筛选：字符 2-gram 重叠度过滤（便宜），只把可能相关的证据对送给 LLM；
2. 矛盾判定：LLM 输出 JSON（是否矛盾 + 事实 + 两侧取值）；
3. 归因策略：
   - 问题涉及「法律/法定/国家规定」→ 公开法规优先；
   - 否则企业制度优先，且采用**较新版本**；
4. 输出显式提示：两侧取值、各自来源、以谁为准及理由。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Optional

from app.citation import Evidence

JudgeFn = Callable[[str, str], Optional[dict]]

_PUBLIC_HINTS = ("公开", "劳动法", "法规", "外部", "参考", "行业")
_LAW_HINTS = ("法律", "法定", "国家规定", "劳动法", "法规")
_OLD_HINTS = ("旧版", "以前", "历史", "过去", "老版")
_VERSION_RE = re.compile(r"(20\d{2})")


@dataclass
class Conflict:
    a: Evidence
    b: Evidence
    fact: str
    value_a: str
    value_b: str
    kind: str = ""  # contradiction | version | scope

    def source_pair(self) -> set[str]:
        return {self.a.source, self.b.source}


def _two_grams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text or "")
    return {text[i : i + 2] for i in range(len(text) - 1)}


def overlap_ratio(a: str, b: str) -> float:
    """字符 2-gram Jaccard，用于廉价的候选对筛选。"""
    ga, gb = _two_grams(a), _two_grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def is_public(source: str) -> bool:
    return any(hint in (source or "") for hint in _PUBLIC_HINTS)


def source_version(source: str) -> int:
    match = _VERSION_RE.search(source or "")
    return int(match.group(1)) if match else 0


def llm_judge_conflict(a_text: str, b_text: str) -> Optional[dict]:
    """判断两段证据是否对同一事实给出不一致的值。"""
    import app.llm as llm

    prompt = (
        "判断下面两段证据是否对【同一个事实】给出了【不一致的值】。\n"
        "口径/适用范围不同也算冲突（kind=scope），例如「公司年假 7 天」vs「法定年假 5/10/15 天」；\n"
        "新旧版本数值不同算 version；数值直接矛盾算 contradiction；数值完全一致则 conflict=false。\n"
        "只输出 JSON："
        '{"conflict": true/false, "kind": "contradiction|version|scope|none", '
        '"fact": "事实名", "value_a": "A 的值", "value_b": "B 的值"}\n\n'
        f"【证据 A】{a_text[:800]}\n\n【证据 B】{b_text[:800]}"
    )
    try:
        raw = llm.chat([{"role": "user", "content": prompt}], temperature=0.0)
        match = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(match.group(0)) if match else {}
        return data if "conflict" in data else None
    except Exception:  # noqa: BLE001
        return None


def detect_conflicts(
    evidence: list[Evidence],
    judge: Optional[JudgeFn] = None,
    min_overlap: float = 0.08,
    max_pairs: int = 12,
) -> list[Conflict]:
    """在证据集合中检测矛盾/版本差异（同一个文档只保留一条证据，冲突发生在文档之间）。"""
    judge = judge or llm_judge_conflict
    # 同文档多分块会让同一事实重复参与判定（浪费调用且容易自相矛盾），先按来源去重
    unique: dict[str, Evidence] = {}
    for e in evidence:
        unique.setdefault(e.source, e)
    items = list(unique.values())
    candidates = [
        (a, b)
        for a, b in combinations(items, 2)
        if overlap_ratio(a.text, b.text) >= min_overlap
    ][:max_pairs]

    conflicts: list[Conflict] = []
    for a, b in candidates:
        result = judge(a.text, b.text)
        if not result or not result.get("conflict"):
            continue
        conflicts.append(
            Conflict(
                a=a,
                b=b,
                fact=str(result.get("fact") or "同一事实"),
                value_a=str(result.get("value_a") or ""),
                value_b=str(result.get("value_b") or ""),
                kind=str(result.get("kind") or ""),
            )
        )
    return conflicts


def resolve_conflict(conflict: Conflict, question: str) -> dict:
    """按问题类型与来源/版本决定"以谁为准"，返回 winner 与理由。"""
    prefer_public = any(hint in question for hint in _LAW_HINTS)
    prefer_external = (
        any(hint in question for hint in ("行业", "外部", "参考")) and "公司" not in question
    )
    prefer_old = any(hint in question for hint in _OLD_HINTS)
    sides = [conflict.a, conflict.b]
    public_sides = [e for e in sides if is_public(e.source)]
    private_sides = [e for e in sides if not is_public(e.source)]

    if (prefer_public or prefer_external) and public_sides:
        winner = max(public_sides, key=lambda e: source_version(e.source))
        reason = (
            "问题涉及法定口径，应以公开法规为准"
            if prefer_public
            else "问题询问外部参考口径，应以公开资料为准"
        )
    elif private_sides:
        pick = min if prefer_old else max
        winner = pick(private_sides, key=lambda e: source_version(e.source))
        reason = (
            "问题询问历史版本，采用较早版本"
            if prefer_old
            else "企业制度优先，且采用较新版本"
        )
    else:
        pick = min if prefer_old else max
        winner = pick(sides, key=lambda e: source_version(e.source))
        reason = "无企业制度可依据，采用较新版本" if not prefer_old else "采用较早版本"
    return {"winner": winner, "reason": reason}


def conflict_note(conflicts: list[Conflict], question: str) -> str:
    """把冲突整理成给用户看的显式说明。"""
    if not conflicts:
        return ""
    lines = []
    for c in conflicts:
        resolved = resolve_conflict(c, question)
        lines.append(
            f"注意：{c.fact}存在差异——{c.a.source}：{c.value_a or c.a.text[:40]}；"
            f"{c.b.source}：{c.value_b or c.b.text[:40]}。"
            f"本回答以 {resolved['winner'].source} 为准（{resolved['reason']}）。"
        )
    return "\n".join(lines)


__all__ = [
    "Conflict",
    "overlap_ratio",
    "is_public",
    "source_version",
    "llm_judge_conflict",
    "detect_conflicts",
    "resolve_conflict",
    "conflict_note",
]
