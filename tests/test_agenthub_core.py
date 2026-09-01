"""agenthub 核心测试：注册中心、REST 端点、SSE 路由、文件服务。

测试覆盖：
1. 注册中心：目录扫描发现、manifest 解析、list_agents/get_agent
2. 优雅降级：缺 manifest.py 的目录跳过、import 失败的包跳过
3. REST /api/agents：返回格式正确、general 存在
4. REST /api/chat：agent 字段路由、未知 agent 404 帧
5. 文件服务 /files：正常下载、防目录穿越
6. SSE 通用对话帧序列：agent_meta/step/token|content/done

运行：
    pytest tests/test_agenthub_core.py -x -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 加入项目 src 路径
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))


# ======================================================================
# 1. 注册中心测试
# ======================================================================

class TestRegistry:
    """agenthub 注册中心目录扫描与发现。"""

    def test_list_agents_contains_general(self):
        """list_agents() 必须包含 general 智能体。"""
        from aidraft.agenthub import list_agents, reset_cache

        reset_cache()
        agents = list_agents()
        ids = [a.agent_id for a in agents]
        assert "general" in ids, f"expected 'general' in {ids}"

    def test_general_manifest_fields(self):
        """general 智能体的 manifest 字段必须完整。"""
        from aidraft.agenthub import get_agent, reset_cache

        reset_cache()
        agent = get_agent("general")
        assert agent is not None
        assert agent.display_name == "通用对话"
        assert agent.description
        assert agent.identity_color == "#3D6CC4"
        assert agent.placeholder
        # graph_fn 必须可调用
        assert agent.graph_fn is not None

    def test_get_agent_unknown_returns_none(self):
        """get_agent 对未知 id 返回 None。"""
        from aidraft.agenthub import get_agent, reset_cache

        reset_cache()
        assert get_agent("nonexistent_agent") is None

    def test_agent_to_dict_format(self):
        """to_dict() 返回的字段名与契约一致（id/display_name/description/identity_color/placeholder）。"""
        from aidraft.agenthub import get_agent, reset_cache

        reset_cache()
        agent = get_agent("general")
        assert agent is not None
        d = agent.to_dict()
        assert d["id"] == "general"
        assert d["display_name"] == "通用对话"
        assert "description" in d
        assert "identity_color" in d
        assert "placeholder" in d

    def test_discover_skips_underscore_dirs(self):
        """_discover 跳过以 _ 开头的目录。"""
        from aidraft.agenthub import _discover, reset_cache

        reset_cache()
        agents = _discover()
        # 确保没有 _ 开头的 key
        for key in agents:
            assert not key.startswith("_"), f"underscore dir leaked: {key}"

    def test_missing_manifest_skips_gracefully(self):
        """缺少 manifest.py 的目录被跳过，不抛异常。"""
        from aidraft.agenthub import _discover, reset_cache

        reset_cache()
        # 创建一个临时目录模拟缺 manifest
        hub = Path(__file__).resolve().parents[1] / "src" / "aidraft" / "agenthub"
        tmp_dir = hub / "_tmp_no_manifest"
        try:
            tmp_dir.mkdir(exist_ok=True)
            # 创建 __init__.py 但无 manifest.py
            (tmp_dir / "__init__.py").write_text("# test")
            reset_cache()
            agents = _discover()
            assert "_tmp_no_manifest" not in [a.agent_id for a in agents.values()]
        finally:
            if tmp_dir.exists():
                for f in tmp_dir.iterdir():
                    f.unlink()
                tmp_dir.rmdir()

    def test_general_build_graph_invocation(self):
        """general.graph.build_graph 返回可调用的图对象（带 compile）。"""
        from aidraft.agenthub.general.graph import build_graph

        # 构造 mock 依赖
        mock_gw = MagicMock()
        mock_registry = MagicMock()
        mock_audit = MagicMock()

        # 通用对话图需要 gateway.stream_chat 返回空迭代器（图编译不依赖运行时）
        mock_gw.stream_chat.return_value = _async_iter([])

        graph = build_graph(
            gateway=mock_gw,
            registry=mock_registry,
            audit=mock_audit,
            emitter=lambda f: None,
        )
        # 编译后的图应有 astream 方法
        assert hasattr(graph, "astream"), "build_graph must return a compiled graph"


# ======================================================================
# 2. REST 端点测试（fastapi TestClient，mock 网关）
# ======================================================================

@pytest.fixture
def client():
    """返回 mock 网关后的 TestClient，避免真实 LLM 调用。

    注意：SSE 端点与 build_chat_graph_runtime 都从 aidraft.gateway 导入
    build_default_gateway，因此必须 patch 源模块（aidraft.gateway）而非
    web.api 的名字引用。
    """
    from aidraft.web.api import app
    from fastapi.testclient import TestClient

    # 打 patch 阻止 build_default_gateway 真实调用（覆盖所有 import 站点）
    with patch("aidraft.gateway.build_default_gateway") as mock_build:
        mock_gw = MagicMock()
        # chat 返回空响应
        mock_gw.chat.return_value = MagicMock(
            content="mock reply",
            provider="test",
            model="test-model",
            latency_ms=100,
            prompt_tokens=10,
            completion_tokens=10,
        )
        # stream_chat 返回空 async 迭代器
        async def _empty_stream(*args, **kwargs):
            for _ in ():
                yield
        mock_gw.stream_chat.side_effect = _empty_stream
        mock_build.return_value = mock_gw

        with TestClient(app) as c:
            yield c


class TestAgentsEndpoint:
    """GET /api/agents。"""

    def test_agents_list_format(self, client):
        """返回 {"agents": [...]}，每个含 id/display_name/description/identity_color/placeholder。"""
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert len(data["agents"]) >= 1
        general = [a for a in data["agents"] if a["id"] == "general"]
        assert len(general) == 1
        g = general[0]
        assert g["display_name"] == "通用对话"
        assert g["identity_color"] == "#3D6CC4"
        assert "description" in g
        assert "placeholder" in g


class TestChatSSEEndpoint:
    """POST /api/chat SSE 端点。"""

    def test_agent_default_is_general(self, client):
        """缺省 agent 走 general 并返回 agent_meta 帧。"""
        resp = client.post("/api/chat", json={"prompt": "你好"})
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        body = resp.text
        assert "agent_meta" in body
        assert "general" in body
        assert "done" in body

    def test_agent_general_returns_agent_meta(self, client):
        """显式指定 agent=general 返回 agent_meta 帧，agent_id 应为 general。"""
        resp = client.post("/api/chat", json={
            "prompt": "你好",
            "agent": "general",
        })
        assert resp.status_code == 200
        # 解析 SSE 帧确认 agent_meta 内容
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                try:
                    frame = json.loads(line[6:])
                except (json.JSONDecodeError, IndexError):
                    continue
                if frame.get("type") == "agent_meta":
                    assert frame["agent_id"] == "general"
                    assert frame["display_name"] == "通用对话"
                    return
        pytest.fail("no agent_meta frame found")

    def test_unknown_agent_returns_404_frame(self, client):
        """未知 agent_id 返回 error 帧含 unknown agent 信息。"""
        resp = client.post("/api/chat", json={
            "prompt": "你好",
            "agent": "nonexistent_agent_xyz",
        })
        assert resp.status_code == 200
        body = resp.text
        assert "data:" in body
        # 应包含 error 帧
        assert "unknown agent" in body or "error" in body

    def test_empty_prompt_returns_error(self, client):
        """空 prompt 返回 error 帧。"""
        resp = client.post("/api/chat", json={"prompt": ""})
        assert resp.status_code == 200
        assert "empty prompt" in resp.text

    def test_history_passed_through(self, client):
        """带 history 的请求不报错。"""
        resp = client.post("/api/chat", json={
            "prompt": "继续",
            "history": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
            ],
        })
        assert resp.status_code == 200
        assert "done" in resp.text

    def test_general_emits_step_frames(self, client):
        """通用对话图产出 step 帧（时间线用）。"""
        resp = client.post("/api/chat", json={"prompt": "hello"})
        assert resp.status_code == 200
        # 解析 SSE 帧，确认至少有一条 step 帧
        found_step = False
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                try:
                    frame = json.loads(line[6:])
                except (json.JSONDecodeError, IndexError):
                    continue
                if frame.get("type") == "step":
                    found_step = True
                    break
        assert found_step, "no step frame found in SSE stream"

    def test_agent_meta_fields(self, client):
        """agent_meta 帧包含所有约定字段。"""
        resp = client.post("/api/chat", json={"prompt": "hi"})
        lines = resp.text.strip().split("\n")
        for line in lines:
            if line.startswith("data: "):
                try:
                    frame = json.loads(line[6:])
                except (json.JSONDecodeError, IndexError):
                    continue
                if frame.get("type") == "agent_meta":
                    assert "agent_id" in frame
                    assert "display_name" in frame
                    assert "description" in frame
                    assert "identity_color" in frame
                    assert "placeholder" in frame
                    return
        pytest.fail("no agent_meta frame found")

    def test_done_frame_has_nodes_visited(self, client):
        """done 帧的 meta 含 nodes_visited 字段。"""
        resp = client.post("/api/chat", json={"prompt": "hi"})
        lines = resp.text.strip().split("\n")
        for line in lines:
            if line.startswith("data: "):
                try:
                    frame = json.loads(line[6:])
                except (json.JSONDecodeError, IndexError):
                    continue
                if frame.get("type") == "done":
                    meta = frame.get("meta", {})
                    assert "nodes_visited" in meta
                    assert isinstance(meta["nodes_visited"], list)
                    return
        pytest.fail("no done frame found")


class TestFilesEndpoint:
    """GET /files/{agent_id}/{session}/{filename} 文件服务。"""

    @pytest.fixture(autouse=True)
    def setup_outputs(self):
        """创建临时 outputs 目录用于测试文件服务。"""
        from aidraft.web.api import OUTPUTS_DIR

        self._test_dir = OUTPUTS_DIR / "test_agent" / "test_session"
        self._test_dir.mkdir(parents=True, exist_ok=True)
        self._test_file = self._test_dir / "hello.txt"
        self._test_file.write_text("hello world")
        yield
        # 清理
        if self._test_dir.exists():
            import shutil
            shutil.rmtree(self._test_dir, ignore_errors=True)

    def test_file_download_ok(self, client):
        """正常文件可下载。"""
        resp = client.get("/files/test_agent/test_session/hello.txt")
        assert resp.status_code == 200
        assert resp.text == "hello world"

    def test_file_not_found(self, client):
        """不存在的文件返回 404。"""
        resp = client.get("/files/test_agent/test_session/nonexistent.txt")
        assert resp.status_code == 404

    def test_path_traversal_denied(self, client):
        """路径穿越被拒绝（../ 超出根目录）。"""
        # 使用编码后的路径防止 httpx 客户端规范化
        resp = client.get("/files/test_agent/test_session/..%2F..%2F..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code == 400

    def test_path_traversal_encoded_denied(self, client):
        """URL 编码的路径穿越也被拒绝。"""
        resp = client.get("/files/test_agent/test_session/..%2F..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code == 400


# ======================================================================
# 辅助：异步空迭代器
# ======================================================================
async def _async_iter(items):
    for item in items:
        yield item