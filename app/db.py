"""SQLite 持久化层：文档 / 分块 / 用户 / ACL / 审计 / 异步任务。

v3.1 变化（多用户与权限体系）：
- 新增 `users`（用户+角色+部门+多租户）、`doc_acl`（文档级授权）、`audit_log`（审计）；
- `documents` 新增 `tenant_id` / `owner_id` / `dept` / `classification`（密级）；
- `allowed_doc_ids()`：把「谁能看哪些文档」下推为一条 SQL，
  供检索层在**召回阶段**做权限过滤（避免越权内容进入候选集）；
- `_migrate()` 继续保证老库零手工升级。
"""
import pickle
import sqlite3
import threading
from datetime import datetime
from typing import Any, Optional

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  md5          TEXT NOT NULL,
  chunk_count  INTEGER NOT NULL,
  added_at     TEXT NOT NULL,
  format       TEXT DEFAULT '',
  pages        INTEGER DEFAULT 0,
  blocks       INTEGER DEFAULT 0,
  status       TEXT DEFAULT 'ready',
  warnings     TEXT DEFAULT '',
  parse_cached INTEGER DEFAULT 0,
  tenant_id    TEXT DEFAULT 'default',
  owner_id     INTEGER DEFAULT 0,
  dept         TEXT DEFAULT '',
  classification TEXT DEFAULT 'internal'
);
CREATE TABLE IF NOT EXISTS chunks(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  text        TEXT NOT NULL,
  embedding   BLOB NOT NULL,
  section     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS users(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id     TEXT NOT NULL DEFAULT 'default',
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',
  dept          TEXT DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    TEXT NOT NULL,
  last_login_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS doc_acl(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  subject_type TEXT NOT NULL,              -- user | role | dept
  subject_id   TEXT NOT NULL,
  permission   TEXT NOT NULL DEFAULT 'read',
  created_at   TEXT NOT NULL,
  UNIQUE(doc_id, subject_type, subject_id, permission)
);
CREATE INDEX IF NOT EXISTS idx_acl_doc ON doc_acl(doc_id);

CREATE TABLE IF NOT EXISTS audit_log(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  tenant_id     TEXT DEFAULT '',
  user_id       INTEGER DEFAULT 0,
  username      TEXT DEFAULT '',
  action        TEXT NOT NULL,             -- login/upload/delete/chat/search/acl_change...
  resource_type TEXT DEFAULT '',
  resource_id   TEXT DEFAULT '',
  detail        TEXT DEFAULT '',
  ip            TEXT DEFAULT '',
  ua            TEXT DEFAULT '',
  success       INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS tasks(
  task_id    TEXT PRIMARY KEY,
  doc_name   TEXT NOT NULL,
  status     TEXT NOT NULL,
  chunks     INTEGER DEFAULT 0,
  error      TEXT DEFAULT '',
  warnings   TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations(
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id      TEXT DEFAULT 'default',
  user_id        INTEGER NOT NULL,
  username       TEXT DEFAULT '',
  client_key     TEXT NOT NULL,              -- 客户端会话标识（前端生成）
  title          TEXT DEFAULT '',
  summary        TEXT DEFAULT '',            -- 滚动摘要（摘要记忆）
  summarized_upto INTEGER DEFAULT 0,          -- 已纳入摘要的最大消息 id
  message_count  INTEGER DEFAULT 0,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(user_id, client_key)
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);

CREATE TABLE IF NOT EXISTS messages(
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,              -- user | assistant | system
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);
"""

# 老库需要补齐的列（幂等）
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("documents", "format", "TEXT DEFAULT ''"),
    ("documents", "pages", "INTEGER DEFAULT 0"),
    ("documents", "blocks", "INTEGER DEFAULT 0"),
    ("documents", "status", "TEXT DEFAULT 'ready'"),
    ("documents", "warnings", "TEXT DEFAULT ''"),
    ("documents", "parse_cached", "INTEGER DEFAULT 0"),
    ("documents", "tenant_id", "TEXT DEFAULT 'default'"),
    ("documents", "owner_id", "INTEGER DEFAULT 0"),
    ("documents", "dept", "TEXT DEFAULT ''"),
    ("documents", "classification", "TEXT DEFAULT 'internal'"),
    ("chunks", "section", "TEXT DEFAULT ''"),
]


class DocumentStore:
    """文档 / 分块 / 用户 / ACL / 审计 / 任务 的统一持久化仓库。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or str(settings.DB_PATH)
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, column, ddl in _MIGRATIONS:
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ==================================================================
    # 文档
    # ==================================================================
    def doc_exists(self, md5: str, name: Optional[str] = None) -> bool:
        with self._connect() as conn:
            if name:
                row = conn.execute(
                    "SELECT 1 FROM documents WHERE md5 = ? OR name = ?", (md5, name)
                ).fetchone()
            else:
                row = conn.execute("SELECT 1 FROM documents WHERE md5 = ?", (md5,)).fetchone()
        return row is not None

    def add_document(
        self,
        name: str,
        md5: str,
        chunks: list[Any],
        meta: Optional[dict] = None,
    ) -> int:
        """写入文档与其分块；chunks 支持 (text, emb) 元组或 dict。"""
        meta = meta or {}
        normalized: list[tuple[str, list[float], str]] = []
        for item in chunks:
            if isinstance(item, dict):
                normalized.append((item["text"], item.get("embedding"), item.get("section", "")))
            else:
                text, emb = item[0], item[1]
                section = item[2] if len(item) > 2 else ""
                normalized.append((text, emb, section))

        warnings = meta.get("warnings") or []
        if isinstance(warnings, list):
            warnings = " | ".join(str(w) for w in warnings)

        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO documents
                  (name, md5, chunk_count, added_at, format, pages, blocks, status, warnings,
                   parse_cached, tenant_id, owner_id, dept, classification)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    md5,
                    len(normalized),
                    datetime.now().isoformat(),
                    meta.get("format", ""),
                    int(meta.get("pages", 0) or 0),
                    int(meta.get("blocks", 0) or 0),
                    meta.get("status", "ready"),
                    warnings,
                    1 if meta.get("parse_cached") else 0,
                    meta.get("tenant_id") or settings.DEFAULT_TENANT,
                    int(meta.get("owner_id", 0) or 0),
                    meta.get("dept", "") or "",
                    meta.get("classification", "internal"),
                ),
            )
            doc_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO chunks (doc_id, chunk_index, text, embedding, section) VALUES (?, ?, ?, ?, ?)",
                [
                    (doc_id, i, text, sqlite3.Binary(pickle.dumps(emb)), section)
                    for i, (text, emb, section) in enumerate(normalized)
                ],
            )
            conn.commit()
        return doc_id

    def delete_document(self, name: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM documents WHERE name = ?", (name,))
            conn.commit()
        return cur.rowcount > 0

    def clear_all(self) -> None:
        with self._lock, self._connect() as conn:
            for table in ("chunks", "documents", "tasks", "doc_acl", "audit_log"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def get_document(self, name: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def load_all(self) -> tuple[list[dict], list[dict]]:
        """读取全部文档清单与分块（含来源、章节、doc_id）。"""
        docs: list[dict] = []
        chunks: list[dict] = []
        with self._connect() as conn:
            for r in conn.execute("SELECT * FROM documents ORDER BY id"):
                docs.append(
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "chunks": r["chunk_count"],
                        "added_at": r["added_at"],
                        "md5": r["md5"],
                        "format": r["format"] or "",
                        "pages": r["pages"] or 0,
                        "blocks": r["blocks"] or 0,
                        "status": r["status"] or "ready",
                        "warnings": r["warnings"] or "",
                        "parse_cached": bool(r["parse_cached"]),
                        "tenant_id": r["tenant_id"] or settings.DEFAULT_TENANT,
                        "owner_id": r["owner_id"] or 0,
                        "dept": r["dept"] or "",
                        "classification": r["classification"] or "internal",
                    }
                )
            for r in conn.execute(
                """
                SELECT c.*, d.name AS source
                FROM chunks c JOIN documents d ON d.id = c.doc_id
                ORDER BY c.id
                """
            ):
                chunks.append(
                    {
                        "text": r["text"],
                        "embedding": pickle.loads(r["embedding"]),
                        "source": r["source"],
                        "section": r["section"] or "",
                        "doc_id": r["doc_id"],
                        "chunk_id": f"{r['doc_id']}_{r['chunk_index']}",
                    }
                )
        return docs, chunks

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return {"documents": docs, "chunks": chunks, "users": users}

    # ==================================================================
    # 用户
    # ==================================================================
    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str = "user",
        dept: str = "",
        tenant_id: str = "",
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (tenant_id, username, password_hash, role, dept, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (
                    tenant_id or settings.DEFAULT_TENANT,
                    username,
                    password_hash,
                    role,
                    dept or "",
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        return cur.lastrowid

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, tenant_id, username, role, dept, status, created_at, last_login_at"
                " FROM users ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_login(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (datetime.now().isoformat(), user_id),
            )
            conn.commit()

    def set_user_status(self, user_id: int, status: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
            conn.commit()
        return cur.rowcount > 0

    def update_user(
        self,
        user_id: int,
        role: Optional[str] = None,
        status: Optional[str] = None,
        dept: Optional[str] = None,
    ) -> bool:
        """按需更新用户字段（None 表示不改动）。"""
        sets, params = [], []
        if role is not None:
            sets.append("role = ?")
            params.append(role)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if dept is not None:
            sets.append("dept = ?")
            params.append(dept)
        if not sets:
            return False
        params.append(user_id)
        with self._lock, self._connect() as conn:
            cur = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
        return cur.rowcount > 0

    def user_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def claim_orphan_documents(self, owner_id: int) -> int:
        """把无归属的历史文档划归指定用户（默认管理员）。

        fail-closed 原则：历史文档不自动公开，而是归属管理员，
        需要共享时显式授权 ACL——避免「升级后老文档突然全员可见」的安全事故。
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE documents SET owner_id = ? WHERE owner_id IS NULL OR owner_id = 0",
                (owner_id,),
            )
            conn.commit()
        return cur.rowcount

    # ==================================================================
    # 文档级 ACL
    # ==================================================================
    def grant_acl(
        self, doc_id: int, subject_type: str, subject_id: str, permission: str = "read"
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO doc_acl (doc_id, subject_type, subject_id, permission, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (doc_id, subject_type, str(subject_id), permission, datetime.now().isoformat()),
            )
            conn.commit()

    def revoke_acl(self, doc_id: int, subject_type: str, subject_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM doc_acl WHERE doc_id = ? AND subject_type = ? AND subject_id = ?",
                (doc_id, subject_type, str(subject_id)),
            )
            conn.commit()
        return cur.rowcount > 0

    def list_acl(self, doc_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT subject_type, subject_id, permission, created_at FROM doc_acl WHERE doc_id = ?",
                (doc_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def allowed_doc_ids(self, user_id: int, role: str, dept: str, tenant_id: str) -> set[int]:
        """计算用户可见的文档 ID 集合（权限过滤的单一事实来源）。

        可见规则：
        1. 同租户；
        2. 本人上传（owner）；
        3. 密级 public；
        4. 密级 internal 且部门匹配；
        5. kb_manager 可见本部门 internal/confidential；
        6. 显式 ACL 授权（user/role/dept 任一维度命中）——这是访问 confidential/secret 的唯一途径。
        """
        sql = """
        SELECT DISTINCT d.id AS id
        FROM documents d
        LEFT JOIN doc_acl a
               ON a.doc_id = d.id AND a.permission = 'read'
        WHERE COALESCE(d.tenant_id, ?) = ?
          AND (
                d.owner_id = ?
             OR d.classification = 'public'
             OR (d.classification = 'internal' AND d.dept <> '' AND d.dept = ?)
             OR (? = 'kb_manager' AND d.classification IN ('internal', 'confidential')
                 AND (d.dept = '' OR d.dept = ?))
             OR (a.subject_type = 'user' AND a.subject_id = ?)
             OR (a.subject_type = 'role' AND a.subject_id = ?)
             OR (a.subject_type = 'dept' AND a.subject_id <> '' AND a.subject_id = ?)
          )
        """
        params = (
            settings.DEFAULT_TENANT,
            tenant_id,
            user_id,
            dept,
            role,
            dept,
            str(user_id),
            role,
            dept,
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {r["id"] for r in rows}

    # ==================================================================
    # 审计日志（append-only）
    # ==================================================================
    def write_audit(
        self,
        action: str,
        *,
        user_id: int = 0,
        username: str = "",
        tenant_id: str = "",
        resource_type: str = "",
        resource_id: str = "",
        detail: str = "",
        ip: str = "",
        ua: str = "",
        success: bool = True,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                  (ts, tenant_id, user_id, username, action, resource_type, resource_id,
                   detail, ip, ua, success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    tenant_id,
                    user_id,
                    username,
                    action,
                    resource_type,
                    resource_id,
                    detail[:1000],
                    ip,
                    ua[:300],
                    1 if success else 0,
                ),
            )
            conn.commit()

    def list_audit(
        self, limit: int = 50, action: Optional[str] = None, username: Optional[str] = None
    ) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if action:
            sql += " AND action = ?"
            params.append(action)
        if username:
            sql += " AND username = ?"
            params.append(username)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def audit_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    # ==================================================================
    # 会话与消息（三层记忆的持久化基础）
    # ==================================================================
    def get_or_create_conversation(
        self, user_id: int, username: str, tenant_id: str, client_key: str, title: str = ""
    ) -> dict:
        """按 (用户, 客户端会话标识) 取会话；不存在则创建。"""
        now = datetime.now().isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? AND client_key = ?",
                (user_id, client_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO conversations
                      (tenant_id, user_id, username, client_key, title, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tenant_id or settings.DEFAULT_TENANT, user_id, username, client_key, title, now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM conversations WHERE user_id = ? AND client_key = ?",
                    (user_id, client_key),
                ).fetchone()
        return dict(row)

    def get_conversation(self, conv_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return dict(row) if row else None

    def list_conversations(self, user_id: int, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def rename_conversation(self, conv_id: int, title: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title[:100], datetime.now().isoformat(), conv_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def delete_conversation(self, conv_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            conn.commit()
        return cur.rowcount > 0

    def add_message(self, conv_id: int, role: str, content: str) -> int:
        """追加消息并同步会话计数与更新时间。"""
        now = datetime.now().isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conv_id, role, content, now),
            )
            conn.execute(
                "UPDATE conversations SET message_count = message_count + 1, updated_at = ? WHERE id = ?",
                (now, conv_id),
            )
            conn.commit()
        return cur.lastrowid

    def list_messages(
        self, conv_id: int, limit: Optional[int] = None, after_id: int = 0
    ) -> list[dict]:
        """返回会话消息（按时间正序）。

        limit: 取最近 N 条（工作记忆窗口）；after_id: 仅取该消息 id 之后的部分（用于增量摘要）。
        """
        with self._connect() as conn:
            if limit:
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? AND id > ?"
                    " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (conv_id, after_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id ASC",
                    (conv_id, after_id),
                ).fetchall()
        return [dict(r) for r in rows]

    def count_messages(self, conv_id: int) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)
            ).fetchone()[0]

    def update_summary(self, conv_id: int, summary: str, upto_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET summary = ?, summarized_upto = ?, updated_at = ? WHERE id = ?",
                (summary, upto_id, datetime.now().isoformat(), conv_id),
            )
            conn.commit()

    def purge_old_conversations(self, days: int) -> int:
        """归档超期会话（TTL 清理）。"""
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,))
            conn.commit()
        return cur.rowcount

    # ==================================================================
    # 异步任务
    # ==================================================================
    def create_task(self, task_id: str, doc_name: str, status: str = "pending") -> None:
        now = datetime.now().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks (task_id, doc_name, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, doc_name, status, now, now),
            )
            conn.commit()

    def update_task(
        self,
        task_id: str,
        status: str,
        chunks: int = 0,
        error: str = "",
        warnings: Optional[list[str]] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, chunks = ?, error = ?, warnings = ?, updated_at = ?"
                " WHERE task_id = ?",
                (
                    status,
                    int(chunks or 0),
                    error or "",
                    " | ".join(warnings or []),
                    datetime.now().isoformat(),
                    task_id,
                ),
            )
            conn.commit()

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_interrupted_tasks(self) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'failed', error = '服务重启导致任务中断，请重新上传',"
                " updated_at = ? WHERE status IN ('pending', 'parsing', 'ocr', 'embedding')",
                (datetime.now().isoformat(),),
            )
            conn.commit()
        return cur.rowcount


__all__ = ["DocumentStore"]
