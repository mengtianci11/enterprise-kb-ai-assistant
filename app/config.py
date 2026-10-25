"""全局配置中心：从 .env 加载、集中校验，杜绝硬编码密钥。

所有可调参数（模型、地址、分块策略、服务端口…）都从这里读取，
保证「配置与代码分离」，这也是企业级工程的基本要求。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


def _get(name: str, default: str = "") -> str:
    """读取环境变量并去除首尾空白。"""
    return os.getenv(name, default).strip()


class Settings:
    """应用配置。"""

    def __init__(self) -> None:
        # ---- 模型服务（硅基流动 / OpenAI 兼容接口）----
        self.API_KEY = _get("SILICONFLOW_API_KEY")
        self.BASE_URL = _get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        self.CHAT_MODEL = _get("CHAT_MODEL", "deepseek-ai/DeepSeek-V3")
        self.EMBEDDING_MODEL = _get("EMBEDDING_MODEL", "BAAI/bge-m3")
        self.RERANK_MODEL = _get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

        # ---- 存储与持久化 ----
        self.DATA_DIR = BASE_DIR / _get("DATA_DIR", "data")
        self.DB_PATH = self.DATA_DIR / "kb.db"
        self.UPLOAD_DIR = self.DATA_DIR / "uploads"      # 原始文件留存（重放/审计）
        self.PARSE_CACHE_DIR = self.DATA_DIR / "parsed"  # 解析结果缓存（按内容 MD5）

        # ---- 服务 ----
        self.HOST = _get("HOST", "127.0.0.1")
        self.PORT = int(_get("PORT", "8000"))

        # ---- 分块与检索 ----
        self.CHUNK_STRATEGY = _get("CHUNK_STRATEGY", "recursive")  # fixed | recursive
        self.CHUNK_SIZE = int(_get("CHUNK_SIZE", "300"))
        self.CHUNK_OVERLAP = int(_get("CHUNK_OVERLAP", "50"))
        self.TOP_K = int(_get("TOP_K", "5"))
        # 结构感知分块：块内注入「文档名 > 章节路径」上下文（提升召回与引用可解释性）
        self.CHUNK_CONTEXT_HEADER = _get("CHUNK_CONTEXT_HEADER", "true").lower() == "true"

        # ---- 引用校验（Citation Verification）----
        self.CITATION_VERIFY = _get("CITATION_VERIFY", "true").lower() == "true"
        self.CITATION_MAX_EVIDENCE = int(_get("CITATION_MAX_EVIDENCE", "5"))

        # ---- 冲突与时效（B）----
        # 开启后 /chat 会检测证据间冲突并追加显式归因说明（每轮多若干次 LLM 判定）
        self.CONFLICT_DETECT = _get("CONFLICT_DETECT", "false").lower() == "true"

        # ---- 解析与 OCR ----
        self.OCR_ENABLED = _get("OCR_ENABLED", "true").lower() == "true"
        self.OCR_DPI = int(_get("OCR_DPI", "200"))
        self.OCR_MIN_CONFIDENCE = float(_get("OCR_MIN_CONFIDENCE", "0.6"))
        self.SCANNED_TEXT_PER_PAGE_MIN = int(_get("SCANNED_TEXT_PER_PAGE_MIN", "50"))
        self.SCANNED_IMAGE_RATIO_MAX = float(_get("SCANNED_IMAGE_RATIO_MAX", "0.6"))
        self.SCANNED_PROBE_PAGES = int(_get("SCANNED_PROBE_PAGES", "5"))
        self.TABLE_MAX_ROWS = int(_get("TABLE_MAX_ROWS", "300"))
        self.TABLE_MAX_COLS = int(_get("TABLE_MAX_COLS", "15"))

        # ---- 异步任务 ----
        # 小于该体积且非耗时格式（OCR/图片）时同步处理，保证前端体验与向后兼容
        self.PARSE_SYNC_MAX_BYTES = int(_get("PARSE_SYNC_MAX_BYTES", str(200 * 1024)))
        self.TASK_WORKERS = int(_get("TASK_WORKERS", "2"))

        # ---- 认证与权限 ----
        self.AUTH_ENABLED = _get("AUTH_ENABLED", "true").lower() == "true"
        self.JWT_SECRET = _get("JWT_SECRET")            # 为空则自动生成并持久化
        self.JWT_EXPIRE_MINUTES = int(_get("JWT_EXPIRE_MINUTES", "60"))
        self.JWT_REFRESH_DAYS = int(_get("JWT_REFRESH_DAYS", "7"))
        self.DEFAULT_TENANT = _get("DEFAULT_TENANT", "default")
        self.DEFAULT_ADMIN_USERNAME = _get("DEFAULT_ADMIN_USERNAME", "admin")
        self.DEFAULT_ADMIN_PASSWORD = _get("DEFAULT_ADMIN_PASSWORD", "admin123")
        # 权限过滤的召回池：先宽召回再按可见性过滤（FAISS 本身不支持元数据过滤）
        self.RETRIEVAL_RECALL_POOL = int(_get("RETRIEVAL_RECALL_POOL", "60"))

        # ---- 语义缓存（v3.3）：命中则跳过改写/召回/重排 ----
        self.CACHE_ENABLED = _get("CACHE_ENABLED", "true").lower() == "true"
        self.CACHE_MAX_ENTRIES = int(_get("CACHE_MAX_ENTRIES", "512"))
        self.CACHE_TTL_SECONDS = int(_get("CACHE_TTL_SECONDS", "600"))
        self.CACHE_MIN_SIMILARITY = float(_get("CACHE_MIN_SIMILARITY", "0.97"))

        # ---- 会话与记忆（三层：工作记忆 / 摘要记忆 / 长期记忆）----
        # 工作记忆：每次请求携带的最近对话轮数（1 轮 = 用户+助手各 1 条）
        self.CONTEXT_WINDOW_TURNS = int(_get("CONTEXT_WINDOW_TURNS", "6"))
        # 消息数超过该阈值后触发滚动摘要，把更早的内容压缩进 summary
        self.SUMMARY_TRIGGER_MESSAGES = int(_get("SUMMARY_TRIGGER_MESSAGES", "12"))
        # 摘要保留的最近未摘要消息数（保证工作记忆的语义连续性）
        self.SUMMARY_KEEP_RECENT = int(_get("SUMMARY_KEEP_RECENT", "6"))
        # 摘要文本上限（字符），防止摘要本身膨胀
        self.SUMMARY_MAX_CHARS = int(_get("SUMMARY_MAX_CHARS", "800"))
        # 会话保留天数（超期会话在启动时归档删除）
        self.CONVERSATION_TTL_DAYS = int(_get("CONVERSATION_TTL_DAYS", "180"))

        # ---- 检索增强 ----
        self.RAG_FUSION = _get("RAG_FUSION", "true").lower() == "true"  # 查询改写开关
        self.RERANK_ENABLED = _get("RERANK_ENABLED", "true").lower() == "true"

        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.PARSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """启动前校验，缺配置时给出可操作的错误信息（fail-fast）。"""
        if not self.API_KEY:
            raise RuntimeError(
                "未检测到 SILICONFLOW_API_KEY。\n"
                f"请在项目根目录的 {ENV_FILE.name} 文件中添加：\n"
                "    SILICONFLOW_API_KEY=sk-你的密钥\n"
                "密钥可到 https://cloud.siliconflow.cn 获取。"
            )
        if not self.API_KEY.startswith("sk-"):
            raise RuntimeError(
                f"SILICONFLOW_API_KEY 格式异常（应以 sk- 开头）。当前值已隐去，请检查 {ENV_FILE.name}。"
            )
        if self.CHUNK_OVERLAP >= self.CHUNK_SIZE:
            raise RuntimeError("CHUNK_OVERLAP 必须小于 CHUNK_SIZE，请检查 .env 配置。")

    def as_dict(self) -> dict:
        """脱敏后的配置快照（可安全打印/写入健康检查）。"""
        return {
            "base_url": self.BASE_URL,
            "chat_model": self.CHAT_MODEL,
            "embedding_model": self.EMBEDDING_MODEL,
            "rerank_model": self.RERANK_MODEL,
            "chunk_strategy": self.CHUNK_STRATEGY,
            "chunk_size": self.CHUNK_SIZE,
            "chunk_overlap": self.CHUNK_OVERLAP,
            "top_k": self.TOP_K,
            "chunk_context_header": self.CHUNK_CONTEXT_HEADER,
            "ocr_enabled": self.OCR_ENABLED,
            "ocr_dpi": self.OCR_DPI,
            "auth_enabled": self.AUTH_ENABLED,
            "jwt_expire_minutes": self.JWT_EXPIRE_MINUTES,
            "recall_pool": self.RETRIEVAL_RECALL_POOL,
            "cache_enabled": self.CACHE_ENABLED,
            "cache_ttl_seconds": self.CACHE_TTL_SECONDS,
            "context_window_turns": self.CONTEXT_WINDOW_TURNS,
            "summary_trigger_messages": self.SUMMARY_TRIGGER_MESSAGES,
            "rag_fusion": self.RAG_FUSION,
            "rerank_enabled": self.RERANK_ENABLED,
            "api_key_configured": bool(self.API_KEY),
        }


settings = Settings()