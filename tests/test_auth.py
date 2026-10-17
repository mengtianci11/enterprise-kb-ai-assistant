"""认证与授权测试：密码哈希、JWT、登录、RBAC、ACL、审计、召回期权限过滤。"""
import io
import time

import pytest


def _txt(content: str) -> bytes:
    return content.encode("utf-8")


def _upload(client, headers, filename, content: str, classification: str = "internal"):
    return client.post(
        "/documents/upload",
        files={"file": (filename, _txt(content), "text/plain")},
        data={"classification": classification},
        headers=headers,
    )


# ======================================================================
# 密码哈希与令牌（纯单元）
# ======================================================================
class TestPasswordAndToken:
    def test_password_roundtrip(self):
        from app.security import encode_password, verify_password

        stored = encode_password("s3cret-pwd")
        assert stored.startswith("pbkdf2_sha256$")
        assert verify_password("s3cret-pwd", stored) is True
        assert verify_password("wrong-pwd", stored) is False
        # 相同密码两次哈希不同（随机盐）
        assert encode_password("s3cret-pwd") != stored

    def test_weak_password_rejected(self):
        from app.security import encode_password

        with pytest.raises(ValueError):
            encode_password("123")

    def test_token_roundtrip_and_type(self):
        from app.security import create_token, decode_token

        user = {"id": 7, "username": "alice", "role": "user", "dept": "销售部", "tenant_id": "default"}
        token, expires = create_token(user, "access")
        assert expires > 0
        payload = decode_token(token, "access")
        assert payload["username"] == "alice" and payload["role"] == "user"

        refresh, _ = create_token(user, "refresh")
        with pytest.raises(Exception):
            decode_token(refresh, "access")  # 类型不符必须拒绝

    def test_tampered_token_rejected(self):
        from fastapi import HTTPException

        from app.security import create_token, decode_token

        user = {"id": 1, "username": "bob", "role": "admin", "dept": "", "tenant_id": "default"}
        token, _ = create_token(user, "access")
        tampered = token[:-3] + "abc"
        with pytest.raises(HTTPException) as exc:
            decode_token(tampered, "access")
        assert exc.value.status_code == 401


# ======================================================================
# 登录与身份
# ======================================================================
class TestLogin:
    def test_default_admin_bootstrapped(self, secure_client):
        resp = secure_client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["user"]["role"] == "admin"
        assert data["access_token"] and data["refresh_token"] and data["expires_in"] > 0

    def test_wrong_password_rejected(self, secure_client):
        resp = secure_client.post("/auth/login", json={"username": "admin", "password": "bad"})
        assert resp.status_code == 401

    def test_me_requires_token(self, secure_client):
        assert secure_client.get("/auth/me").status_code == 401
        resp = secure_client.get("/auth/me", headers=secure_client.admin_headers)
        assert resp.status_code == 200 and resp.json()["username"] == "admin"

    def test_protected_endpoints_require_auth(self, secure_client):
        for method, path in [("get", "/documents"), ("get", "/admin/users"), ("get", "/admin/audit")]:
            assert getattr(secure_client, method)(path).status_code == 401
        assert secure_client.post("/chat", json={"question": "hi"}).status_code == 401

    def test_refresh_token_flow(self, secure_client):
        login = secure_client.post(
            "/auth/login", json={"username": "admin", "password": "admin123"}
        ).json()
        resp = secure_client.post("/auth/refresh", json={"refresh_token": login["refresh_token"]})
        assert resp.status_code == 200
        assert resp.json()["access_token"]


