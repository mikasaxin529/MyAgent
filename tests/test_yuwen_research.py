"""M2 大纲前联网搜索：research 节点测试。

覆盖：
1. 降级链：无 TAVILY_API_KEY 跳过、搜索返回降级串不进 payload、
   单路异常不影响另一路
2. 正常路径：两路搜索 → content/sources 落盘 state.json、TTL 内复用
3. gen_outline 消费：research 结果拼进 user prompt（联网参考资料段）
4. 图接线：_route_after_params 首轮分支 → research

运行：
    PYTHONIOENCODING=utf-8 pytest tests/test_yuwen_research.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))

PARAMS = {"title": "静夜思", "grade": 1, "lesson_type": "古诗词",
          "textbook": "部编版一年级"}


@pytest.fixture
def outputs_tmp(tmp_path, monkeypatch):
    """把 state._OUTPUTS_DIR patch 到 tmp_path。"""
    from aidraft.agenthub.yuwen import state as st
    monkeypatch.setattr(st, "_OUTPUTS_DIR", tmp_path)
    return tmp_path


def _fake_skill_cls(monkeypatch, search_returns):
    """构造假的 WebSearchSkill 类：search 按调用序返回 search_returns。"""
    fake = MagicMock()
    fake.available = True
    fake.search = MagicMock(side_effect=search_returns)
    cls = MagicMock(return_value=fake)
    mod = MagicMock(WebSearchSkill=cls)
    monkeypatch.setitem(sys.modules, "aidraft.skills.websearch_skill", mod)
    return fake


class TestDegradation:
    def test_no_key_skips(self, outputs_tmp, monkeypatch):
        """skill.available=False → 跳过，payload 空，不写盘。"""
        from aidraft.agenthub.yuwen.nodes.research import _make_research_node
        fake = MagicMock()
        fake.available = False
        fake.search = MagicMock()
        cls = MagicMock(return_value=fake)
        mod = MagicMock(WebSearchSkill=cls)
        monkeypatch.setitem(sys.modules, "aidraft.skills.websearch_skill", mod)
        node = _make_research_node(None)
        result = asyncio.run(node({"yuwen_params": PARAMS}))
        assert result["yuwen_research"] == {}
        assert fake.search.call_count == 0

    def test_degraded_text_not_in_payload(self, outputs_tmp, monkeypatch):
        """search 返回降级提示串（[websearch] 开头）→ 视为无结果。"""
        from aidraft.agenthub.yuwen.nodes.research import _make_research_node
        _fake_skill_cls(monkeypatch, [
            "[websearch] Tavily 未配置（缺少 TAVILY_API_KEY），无法联网搜索。",
            "[websearch] 搜索失败：TimeoutError()",
        ])
        node = _make_research_node(None)
        result = asyncio.run(node({"yuwen_params": PARAMS}))
        assert result["yuwen_research"] == {}

    def test_one_query_fails_other_survives(self, outputs_tmp, monkeypatch):
        """单路抛异常 → 另一路结果仍进 payload。"""
        from aidraft.agenthub.yuwen.nodes.research import _make_research_node
        _fake_skill_cls(monkeypatch, [
            RuntimeError("network down"),
            "搜索关键词：静夜思 课文原文（共 2 条）\n"
            "1. 静夜思原文\n   https://example.com/poem\n   床前明月光…",
        ])
        node = _make_research_node(None)
        result = asyncio.run(node({"yuwen_params": PARAMS}))
        payload = result["yuwen_research"]
        assert "静夜思原文" in payload["content"]
        assert payload["sources"] == [
            {"query": "静夜思 课文原文", "url": "https://example.com/poem"}]

    def test_no_title_skips(self, outputs_tmp):
        """params 无课文名（防御路径）→ 跳过。"""
        from aidraft.agenthub.yuwen.nodes.research import _make_research_node
        node = _make_research_node(None)
        result = asyncio.run(node({"yuwen_params": {}}))
        assert result["yuwen_research"] == {}


class TestNormalPath:
    def _run(self, monkeypatch):
        from aidraft.agenthub.yuwen.nodes.research import _make_research_node
        _fake_skill_cls(monkeypatch, [
            "搜索关键词：静夜思 1年级 教学设计（共 3 条）\n"
            "1. 静夜思优质课教学设计\n   https://example.com/design\n   "
            "通过明月意象引导…",
            "搜索关键词：静夜思 课文原文（共 2 条）\n"
            "1. 静夜思全文\n   https://example.com/poem\n   床前明月光…",
        ])
        node = _make_research_node(None)
        return asyncio.run(node({"yuwen_params": PARAMS}))

    def test_payload_and_disk(self, outputs_tmp, monkeypatch):
        """正常两路搜索 → content 拼接、sources 抽取、state.json 落盘。"""
        result = self._run(monkeypatch)
        payload = result["yuwen_research"]
        assert "教学设计" in payload["content"]
        assert "课文原文" in payload["content"]
        urls = {s["url"] for s in payload["sources"]}
        assert urls == {"https://example.com/design", "https://example.com/poem"}
        assert payload["ts"] > 0
        # 落盘验证
        from aidraft.agenthub.yuwen.state import _load_state
        disk = _load_state(PARAMS)
        assert disk["yuwen_research"]["content"] == payload["content"]

    def test_ttl_cache_reuse(self, outputs_tmp, monkeypatch):
        """盘上有新鲜资料 → 复用不重搜（Tavily 按次计费）。"""
        self._run(monkeypatch)  # 第一轮落盘
        # 第二轮换一个会返回不同结果的 skill——不应被调用
        fake = _fake_skill_cls(monkeypatch, ["不该被调用"])
        from aidraft.agenthub.yuwen.nodes.research import _make_research_node
        node = _make_research_node(None)
        result = asyncio.run(node({"yuwen_params": PARAMS}))
        assert fake.search.call_count == 0
        assert "教学设计" in result["yuwen_research"]["content"]

    def test_stale_cache_researches(self, outputs_tmp, monkeypatch):
        """盘上资料超龄（ts 很旧）→ 重新搜索。"""
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_research={
            "content": "旧资料", "sources": [], "ts": 0})
        fake = _fake_skill_cls(monkeypatch, [
            "搜索关键词：q1（共 1 条）\n1. 新资料\n   https://e.com/a\n   内容",
            "搜索关键词：q2（共 1 条）\n2. 新资料2\n   https://e.com/b\n   内容",
        ])
        from aidraft.agenthub.yuwen.nodes.research import _make_research_node
        node = _make_research_node(None)
        result = asyncio.run(node({"yuwen_params": PARAMS}))
        assert fake.search.call_count == 2
        assert "新资料" in result["yuwen_research"]["content"]


class TestGenOutlineConsumes:
    def test_research_injected_into_prompt(self, outputs_tmp, monkeypatch):
        """research 结果 → gen_outline 的 user prompt 含"联网参考资料"段。"""
        from aidraft.agenthub.yuwen.nodes.gen_outline import _make_gen_outline_node
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_research={
            "content": "### 静夜思 教学设计\n通过明月意象引导…",
            "sources": [], "ts": __import__("time").time()})

        outline_json = json.dumps({
            "pages": [{"id": "s01", "kind": "cover", "title": "静夜思",
                       "period": 1, "points": "导入"}],
            "meta": {"title": "静夜思", "grade": 1, "lessonType": "古诗词",
                     "periods": 1, "theme": "default"},
        }, ensure_ascii=False)

        mock_gw = MagicMock()
        mock_gw.chat.return_value = MagicMock(content=outline_json)
        node = _make_gen_outline_node(mock_gw, None)
        asyncio.run(node({
            "yuwen_params": PARAMS,
            "yuwen_params_ready": True,
            "yuwen_research": {"content": "### 静夜思 教学设计\n通过明月意象…",
                               "sources": [], "ts": 1},
        }))
        user_prompt = mock_gw.chat.call_args[0][0][-1].content
        assert "联网参考资料" in user_prompt
        assert "明月意象" in user_prompt

    def test_no_research_no_segment(self, outputs_tmp):
        """无 research 结果 → prompt 无"联网参考资料"段（旧路径不变）。"""
        from aidraft.agenthub.yuwen.nodes.gen_outline import _make_gen_outline_node
        outline_json = json.dumps({
            "pages": [{"id": "s01", "kind": "cover", "title": "静夜思",
                       "period": 1, "points": "导入"}],
            "meta": {"title": "静夜思", "grade": 1, "lessonType": "古诗词",
                     "periods": 1, "theme": "default"},
        }, ensure_ascii=False)
        mock_gw = MagicMock()
        mock_gw.chat.return_value = MagicMock(content=outline_json)
        node = _make_gen_outline_node(mock_gw, None)
        asyncio.run(node({"yuwen_params": PARAMS, "yuwen_params_ready": True}))
        user_prompt = mock_gw.chat.call_args[0][0][-1].content
        assert "联网参考资料" not in user_prompt


class TestGraphWiring:
    def test_route_first_round_research(self, outputs_tmp):
        """图接线：参数齐 + 盘上无大纲 → research（M2 后的首轮入口）。"""
        from aidraft.agenthub.yuwen.graph import _route_after_params
        got = _route_after_params({
            "yuwen_params_ready": True, "yuwen_params": PARAMS})
        assert got == "research"

    def test_graph_compiles_with_research(self):
        """建图成功：research 节点注册 + 边接通（langgraph 校验 DAG 完整）。"""
        from aidraft.agenthub.yuwen.graph import build_graph
        g = build_graph(MagicMock(), MagicMock())
        assert "research" in g.get_graph().nodes
