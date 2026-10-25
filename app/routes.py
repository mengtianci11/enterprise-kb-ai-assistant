"""HTTP 路由层：认证 / 权限 / 文档 / 问答 / 异步任务 / 审计。

v3.1 变化（多用户与权限隔离）：
- 所有业务接口都需要认证（Bearer Token），按角色权限点鉴权；
- 检索在**召回阶段**按「当前用户可见文档集合」过滤（见 app/context.py）；
- 文档带 owner / 部门 / 密级 / 租户标签，支持文档级 ACL 授权；
- 关键操作写入 append-only 审计日志（登录、上传、删除、问答、权限变更）；
- `/health` 只暴露非敏感摘要，详细配置移到需鉴权的 `/admin/config`。
"""
import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sse_starlette import EventSourceResponse

import app.llm as llm  # noqa: F401  (确保模块可打桩的同时被显式依赖)
from app import __version__
from app.config import settings
from app.citation import build_evidence, sanitize_answer, verify_answer
from app.context import (
    current_evidence,
    reset_evidence_context,
    reset_permission_context,
    set_evidence_context,
    set_permission_context,
)
from app.parsers import (
    PARSERS,
    ParseError,
    content_md5,
    needs_async,
    ocr as ocr_mod,
    supported_extensions,
)
from app.schemas import (
    AclGrantRequest,
    AclItem,
    AclListResponse,
    AuditItem,
    AuditListResponse,
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationItem,
    ConversationListResponse,
    CreateUserRequest,
    DeleteResponse,
    DocListResponse,
    DocumentItem,
    HealthResponse,
    LoginRequest,
    MessageItem,
    RefreshRequest,
    RenameConversationRequest,
    TaskInfo,
    TaskListResponse,
    TokenResponse,
    UpdateUserRequest,
    UploadResponse,
    UserInfo,
    UserListResponse,
)
from app.memory import build_context_messages, should_summarize, update_rolling_summary
from app.security import (
    VALID_CLASSIFICATIONS,
    VALID_ROLES,
    CurrentUser,
    allowed_doc_ids_for,
    create_token,
    decode_token,
    encode_password,
    get_current_user,
    require_permission,
    verify_password,
)
from app.state import state

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================================================================
# 公共工具
# ==================================================================
def _stream_text(message) -> str:
    """从流式消息块中提取可展示文本（兼容 token 块与完整消息两种形态）。

    过滤规则：
    - 跳过工具消息（ToolMessage）：它包含的是检索到的资料，不是回答；
    - 跳过工具调用片段（tool_call_chunks）：否则会把 JSON 参数当答案吐给用户；
    - content 可能是 str，也可能是结构化列表（取 type=text 的部分）。

    说明：langchain 1.x 的 create_agent 内部使用 ainvoke，通常拿不到逐字 token 块，
    而是拿到「完整 AIMessage」；因此这里同时兼容 AIMessageChunk 与 AIMessage，
    由调用方决定是否分段下发（保持前端打字机体验）。
    """
    try:
        from langchain_core.messages import AIMessage, ToolMessage

        if isinstance(message, ToolMessage):
            return ""
        if not isinstance(message, AIMessage):  # AIMessageChunk 是 AIMessage 的子类
            return ""
        if getattr(message, "tool_call_chunks", None):
            return ""
        content = message.content
    except Exception:  # noqa: BLE001
        return ""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def _client_meta(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    return ip, request.headers.get("user-agent", "")


def _audit(
    request: Request,
    action: str,
    *,
    user: Optional[CurrentUser] = None,
    resource_type: str = "",
    resource_id: str = "",
    detail: str = "",
    success: bool = True,
) -> None:
    """写审计日志（失败不影响主流程）。"""
    try:
        ip, ua = _client_meta(request)
        state.store.write_audit(
            action,
            user_id=user.id if user else 0,
            username=user.username if user else "",
            tenant_id=user.tenant_id if user else "",
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            ip=ip,
            ua=ua,
            success=success,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计日志写入失败：%s", exc)


def _user_info(row: dict) -> UserInfo:
    return UserInfo(
        id=row.get("id", 0),
        username=row.get("username", ""),
        role=row.get("role", "user"),
        dept=row.get("dept") or "",
        tenant_id=row.get("tenant_id") or settings.DEFAULT_TENANT,
        status=row.get("status") or "active",
        last_login_at=row.get("last_login_at") or "",
    )


# ==================================================================
# 认证
# ==================================================================
@router.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    """账号密码登录 → 返回 access/refresh 令牌。"""
    row = state.store.get_user_by_username(req.username)
    if row is None or not verify_password(req.password, row["password_hash"]):
        _audit(request, "login", detail=f"username={req.username}", success=False)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if row.get("status") != "active":
        _audit(request, "login", detail=f"username={req.username} status={row.get('status')}", success=False)
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")

    state.store.touch_login(row["id"])
    access_token, expires_in = create_token(row, "access")
    refresh_token, _ = create_token(row, "refresh")
    user = CurrentUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        dept=row.get("dept") or "",
        tenant_id=row.get("tenant_id") or settings.DEFAULT_TENANT,
    )
    _audit(request, "login", user=user)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=_user_info(row),
    )


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest):
    """用 refresh token 换新的 access token。"""
    payload = decode_token(req.refresh_token, expected_type="refresh")
    row = state.store.get_user(int(payload["sub"]))
    if row is None or row.get("status") != "active":
        raise HTTPException(status_code=401, detail="用户不存在或已被停用")
    access_token, expires_in = create_token(row, "access")
    refresh_token, _ = create_token(row, "refresh")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=_user_info(row),
    )


