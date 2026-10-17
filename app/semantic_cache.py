"""语义缓存：按「问题语义 + 权限范围 + 索引版本」缓存检索结果。

为什么值得做（真实收益，且不需要 GPU）：
- 相同/近似问题在生产里占比很高（员工反复问制度问题）；
- 命中缓存可跳过「查询改写 → 双路召回 → RRF → 重排」，延迟从秒级降到百毫秒级、外部调用成本接近 0。

为什么必须带权限范围（最容易踩的坑）：
- 缓存只按问题语义匹配是**危险的**：A 用户有权限的机密文档结果可能被 B 用户命中；
- 因此缓存键 = 权限范围指纹（可见文档集合的哈希，管理员为 special） + 索引版本号；
- 文档增删会改变索引 → `bump_version()` 使旧缓存整体失效，避免"删了文档还能搜到"。

设计取舍：
- 只在**检索层**做缓存，不做「答案级缓存」：答案受多轮上下文影响，且一旦命中会绕过权限过滤，
  风险大于收益（如需答案缓存，必须把会话上下文与权限一并纳入键，复杂度高）。
- 进程内实现（LRU + TTL），多副本场景下每个副本独立缓存——正确但命中率低；后续可用 Redis 共享。
"""
import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    scope: str                 # 权限范围指纹 + 索引版本
    embedding: list[float]     # 问题向量（用于相似度匹配）
    result: list[dict]         # 检索结果快照
    created: float             # 时间戳（TTL 判定）


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class SemanticCache:
    """进程内语义缓存（LRU + TTL + 权限范围隔离）。"""

    def __init__(
        self,
        max_entries: int = 512,
        ttl_seconds: int = 600,
        min_similarity: float = 0.97,
        enabled: bool = True,
    ) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl_seconds = max(0, ttl_seconds)
        self.min_similarity = min_similarity
        self.enabled = enabled
        self._entries: list[CacheEntry] = []
        self._lock = threading.Lock()
        self._version = 0
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    # 键
    # ------------------------------------------------------------------
    def scope_key(self, allowed_doc_ids: Optional[set[int]]) -> str:
        """权限范围指纹：可见文档集合 + 索引版本。

        None 表示不限制（管理员）：用 "all" 标记，避免和其他范围的缓存混用。
        """
        if allowed_doc_ids is None:
            term = "all"
        else:
            joined = ",".join(str(i) for i in sorted(allowed_doc_ids))
            term = hashlib.md5(joined.encode("utf-8")).hexdigest()[:12]
        return f"{term}:v{self._version}"

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def get(self, embedding: list[float], scope: str) -> Optional[list[dict]]:
        if not self.enabled:
            return None
        now = time.time()
        with self._lock:
            best: Optional[CacheEntry] = None
            best_sim = 0.0
            alive: list[CacheEntry] = []
            for entry in self._entries:
                if now - entry.created > self.ttl_seconds:
                    continue
                alive.append(entry)
                if entry.scope != scope:
                    continue
                sim = _cosine(entry.embedding, embedding)
                if sim > best_sim:
                    best_sim, best = sim, entry
            self._entries = alive  # 顺带做过期清理

            if best is not None and best_sim >= self.min_similarity:
                self.hits += 1
                logger.info("语义缓存命中：相似度 %.3f", best_sim)
                return [dict(item) for item in best.result]
            self.misses += 1
            return None

    def put(self, embedding: list[float], scope: str, result: list[dict]) -> None:
        if not self.enabled or not result:
            return
        with self._lock:
            # 同一 scope 下已存在高度相似条目则覆盖，避免重复堆积
            kept: list[CacheEntry] = []
            for entry in self._entries:
                if (
                    entry.scope == scope
                    and _cosine(entry.embedding, embedding) >= self.min_similarity
                ):
                    continue
                kept.append(entry)
            kept.append(CacheEntry(scope=scope, embedding=list(embedding), result=[dict(i) for i in result], created=time.time()))
            if len(kept) > self.max_entries:
                kept = kept[-self.max_entries :]
            self._entries = kept

    # ------------------------------------------------------------------
    # 失效与观测
    # ------------------------------------------------------------------
    def bump_version(self) -> None:
        """索引发生变化（文档增删/重建）时调用：整体失效。"""
        with self._lock:
            self._version += 1
            self._entries.clear()
        logger.info("语义缓存已清空（索引版本 -> v%d）", self._version)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "enabled": self.enabled,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total * 100, 1) if total else 0.0,
                "version": self._version,
                "ttl_seconds": self.ttl_seconds,
                "min_similarity": self.min_similarity,
            }


__all__ = ["SemanticCache"]
