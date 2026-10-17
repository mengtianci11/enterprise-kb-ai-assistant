"""认证与授权核心：密码哈希、JWT 签发/校验、角色权限依赖、可见文档集合计算。

选型与原理：
- **密码哈希**：stdlib `hashlib.pbkdf2_hmac(sha256, 200_000 迭代)` + 每用户随机盐，
  存储格式自带算法与参数（`pbkdf2_sha256$迭代数$盐$哈希`），
  便于日后平滑升级到 argon2（无需改表结构）。选它是因为零额外依赖、
  Windows 上不涉及编译轮子；校验用 `hmac.compare_digest` 做常量时间比较，防时序攻击。
- **令牌**：PyJWT HS256。不手写 JWT——签名/校验/过期/算法混淆防护这些细节
  属于「必须用成熟实现」的范畴。生产可换 RS256 + OIDC（issuer/audience 已预留）。
- **权限模型**：RBAC（角色 → 权限点）+ 文档级 ACL（用户/角色/部门授权）+ 密级标签。
  令牌中的角色只作为「提示」，**每次请求都以数据库为准**（防止改权限后旧令牌继续生效）。
- **可见文档集合**：`allowed_doc_ids_for()` 统一计算，供检索层在**召回阶段**过滤，
  这是「不越权看到内容」的关键（详见 docs/design_auth.md）。
"""
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
ISSUER = "kb-assistant"
PBKDF2_ITERATIONS = 200_000

# 角色 → 权限点
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"doc:read", "doc:write", "doc:delete", "doc:acl", "user:manage", "audit:read", "chat"},
    "kb_manager": {"doc:read", "doc:write", "doc:delete", "doc:acl", "audit:read", "chat"},
    "user": {"doc:read", "chat"},
    "auditor": {"doc:read", "audit:read"},
}
VALID_ROLES = tuple(ROLE_PERMISSIONS.keys())
VALID_CLASSIFICATIONS = ("public", "internal", "confidential", "secret")


# ======================================================================
# 密码哈希
# ======================================================================
def encode_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """生成可存储的密码哈希串（算法与参数内嵌，便于升级）。"""
    if not password or len(password) < 6:
        raise ValueError("密码长度至少 6 位")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验密码。"""
    try:
        algo, iter_s, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iter_s)
        ).hex()
        return hmac.compare_digest(calc, digest)
    except Exception:  # noqa: BLE001
        return False


# ======================================================================
# JWT
# ======================================================================
_secret_cache: Optional[str] = None


def get_secret() -> str:
    """获取签名密钥：优先 .env；否则首次运行生成并持久化（重启后令牌仍有效）。"""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    if settings.JWT_SECRET:
        _secret_cache = settings.JWT_SECRET
        return _secret_cache

    path: Path = settings.DATA_DIR / ".jwt_secret"
    if path.exists():
        _secret_cache = path.read_text(encoding="utf-8").strip()
    else:
        _secret_cache = secrets.token_urlsafe(48)
        path.write_text(_secret_cache, encoding="utf-8")
        logger.warning("已自动生成 JWT 密钥并保存到 %s（生产环境建议改用 .env 注入或 KMS）", path)
    return _secret_cache


def create_token(user: dict, token_type: str = "access") -> tuple[str, int]:
    """签发令牌，返回 (token, 有效期秒数)。"""
    minutes = settings.JWT_EXPIRE_MINUTES if token_type == "access" else settings.JWT_REFRESH_DAYS * 24 * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user["role"],
        "dept": user.get("dept") or "",
        "tenant": user.get("tenant_id") or settings.DEFAULT_TENANT,
        "type": token_type,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    token = jwt.encode(payload, get_secret(), algorithm=ALGORITHM)
    return token, minutes * 60


def decode_token(token: str, expected_type: str = "access") -> dict:
    """校验并解析令牌；任何异常都转为 401。"""
    try:
        payload = jwt.decode(
            token, get_secret(), algorithms=[ALGORITHM], issuer=ISSUER,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效") from exc

    if payload.get("type") != expected_type:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌类型不正确")
    return payload


# ======================================================================
# 当前用户与权限依赖
# ======================================================================
@dataclass
class CurrentUser:
    id: int
    username: str
    role: str
    dept: str
    tenant_id: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def has(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, set())


SYSTEM_USER = CurrentUser(id=0, username="system", role="admin", dept="", tenant_id="default")

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> CurrentUser:
    """解析 Bearer 令牌 → 当前用户（角色以数据库为准）。"""
    if not settings.AUTH_ENABLED:
        return SYSTEM_USER

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "未认证：请在请求头携带 Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials, expected_type="access")

    from app.state import state  # 延迟导入，避免循环依赖

    row = state.store.get_user(int(payload["sub"]))
    if row is None or row.get("status") != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被停用")

    return CurrentUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        dept=row.get("dept") or "",
        tenant_id=row.get("tenant_id") or settings.DEFAULT_TENANT,
    )


def require_permission(permission: str):
    """依赖工厂：要求当前用户具备某权限点。"""

    async def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has(permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"权限不足：角色 {user.role} 不具备 {permission}",
            )
        return user

    return _dep


def allowed_doc_ids_for(user: CurrentUser) -> Optional[set[int]]:
    """当前用户可见的文档 ID 集合。

    返回 None 表示不限制（管理员）；返回集合表示白名单（可能为空集）。
    """
    from app.state import state

    if user.is_admin:
        return None
    return state.store.allowed_doc_ids(
        user_id=user.id, role=user.role, dept=user.dept or "", tenant_id=user.tenant_id
    )


def can_read_document(user: CurrentUser, doc: dict) -> bool:
    """单文档读权限判定（用于列表过滤与引用二次校验）。"""
    if user.is_admin:
        return True
    if doc.get("owner_id") == user.id:
        return True
    if (doc.get("tenant_id") or settings.DEFAULT_TENANT) != user.tenant_id:
        return False
    classification = doc.get("classification") or "internal"
    if classification == "public":
        return True
    if classification == "internal" and doc.get("dept") and doc.get("dept") == user.dept:
        return True
    if user.role == "kb_manager" and classification in ("internal", "confidential"):
        return doc.get("dept") in ("", None) or doc.get("dept") == user.dept
    return False


__all__ = [
    "CurrentUser",
    "SYSTEM_USER",
    "VALID_ROLES",
    "VALID_CLASSIFICATIONS",
    "ROLE_PERMISSIONS",
    "encode_password",
    "verify_password",
    "create_token",
    "decode_token",
    "get_current_user",
    "require_permission",
    "allowed_doc_ids_for",
    "can_read_document",
    "get_secret",
]