# ======================================================================
# RBAC
# ======================================================================
class TestRBAC:
    def test_only_admin_can_manage_users(self, secure_client):
        secure_client.create_user("alice", "alice123", role="user", dept="销售部")
        user_headers = secure_client.login("alice", "alice123")
        assert secure_client.get("/admin/users", headers=user_headers).status_code == 403
        assert secure_client.post(
            "/admin/users",
            json={"username": "x", "password": "xxxxxx", "role": "user"},
            headers=user_headers,
        ).status_code == 403
        assert secure_client.get("/admin/users", headers=secure_client.admin_headers).status_code == 200

    def test_user_cannot_upload(self, secure_client):
        secure_client.create_user("bob", "bob12345", role="user", dept="销售部")
        headers = secure_client.login("bob", "bob12345")
        resp = _upload(secure_client, headers, "a.txt", "内容")
        assert resp.status_code == 403

    def test_kb_manager_can_upload_but_not_manage_users(self, secure_client):
        secure_client.create_user("kbmgr", "kbmgr123", role="kb_manager", dept="技术部")
        headers = secure_client.login("kbmgr", "kbmgr123")
        assert _upload(secure_client, headers, "km.txt", "知识库管理员上传").status_code == 200
        assert secure_client.get("/admin/users", headers=headers).status_code == 403

    def test_invalid_role_rejected(self, secure_client):
        # 直接调接口（不经 fail-fast 辅助函数），验证非法角色被拒
        resp = secure_client.post(
            "/admin/users",
            json={"username": "eve", "password": "eve12345", "role": "superuser"},
            headers=secure_client.admin_headers,
        )
        assert resp.status_code == 400
        assert "角色" in resp.json()["detail"]

    def test_disabled_user_cannot_login(self, secure_client):
        secure_client.create_user("dan", "dan12345", role="user")
        uid = [u for u in secure_client.get("/admin/users", headers=secure_client.admin_headers).json()["users"]
               if u["username"] == "dan"][0]["id"]
        secure_client.patch(f"/admin/users/{uid}", json={"status": "disabled"},
                            headers=secure_client.admin_headers)
        assert secure_client.post("/auth/login", json={"username": "dan", "password": "dan12345"}).status_code == 403


# ======================================================================
# 文档可见性与 ACL（用户视角）
# ======================================================================
class TestDocumentVisibility:
    def _users(self, client):
        client.create_user("alice", "alice123", role="kb_manager", dept="销售部")
        client.create_user("bob", "bob12345", role="user", dept="研发部")
        return client.login("alice", "alice123"), client.login("bob", "bob12345")

    def test_documents_filtered_by_permission(self, secure_client):
        alice_h, bob_h = self._users(secure_client)
        # 销售部的内部文档
        _upload(secure_client, alice_h, "销售制度.txt", "销售部内部制度内容", "internal")
        # 机密文档（仅授权可见）
        _upload(secure_client, alice_h, "薪酬方案.txt", "机密薪酬数据内容", "confidential")

        alice_docs = {d["name"] for d in secure_client.get("/documents", headers=alice_h).json()["documents"]}
        bob_docs = {d["name"] for d in secure_client.get("/documents", headers=bob_h).json()["documents"]}

        assert "销售制度.txt" in alice_docs          # 同部门（销售部）内部文档
        assert "薪酬方案.txt" in alice_docs          # owner 自己的文档
        assert bob_docs == set()                    # 研发部用户什么都看不到

    def test_public_document_visible_to_all(self, secure_client):
        alice_h, bob_h = self._users(secure_client)
        _upload(secure_client, alice_h, "公告.txt", "全员公告内容", "public")
        bob_docs = {d["name"] for d in secure_client.get("/documents", headers=bob_h).json()["documents"]}
        assert "公告.txt" in bob_docs

    def test_acl_grant_and_revoke(self, secure_client):
        alice_h, bob_h = self._users(secure_client)
        _upload(secure_client, alice_h, "机密文档.txt", "机密内容", "confidential")
        assert secure_client.get("/documents", headers=bob_h).json()["documents"] == []

        # 授权给 bob
        resp = secure_client.post(
            "/documents/机密文档.txt/acl",
            json={"subject_type": "user", "subject_id": "bob", "permission": "read"},
            headers=alice_h,
        )
        assert resp.status_code == 200, resp.text
        assert "机密文档.txt" in {d["name"] for d in secure_client.get("/documents", headers=bob_h).json()["documents"]}

        # 撤销后再不可见
        resp = secure_client.delete(
            "/documents/机密文档.txt/acl?subject_type=user&subject_id=bob", headers=alice_h
        )
        assert resp.status_code == 200
        assert secure_client.get("/documents", headers=bob_h).json()["documents"] == []

    def test_acl_grant_to_role(self, secure_client):
        alice_h, bob_h = self._users(secure_client)
        _upload(secure_client, alice_h, "全员机密.txt", "给所有 user 角色授权", "confidential")
        secure_client.post(
            "/documents/全员机密.txt/acl",
            json={"subject_type": "role", "subject_id": "user", "permission": "read"},
            headers=alice_h,
        )
        assert "全员机密.txt" in {d["name"] for d in secure_client.get("/documents", headers=bob_h).json()["documents"]}

    def test_acl_requires_permission(self, secure_client):
        alice_h, bob_h = self._users(secure_client)
        _upload(secure_client, alice_h, "x.txt", "内容", "internal")
        resp = secure_client.post(
            "/documents/x.txt/acl",
            json={"subject_type": "user", "subject_id": "bob"},
            headers=bob_h,
        )
        assert resp.status_code == 403

    def test_delete_requires_ownership_or_role(self, secure_client):
        alice_h, bob_h = self._users(secure_client)
        _upload(secure_client, alice_h, "敏感.txt", "内容", "confidential")
        secure_client.post(
            "/documents/敏感.txt/acl",
            json={"subject_type": "user", "subject_id": "bob", "permission": "read"},
            headers=alice_h,
        )
        # bob 有读权限但没有删除权限
        assert secure_client.delete("/documents/敏感.txt", headers=bob_h).status_code == 403
        assert secure_client.delete("/documents/敏感.txt", headers=secure_client.admin_headers).status_code == 200