@router.get("/auth/me", response_model=UserInfo)
async def me(user: CurrentUser = Depends(get_current_user)):
    """当前登录用户信息。"""
    row = state.store.get_user(user.id) or {}
    return _user_info(row)


# ==================================================================
# 用户管理（管理员）
# ==================================================================
@router.get("/admin/users", response_model=UserListResponse)
async def list_users(user: CurrentUser = Depends(require_permission("user:manage"))):
    rows = state.store.list_users()
    if not user.is_admin:
        rows = [r for r in rows if r.get("tenant_id") == user.tenant_id]
    return UserListResponse(users=[_user_info(r) for r in rows])


@router.post("/admin/users", response_model=UserInfo)
async def create_user(
    req: CreateUserRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("user:manage")),
):
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"角色必须是 {VALID_ROLES} 之一")
    if state.store.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail=f"用户名 {req.username} 已存在")
    try:
        password_hash = encode_password(req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    uid = state.store.create_user(
        req.username, password_hash, req.role, req.dept, user.tenant_id
    )
    _audit(
        request,
        "user_create",
        user=user,
        resource_type="user",
        resource_id=req.username,
        detail=f"role={req.role} dept={req.dept}",
    )
    return _user_info(state.store.get_user(uid))


@router.patch("/admin/users/{user_id}", response_model=UserInfo)
async def update_user(
    user_id: int,
    req: UpdateUserRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("user:manage")),
):
    target = state.store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if req.role and req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"角色必须是 {VALID_ROLES} 之一")
    if req.status and req.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="状态只能是 active 或 disabled")
    if target["id"] == user.id and req.status == "disabled":
        raise HTTPException(status_code=400, detail="不能停用当前登录账号")

    state.store.update_user(user_id, role=req.role, status=req.status, dept=req.dept)
    _audit(
        request,
        "user_update",
        user=user,
        resource_type="user",
        resource_id=target["username"],
        detail=f"role={req.role} status={req.status} dept={req.dept}",
    )
    return _user_info(state.store.get_user(user_id))


# ==================================================================
# 审计与配置（管理员/审计员）
# ==================================================================
@router.get("/admin/audit", response_model=AuditListResponse)
async def list_audit(
    limit: int = 50,
    action: Optional[str] = None,
    username: Optional[str] = None,
    user: CurrentUser = Depends(require_permission("audit:read")),
):
    """审计日志（append-only，按时间倒序）。"""
    rows = state.store.list_audit(limit=min(limit, 500), action=action, username=username)
    return AuditListResponse(logs=[AuditItem(**r) for r in rows])


