"""异步任务层：解析 / OCR / 向量化的后台执行与状态机。

为什么需要（企业级缺口）：
- OCR 单文档约 10–60 秒、大文件向量化也要数秒到数十秒；
  若在 HTTP 请求里同步执行，必然造成请求超时（502）与前端假死；
- 因此上传接口只做「登记 + 落原件 + 投递任务」，立即返回 `task_id`，
  客户端轮询 `GET /documents/tasks/{task_id}` 获取进度。

设计要点：
- **状态机**：pending → parsing/ocr → embedding → ready / failed，
  状态落 SQLite（`tasks` 表），多副本部署时可共享；
- **可替换性**：`TaskRunner` 抽象出提交接口，本期用进程内线程池实现（零外部依赖），
  P1 换成 MQ（Kafka/RabbitMQ）+ 独立 Worker 时，业务代码不需要改动；
- **中断恢复**：服务重启时把残留的进行中任务标记为失败（见 `mark_interrupted_tasks`），
  避免任务永久悬挂在「处理中」。
"""
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

from app.config import settings
from app.document_processor import DocumentProcessor

logger = logging.getLogger(__name__)


class TaskRunner:
    """进程内异步任务执行器。"""

    def __init__(
        self,
        processor: DocumentProcessor,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        self.processor = processor
        self.store = processor.store
        self.on_complete = on_complete
        self._pool = ThreadPoolExecutor(
            max_workers=max(settings.TASK_WORKERS, 1), thread_name_prefix="parse"
        )
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def submit(
        self,
        filename: str,
        content: bytes,
        md5: str,
        *,
        owner_id: int = 0,
        classification: str = "internal",
        tenant_id: str = "",
        dept: str = "",
    ) -> str:
        """提交异步解析任务，返回 task_id（携带权限元数据）。"""
        task_id = uuid.uuid4().hex[:12]
        self.store.create_task(task_id, filename, "pending")
        future = self._pool.submit(
            self._run,
            task_id,
            filename,
            content,
            md5,
            {
                "owner_id": owner_id,
                "classification": classification,
                "tenant_id": tenant_id,
                "dept": dept,
            },
        )
        with self._lock:
            self._futures[task_id] = future
        logger.info("已提交异步解析任务 %s：%s", task_id, filename)
        return task_id

    def _run(
        self,
        task_id: str,
        filename: str,
        content: bytes,
        md5: str,
        meta: Optional[dict] = None,
    ) -> None:
        def on_phase(phase: str) -> None:
            self.store.update_task(task_id, phase)

        try:
            result = self.processor.add_document(
                filename, content, md5=md5, on_phase=on_phase, **(meta or {})
            )
            if self.on_complete is not None:
                self.on_complete()  # 重建 FAISS/BM25 索引与 Agent
            self.store.update_task(
                task_id,
                "ready",
                chunks=result["chunks"],
                warnings=result["warnings"],
            )
            logger.info("任务 %s 完成：%s（%d 块）", task_id, filename, result["chunks"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务 %s 失败：%s", task_id, filename)
            self.store.update_task(task_id, "failed", error=str(exc))

    # ------------------------------------------------------------------
    def get(self, task_id: str) -> Optional[dict]:
        return self.store.get_task(task_id)

    def list(self, limit: int = 20) -> list[dict]:
        return self.store.list_tasks(limit)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for f in self._futures.values() if not f.done())

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)


__all__ = ["TaskRunner"]