# ======================================================================
# 召回期权限过滤（核心安全目标）
# ======================================================================
class TestRetrievalPermissionFiltering:
    def test_retrieval_layer_filters_by_allowed_ids(self, processor, rag):
        """检索层必须把无权文档从候选集中剔除（不是只在展示层过滤）。"""
        processor.add_document("销售手册.md", "销售提成制度：销售额的 5% 作为提成。".encode("utf-8"))
        processor.add_document("研发机密.md", "研发机密：核心算法密钥为 ABC-123。".encode("utf-8"))
        rag.build_indices()

        docs = {d["name"]: d["id"] for d in processor.documents}
        allowed = {docs["销售手册.md"]}

        # 混合检索与完整管线都只能返回可见文档
        for results in (
            rag.vector_search("密钥 算法", top_k=5, allowed_doc_ids=allowed),
            rag.bm25_search("密钥 算法", top_k=5, allowed_doc_ids=allowed),
            rag.retrieve("核心算法密钥是什么", top_n=5, allowed_doc_ids=allowed),
        ):
            assert all(r["metadata"]["source"] == "销售手册.md" for r in results), results
            assert not any("ABC-123" in r["content"] for r in results)

        # 空可见集合 → 直接返回空（短路，避免无谓的向量化调用）
        assert rag.vector_search("密钥", top_k=5, allowed_doc_ids=set()) == []
        assert rag.bm25_search("密钥", top_k=5, allowed_doc_ids=set()) == []

        # 不限制（管理员）时才可能召回机密文档
        admin_results = rag.bm25_search("密钥 算法", top_k=5, allowed_doc_ids=None)
        assert any("ABC-123" in r["content"] for r in admin_results)

    def test_chat_path_applies_user_permission(self, secure_client, monkeypatch):
        """HTTP 链路：路由设置权限上下文 → 工具读取 → 检索过滤。"""
        secure_client.create_user("alice", "alice123", role="kb_manager", dept="销售部")
        secure_client.create_user("bob", "bob12345", role="user", dept="研发部")
        alice_h = secure_client.login("alice", "alice123")
        bob_h = secure_client.login("bob", "bob12345")

        _upload(secure_client, alice_h, "公开资料.txt", "公开资料：产品价格表见附件。", "public")
        _upload(secure_client, alice_h, "机密资料.txt", "机密资料：核心算法密钥 ABC-123。", "confidential")

        state = secure_client.state_mod
        state.rag.build_indices()

        # 用「真实检索 + 假 LLM」的 Agent 模拟真实工具调用链
        from app.context import current_allowed_docs

        class RetrievalAgent:
            def __init__(self, rag):
                self.rag = rag
                self.seen: list[str] = []

            def invoke(self, messages, config):
                allowed = current_allowed_docs.get()
                docs = self.rag.retrieve("核心算法密钥", allowed_doc_ids=allowed)
                self.seen = [d["content"] for d in docs]
                import types

                return {"messages": [types.SimpleNamespace(content=" | ".join(self.seen) or "无结果")]}

        agent = RetrievalAgent(state.rag)
        monkeypatch.setattr(state, "agent", agent)

        resp = secure_client.post("/chat", json={"question": "核心算法密钥是什么"}, headers=bob_h)
        assert resp.status_code == 200
        answer = resp.json()["answer"]
        assert "ABC-123" not in answer          # 研发部用户看不到机密文档
        assert "公开资料" in answer or answer == "无结果"

        resp = secure_client.post("/chat", json={"question": "核心算法密钥是什么"},
                                  headers=secure_client.admin_headers)
        assert resp.status_code == 200
        assert "ABC-123" in resp.json()["answer"]   # 管理员可见


