"""应用级共享状态（单例注册表 / 组合根）。

v3.0 变化：
- 接入 `TaskRunner`（进程内异步任务执行器），上传大文件/扫描件时不再阻塞请求；
- `rebuild_after_change()` 加锁，避免「异步任务完成」与「HTTP 请求」
  并发重建索引导致的状态竞争；
- 启动时把上次中断的任务标记为失败（任务状态不悬挂）。
"""
import threading

from app.agent import build_agent
from app.config import settings
from app.db import DocumentStore
from app.document_processor import DocumentProcessor
from app.retrieval import AdvancedRAG
from app.semantic_cache import SemanticCache
from app.tasks import TaskRunner


class AppState:
    """服务运行时组合根（composition root）。"""

    def __init__(self) -> None:
        self.store: DocumentStore = DocumentStore()
        self.processor: DocumentProcessor = DocumentProcessor(self.store)
        self.rag: AdvancedRAG = AdvancedRAG(self.processor)
        self.cache = SemanticCache(
            max_entries=settings.CACHE_MAX_ENTRIES,
            ttl_seconds=settings.CACHE_TTL_SECONDS,
            min_similarity=settings.CACHE_MIN_SIMILARITY,
            enabled=settings.CACHE_ENABLED,
        )
        self.rag.cache = self.cache  # 语义缓存注入检索管线
        self.agent = None  # 上传文档后惰性构建
        self.runner: TaskRunner = TaskRunner(self.processor, on_complete=self.rebuild_after_change)
        self._rebuild_lock = threading.Lock()

    # ------------------------------------------------------------------
    def restore(self) -> None:
        """启动时从持久化存储恢复（重启不丢文档）+ 认证初始化。"""
        interrupted = self.store.mark_interrupted_tasks()
        if interrupted:
            print(f"[恢复] {interrupted} 个进行中的解析任务因服务重启被标记为失败")
        self.bootstrap_auth()
        self.processor.rebuild_from_store()
        if self.processor.chunks:
            self.rag.build_indices()
            self.agent = build_agent(self.rag)
            print(
                f"[恢复] 已加载 {len(self.processor.documents)} 份文档 / "
                f"{len(self.processor.chunks)} 个分块"
            )

    def bootstrap_auth(self) -> None:
        """初始化认证：用户表为空时创建默认管理员；老文档归属管理员（fail-closed）。"""
        if not settings.AUTH_ENABLED:
            return
        from app.security import encode_password

        if self.store.user_count() == 0:
            password = settings.DEFAULT_ADMIN_PASSWORD
            self.store.create_user(
                settings.DEFAULT_ADMIN_USERNAME,
                encode_password(password),
                role="admin",
                dept="",
                tenant_id=settings.DEFAULT_TENANT,
            )
            print(
                f"[安全] 已创建默认管理员：{settings.DEFAULT_ADMIN_USERNAME} / {password}\n"
                f"       请立即修改 .env 中的 DEFAULT_ADMIN_PASSWORD 并重启（或建号后停用默认账号）"
            )

        admin = self.store.get_user_by_username(settings.DEFAULT_ADMIN_USERNAME)
        if admin is not None:
            moved = self.store.claim_orphan_documents(admin["id"])
            if moved:
                print(
                    f"[安全] {moved} 份历史文档（无归属）已划归管理员所有，"
                    f"如需共享请通过 ACL 授权（POST /documents/{{name}}/acl）"
                )

    def rebuild_after_change(self) -> None:
        """文档增删后重建索引与 Agent（加锁，防止并发重建竞争）。

        语义缓存必须使旧结果整体失效：用 bump_version 让"索引版本"变化，
        避免出现「文档删了，缓存里还能搜到」的越权/过期问题。
        """
        with self._rebuild_lock:
            self.rag.build_indices()
            self.agent = build_agent(self.rag)
            self.cache.bump_version()

    def reset(self) -> None:
        self.processor.clear_all()
        self.rag.reset()
        self.agent = None
        self.cache.bump_version()


state = AppState()


__all__ = ["state"]
