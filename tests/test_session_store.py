"""会话持久化存储测试：SQLite CRUD、摘要、长期事实。

测试覆盖：
1. sessions CRUD：upsert 新建/更新、list 过滤、get 含消息、删除级联
2. 中文/emoji round-trip
3. summaries：FK 兜底（会话行未建时先插占位）、短会话不压缩由节点层管
4. facts：追加、按时间倒序
5. REST 端点：/api/sessions 系列契约（404、PUT upsert、DELETE）

运行：
    pytest tests/test_session_store.py -x -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """每个测试一个独立临时库（避免共享 .devpilot/store.db）。"""
    monkeypatch.setattr("devpilot.web.store._conn", None)
    monkeypatch.setattr("devpilot.web.store._DB_PATH", tmp_path / "store.db")
    from devpilot.web import store as s
    yield s
    if s._conn is not None:
        s._conn.close()
        s._conn = None


def _msg(role="user", content="你好"):
    return {"role": role, "content": content, "reasoning": "",
            "steps": [], "files": [], "done": True, "ts": 1756600000}


# ----------------------------------------------------------------------
# sessions CRUD
# ----------------------------------------------------------------------
class TestSessionsCRUD:
    def test_upsert_and_get(self, store):
        msgs = [_msg(), _msg("assistant", "回复")]
        store.upsert_session("s1", "general", "标题", msgs)
        sess = store.get_session("s1")
        assert sess is not None
        assert sess["agent_id"] == "general"
        assert sess["title"] == "标题"
        assert len(sess["messages"]) == 2
        assert sess["messages"][1]["content"] == "回复"

    def test_upsert_overwrites(self, store):
        store.upsert_session("s1", "general", "v1", [_msg()])
        store.upsert_session("s1", "general", "v2", [_msg(), _msg()])
        sess = store.get_session("s1")
        assert sess["title"] == "v2"
        assert len(sess["messages"]) == 2  # 旧消息被整段替换

    def test_list_filters_by_agent(self, store):
        store.upsert_session("a", "general", "t", [_msg()])
        store.upsert_session("b", "yuwen_skill", "t", [_msg()])
        assert len(store.list_sessions()) == 2
        only = store.list_sessions("yuwen_skill")
        assert len(only) == 1 and only[0]["id"] == "b"

    def test_list_orders_by_updated_desc(self, store):
        import time as _t
        store.upsert_session("old", "general", "t", [_msg()])
        _t.sleep(0.05)
        store.upsert_session("new", "general", "t", [_msg()])
        ids = [s["id"] for s in store.list_sessions()]
        assert ids[0] == "new"

    def test_delete_cascades_messages(self, store):
        store.upsert_session("s1", "general", "t", [_msg()])
        assert store.delete_session("s1") is True
        assert store.get_session("s1") is None
        assert store.delete_session("s1") is False  # 再删 → 没删到

    def test_get_missing_returns_none(self, store):
        assert store.get_session("nope") is None

    def test_chinese_emoji_roundtrip(self, store):
        store.upsert_session("s1", "general", "静夜思🌙课件", [_msg(content="《静夜思》pptx")])
        sess = store.get_session("s1")
        assert sess["title"] == "静夜思🌙课件"
        assert sess["messages"][0]["content"] == "《静夜思》pptx"


# ----------------------------------------------------------------------
# summaries（中期记忆）
# ----------------------------------------------------------------------
class TestSummaries:
    def test_save_before_session_row(self, store):
        """FK 兜底：会话行不存在时应先插占位再存摘要。"""
        store.save_summary("ghost", 14, "早期对话摘要")
        s = store.latest_summary("ghost")
        assert s is not None
        assert s["content"] == "早期对话摘要"
        assert s["seq"] == 14

    def test_latest_overwrites_same_seq(self, store):
        store.upsert_session("s1", "general", "t", [_msg()])
        store.save_summary("s1", 14, "第一版")
        store.save_summary("s1", 14, "第二版")
        assert store.latest_summary("s1")["content"] == "第二版"

    def test_latest_picks_max_seq(self, store):
        store.upsert_session("s1", "general", "t", [_msg()])
        store.save_summary("s1", 10, "早段")
        store.save_summary("s1", 20, "晚段")
        assert store.latest_summary("s1")["content"] == "晚段"

    def test_delete_session_removes_summaries(self, store):
        store.upsert_session("s1", "general", "t", [_msg()])
        store.save_summary("s1", 10, "x")
        store.delete_session("s1")
        assert store.latest_summary("s1") is None


# ----------------------------------------------------------------------
# facts（长期记忆）
# ----------------------------------------------------------------------
class TestFacts:
    def test_add_and_recent(self, store):
        store.add_facts(["事实A", "事实B"], source="s1")
        facts = store.recent_facts(10)
        assert [f["fact"] for f in facts] == ["事实B", "事实A"]  # 倒序

    def test_recent_limit(self, store):
        store.add_facts([f"事实{i}" for i in range(10)])
        assert len(store.recent_facts(3)) == 3

    def test_empty_add_noop(self, store):
        store.add_facts([])
        assert store.recent_facts(5) == []


# ----------------------------------------------------------------------
# REST 端点
# ----------------------------------------------------------------------
class TestSessionAPI:
    @pytest.fixture()
    def client(self, store, monkeypatch):
        from fastapi.testclient import TestClient
        from devpilot.web import api as api_mod
        monkeypatch.setattr(api_mod, "OUTPUTS_DIR", _PROJECT_ROOT / "outputs")
        return TestClient(api_mod.app)

    def test_put_get_delete_cycle(self, client):
        r = client.put("/api/sessions/s_rest", json={
            "agent": "general", "title": "REST会话",
            "messages": [_msg(), _msg("assistant", "ok")]})
        assert r.status_code == 200 and r.json()["ok"] is True

        r = client.get("/api/sessions/s_rest")
        assert r.status_code == 200
        body = r.json()["session"]
        assert body["title"] == "REST会话"
        assert len(body["messages"]) == 2

        r = client.delete("/api/sessions/s_rest")
        assert r.json()["ok"] is True
        assert client.get("/api/sessions/s_rest").status_code == 404

    def test_list_endpoint(self, client):
        client.put("/api/sessions/s_rest2", json={
            "agent": "yuwen_skill", "title": "t", "messages": [_msg()]})
        r = client.get("/api/sessions")
        ids = [s["id"] for s in r.json()["sessions"]]
        assert "s_rest2" in ids

    def test_get_404_for_missing(self, client):
        assert client.get("/api/sessions/never").status_code == 404

    def test_facts_endpoint(self, client, store):
        store.add_facts(["REST事实"])
        r = client.get("/api/memory/facts")
        assert r.status_code == 200
        assert "REST事实" in [f["fact"] for f in r.json()["facts"]]