# ======================================================================
# 审计
# ======================================================================
class TestAudit:
    def test_actions_are_audited(self, secure_client):
        secure_client.create_user("alice", "alice123", role="kb_manager", dept="销售部")
        alice_h = secure_client.login("alice", "alice123")
        _upload(secure_client, alice_h, "审计样本.txt", "内容", "internal")

        logs = secure_client.get("/admin/audit?limit=50", headers=secure_client.admin_headers).json()["logs"]
        actions = {entry["action"] for entry in logs}
        assert {"login", "user_create", "upload"} <= actions
        assert any(entry["username"] == "alice" and entry["action"] == "upload" for entry in logs)

    def test_failed_login_is_audited(self, secure_client):
        secure_client.post("/auth/login", json={"username": "admin", "password": "wrong"})
        logs = secure_client.get("/admin/audit?action=login", headers=secure_client.admin_headers).json()["logs"]
        assert any(entry["success"] == 0 for entry in logs)

    def test_auditor_can_read_but_not_write(self, secure_client):
        secure_client.create_user("aud", "audit123", role="auditor", dept="")
        aud_h = secure_client.login("aud", "audit123")
        assert secure_client.get("/admin/audit", headers=aud_h).status_code == 200
        assert _upload(secure_client, aud_h, "a.txt", "内容").status_code == 403

    def test_config_endpoint_requires_admin(self, secure_client):
        secure_client.create_user("aud2", "audit123", role="auditor")
        aud_h = secure_client.login("aud2", "audit123")
        assert secure_client.get("/admin/config", headers=aud_h).status_code == 403
        resp = secure_client.get("/admin/config", headers=secure_client.admin_headers)
        assert resp.status_code == 200
        assert resp.json()["config"]["api_key_configured"] is True


# ======================================================================
# 历史文档归属（fail-closed）
# ======================================================================
class TestLegacyDocuments:
    def test_orphan_documents_assigned_to_admin(self, secure_client):
        """升级前无归属的历史文档划归管理员，不会自动对全员可见。"""
        state = secure_client.state_mod
        state.processor.add_document("历史文档.txt", "历史内容".encode("utf-8"))
        state.store.create_user  # noqa: B018  (确保方法存在)
        # 模拟老数据：owner_id=0
        with state.store._connect() as conn:
            conn.execute("UPDATE documents SET owner_id = 0")
            conn.commit()

        claimed = state.store.claim_orphan_documents(1)
        assert claimed == 1
        doc = state.store.get_document("历史文档.txt")
        assert doc["owner_id"] == 1


# ======================================================================
# 会话管理（持久化 + 隔离）
# ======================================================================
class RecordingAgent:
    """记录实际收到的上下文消息，用于验证多轮记忆是否注入。"""

    def __init__(self, answer: str = "收到"):
        self.answer = answer
        self.received: list = []

    def invoke(self, messages, config=None):
        import types

        self.received = list(messages["messages"])
        return {"messages": [types.SimpleNamespace(content=self.answer)]}

    async def astream(self, messages, config=None, stream_mode="messages"):
        from langchain_core.messages import AIMessageChunk

        self.received = list(messages["messages"])
        for ch in self.answer:
            yield (AIMessageChunk(content=ch), {"langgraph_node": "model"})

    async def astream_events(self, messages, config=None, version="v2"):
        import types

        self.received = list(messages["messages"])
        for ch in self.answer:
            yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content=ch)}}
        yield {"event": "on_chain_end", "data": {}}