@router.get("/admin/config")
async def admin_config(user: CurrentUser = Depends(require_permission("user:manage"))):
    """脱敏配置与统计（不再通过公开的 /health 暴露）。"""
    return {
        "config": settings.as_dict(),
        "stats": state.store.stats(),
        "cache": state.cache.stats(),
    }


# ==================================================================
# 文档
# ==================================================================
@router.post("/documents/upload", response_model=UploadResponse)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    classification: str = Form("internal"),
    user: CurrentUser = Depends(require_permission("doc:write")),
):
    """上传文档：解析 → 分块 → 向量化 → 持久化。

    - 小文件 + 非耗时格式 → 同步就绪；扫描件/图片/大文件 → 后台任务；
    - 文档自动打上 owner / 部门 / 租户 / 密级标签。
    """
    name = (file.filename or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if classification not in VALID_CLASSIFICATIONS:
        raise HTTPException(
            status_code=400, detail=f"密级必须是 {VALID_CLASSIFICATIONS} 之一"
        )

    ext = Path(name).suffix.lower()
    supported = supported_extensions()
    if ext not in supported:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext or '(无扩展名)'}，当前支持：{', '.join(sorted(supported))}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")

    md5 = content_md5(content)
    if state.processor.store.doc_exists(md5, name):
        raise HTTPException(status_code=409, detail=f"文档已存在（{name} 或相同内容），请勿重复上传")

    perm_meta = {
        "owner_id": user.id,
        "classification": classification,
        "tenant_id": user.tenant_id,
        "dept": user.dept,
    }
    heavy = needs_async(content, name)
    too_big = len(content) > settings.PARSE_SYNC_MAX_BYTES

    if heavy or too_big:
        task_id = state.runner.submit(name, content, md5, **perm_meta)
        _audit(
            request,
            "upload",
            user=user,
            resource_type="document",
            resource_id=name,
            detail=f"async task={task_id} classification={classification} size={len(content)}",
        )
        return UploadResponse(
            filename=name,
            status="processing",
            chunks=0,
            total_chunks=len(state.processor.chunks),
            task_id=task_id,
            classification=classification,
        )

    try:
        result = state.processor.add_document(name, content, md5=md5, **perm_meta)
    except ParseError as exc:
        _audit(request, "upload", user=user, resource_type="document", resource_id=name,
               detail=f"parse_error={exc}", success=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=409 if "已存在" in detail else 400, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("文档处理失败")
        raise HTTPException(status_code=502, detail=f"文档处理失败：{exc}") from exc

    try:
        state.rebuild_after_change()
    except Exception as exc:  # noqa: BLE001
        logger.exception("索引/Agent 重建失败")
        raise HTTPException(status_code=502, detail=f"索引构建失败：{exc}") from exc

    _audit(
        request,
        "upload",
        user=user,
        resource_type="document",
        resource_id=name,
        detail=f"chunks={result['chunks']} classification={classification}",
    )
    return UploadResponse(
        filename=name,
        status="ready",
        chunks=result["chunks"],
        total_chunks=len(state.processor.chunks),
        format=result.get("format", ""),
        pages=int(result.get("pages") or 0),
        blocks=int(result.get("blocks") or 0),
        warnings=result.get("warnings") or [],
        cached=bool(result.get("cached")),
        classification=classification,
    )


@router.get("/documents", response_model=DocListResponse)
async def list_documents(user: CurrentUser = Depends(require_permission("doc:read"))):
    """文档清单（按权限过滤，只返回当前用户可见的文档）。"""
    allowed = allowed_doc_ids_for(user)
    all_docs = state.processor.documents
    visible = [d for d in all_docs if allowed is None or d.get("id") in allowed]
    return DocListResponse(
        documents=[DocumentItem(**d) for d in visible],
        visible_count=len(visible),
        total_count=len(all_docs),
    )


@router.delete("/documents/{filename}", response_model=DeleteResponse)
async def delete_document(
    filename: str,
    request: Request,
    user: CurrentUser = Depends(require_permission("doc:delete")),
):
    """删除文档：需管理员、知识库管理员，或文档 owner。"""
    doc = state.store.get_document(filename)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档 {filename} 不存在")
    if not user.is_admin and doc.get("owner_id") != user.id and user.role != "kb_manager":
        _audit(request, "delete", user=user, resource_type="document", resource_id=filename,
               detail="permission_denied", success=False)
        raise HTTPException(status_code=403, detail="无权删除该文档（仅管理员、知识库管理员或文档所有者可删除）")

    ok = state.processor.delete_document(filename)
    if not ok:
        raise HTTPException(status_code=404, detail=f"文档 {filename} 不存在")
    if state.processor.chunks:
        state.rebuild_after_change()
    else:
        state.rag.reset()
    _audit(request, "delete", user=user, resource_type="document", resource_id=filename)
    return DeleteResponse(filename=filename, deleted=True)


# ==================================================================
# 文档级 ACL
# ==================================================================
@router.get("/documents/{filename}/acl", response_model=AclListResponse)
async def get_document_acl(
    filename: str,
    user: CurrentUser = Depends(require_permission("doc:acl")),
):
    doc = state.store.get_document(filename)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档 {filename} 不存在")
    return AclListResponse(
        document=filename,
        owner_id=doc.get("owner_id") or 0,
        dept=doc.get("dept") or "",
        classification=doc.get("classification") or "internal",
        acl=[AclItem(**a) for a in state.store.list_acl(doc["id"])],
    )


@router.post("/documents/{filename}/acl", response_model=AclListResponse)
async def grant_document_acl(
    filename: str,
    req: AclGrantRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("doc:acl")),
):
    """授予访问权限：subject_type ∈ user | role | dept。

    说明：密级 confidential / secret 的文档，只有通过显式 ACL 才能被访问。
    """
    if req.subject_type not in ("user", "role", "dept"):
        raise HTTPException(status_code=400, detail="subject_type 必须是 user / role / dept")
    if req.subject_type == "role" and req.subject_id not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"角色必须是 {VALID_ROLES} 之一")

    doc = state.store.get_document(filename)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档 {filename} 不存在")

    # 用户维度统一规范化为「用户 ID」存储：ID 稳定，用户名可能被修改
    subject_id = req.subject_id
    if req.subject_type == "user":
        target = state.store.get_user_by_username(req.subject_id)
        if target is None and str(req.subject_id).isdigit():
            target = state.store.get_user(int(req.subject_id))
        if target is None:
            raise HTTPException(status_code=404, detail=f"用户 {req.subject_id} 不存在")
        subject_id = str(target["id"])

    state.store.grant_acl(doc["id"], req.subject_type, subject_id, req.permission)
    _audit(
        request,
        "acl_change",
        user=user,
        resource_type="document",
        resource_id=filename,
        detail=f"grant {req.subject_type}:{subject_id} {req.permission}",
    )
    return await get_document_acl(filename, user)


