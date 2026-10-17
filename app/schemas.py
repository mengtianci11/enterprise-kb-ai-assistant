"""接口数据模型（Pydantic Schema）。"""
from typing import List, Optional

from pydantic import BaseModel, Field


# ======================================================================
# 认证与用户
# ======================================================================
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class UserInfo(BaseModel):
    id: int
    username: str
    role: str
    dept: str = ""
    tenant_id: str = ""
    status: str = "active"
    last_login_at: str = ""


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="access_token 有效期（秒）")
    user: UserInfo


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field("user", description="admin | kb_manager | user | auditor")
    dept: str = ""


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = Field(None, description="active | disabled")
    dept: Optional[str] = None


class UserListResponse(BaseModel):
    users: List[UserInfo]


# ======================================================================
# 文档 ACL
# ======================================================================
class AclGrantRequest(BaseModel):
    subject_type: str = Field(..., description="user | role | dept")
    subject_id: str
    permission: str = "read"


class AclItem(BaseModel):
    subject_type: str
    subject_id: str
    permission: str = "read"
    created_at: str = ""


class AclListResponse(BaseModel):
    document: str
    owner_id: int = 0
    dept: str = ""
    classification: str = "internal"
    acl: List[AclItem] = []


# ======================================================================
# 审计
# ======================================================================
class AuditItem(BaseModel):
    id: int
    ts: str
    username: str = ""
    action: str
    resource_type: str = ""
    resource_id: str = ""
    detail: str = ""
    ip: str = ""
    success: int = 1


class AuditListResponse(BaseModel):
    logs: List[AuditItem]


# ======================================================================
# 问答
# ======================================================================
class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, description="用户问题")
    conversation_id: str = Field("default", max_length=64, description="会话 ID（用于多轮记忆）")


class ChatResponse(BaseModel):
    answer: str
    request_id: str
    sources: List[dict] = []


# ======================================================================
# 文档
# ======================================================================
class UploadResponse(BaseModel):
    filename: str
    status: str = Field(..., description="ready（已就绪）或 processing（后台解析中）")
    chunks: int = Field(0, description="本次新增分块数（异步时为 0，完成后查任务）")
    total_chunks: int = Field(..., description="库内全部分块数")
    task_id: Optional[str] = Field(None, description="异步任务 ID（status=processing 时有值）")
    format: str = Field("", description="识别到的文档格式")
    pages: int = Field(0, description="页数（PDF/PPT）")
    blocks: int = Field(0, description="解析出的结构块数量")
    warnings: List[str] = Field(default_factory=list, description="解析告警（如 OCR 低置信度）")
    cached: bool = Field(False, description="是否命中解析缓存")
    classification: str = Field("internal", description="文档密级")


class DocumentItem(BaseModel):
    id: int = 0
    name: str
    chunks: int
    added_at: str
    format: str = ""
    pages: int = 0
    blocks: int = 0
    status: str = "ready"
    warnings: str = ""
    classification: str = "internal"
    owner_id: int = 0
    dept: str = ""
    tenant_id: str = ""


class DocListResponse(BaseModel):
    documents: List[DocumentItem]
    visible_count: int = 0
    total_count: int = 0


class TaskInfo(BaseModel):
    task_id: str
    doc_name: str
    status: str = Field(..., description="pending/parsing/ocr/embedding/ready/failed")
    chunks: int = 0
    error: str = ""
    warnings: str = ""
    created_at: str = ""
    updated_at: str = ""


class TaskListResponse(BaseModel):
    tasks: List[TaskInfo]


# ======================================================================
# 会话与记忆
# ======================================================================
class ConversationItem(BaseModel):
    id: int
    client_key: str = ""
    title: str = ""
    summary: str = ""
    message_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class ConversationListResponse(BaseModel):
    conversations: List[ConversationItem]


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    created_at: str = ""


class ConversationDetail(BaseModel):
    conversation: ConversationItem
    messages: List[MessageItem]


class RenameConversationRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)


class HealthResponse(BaseModel):
    status: str
    version: str
    model: str
    documents: int
    chunks: int
    parsers: List[str] = Field(default_factory=list, description="已注册的解析器")
    ocr_available: bool = Field(False, description="OCR 组件是否可用")
    pending_tasks: int = Field(0, description="进行中的异步任务数")
    auth_enabled: bool = Field(True, description="是否启用了认证")


class DeleteResponse(BaseModel):
    filename: str
    deleted: bool


class ErrorResponse(BaseModel):
    detail: str