class TestConversations:
    def _setup_user(self, secure_client):
        secure_client.create_user("alice", "alice123", role="kb_manager", dept="销售部")
        return secure_client.login("alice", "alice123")

    def test_chat_creates_conversation_and_messages(self, secure_client, monkeypatch):
        alice_h = self._setup_user(secure_client)
        secure_client.post(
            "/documents/upload",
            files={"file": ("制度.txt", "年假 5 天".encode("utf-8"), "text/plain")},
            headers=alice_h,
        )
        monkeypatch.setattr(secure_client.state_mod, "agent", RecordingAgent("年假 5 天"))

        resp = secure_client.post(
            "/chat", json={"question": "年假几天？", "conversation_id": "c1"}, headers=alice_h
        )
        assert resp.status_code == 200

        convs = secure_client.get("/conversations", headers=alice_h).json()["conversations"]
        assert len(convs) == 1
        assert convs[0]["title"].startswith("年假几天")
        assert convs[0]["message_count"] == 2

        detail = secure_client.get(f"/conversations/{convs[0]['id']}", headers=alice_h).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert detail["messages"][1]["content"] == "年假 5 天"

    def test_second_turn_carries_working_memory(self, secure_client, monkeypatch):
        alice_h = self._setup_user(secure_client)
        secure_client.post(
            "/documents/upload",
            files={"file": ("制度.txt", "年假 5 天".encode("utf-8"), "text/plain")},
            headers=alice_h,
        )
        agent = RecordingAgent("好的")
        monkeypatch.setattr(secure_client.state_mod, "agent", agent)

        secure_client.post("/chat", json={"question": "年假几天？", "conversation_id": "c1"}, headers=alice_h)
        secure_client.post("/chat", json={"question": "那产假呢？", "conversation_id": "c1"}, headers=alice_h)

        # 第二次请求应携带第一轮问答 + 本次提问（3 条，消息形如 (role, content) 元组）
        contents = [m[1] for m in agent.received]
        assert "年假几天？" in contents
        assert "好的" in contents
        assert contents[-1] == "那产假呢？"
        assert len(contents) == 3

    def test_stream_saves_assistant_message(self, secure_client, monkeypatch):
        alice_h = self._setup_user(secure_client)
        secure_client.post(
            "/documents/upload",
            files={"file": ("制度.txt", "内容".encode("utf-8"), "text/plain")},
            headers=alice_h,
        )
        monkeypatch.setattr(secure_client.state_mod, "agent", RecordingAgent("流式回答"))
        with secure_client.stream(
            "POST", "/chat/stream",
            json={"question": "问题", "conversation_id": "c2"}, headers=alice_h,
        ) as resp:
            assert resp.status_code == 200
            "".join(resp.iter_text())

        convs = secure_client.get("/conversations", headers=alice_h).json()["conversations"]
        detail = secure_client.get(f"/conversations/{convs[0]['id']}", headers=alice_h).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert "流式回答" in detail["messages"][1]["content"]

    def test_conversation_isolated_between_users(self, secure_client, monkeypatch):
        alice_h = self._setup_user(secure_client)
        alice_h2 = secure_client.login("alice", "alice123")
        secure_client.post(
            "/documents/upload",
            files={"file": ("制度.txt", "内容".encode("utf-8"), "text/plain")},
            headers=alice_h,
        )
        monkeypatch.setattr(secure_client.state_mod, "agent", RecordingAgent("回答"))
        secure_client.post("/chat", json={"question": "我的问题", "conversation_id": "c1"}, headers=alice_h)
        conv_id = secure_client.get("/conversations", headers=alice_h).json()["conversations"][0]["id"]

        secure_client.create_user("bob", "bob12345", role="user", dept="研发部")
        bob_h = secure_client.login("bob", "bob12345")
        # bob 既看不到该会话，也不能访问
        assert secure_client.get("/conversations", headers=bob_h).json()["conversations"] == []
        assert secure_client.get(f"/conversations/{conv_id}", headers=bob_h).status_code == 404

    def test_rename_and_delete(self, secure_client, monkeypatch):
        alice_h = self._setup_user(secure_client)
        secure_client.post(
            "/documents/upload",
            files={"file": ("制度.txt", "内容".encode("utf-8"), "text/plain")},
            headers=alice_h,
        )
        monkeypatch.setattr(secure_client.state_mod, "agent", RecordingAgent("回答"))
        secure_client.post("/chat", json={"question": "问题", "conversation_id": "c1"}, headers=alice_h)
        conv_id = secure_client.get("/conversations", headers=alice_h).json()["conversations"][0]["id"]

        renamed = secure_client.patch(
            f"/conversations/{conv_id}", json={"title": "我的新标题"}, headers=alice_h
        )
        assert renamed.status_code == 200 and renamed.json()["title"] == "我的新标题"

        assert secure_client.delete(f"/conversations/{conv_id}", headers=alice_h).status_code == 200
        assert secure_client.get(f"/conversations/{conv_id}", headers=alice_h).status_code == 404
