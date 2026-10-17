"""pytest 公共夹具：离线化（不打外部 API）+ 隔离数据目录。

所有测试通过 monkeypatch 替换 embedding / rerank / agent，
保证在没有网络和 API Key 的环境下也能完整跑通。
"""
import hashlib
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fake_embed(text: str) -> list[float]:
    """确定性伪向量（8 维），用于离线测试。"""
    h = hashlib.md5(text.encode("utf-8")).digest()
    return [((b % 20) - 10) / 10.0 for b in h[:8]]


class FakeAgent:
    """不联网的假 Agent，固定输出 + 流式逐字。"""

    def invoke(self, messages, config):
        return {"messages": [types.SimpleNamespace(content="这是一条离线测试回答。")]}

    async def astream(self, messages, config=None, stream_mode="messages"):
        """模拟 LangGraph 1.x 的 token 流：yield (AIMessageChunk, metadata)。"""
        from langchain_core.messages import AIMessageChunk

        for ch in "测试流式":
            yield (AIMessageChunk(content=ch), {"langgraph_node": "model"})

    async def astream_events(self, messages, config=None, version="v2"):
        for ch in "测试流式":
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": types.SimpleNamespace(content=ch)},
            }
        yield {"event": "on_chain_end", "data": {}}


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch, tmp_path):
    """全局：注入假 Key、关闭外部 Rerank、打桩向量化、隔离数据目录，保证测试离线且无副作用。"""
    from app import config
    import app.llm as llm

    monkeypatch.setattr(config.settings, "API_KEY", "sk-test-fake")
    monkeypatch.setattr(config.settings, "RERANK_ENABLED", False)
    monkeypatch.setattr(config.settings, "RAG_FUSION", False)
    monkeypatch.setattr(llm, "embed_text", fake_embed)
    monkeypatch.setattr(llm, "embed_texts", lambda texts: [fake_embed(t) for t in texts])
    # 隔离解析缓存与原件目录：避免测试写脏真实 data/，并保证缓存相关断言可复现
    monkeypatch.setattr(config.settings, "PARSE_CACHE_DIR", tmp_path / "parsed")
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", tmp_path / "uploads")
    (tmp_path / "parsed").mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    return config.settings


@pytest.fixture
def doc_store(tmp_path):
    from app.db import DocumentStore

    return DocumentStore(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def processor(doc_store, monkeypatch):
    """隔离的文档处理器（伪向量化）。"""
    from app.document_processor import DocumentProcessor

    p = DocumentProcessor(doc_store)
    monkeypatch.setattr(p, "embed_fn", fake_embed)
    return p


@pytest.fixture
def rag(processor):
    from app.retrieval import AdvancedRAG

    return AdvancedRAG(processor)


def _build_client(tmp_path, monkeypatch, *, auth_enabled: bool):
    """构建隔离的测试客户端（tmp 库 + 伪向量 + 隔离任务执行器）。"""
    import app.routes as routes
    from app import config
    from app import state as state_mod
    from app.db import DocumentStore
    from app.document_processor import DocumentProcessor
    from app.main import create_app
    from app.retrieval import AdvancedRAG
    from app.tasks import TaskRunner

    monkeypatch.setattr(config.settings, "AUTH_ENABLED", auth_enabled)

    store = DocumentStore(db_path=str(tmp_path / ("auth.db" if auth_enabled else "api.db")))
    processor = DocumentProcessor(store)
    monkeypatch.setattr(processor, "embed_fn", fake_embed)
    rag_inst = AdvancedRAG(processor)

    for attr, val in [("store", store), ("processor", processor), ("rag", rag_inst), ("agent", None)]:
        setattr(state_mod.state, attr, val)
    # 关键：任务执行器必须绑定到隔离后的 processor/store，否则异步任务会写到真实库
    state_mod.state.runner = TaskRunner(processor, on_complete=None)
    monkeypatch.setattr(routes, "state", state_mod.state)

    if auth_enabled:
        state_mod.state.bootstrap_auth()

    app = create_app(with_persistence=False)
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.state_mod = state_mod.state  # type: ignore[attr-defined]
    return client


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """免认证客户端（AUTH_ENABLED=false）：用于文档/问答等既有功能测试。"""
    return _build_client(tmp_path, monkeypatch, auth_enabled=False)


@pytest.fixture
def secure_client(tmp_path, monkeypatch):
    """启用认证的客户端：附登录辅助方法。"""
    client = _build_client(tmp_path, monkeypatch, auth_enabled=True)

    def login(username: str, password: str) -> dict:
        resp = client.post("/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def create_user(username, password, role="user", dept="", headers=None):
        resp = client.post(
            "/admin/users",
            json={"username": username, "password": password, "role": role, "dept": dept},
            headers=headers or client.admin_headers,
        )
        assert resp.status_code == 200, f"建号失败 {username}: {resp.status_code} {resp.text}"
        return resp

    client.login = login  # type: ignore[attr-defined]
    client.create_user = create_user  # type: ignore[attr-defined]
    client.admin_headers = login("admin", "admin123")  # type: ignore[attr-defined]
    return client