@router.delete("/documents/{filename}/acl", response_model=AclListResponse)
async def revoke_document_acl(
    filename: str,
    request: Request,
    subject_type: str,
    subject_id: str,
    user: CurrentUser = Depends(require_permission("doc:acl")),
):
    doc = state.store.get_document(filename)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档 {filename} 不存在")
    subject_id = subject_id
    if subject_type == "user":
        target = state.store.get_user_by_username(subject_id)
        if target is None and str(subject_id).isdigit():
            target = state.store.get_user(int(subject_id))
        if target is not None:
            subject_id = str(target["id"])
    state.store.revoke_acl(doc["id"], subject_type, subject_id)
    _audit(
        request,
        "acl_change",
        user=user,
        resource_type="document",
        resource_id=filename,
        detail=f"revoke {subject_type}:{subject_id}",
    )
    return await get_document_acl(filename, user)


# ==================================================================
# 异步任务
# ==================================================================
@router.get("/documents/tasks", response_model=TaskListResponse)
async def list_tasks(limit: int = 20, user: CurrentUser = Depends(require_permission("doc:write"))):
    return TaskListResponse(tasks=[TaskInfo(**t) for t in state.runner.list(min(limit, 100))])


@router.get("/documents/tasks/{task_id}", response_model=TaskInfo)
async def get_task(task_id: str, user: CurrentUser = Depends(get_current_user)):
    task = state.runner.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return TaskInfo(**task)


# ==================================================================
# 会话与记忆
# ==================================================================
def _prepare_conversation(user: CurrentUser, client_key: str, first_question: str) -> dict:
    """取/建会话，并用首问生成标题。"""
    conv = state.store.get_or_create_conversation(
        user.id,
        user.username,
        user.tenant_id,
        client_key or "default",
        title=first_question[:40],
    )
    if not conv.get("title"):
        title = first_question[:40]
        state.store.rename_conversation(conv["id"], title)
        conv["title"] = title
    return conv


def _kick_summary(conv: dict) -> None:
    """触发滚动摘要（后台线程，不阻塞响应）。"""
    try:
        if not should_summarize(state.store, conv):
            return
        import threading

        threading.Thread(
            target=update_rolling_summary,
            args=(
                state.store,
                conv,
                lambda messages, temperature=0.2: llm.chat(messages, temperature=temperature),
            ),
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("摘要任务调度失败：%s", exc)


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(user: CurrentUser = Depends(require_permission("chat"))):
    """我的会话列表（按更新时间倒序）。"""
    rows = state.store.list_conversations(user.id, limit=50)
    return ConversationListResponse(conversations=[ConversationItem(**r) for r in rows])


@router.get("/conversations/{conv_id}", response_model=ConversationDetail)
async def get_conversation(conv_id: int, user: CurrentUser = Depends(require_permission("chat"))):
    """会话详情（消息历史）。仅本人可访问。"""
    conv = state.store.get_conversation(conv_id)
    if conv is None or conv["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = state.store.list_messages(conv_id)
    return ConversationDetail(
        conversation=ConversationItem(**conv),
        messages=[MessageItem(**m) for m in messages],
    )


@router.patch("/conversations/{conv_id}", response_model=ConversationItem)
async def rename_conversation(
    conv_id: int,
    req: RenameConversationRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("chat")),
):
    conv = state.store.get_conversation(conv_id)
    if conv is None or conv["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    state.store.rename_conversation(conv_id, req.title)
    _audit(request, "conversation_rename", user=user, resource_type="conversation",
           resource_id=str(conv_id), detail=req.title)
    return ConversationItem(**state.store.get_conversation(conv_id))


@router.delete("/conversations/{conv_id}", response_model=DeleteResponse)
async def delete_conversation(
    conv_id: int,
    request: Request,
    user: CurrentUser = Depends(require_permission("chat")),
):
    conv = state.store.get_conversation(conv_id)
    if conv is None or conv["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    state.store.delete_conversation(conv_id)
    _audit(request, "conversation_delete", user=user, resource_type="conversation",
           resource_id=str(conv_id))
    return DeleteResponse(filename=str(conv_id), deleted=True)


# ==================================================================
# 问答（权限过滤在召回阶段生效 + 三层记忆）
# ==================================================================
@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("chat")),
):
    """同步问答：工作记忆（最近 K 轮）+ 摘要记忆 + 召回期权限过滤。"""
    if state.agent is None:
        raise HTTPException(status_code=400, detail="知识库暂无文档，请先上传文档再提问")

    conv = _prepare_conversation(user, req.conversation_id, req.question)
    messages = build_context_messages(state.store, conv, req.question)
    state.store.add_message(conv["id"], "user", req.question)

    allowed = allowed_doc_ids_for(user)
    tokens = set_permission_context(allowed, user.username)
    evidence_token = set_evidence_context()
    try:
        result = state.agent.invoke(
            {"messages": messages},
            {"configurable": {"thread_id": f"{user.username}:{conv['id']}"}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("问答调用失败")
        _audit(request, "chat", user=user, resource_type="conversation",
               resource_id=str(conv["id"]), detail=req.question, success=False)
        raise HTTPException(status_code=502, detail=f"问答服务异常：{exc}") from exc
    finally:
        reset_permission_context(tokens)
        evidence = build_evidence(
            current_evidence.get(), limit=settings.CITATION_MAX_EVIDENCE
        )
        reset_evidence_context(evidence_token)

    answer = result["messages"][-1].content
    citations: list[dict] = []
    citation_metrics: dict = {}
    if settings.CITATION_VERIFY and evidence and "[E" in (answer or ""):
        verification = verify_answer(answer, evidence)
        answer, removed = sanitize_answer(answer, verification)
        citation_metrics = {
            "citation_precision": verification["citation_precision"],
            "citation_recall": verification["citation_recall"],
            "uncited_rate": verification["uncited_rate"],
            "n_sentences": verification["n_sentences"],
            "n_citation_pairs": verification["n_citation_pairs"],
            "removed_sentences": removed,
        }
        citations = [
            {"eid": e.eid, "source": e.source, "chunk_id": e.chunk_id}
            for e in evidence
        ]
    state.store.add_message(conv["id"], "assistant", answer)
    _kick_summary(conv)
    _audit(
        request,
        "chat",
        user=user,
        resource_type="conversation",
        resource_id=str(conv["id"]),
        detail=req.question,
    )
    sources = [{"eid": e.eid, "source": e.source, "chunk_id": e.chunk_id} for e in evidence]
    return ChatResponse(
        answer=answer,
        request_id=str(uuid.uuid4()),
        sources=sources,
        citations=citations,
        citation_metrics=citation_metrics,
    )


@router.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("chat")),
):
    """流式问答（SSE 逐字输出，携带工作记忆 + 摘要记忆）。"""
    if state.agent is None:
        raise HTTPException(status_code=400, detail="知识库暂无文档，请先上传文档再提问")

    conv = _prepare_conversation(user, req.conversation_id, req.question)
    messages = build_context_messages(state.store, conv, req.question)
    state.store.add_message(conv["id"], "user", req.question)

    allowed = allowed_doc_ids_for(user)
    _audit(request, "chat", user=user, resource_type="conversation",
           resource_id=str(conv["id"]), detail=f"[stream] {req.question}")

    async def generate():
        tokens = set_permission_context(allowed, user.username)
        collected: list[str] = []
        try:
            # LangGraph 1.x：astream_events 不再产生 on_chat_model_stream；
            # 用 stream_mode="messages" 取消息（Agent 内部 ainvoke，通常是完整消息）。
            async for chunk in state.agent.astream(
                {"messages": messages},
                config={"configurable": {"thread_id": f"{user.username}:{conv['id']}"}},
                stream_mode="messages",
            ):
                text = _stream_text(chunk[0] if isinstance(chunk, tuple) else chunk)
                if text:
                    collected.append(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("流式问答异常")
            yield {"data": f"\n[错误] {exc}"}
        finally:
            reset_permission_context(tokens)
            answer = "".join(collected)
            # 分段下发，保持前端打字机体验（若上游已是 token 块，这里等价于逐块转发）
            for i in range(0, len(answer), 12):
                yield {"data": answer[i : i + 12]}
                await asyncio.sleep(0.01)
            if answer:
                try:
                    state.store.add_message(conv["id"], "assistant", answer)
                    _kick_summary(conv)
                except Exception:  # noqa: BLE001
                    logger.exception("会话消息保存失败")
        yield {"data": "[DONE]"}

    return EventSourceResponse(generate())


# ==================================================================
# 系统
# ==================================================================
@router.get("/health", response_model=HealthResponse)
async def health():
    """健康检查（公开）：只暴露非敏感摘要，详细配置见 /admin/config。"""
    return HealthResponse(
        status="ok",
        version=__version__,
        model=settings.CHAT_MODEL,
        documents=len(state.processor.documents),
        chunks=len(state.processor.chunks),
        parsers=[p.name for p in PARSERS],
        ocr_available=ocr_mod.ocr_available(),
        pending_tasks=state.runner.pending_count(),
        auth_enabled=settings.AUTH_ENABLED,
    )


__all__ = ["router"]
