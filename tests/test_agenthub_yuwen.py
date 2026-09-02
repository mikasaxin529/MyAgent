"""语文智能体图测试：阶段 2a 多阶段管线节点。

测试覆盖：
1. manifest.py 字段完整性
2. graph.py 编译正确 + 10 节点齐备
3. 跨轮状态机路由 _route_after_params / _route_after_confirm / _route_after_review
4. state.json 落盘（_save_state/_load_state/_parse_llm_json）
5. gen_outline：大纲生成 + outline 帧 + 落盘
6. confirm：确认放行 / 主题切换 / LLM 改纲 / 盘上找回会话
7. gen_slides：逐页生成 + 页级重试 + doc 合成落盘
8. gen_plan：教案+学习单融进 doc + tmp 重写
9. review：LLM 评分 + 降级放行 + _compute_pass
10. revise：按问题清单修页 + 轮数计数
11. gen_images：无 key 跳过 / 有 key 落盘回填 src + tmp 重写
12. _call_llm provider 签名过滤
13. render_all.py 纯 Python 渲染（无 LLM 依赖）
14. 图集成：盘上已确认 → 全链跑通

运行：
    pytest tests/test_agenthub_yuwen.py -x -v
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))


# ======================================================================
# 辅助
# ======================================================================

class _AsyncIter:
    """模拟 async for 迭代器。"""
    def __init__(self, items):
        self._items = items
    def __aiter__(self):
        return self
    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration


def _chunk(delta: str = "", reasoning: str = "", done: bool = False,
           finish_reason: str = ""):
    from aidraft.gateway import ChatChunk
    return ChatChunk(delta=delta, reasoning=reasoning, done=done,
                     finish_reason=finish_reason)


def _chat_response(content: str, finish_reason: str = "stop"):
    from aidraft.gateway import ChatResponse
    return ChatResponse(content=content, provider="test", model="test",
                        latency_ms=100, finish_reason=finish_reason)


PARAMS = {"title": "静夜思", "grade": 1, "lesson_type": "古诗词",
          "textbook": "部编版一年级下册"}

SAMPLE_OUTLINE = {
    "pages": [
        {"id": "s01", "kind": "cover", "title": "静夜思", "period": 1,
         "points": "配乐范读，整体感知"},
        {"id": "s02", "kind": "word-cards", "title": "生字朋友", "period": 1,
         "points": "9 个生字认读"},
        {"id": "s03", "kind": "read-rhythm", "title": "初读节奏", "period": 1,
         "points": "停顿划分，节奏朗读"},
    ],
    "meta": {"title": "静夜思", "grade": 1, "lessonType": "古诗词",
             "textbook": "部编版一年级下册", "periods": 1, "theme": "default"},
}


def _page_json(i: int, kind: str = "cover", extra_elem=None):
    """gen_slides mock 的单页 JSON（合法 schema，过最小骨架校验）。"""
    elements = [{"type": "heading", "content": f"页{i}", "size": "h1"}]
    if extra_elem:
        elements.append(extra_elem)
    return {"id": f"s0{i}", "kind": kind, "title": f"标题{i}", "period": 1,
            "elements": elements}


PLAN_JSON = {
    "lessonPlan": {
        "title": "静夜思",
        "base": {"textbook": "部编版一年级下册", "grade": "一年级",
                 "periods": "1", "lessonType": "古诗词"},
        "objectives": [{"content": "认识9个生字", "competency": "语言运用"}],
        "keyPoints": ["识字"], "difficulties": ["体会情感"],
        "preparation": "课件", "periods": "1课时",
        "teachingProcess": [{"phase": "一、导入", "duration": "5分钟",
                             "activities": [{"teacher": "出示明月图",
                                             "student": "观察"}],
                             "design": "情境"}],
        "boardDesign": {"structure": "静夜思"},
        "homework": {"levels": [{"level": "基础", "items": ["背诵"]}]},
        "reflection": "",
    },
    "handout": {"levels": [{"level": "基础", "items": ["背诵古诗"]}]},
}

REVIEW_PASS = {"scores": {"structure": 5, "pedagogy": 4, "content": 4,
                          "stage_fit": 5},
               "issues": [], "pass": True}


@pytest.fixture(autouse=True)
def outputs_tmp(tmp_path, monkeypatch):
    """把 state._OUTPUTS_DIR patch 到 tmp_path（所有盘读写经模块全局查找）。

    autouse：extract 询问轮/路由查盘成为常态后，不隔离的测试会读到仓库
    outputs/ 的真实 state.json（用户历史会话残留），结果随磁盘状态漂移。
    """
    from aidraft.agenthub.yuwen import state as st
    monkeypatch.setattr(st, "_OUTPUTS_DIR", tmp_path)
    return tmp_path


# ======================================================================
# 1. manifest 测试
# ======================================================================

class TestYuwenManifest:
    """语文智能体 manifest 字段完整性与注册。"""

    def test_manifest_fields(self):
        from aidraft.agenthub.yuwen import manifest as m
        assert m.AGENT_ID == "yuwen"
        assert m.DISPLAY_NAME == "语文课件生成"
        assert m.DESCRIPTION
        assert m.IDENTITY_COLOR
        assert m.PLACEHOLDER

    def test_registry_discovers_yuwen(self):
        from aidraft.agenthub import list_agents, reset_cache

        reset_cache()
        agents = list_agents()
        ids = [a.agent_id for a in agents]
        assert "yuwen" in ids, f"expected 'yuwen' in {ids}"

    def test_manifest_to_dict_format(self):
        from aidraft.agenthub import get_agent, reset_cache

        reset_cache()
        agent = get_agent("yuwen")
        assert agent is not None
        d = agent.to_dict()
        assert d["id"] == "yuwen"
        assert d["display_name"] == "语文课件生成"
        assert "description" in d
        assert "identity_color" in d
        assert "placeholder" in d


# ======================================================================
# 2. graph 编译测试
# ======================================================================

class TestYuwenGraph:
    """图编译与结构（阶段 2a：10 节点管线）。"""

    def test_graph_compiles(self):
        from aidraft.agenthub.yuwen.graph import build_graph

        graph = build_graph(gateway=MagicMock(), registry=MagicMock(),
                            emitter=lambda f: None)
        assert hasattr(graph, "astream"), "build_graph must return a compiled graph"

    def test_graph_has_pipeline_nodes(self):
        """图有全部 10 个管线节点。"""
        from aidraft.agenthub.yuwen.graph import build_graph

        graph = build_graph(gateway=MagicMock(), registry=MagicMock())
        for node in ("extract_params", "gen_outline", "confirm", "gen_slides",
                     "gen_plan", "review", "revise", "gen_images", "render",
                     "report"):
            assert node in graph.nodes, f"missing node: {node}"

    def test_entry_point_is_extract_params(self):
        from aidraft.agenthub.yuwen.graph import build_graph

        graph = build_graph(gateway=MagicMock(), registry=MagicMock())
        g = graph.get_graph()
        assert g.nodes and "extract_params" in g.nodes


# ======================================================================
# 3. 跨轮路由测试（状态机心脏）
# ======================================================================

class TestRouteAfterParams:
    """_route_after_params 查盘路由：直接调条件边函数断言返回值。"""

    def _route(self, state, disk=None):
        from aidraft.agenthub.yuwen import graph as gr
        with patch("aidraft.agenthub.yuwen.graph._load_state",
                   return_value=disk or {}), \
             patch("aidraft.agenthub.yuwen.graph._find_pending_session",
                   return_value=None):
            return gr._route_after_params(state)

    def test_not_ready_ends(self):
        """参数未齐 → END。"""
        assert self._route({"yuwen_params_ready": False}) == "__end__"

    def test_ready_no_outline_research(self):
        """参数齐 + 盘上无大纲 → research（M2：搜索后进 gen_outline）。"""
        got = self._route({"yuwen_params_ready": True, "yuwen_params": PARAMS},
                          disk={})
        assert got == "research"

    def test_ready_unconfirmed_outline_confirm(self):
        """盘上有未确认大纲 → confirm。"""
        got = self._route({"yuwen_params_ready": True, "yuwen_params": PARAMS},
                          disk={"yuwen_outline": SAMPLE_OUTLINE,
                                "yuwen_outline_confirmed": False})
        assert got == "confirm"

    def test_ready_confirmed_outline_slides(self):
        """盘上大纲已确认 → gen_slides（续跑）。"""
        got = self._route({"yuwen_params_ready": True, "yuwen_params": PARAMS},
                          disk={"yuwen_outline": SAMPLE_OUTLINE,
                                "yuwen_outline_confirmed": True})
        assert got == "gen_slides"

    def test_outline_command_not_ready_falls_to_confirm(self):
        """参数未齐但消息像大纲指令 + 盘有待确认会话 → confirm 兜底。"""
        from aidraft.agenthub.yuwen import graph as gr
        with patch("aidraft.agenthub.yuwen.graph._find_pending_session",
                   return_value=(PARAMS, {"yuwen_outline": SAMPLE_OUTLINE})):
            got = gr._route_after_params({
                "yuwen_params_ready": False,
                "user_message": "确认大纲，开始生成",
            })
        assert got == "confirm"

    def test_outline_command_without_pending_ends(self):
        """消息像指令但盘上无待确认会话 → 仍 END（追问语义）。"""
        from aidraft.agenthub.yuwen import graph as gr
        with patch("aidraft.agenthub.yuwen.graph._find_pending_session",
                   return_value=None):
            got = gr._route_after_params({
                "yuwen_params_ready": False,
                "user_message": "确认",
            })
        assert got == "__end__"


class TestSessionIsolation:
    """前端 session_id 并入 state.json 会话键：同课名新会话不被旧状态劫持。"""

    def test_session_name_with_short(self):
        from aidraft.agenthub.yuwen.state import _session_name
        assert _session_name({"title": "静夜思", "lesson_type": "古诗词",
                              "_session": "ab12cd34"}) == "静夜思-古诗词-ab12cd34"
        # 无短码：历史会话目录名不变
        assert _session_name({"title": "静夜思",
                              "lesson_type": "古诗词"}) == "静夜思-古诗词"

    def test_extract_params_stamps_session(self):
        """extract_params 把前端 session_id 后 8 位写进 params（不进 prefs）。"""
        from aidraft.agenthub.yuwen.nodes.extract_params import _make_extract_params_node
        gw = MagicMock()
        gw.chat.return_value = MagicMock(content=json.dumps(
            {"title": "静夜思", "grade": 1, "lesson_type": "古诗词",
             "textbook": "", "question": "", "chips": []}, ensure_ascii=False))
        node = _make_extract_params_node(gw, None)
        result = asyncio.run(node({
            "task": "静夜思", "user_message": "静夜思", "messages": [],
            "session_id": "sess_1788314431714_ab12cd34"}))
        assert result["yuwen_params"]["_session"] == "ab12cd34"
        # 追问轮也带短码（半填的 params 落盘后可被找回）
        gw.chat.return_value = MagicMock(content=json.dumps(
            {"title": "静夜思", "grade": 0, "lesson_type": "",
             "textbook": "", "question": "几年级？", "chips": []}, ensure_ascii=False))
        result2 = asyncio.run(node({
            "task": "静夜思", "user_message": "静夜思", "messages": [],
            "session_id": "sess_1788314431714_ab12cd34"}))
        assert result2["yuwen_params"]["_session"] == "ab12cd34"

    def test_route_new_session_not_hijacked(self):
        """旧会话大纲已确认，新会话（不同 session 短码）仍从大纲重新开始。

        修复前：state.json 按课文名落盘，新会话被旧会话状态劫持直跳
        gen_slides，跳过大纲确认。
        """
        from aidraft.agenthub.yuwen import graph as gr
        new_params = dict(PARAMS, _session="new88888")
        # 盘上无此新会话的 state（_load_state 返回 {}）
        with patch("aidraft.agenthub.yuwen.graph._load_state", return_value={}):
            got = gr._route_after_params({
                "yuwen_params_ready": True, "yuwen_params": new_params})
        assert got == "research"  # 走 research → gen_outline，不劫持

    def test_find_pending_filters_by_session(self, outputs_tmp):
        """_find_pending_session 只扫本会话目录，不劫持其他会话的大纲。"""
        from aidraft.agenthub.yuwen import state as st

        # 旧会话（无短码）有未确认大纲；本会话短码 aa111111 的目录为空
        st._save_state(dict(PARAMS), yuwen_outline=SAMPLE_OUTLINE,
                       yuwen_params=dict(PARAMS))
        got = st._find_pending_session(session_short="aa111111")
        assert got is None  # 不被旧会话劫持

        # 本会话自己的待确认大纲能找回
        mine = dict(PARAMS, _session="aa111111")
        st._save_state(mine, yuwen_outline=SAMPLE_OUTLINE,
                       yuwen_params=mine, yuwen_outline_confirmed=False)
        got2 = st._find_pending_session(session_short="aa111111")
        assert got2 is not None
        assert got2[0]["_session"] == "aa111111"

        # 无短码：保持旧行为（全局扫 mtime 最新的未确认会话）
        got3 = st._find_pending_session()
        assert got3 is not None


class TestRouteAfterConfirmAndReview:
    """confirm / review 出口路由。"""

    def test_confirm_release(self):
        from aidraft.agenthub.yuwen.graph import _route_after_confirm
        assert _route_after_confirm({"yuwen_outline_confirmed": True}) == "gen_slides"
        assert _route_after_confirm({"yuwen_outline_confirmed": False}) == "__end__"
        assert _route_after_confirm({}) == "__end__"

    def test_review_pass_images(self):
        from aidraft.agenthub.yuwen.graph import _route_after_review
        assert _route_after_review({"yuwen_review": {"pass": True}}) == "gen_images"

    def test_review_fail_revise(self):
        from aidraft.agenthub.yuwen.graph import _route_after_review
        st = {"yuwen_review": {"pass": False}, "yuwen_revise_rounds": 1}
        assert _route_after_review(st) == "revise"

    def test_review_rounds_exhausted_releases(self):
        """2 轮修订仍不过 → 放行 gen_images（审查是提质不是阻断）。"""
        from aidraft.agenthub.yuwen.graph import _route_after_review
        st = {"yuwen_review": {"pass": False}, "yuwen_revise_rounds": 2}
        assert _route_after_review(st) == "gen_images"


# ======================================================================
# 4. 状态落盘 / JSON 解析
# ======================================================================

class TestStatePersistence:
    """state.json 读-改-写与 LLM JSON 解析降级。"""

    def test_save_and_load_roundtrip(self, outputs_tmp):
        from aidraft.agenthub.yuwen.state import _save_state, _load_state, _state_path
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE)
        _save_state(PARAMS, yuwen_outline_confirmed=True)  # 第二次只加字段
        data = _load_state(PARAMS)
        assert data["yuwen_outline"]["pages"][0]["id"] == "s01"
        assert data["yuwen_outline_confirmed"] is True
        assert _state_path(PARAMS).exists()

    def test_load_missing_returns_empty(self, outputs_tmp):
        from aidraft.agenthub.yuwen.state import _load_state
        assert _load_state({"title": "不存在", "lesson_type": "精读"}) == {}

    def test_load_corrupt_returns_empty(self, outputs_tmp):
        from aidraft.agenthub.yuwen.state import _load_state, _state_path
        p = _state_path(PARAMS)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        assert _load_state(PARAMS) == {}

    def test_parse_llm_json_direct(self):
        from aidraft.agenthub.yuwen.state import _parse_llm_json
        assert _parse_llm_json('{"a": 1}') == {"a": 1}

    def test_parse_llm_json_codeblock(self):
        from aidraft.agenthub.yuwen.state import _parse_llm_json
        assert _parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_parse_llm_json_fenced_text(self):
        from aidraft.agenthub.yuwen.state import _parse_llm_json
        assert _parse_llm_json('好的：\n{"a": [1,2]}\n以上。')["a"] == [1, 2]

    def test_parse_llm_json_failure_raises(self):
        from aidraft.agenthub.yuwen.state import _parse_llm_json
        with pytest.raises(ValueError):
            _parse_llm_json("彻底不是 JSON")


# ======================================================================
# 5. gen_outline 节点
# ======================================================================

class TestGenOutline:
    """gen_outline：大纲生成 → state.json + outline 帧 + content 摘要。"""

    def _run(self, mock_gw):
        from aidraft.agenthub.yuwen.nodes.gen_outline import _make_gen_outline_node
        frames = []
        node = _make_gen_outline_node(mock_gw, lambda f: frames.append(f))
        result = asyncio.run(node({"yuwen_params": PARAMS}))
        return result, frames

    def test_valid_outline_persists_and_emits(self, outputs_tmp):
        from aidraft.agenthub.yuwen.state import _load_state
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            json.dumps(SAMPLE_OUTLINE, ensure_ascii=False))
        result, frames = self._run(mock_gw)

        assert result["yuwen_outline_confirmed"] is False
        assert result["yuwen_outline"]["pages"][0]["id"] == "s01"
        # state.json 落盘
        disk = _load_state(PARAMS)
        assert disk["yuwen_outline"]["pages"]
        assert disk["yuwen_outline_confirmed"] is False
        assert disk["yuwen_params"] == PARAMS
        # outline 帧（帧契约：type/outline/chips）
        outline_frames = [f for f in frames if f.get("type") == "outline"]
        assert len(outline_frames) == 1
        assert outline_frames[0]["outline"]["pages"][0]["kind"] == "cover"
        assert any("确认" in c for c in outline_frames[0]["chips"])
        # content 摘要帧（旧前端兼容）
        content_frames = [f for f in frames if f.get("type") == "content"]
        assert content_frames and "3 页" in content_frames[0]["delta"]
        # 只发 content 不发 token（防 final_answer 翻倍）
        assert not [f for f in frames if f.get("type") == "token"]

    def test_invalid_then_valid_retry(self, outputs_tmp):
        """第一次 pages 缺 id → 带反馈重试成功。"""
        bad = {"pages": [{"kind": "cover", "title": "t", "period": 1}],
               "meta": SAMPLE_OUTLINE["meta"]}
        calls = []
        mock_gw = MagicMock()

        def _side(msgs, **kw):
            calls.append(list(msgs))
            payload = bad if len(calls) == 1 else SAMPLE_OUTLINE
            return _chat_response(json.dumps(payload, ensure_ascii=False))

        mock_gw.chat.side_effect = _side
        result, frames = self._run(mock_gw)
        assert result["yuwen_outline"]["pages"]
        assert mock_gw.chat.call_count == 2
        # 第 2 次调用带纠错反馈
        assert [m.role for m in calls[1]] == ["system", "user", "assistant", "user"]
        assert "pages[0] 缺 id" in calls[1][3].content

    def test_both_attempts_fail(self, outputs_tmp):
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response("garbage")
        result, frames = self._run(mock_gw)
        assert result["yuwen_outline"] == {}
        assert "大纲生成失败" in result["yuwen_error"]
        assert any(f.get("status") == "error" for f in frames if f.get("type") == "step")

    def test_gateway_exception_degrades(self, outputs_tmp):
        mock_gw = MagicMock()
        mock_gw.chat.side_effect = RuntimeError("API down")
        result, _ = self._run(mock_gw)
        assert result["yuwen_outline_confirmed"] is False


# ======================================================================
# 6. confirm 节点
# ======================================================================

class TestConfirm:
    """confirm：确认放行 / 主题切换 / 改纲 / 盘上找回会话。"""

    def _run(self, user_msg, mock_gw=None, params=PARAMS):
        from aidraft.agenthub.yuwen.nodes.confirm import _make_confirm_node
        frames = []
        node = _make_confirm_node(mock_gw or MagicMock(),
                                  lambda f: frames.append(f))
        result = asyncio.run(node({"yuwen_params": params,
                                   "user_message": user_msg}))
        return result, frames

    def test_confirm_releases(self, outputs_tmp):
        from aidraft.agenthub.yuwen.state import _save_state, _load_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=False)
        result, _ = self._run("确认")
        assert result["yuwen_outline_confirmed"] is True
        assert _load_state(PARAMS)["yuwen_outline_confirmed"] is True

    def test_theme_switch_waits_for_confirm(self, outputs_tmp):
        from aidraft.agenthub.yuwen.state import _save_state, _load_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=False)
        result, frames = self._run("换成蓝色主题")
        assert result["yuwen_outline_confirmed"] is False
        assert result["yuwen_outline"]["meta"]["theme"] == "fresh-blue"
        assert _load_state(PARAMS)["yuwen_outline"]["meta"]["theme"] == "fresh-blue"
        # 重发 outline 帧（前端刷新预览）
        assert any(f.get("type") == "outline" for f in frames)

    def test_theme_plus_confirm(self, outputs_tmp):
        """主题切换与确认同句 → 切主题并放行。"""
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=False)
        result, _ = self._run("换成墨绿主题，确认")
        assert result["yuwen_outline_confirmed"] is True
        assert result["yuwen_outline"]["meta"]["theme"] == "warm-green"

    def test_edit_via_llm(self, outputs_tmp):
        """自然语言修改 → LLM 改纲，confirmed 保持 False。"""
        from aidraft.agenthub.yuwen.state import _save_state, _load_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=False)
        edited = json.loads(json.dumps(SAMPLE_OUTLINE))
        edited["pages"].insert(1, {"id": "s99", "kind": "game",
                                   "title": "识字游戏", "period": 1,
                                   "points": "摘苹果"})
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            json.dumps(edited, ensure_ascii=False))
        result, frames = self._run("每页加一个游戏环节", mock_gw=mock_gw)

        assert result["yuwen_outline_confirmed"] is False
        assert any(p["kind"] == "game" for p in result["yuwen_outline"]["pages"])
        assert _load_state(PARAMS)["yuwen_outline"] == edited
        assert any(f.get("type") == "outline" for f in frames)
        # system prompt 带当前大纲（编辑有据可依）
        sent = mock_gw.chat.call_args[0][0]
        assert "当前大纲" in sent[0].content

    def test_edit_failure_keeps_original(self, outputs_tmp):
        """改纲输出非法 → 保留原大纲 + 提示，不炸。"""
        from aidraft.agenthub.yuwen.state import _save_state, _load_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE)
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response("完全不是 JSON")
        result, frames = self._run("改一下", mock_gw=mock_gw)
        assert result["yuwen_outline_confirmed"] is False
        assert _load_state(PARAMS)["yuwen_outline"]["pages"][0]["id"] == "s01"

    def test_no_outline_on_disk_defensive(self, outputs_tmp):
        """盘上无大纲（理论上路由不会放行到这）→ 防御式未确认返回。"""
        result, _ = self._run("确认")
        assert result["yuwen_outline_confirmed"] is False

    def test_chip_message_finds_pending_session(self, outputs_tmp):
        """params 缺失（chip 点击兜底路由）→ 盘上找回待确认会话并放行。"""
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=False, yuwen_params=PARAMS)
        result, _ = self._run("确认大纲，开始生成", params={})
        assert result["yuwen_outline_confirmed"] is True
        assert result["yuwen_outline"]["pages"][0]["id"] == "s01"

    def test_confirm_word_detection(self):
        from aidraft.agenthub.yuwen.nodes.confirm import _detect_confirm, _detect_theme
        assert _detect_confirm("确认") is True
        assert _detect_confirm("没问题") is True
        assert _detect_confirm("ok") is True
        assert _detect_confirm("好像不太对") is False  # "好"前缀不误伤
        assert _detect_confirm("第2页改成游戏") is False
        assert _detect_theme("换成青蓝主题") == "fresh-blue"
        assert _detect_theme("绿色") == "warm-green"
        assert _detect_theme("恢复默认") == "default"
        assert _detect_theme("普通消息") is None

    def test_image_prefs_detection(self):
        from aidraft.agenthub.yuwen.nodes.confirm import _detect_image_prefs
        assert _detect_image_prefs("配图用水彩风格") == {"image_style": "水彩"}
        assert _detect_image_prefs("插图多一些") == {"image_count": "all"}
        assert _detect_image_prefs("不要配图") == {"image_count": "none"}
        assert _detect_image_prefs("生图换成国风，插图全部要") == {
            "image_style": "国风", "image_count": "all"}
        # 无触发词不误伤（课文内容里出现风格词）
        assert _detect_image_prefs("这课讲的是水彩画") == {}
        assert _detect_image_prefs("普通消息") == {}

    def test_image_style_switch_persists(self, outputs_tmp):
        """确认轮改配图风格 → params 更新并落盘（gen_images 据此生效）。"""
        from aidraft.agenthub.yuwen.state import _save_state, _load_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=False, yuwen_params=PARAMS)
        result, frames = self._run("配图用剪纸风格")
        assert result["yuwen_outline_confirmed"] is False
        assert result["yuwen_params"]["image_style"] == "剪纸"
        assert _load_state(PARAMS)["yuwen_params"]["image_style"] == "剪纸"
        assert any(f.get("type") == "content" and "配图" in f.get("delta", "")
                   for f in frames)

    def test_image_prefs_plus_confirm(self, outputs_tmp):
        """配图切换与确认同句 → 更新 params 并放行。"""
        from aidraft.agenthub.yuwen.state import _save_state, _load_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=False, yuwen_params=PARAMS)
        result, _ = self._run("不要配图，确认")
        assert result["yuwen_outline_confirmed"] is True
        assert result["yuwen_params"]["image_count"] == "none"
        assert _load_state(PARAMS)["yuwen_params"]["image_count"] == "none"


# ======================================================================
# 7. gen_slides 节点
# ======================================================================

class TestGenSlides:
    """gen_slides：逐页生成（每页一次 stream）+ 页级重试 + doc 落盘。"""

    def _stream_for_pages(self, pages_json: list[str]):
        """side_effect：按调用顺序返回各页的 chunk 流。"""
        seq = iter(pages_json)

        def _side(msgs, **kw):
            text = next(seq)
            return _AsyncIter([_chunk(delta=text), _chunk(done=True)])
        return _side

    def test_three_pages_merge_into_doc(self, outputs_tmp):
        from aidraft.agenthub.yuwen.nodes.gen_slides import _make_gen_slides_node
        mock_gw = MagicMock()
        mock_gw.stream_chat.side_effect = self._stream_for_pages(
            [json.dumps(_page_json(1, "cover")),
             json.dumps(_page_json(2, "word-cards")),
             json.dumps(_page_json(3, "read-rhythm"))])
        frames = []
        node = _make_gen_slides_node(mock_gw, lambda f: frames.append(f))
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_outline": SAMPLE_OUTLINE}))

        assert mock_gw.stream_chat.call_count == 3
        doc = result["yuwen_content"]
        assert len(doc["slides"]) == 3
        assert doc["handout"] == {"levels": []}  # 空 dict 过不了 render 校验
        assert doc["meta"]["title"] == "静夜思"
        assert doc["meta"]["lessonType"] == "古诗词"
        # 落盘 tmp_content.json
        path = Path(result["yuwen_content_path"])
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["slides"][0]["id"] == "s01"
        # 进度 step 帧："1/3 页：..."
        running = [f for f in frames if f.get("type") == "step"
                   and f.get("id") == "gen_slides" and f.get("status") == "running"]
        assert any("1/3" in (f.get("detail") or "") for f in running)

    def test_page_retry_with_feedback(self, outputs_tmp):
        """第 1 页第 1 次坏 JSON、第 2 次好 → 成功且不报废整课。"""
        from aidraft.agenthub.yuwen.nodes.gen_slides import _make_gen_slides_node
        calls = []

        def _side(msgs, **kw):
            calls.append(list(msgs))
            if len(calls) == 1:
                return _AsyncIter([_chunk(delta="broken"), _chunk(done=True)])
            i = len(calls)  # 第 2 次调用对应第 1 页，之后依次
            page = i - 1 if i <= 3 else i - 1
            return _AsyncIter([_chunk(delta=json.dumps(_page_json(page))),
                               _chunk(done=True)])

        mock_gw = MagicMock()
        mock_gw.stream_chat.side_effect = _side
        node = _make_gen_slides_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_outline": SAMPLE_OUTLINE}))
        doc = result["yuwen_content"]
        assert len(doc["slides"]) == 3
        # 第 2 次调用带 assistant+user 反馈
        assert [m.role for m in calls[1]] == ["system", "user", "assistant", "user"]
        assert "校验" in calls[1][3].content or "JSON" in calls[1][3].content

    def test_page_hard_fail_continues(self, outputs_tmp):
        """第 1 页 3 次全坏 → 其余页继续，error 记录失败页。"""
        from aidraft.agenthub.yuwen.nodes.gen_slides import _make_gen_slides_node
        calls = []

        def _side(msgs, **kw):
            calls.append(1)
            if len(calls) <= 3:  # 第 1 页的 3 次尝试全失败
                return _AsyncIter([_chunk(delta="garbage"), _chunk(done=True)])
            return _AsyncIter([_chunk(delta=json.dumps(_page_json(len(calls) - 2))),
                               _chunk(done=True)])

        mock_gw = MagicMock()
        mock_gw.stream_chat.side_effect = _side
        node = _make_gen_slides_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_outline": SAMPLE_OUTLINE}))
        assert len(result["yuwen_content"]["slides"]) == 2
        assert "生成失败" in result["yuwen_error"]
        assert result["yuwen_content_path"]  # 部分产出仍落盘

    def test_doc_wrap_output_recovered(self, outputs_tmp):
        """模型误输出整 doc → 拆 slides[0] 用（常见偏差抢救）。"""
        from aidraft.agenthub.yuwen.nodes.gen_slides import _make_gen_slides_node
        wrapped = {"meta": {}, "slides": [_page_json(1, "cover")]}

        def _side(msgs, **kw):
            return _AsyncIter([_chunk(delta=json.dumps(wrapped)),
                               _chunk(done=True)])

        mock_gw = MagicMock()
        mock_gw.stream_chat.side_effect = _side
        node = _make_gen_slides_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_outline":
                                       {**SAMPLE_OUTLINE,
                                        "pages": SAMPLE_OUTLINE["pages"][:1]}}))
        assert result["yuwen_content"]["slides"][0]["kind"] == "cover"

    def test_outline_from_disk_when_state_empty(self, outputs_tmp):
        """路由可从盘直跳 gen_slides：state 无 outline 时查盘兜底。"""
        from aidraft.agenthub.yuwen.nodes.gen_slides import _make_gen_slides_node
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_outline=SAMPLE_OUTLINE,
                    yuwen_outline_confirmed=True)
        mock_gw = MagicMock()
        mock_gw.stream_chat.side_effect = self._stream_for_pages(
            [json.dumps(_page_json(1)), json.dumps(_page_json(2)),
             json.dumps(_page_json(3))])
        node = _make_gen_slides_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS}))
        assert len(result["yuwen_content"]["slides"]) == 3

    def test_normalize_applied_per_page(self, outputs_tmp):
        """单页里的模型偏差（text→paragraph）在校验前被 normalize 纠偏。"""
        from aidraft.agenthub.yuwen.nodes.gen_slides import _make_gen_slides_node
        bad_page = {"id": "s01", "kind": "cover", "title": "t", "period": 1,
                    "elements": [{"type": "text", "content": "床前明月光"}]}
        mock_gw = MagicMock()
        mock_gw.stream_chat.side_effect = self._stream_for_pages(
            [json.dumps(bad_page), json.dumps(_page_json(2)),
             json.dumps(_page_json(3))])
        node = _make_gen_slides_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_outline": SAMPLE_OUTLINE}))
        types = [el["type"] for s in result["yuwen_content"]["slides"]
                 for el in s["elements"]]
        assert "paragraph" in types and "text" not in types


# ======================================================================
# 8. gen_plan 节点
# ======================================================================

class TestGenPlan:
    """gen_plan：教案+学习单融进 doc 并重写 tmp json。"""

    def _doc(self, outputs_tmp, params=PARAMS):
        return {"version": "1.0", "meta": SAMPLE_OUTLINE["meta"],
                "slides": [_page_json(1), _page_json(2), _page_json(3)],
                "lessonPlan": {}, "handout": {"levels": []}}

    def test_plan_merged_and_rewritten(self, outputs_tmp):
        from aidraft.agenthub.yuwen.nodes.gen_plan import _make_gen_plan_node
        doc = self._doc(outputs_tmp)
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            json.dumps(PLAN_JSON, ensure_ascii=False))
        node = _make_gen_plan_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_content": doc}))
        assert result["yuwen_content"]["lessonPlan"]["title"] == "静夜思"
        assert result["yuwen_content"]["handout"]["levels"]
        # 盘上 tmp 重写
        from aidraft.agenthub.yuwen.state import _content_path
        on_disk = json.loads(_content_path(PARAMS).read_text(encoding="utf-8"))
        assert on_disk["lessonPlan"]["teachingProcess"]

    def test_plan_failure_not_blocking(self, outputs_tmp):
        """两次都坏 → 不返回新 content（state 保留原 doc，lessonPlan 维持占位），管线继续。"""
        from aidraft.agenthub.yuwen.nodes.gen_plan import _make_gen_plan_node
        doc = self._doc(outputs_tmp)
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response("not json")
        node = _make_gen_plan_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_content": doc}))
        assert "yuwen_content" not in result, "失败不应覆盖 state 里的 doc"
        assert "yuwen_error" not in result or not result.get("yuwen_error")

    def test_handout_levels_backfilled(self, outputs_tmp):
        """LLM 忘给 handout.levels → 程序补 []（render validate 硬要求）。"""
        from aidraft.agenthub.yuwen.nodes.gen_plan import _make_gen_plan_node
        plan = {"lessonPlan": PLAN_JSON["lessonPlan"], "handout": {}}
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps(plan, ensure_ascii=False))
        node = _make_gen_plan_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_content": self._doc(outputs_tmp)}))
        assert result["yuwen_content"]["handout"]["levels"] == []


# ======================================================================
# 9. review / revise 节点
# ======================================================================

class TestReview:
    """review：LLM 评分 + review 帧 + 失败降级。"""

    def _doc(self):
        return {"version": "1.0", "meta": {**SAMPLE_OUTLINE["meta"], "stage": "低段"},
                "slides": [_page_json(1), _page_json(2), _page_json(3)],
                "lessonPlan": {}, "handout": {"levels": []}}

    def test_pass_emits_review_frame(self, outputs_tmp):
        from aidraft.agenthub.yuwen.nodes.review import _make_review_node
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            json.dumps(REVIEW_PASS, ensure_ascii=False))
        frames = []
        node = _make_review_node(mock_gw, lambda f: frames.append(f))
        result = asyncio.run(node({"yuwen_content": self._doc()}))
        assert result["yuwen_review"]["pass"] is True
        review_frames = [f for f in frames if f.get("type") == "review"]
        assert len(review_frames) == 1
        assert review_frames[0]["review"]["scores"]["structure"] == 5

    def test_issues_with_pass_recompute(self, outputs_tmp):
        """LLM 给 issues 却标 pass=true → 程序按规则重算为 False。"""
        from aidraft.agenthub.yuwen.nodes.review import _make_review_node
        payload = {"scores": {"structure": 4, "pedagogy": 4, "content": 4,
                              "stage_fit": 4},
                   "issues": [{"page_id": "s02", "problems": ["元素内容空泛"]}],
                   "pass": True}
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps(payload, ensure_ascii=False))
        node = _make_review_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_content": self._doc()}))
        assert result["yuwen_review"]["pass"] is False
        assert result["yuwen_review"]["issues"]

    def test_review_failure_degrades_pass(self, outputs_tmp):
        """审查不可用（网关挂）→ 降级放行，不阻断。"""
        from aidraft.agenthub.yuwen.nodes.review import _make_review_node
        mock_gw = MagicMock()
        mock_gw.chat.side_effect = RuntimeError("down")
        node = _make_review_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_content": self._doc()}))
        assert result["yuwen_review"]["pass"] is True
        assert "error" in result["yuwen_review"]

    def test_no_content_skips(self, outputs_tmp):
        from aidraft.agenthub.yuwen.nodes.review import _make_review_node
        node = _make_review_node(MagicMock(), None)
        result = asyncio.run(node({"yuwen_content": {}}))
        assert result["yuwen_review"]["pass"] is True

    def test_sample_pages_deterministic(self, outputs_tmp):
        """抽查页固定种子可重现。"""
        from aidraft.agenthub.yuwen.nodes.review import _sample_pages
        doc = {"slides": [_page_json(i) for i in range(1, 9)],
               "meta": {"stage": "低段"}}
        a, b = _sample_pages(doc), _sample_pages(doc)
        assert a == b


class TestRevise:
    """revise：按问题清单单页重生成，轮数计数。"""

    def test_issue_page_replaced(self, outputs_tmp):
        from aidraft.agenthub.yuwen.nodes.revise import _make_revise_node
        doc = {"version": "1.0", "meta": {**SAMPLE_OUTLINE["meta"], "stage": "低段"},
               "slides": [_page_json(1), _page_json(2), _page_json(3)],
               "lessonPlan": {}, "handout": {"levels": []}}
        review = {"scores": {"structure": 3, "pedagogy": 4, "content": 3,
                             "stage_fit": 4},
                  "issues": [{"page_id": "s02", "problems": ["discussion hint 为空"]}],
                  "pass": False}
        fixed = _page_json(2, "word-cards", extra_elem={
            "type": "discussion", "question": "问题？", "hint": "提示"})
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            json.dumps(fixed, ensure_ascii=False))
        node = _make_revise_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS, "yuwen_content": doc,
                                   "yuwen_review": review, "yuwen_revise_rounds": 0}))
        assert result["yuwen_revise_rounds"] == 1
        s02 = next(s for s in result["yuwen_content"]["slides"] if s["id"] == "s02")
        types = [e["type"] for e in s02["elements"]]
        assert "discussion" in types
        # 修订结果落盘
        from aidraft.agenthub.yuwen.state import _content_path
        disk = json.loads(_content_path(PARAMS).read_text(encoding="utf-8"))
        assert any("discussion" in [e["type"] for e in s["elements"]]
                   for s in disk["slides"])

    def test_ghost_page_id_skipped(self, outputs_tmp):
        """LLM 幻觉页 ID → 跳过不炸。"""
        from aidraft.agenthub.yuwen.nodes.revise import _make_revise_node
        doc = {"version": "1.0", "meta": SAMPLE_OUTLINE["meta"],
               "slides": [_page_json(1)], "lessonPlan": {},
               "handout": {"levels": []}}
        review = {"scores": {}, "issues": [{"page_id": "s99",
                                            "problems": ["x"]}], "pass": False}
        node = _make_revise_node(MagicMock(), None)
        result = asyncio.run(node({"yuwen_params": PARAMS, "yuwen_content": doc,
                                   "yuwen_review": review, "yuwen_revise_rounds": 1}))
        assert result["yuwen_revise_rounds"] == 2
        assert result["yuwen_content"]["slides"][0]["id"] == "s01"

    def test_fix_failure_keeps_original(self, outputs_tmp):
        """修订输出坏 JSON → 保留原页（改坏不如不改）。"""
        from aidraft.agenthub.yuwen.nodes.revise import _make_revise_node
        doc = {"version": "1.0", "meta": SAMPLE_OUTLINE["meta"],
               "slides": [_page_json(2, "word-cards")], "lessonPlan": {},
               "handout": {"levels": []}}
        review = {"scores": {}, "issues": [{"page_id": "s02",
                                            "problems": ["p"]}], "pass": False}
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response("garbage")
        node = _make_revise_node(mock_gw, None)
        result = asyncio.run(node({"yuwen_params": PARAMS, "yuwen_content": doc,
                                   "yuwen_review": review, "yuwen_revise_rounds": 0}))
        assert result["yuwen_content"]["slides"][0]["kind"] == "word-cards"


# ======================================================================
# 10. gen_images 节点
# ======================================================================

class TestGenImages:
    """gen_images：可选 AI 配图（无 key 跳过 / 有 key 落盘回填）。"""

    def _doc_with_images(self):
        return {"version": "1.0", "meta": SAMPLE_OUTLINE["meta"],
                "slides": [
                    {"id": "s01", "kind": "cover", "title": "静夜思", "period": 1,
                     "elements": [
                         {"type": "heading", "content": "静夜思", "size": "h1"},
                         {"type": "image", "caption": "月夜意境图", "src": ""}]},
                    {"id": "s02", "kind": "intro", "title": "诗人", "period": 1,
                     "elements": [
                         {"type": "image", "caption": "李白像", "src": ""}]},
                ],
                "lessonPlan": {}, "handout": {"levels": []}}

    def test_no_key_skips(self, outputs_tmp):
        """无 key → 不生图；多元素页的空 src image 被清理，孤元素页豁免。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = self._doc_with_images()
        fake_cls = MagicMock()
        fake_cls.return_value.available = False
        frames = []
        node = _make_gen_images_node(lambda f: frames.append(f))
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen", fake_cls):
            result = asyncio.run(node({"yuwen_params": PARAMS,
                                       "yuwen_content": doc}))
        slides = result["yuwen_content"]["slides"]
        # s01（heading+image 两元素）：空 image 删除，正文不留灰块
        assert [e["type"] for e in slides[0]["elements"]] == ["heading"]
        # s02 唯一元素就是空 image：删光会让页面无元素（validate 报错），保留
        assert slides[1]["elements"][0]["src"] == ""
        done = [f for f in frames if f.get("status") == "done"]
        assert any("DASHSCOPE_API_KEY" in f.get("detail", "") for f in done)

    def test_no_empty_image_noop(self, outputs_tmp):
        """无待配图元素（src 已填或无 image）→ 直接过，不调网关。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = self._doc_with_images()
        doc["slides"][0]["elements"][1]["src"] = "assets/old.png"
        doc["slides"][1]["elements"] = [{"type": "paragraph", "content": "x"}]
        node = _make_gen_images_node(None)
        result = asyncio.run(node({"yuwen_params": PARAMS,
                                   "yuwen_content": doc}))
        assert result["yuwen_content"]["slides"][0]["elements"][1]["src"] \
            == "assets/old.png"

    def test_generate_success_backfills(self, outputs_tmp):
        """生图成功 → 落盘 assets/*.png + src 相对路径回填 + tmp 重写。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        from aidraft.agenthub.yuwen.state import _content_path, _session_dir
        doc = self._doc_with_images()

        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG fake")
        fake_cls = MagicMock(return_value=fake_gen)
        node = _make_gen_images_node(None)
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen", fake_cls):
            result = asyncio.run(node({"yuwen_params": PARAMS,
                                       "yuwen_content": doc}))

        el0 = result["yuwen_content"]["slides"][0]["elements"][1]
        el1 = result["yuwen_content"]["slides"][1]["elements"][0]
        assert el0["src"] == "assets/s01_1.png"   # 相对 session 目录（帧契约）
        assert el1["src"] == "assets/s02_0.png"
        assets = _session_dir(PARAMS) / "assets"
        assert (assets / "s01_1.png").read_bytes() == b"\x89PNG fake"
        # tmp json 重写（render 读盘）
        disk = json.loads(_content_path(PARAMS).read_text(encoding="utf-8"))
        assert disk["slides"][0]["elements"][1]["src"] == "assets/s01_1.png"
        # prompt 由 caption+页标题+课文名拼中文（两张图各查一次调用）
        prompts = [c[0][0] for c in fake_gen.generate.call_args_list]
        assert any("月夜意境图" in p and "静夜思" in p for p in prompts)
        assert any("李白像" in p for p in prompts)

    def test_generate_failure_pruned_not_gray(self, outputs_tmp):
        """生图失败 → 多元素页的空 src image 被删除（渲染不留灰块），不 raise。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = self._doc_with_images()
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(side_effect=RuntimeError("gateway 503"))
        fake_cls = MagicMock(return_value=fake_gen)
        node = _make_gen_images_node(None)
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen", fake_cls):
            result = asyncio.run(node({"yuwen_params": PARAMS,
                                       "yuwen_content": doc}))
        slides = result["yuwen_content"]["slides"]
        assert [e["type"] for e in slides[0]["elements"]] == ["heading"]
        # 孤元素页豁免（删光过不了 validate）——src 留空由渲染兜底
        assert slides[1]["elements"][0]["src"] == ""

    def test_style_injected_into_prompt(self, outputs_tmp):
        """image_style=水彩 → prompt 含水彩风格短语。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = self._doc_with_images()
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        node = _make_gen_images_node(None)
        params = {**PARAMS, "image_style": "水彩"}
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen",
                   MagicMock(return_value=fake_gen)):
            asyncio.run(node({"yuwen_params": params, "yuwen_content": doc}))
        prompts = [c[0][0] for c in fake_gen.generate.call_args_list]
        assert prompts and all("水彩插画风格" in p for p in prompts)
        assert all("无文字，无水印" in p for p in prompts)

    def test_freeform_style_passthrough(self, outputs_tmp):
        """表外 image_style（自由风格"赛博朋克"）→ 透传为"赛博朋克风格"短语。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = self._doc_with_images()
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        node = _make_gen_images_node(None)
        params = {**PARAMS, "image_style": "赛博朋克"}
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen",
                   MagicMock(return_value=fake_gen)):
            asyncio.run(node({"yuwen_params": params, "yuwen_content": doc}))
        prompts = [c[0][0] for c in fake_gen.generate.call_args_list]
        assert prompts and all("赛博朋克风格" in p for p in prompts)

    def test_empty_style_falls_back_default(self, outputs_tmp):
        """空 image_style → 回退默认"绘本"短语。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = self._doc_with_images()
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        node = _make_gen_images_node(None)
        params = {**PARAMS, "image_style": ""}
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen",
                   MagicMock(return_value=fake_gen)):
            asyncio.run(node({"yuwen_params": params, "yuwen_content": doc}))
        prompts = [c[0][0] for c in fake_gen.generate.call_args_list]
        assert prompts and all("儿童绘本风格" in p for p in prompts)

    def test_minimal_truncation_priority(self, outputs_tmp):
        """minimal（默认）：上限 max(2, periods)，封面 > 每课时首页优先。

        4 个候选、periods=1 → 上限 2：s01 封面（rank0）+ s03 第二课时
        首页（rank1）入选；s02（rank2 有 caption）与 s04（rank3）走占位。
        """
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        img = lambda cap="": {"type": "image", "caption": cap, "src": ""}
        doc = {"version": "1.0",
               "meta": {**SAMPLE_OUTLINE["meta"], "periods": 1},
               "slides": [
                   {"id": "s01", "kind": "cover", "title": "静夜思", "period": 1,
                    "elements": [img("封面图")]},
                   {"id": "s02", "kind": "intro", "title": "诗人", "period": 1,
                    "elements": [img("李白像")]},
                   {"id": "s03", "kind": "read", "title": "朗读", "period": 2,
                    "elements": [img("朗读场景")]},
                   {"id": "s04", "kind": "extend", "title": "拓展", "period": 2,
                    "elements": [img("拓展图")]},
               ],
               "lessonPlan": {}, "handout": {"levels": []}}
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        node = _make_gen_images_node(None)
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen",
                   MagicMock(return_value=fake_gen)):
            result = asyncio.run(node({"yuwen_params": PARAMS,
                                       "yuwen_content": doc}))
        srcs = {s["id"]: s["elements"][0]["src"]
                for s in result["yuwen_content"]["slides"]}
        assert srcs["s01"] == "assets/s01_0.png"   # 封面优先
        assert srcs["s03"] == "assets/s03_0.png"   # 第二课时首页优先
        assert srcs["s02"] == "" and srcs["s04"] == ""  # 截断走占位
        assert fake_gen.generate.await_count == 2

    def test_scene_strip_collected_and_ranked(self, outputs_tmp):
        """scene-strip 空 src 也进生图目标；minimal 档优先级仅次于封面
        背景图——同一批候选里四格图解挤掉普通内嵌图。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = {"version": "1.0",
               "meta": {**SAMPLE_OUTLINE["meta"], "periods": 1},
               "slides": [
                   {"id": "s01", "kind": "cover", "title": "静夜思", "period": 1,
                    "elements": [{"type": "image", "src": "", "background": True,
                                  "caption": "月夜窗前"}]},
                   {"id": "s02", "kind": "intro", "title": "诗人", "period": 1,
                    "elements": [{"type": "image", "src": "",
                                  "caption": "李白像"}]},
                   {"id": "s03", "kind": "reading", "title": "情景画卷", "period": 1,
                    "elements": [{"type": "scene-strip", "src": "",
                                  "scenes": [{"caption": "床前明月光"},
                                             {"caption": "疑是地上霜"},
                                             {"caption": "举头望明月"},
                                             {"caption": "低头思故乡"}]}]},
               ],
               "lessonPlan": {}, "handout": {"levels": []}}
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        node = _make_gen_images_node(None)
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen",
                   MagicMock(return_value=fake_gen)):
            result = asyncio.run(node({"yuwen_params": PARAMS,
                                       "yuwen_content": doc}))
        # periods=1 → 上限 2：背景封面(rank0) + 四格图解(rank2) 入选，
        # 普通内嵌图 s02(rank4) 被截断走占位
        assert result["yuwen_content"]["slides"][0]["elements"][0]["src"] \
            == "assets/s01_0.png"
        assert result["yuwen_content"]["slides"][2]["elements"][0]["src"] \
            == "assets/s03_0.png"
        assert result["yuwen_content"]["slides"][1]["elements"][0]["src"] == ""

    def test_prompt_roles_background_and_scene(self, outputs_tmp):
        """prompt 三角色：背景图=横构图压标题指引；四格=田字 2×2 依次入画。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = {"version": "1.0", "meta": SAMPLE_OUTLINE["meta"],
               "slides": [
                   {"id": "s01", "kind": "cover", "title": "静夜思", "period": 1,
                    "elements": [{"type": "image", "src": "", "background": True,
                                  "caption": "月夜窗前"}]},
                   {"id": "s02", "kind": "reading", "title": "情景画卷", "period": 1,
                    "elements": [{"type": "scene-strip", "src": "",
                                  "scenes": [{"caption": "床前明月光"},
                                             {"caption": "疑是地上霜"},
                                             {"caption": "举头望明月"},
                                             {"caption": "低头思故乡"}]}]},
               ],
               "lessonPlan": {}, "handout": {"levels": []}}
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        node = _make_gen_images_node(None)
        params = {**PARAMS, "image_count": "all"}
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen",
                   MagicMock(return_value=fake_gen)):
            asyncio.run(node({"yuwen_params": params, "yuwen_content": doc}))
        prompts = [c[0][0] for c in fake_gen.generate.call_args_list]
        bg = next(p for p in prompts if "背景插画" in p)
        assert "横幅全景构图" in bg and "压标题" in bg
        scene = next(p for p in prompts if "四格连环画" in p)
        assert "田字" in scene and "第1格：床前明月光" in scene
        assert "第4格：低头思故乡" in scene and "画风四格保持一致" in scene

    def test_all_generates_everything(self, outputs_tmp):
        """image_count=all → 不受上限截断，全部生成。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        img = lambda: {"type": "image", "caption": "x", "src": ""}
        doc = {"version": "1.0", "meta": SAMPLE_OUTLINE["meta"],
               "slides": [{"id": f"s0{i}", "kind": "intro", "title": f"页{i}",
                           "period": 1, "elements": [img()]} for i in range(1, 5)],
               "lessonPlan": {}, "handout": {"levels": []}}
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        node = _make_gen_images_node(None)
        with patch("aidraft.agenthub.yuwen.imagegen.ImageGen",
                   MagicMock(return_value=fake_gen)):
            result = asyncio.run(node({"yuwen_params": {**PARAMS,
                                                        "image_count": "all"},
                                       "yuwen_content": doc}))
        assert all(s["elements"][0]["src"]
                   for s in result["yuwen_content"]["slides"])
        assert fake_gen.generate.await_count == 4

    def test_none_skips_generation(self, outputs_tmp):
        """image_count=none → 不调网关，且清理空 src 占位（用户明说不配图）。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _make_gen_images_node
        doc = self._doc_with_images()
        fake_gen = MagicMock()
        fake_gen.available = True
        fake_gen.generate = AsyncMock(return_value=b"\x89PNG")
        frames = []
        node = _make_gen_images_node(lambda f: frames.append(f))
        result = asyncio.run(node({"yuwen_params": {**PARAMS,
                                                    "image_count": "none"},
                                   "yuwen_content": doc}))
        slides = result["yuwen_content"]["slides"]
        assert [e["type"] for e in slides[0]["elements"]] == ["heading"]
        assert slides[1]["elements"][0]["src"] == ""  # 孤元素页豁免
        fake_gen.generate.assert_not_awaited()
        done = [f for f in frames if f.get("status") == "done"]
        assert any("不配图" in f.get("detail", "") for f in done)

    def test_imagegen_unavailable_without_env(self, monkeypatch):
        from aidraft.agenthub.yuwen import imagegen as ig
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        assert ig.ImageGen().available is False
        monkeypatch.delenv("DASHSCOPE_IMAGE_MODEL", raising=False)
        monkeypatch.delenv("DASHSCOPE_IMAGE_BASE", raising=False)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        g = ig.ImageGen()
        assert g.available is True and g._model == "qwen-image-3.0-pro"
        assert "dashscope" in g._base
        monkeypatch.setenv("DASHSCOPE_IMAGE_MODEL", "wan2.7-image")
        assert ig.ImageGen()._model == "wan2.7-image"

    def test_imagegen_native_url_normalize(self, monkeypatch):
        """base 兼容层路径（compatible-mode/v1）自动归一成原生协议 URL。"""
        from aidraft.agenthub.yuwen import imagegen as ig
        assert ig._native_url(
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        ) == ("https://token-plan.cn-beijing.maas.aliyuncs.com"
              "/api/v1/services/aigc/multimodal-generation/generation")
        # 域名根形态原样拼原生路径
        assert ig._native_url(
            "https://dashscope.aliyuncs.com/"
        ).endswith("/api/v1/services/aigc/multimodal-generation/generation")

    def test_compress_image_resizes_to_jpeg(self):
        """2048px PNG → 长边 ≤1600 JPEG（体积显著下降，扩展名跟着实走）。"""
        import io

        from PIL import Image as PILImage
        from aidraft.agenthub.yuwen.nodes.gen_images import _compress_image
        buf = io.BytesIO()
        PILImage.new("RGB", (2048, 1536), (30, 60, 90)).save(buf, format="PNG")
        data, ext = _compress_image(buf.getvalue())
        assert ext == "jpg"
        im = PILImage.open(io.BytesIO(data))
        assert max(im.size) <= 1600
        assert im.format == "JPEG"
        assert len(data) < len(buf.getvalue())

    def test_compress_image_keeps_small_undersized(self):
        """1024px 原图（≤上限）只转码不放大；非 RGB（带 alpha）也能压。"""
        import io

        from PIL import Image as PILImage
        from aidraft.agenthub.yuwen.nodes.gen_images import _compress_image
        buf = io.BytesIO()
        PILImage.new("RGBA", (1024, 1024), (200, 30, 30, 128)).save(
            buf, format="PNG")
        data, ext = _compress_image(buf.getvalue())
        assert ext == "jpg"
        im = PILImage.open(io.BytesIO(data))
        assert im.size == (1024, 1024)

    def test_compress_image_garbage_falls_back(self):
        """非图片字节 → 回退原始 bytes + png（压缩失败不炸生图链路）。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _compress_image
        data, ext = _compress_image(b"not an image at all")
        assert data == b"not an image at all" and ext == "png"

    def test_prune_empty_images_rules(self):
        """删除规则：多元素页空 image 删；background/孤元素页/已回填不碰。"""
        from aidraft.agenthub.yuwen.nodes.gen_images import _prune_empty_images
        doc = {"slides": [
            # s01：heading + 空 image → image 删除
            {"id": "s01", "elements": [
                {"type": "heading", "content": "t"},
                {"type": "image", "src": "", "caption": "c"}]},
            # s02：唯一元素是空 image → 豁免（删光过不了 validate）
            {"id": "s02", "elements": [{"type": "image", "src": ""}]},
            # s03：空 background image（封面全出血）→ 豁免（渲染有底色兜底）
            {"id": "s03", "elements": [
                {"type": "heading", "content": "t"},
                {"type": "image", "src": "", "background": True}]},
            # s04：有 src 的 image 与非 image 类型 → 原样保留
            {"id": "s04", "elements": [
                {"type": "image", "src": "assets/a.jpg"},
                {"type": "scene-strip", "src": ""},
                {"type": "paragraph", "content": "p"}]},
        ]}
        removed = _prune_empty_images(doc["slides"])
        assert removed == 1
        assert [e["type"] for e in doc["slides"][0]["elements"]] == ["heading"]
        assert len(doc["slides"][1]["elements"]) == 1
        assert len(doc["slides"][2]["elements"]) == 2
        assert len(doc["slides"][3]["elements"]) == 3


# ======================================================================
# 11. extract_params 节点（沿用）
# ======================================================================

class TestExtractParams:
    """extract_params 节点：LLM 参数提取与追问。"""

    def test_extract_params_full_parsed(self):
        from aidraft.agenthub.yuwen.nodes.extract_params import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思", "grade": 1, "lesson_type": "古诗词",
            "textbook": "部编版一年级下册",
            "image_style": "绘本", "image_count": "minimal",
            "params_ready": True, "question": "", "chips": [],
        }, ensure_ascii=False))

        frames = []
        node = _make_extract_params_node(mock_gw, lambda f: frames.append(f))
        result = asyncio.run(node({
            "task": "帮我做《静夜思》的课件",
            "user_message": "帮我做《静夜思》的课件", "messages": []}))

        assert result["yuwen_params_ready"] is True
        assert result["yuwen_params"]["title"] == "静夜思"
        step_frames = [f for f in frames if f.get("type") == "step"]
        assert len(step_frames) == 2  # running + done

    def test_extract_params_missing_grade(self):
        from aidraft.agenthub.yuwen.nodes.extract_params import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思", "grade": 0, "lesson_type": "", "textbook": "",
            "params_ready": False, "question": "请提供年级和课型",
            "chips": ["一年级 古诗词", "二年级 精读"],
        }, ensure_ascii=False))

        frames = []
        node = _make_extract_params_node(mock_gw, lambda f: frames.append(f))
        result = asyncio.run(node({
            "task": "做《静夜思》", "user_message": "做《静夜思》", "messages": []}))

        assert result["yuwen_params_ready"] is False
        content_frames = [f for f in frames if f.get("type") == "content"]
        assert content_frames and "年级" in content_frames[0].get("delta", "")

    def test_extract_params_llm_failure(self):
        from aidraft.agenthub.yuwen.nodes.extract_params import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = RuntimeError("API 不可用")
        node = _make_extract_params_node(mock_gw, None)
        result = asyncio.run(node({
            "task": "帮我做课件", "user_message": "帮我做课件", "messages": []}))
        assert result["yuwen_params_ready"] is False

    def test_extract_params_gateway_called_with_json_mode(self):
        from aidraft.agenthub.yuwen.nodes.extract_params import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思", "grade": 1, "lesson_type": "古诗词",
            "textbook": "部编版", "params_ready": True, "question": "", "chips": [],
        }))
        node = _make_extract_params_node(mock_gw, None)
        asyncio.run(node({
            "task": "静夜思 一年级", "user_message": "静夜思 一年级", "messages": []}))
        _, kwargs = mock_gw.chat.call_args
        assert kwargs.get("json_mode") is True

    def test_extract_params_image_prefs(self):
        """首轮抽到配图偏好 → 进 yuwen_params；没提就不写键。"""
        from aidraft.agenthub.yuwen.nodes.extract_params import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思", "grade": 1, "lesson_type": "古诗词",
            "textbook": "部编版", "image_style": "水彩", "image_count": "none",
            "params_ready": True, "question": "", "chips": [],
        }, ensure_ascii=False))
        node = _make_extract_params_node(mock_gw, None)
        result = asyncio.run(node({
            "task": "做《静夜思》，配图用水彩，不要插图",
            "user_message": "做《静夜思》，配图用水彩，不要插图", "messages": []}))
        assert result["yuwen_params"]["image_style"] == "水彩"
        assert result["yuwen_params"]["image_count"] == "none"

        # 风格开放透传：表外风格（用户原话）原样收进 params
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思", "grade": 1, "lesson_type": "古诗词",
            "textbook": "部编版", "image_style": "抽象派", "image_count": "lots",
            "params_ready": True, "question": "", "chips": [],
        }, ensure_ascii=False))
        result = asyncio.run(node({
            "task": "做《静夜思》", "user_message": "做《静夜思》", "messages": []}))
        assert result["yuwen_params"]["image_style"] == "抽象派"
        # 数量档仍是三档枚举：非法值丢弃
        assert "image_count" not in result["yuwen_params"]

    def _extract_node(self, mock_gw):
        from aidraft.agenthub.yuwen.nodes.extract_params import \
            _make_extract_params_node
        return _make_extract_params_node(mock_gw, None)

    def _params_ready_json(self, **extra):
        base = {"title": "静夜思", "grade": 1, "lesson_type": "古诗词",
                "textbook": "部编版", "params_ready": True,
                "question": "", "chips": []}
        base.update(extra)
        return json.dumps(base, ensure_ascii=False)

    def test_image_ask_round(self, outputs_tmp):
        """齐参数但没提配图 → 不直接放行：追问一轮 + 盘上记询问标记。"""
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(self._params_ready_json())
        frames = []
        from aidraft.agenthub.yuwen.nodes.extract_params import \
            _make_extract_params_node
        node = _make_extract_params_node(mock_gw, lambda f: frames.append(f))
        result = asyncio.run(node({
            "task": "静夜思 一年级 古诗词",
            "user_message": "静夜思 一年级 古诗词", "messages": []}))

        assert result["yuwen_params_ready"] is False
        assert "配图" in result["final_answer"]
        content = [f for f in frames if f.get("type") == "content"]
        assert content and "配图" in content[0]["delta"]
        assert content[0].get("chips")  # 快捷选项带着
        # 防循环标记落盘
        from aidraft.agenthub.yuwen.state import _load_state
        disk = _load_state(PARAMS)
        assert disk.get("yuwen_image_asked") is True
        # params 已落盘：下一轮 _route_after_params 查盘判缺 prefs → END 等回复

    def test_image_ask_second_round_passes_even_default(self, outputs_tmp):
        """第二轮回"默认"没抽到偏好 → 放行（问过就绝不二次追问）。"""
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_params=PARAMS, yuwen_image_asked=True)
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(self._params_ready_json())
        node = self._extract_node(mock_gw)
        result = asyncio.run(node({
            "task": "默认", "user_message": "默认", "messages": []}))
        assert result["yuwen_params_ready"] is True

    def test_image_ask_reply_not_routed_to_confirm(self, outputs_tmp):
        """询问轮回"不要配图"（含大纲指令词 _IMAGE_WORDS）：research 未跑、
        盘上无 outline → _find_pending_session 无从命中，仍走 extract_params
        第二轮放行，不会被 confirm 兜底劫持。"""
        from aidraft.agenthub.yuwen import graph as gr
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_params=PARAMS, yuwen_image_asked=True)
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            self._params_ready_json(image_count="none"))
        node = self._extract_node(mock_gw)
        result = asyncio.run(node({
            "task": "不要配图", "user_message": "不要配图", "messages": []}))
        assert result["yuwen_params_ready"] is True
        assert result["yuwen_params"]["image_count"] == "none"
        # 放行后路由：盘上无大纲 → research（正常首轮），不是 confirm
        with patch("aidraft.agenthub.yuwen.graph._find_pending_session",
                   return_value=None):
            got = gr._route_after_params({
                "yuwen_params_ready": True,
                "yuwen_params": result["yuwen_params"]})
        assert got == "research"

    def test_image_ask_second_round_merges_disk_prefs(self, outputs_tmp):
        """第二轮用户答"水彩" → 本轮抽取值 + 盘上旧 params 合并放行。"""
        from aidraft.agenthub.yuwen.state import _save_state
        _save_state(PARAMS, yuwen_params=PARAMS, yuwen_image_asked=True)
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            self._params_ready_json(image_style="水彩"))
        node = self._extract_node(mock_gw)
        result = asyncio.run(node({
            "task": "水彩", "user_message": "水彩", "messages": []}))
        assert result["yuwen_params_ready"] is True
        assert result["yuwen_params"]["image_style"] == "水彩"

    def test_image_ask_skipped_when_prefs_present(self, outputs_tmp):
        """首轮就说了偏好 → 不触发询问轮，直接放行。"""
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(
            self._params_ready_json(image_style="国风", image_count="all"))
        node = self._extract_node(mock_gw)
        result = asyncio.run(node({
            "task": "静夜思 一年级 古诗词 国风全配",
            "user_message": "静夜思 一年级 古诗词 国风全配", "messages": []}))
        assert result["yuwen_params_ready"] is True
        assert result["yuwen_params"]["image_count"] == "all"
        from aidraft.agenthub.yuwen.state import _load_state
        assert not _load_state(PARAMS).get("yuwen_image_asked")


# ======================================================================
# 12. _call_llm provider 签名过滤
# ======================================================================

class TestCallLlm:
    """_call_llm：方法能接 provider 才传，否则静默降级默认链。"""

    def test_real_chat_signature_drops_provider(self):
        from aidraft.agenthub.yuwen.nodes._page import _call_llm
        gw = MagicMock()

        # 模拟真实 Gateway.chat：无 **kwargs，显式参数里没有 provider
        def chat(messages, *, temperature=0.7, json_mode=False,
                 tools=None, tool_choice=None, use_cache=False):
            return _chat_response("{}")

        gw.chat = MagicMock(side_effect=chat)
        gw.chat.__signature__ = None  # 用 side_effect 函数签名不行，手工建 stub：
        # 改用普通函数对象直接测（绕过 MagicMock 签名恒真）
        class StubGw:
            def chat(self, messages, *, temperature=0.7, json_mode=False,
                     tools=None, tool_choice=None, use_cache=False):
                return _chat_response("{}")
        stub = StubGw()
        # 不抛 TypeError 即正确丢弃了 provider
        resp = _call_llm(stub, "chat", [], {"provider": "deepseek",
                                            "model": "x"}, temperature=0.2)
        assert resp.content == "{}"

    def test_stream_passes_provider(self):
        from aidraft.agenthub.yuwen.nodes._page import _call_llm
        gw = MagicMock()
        captured = {}

        async def stream_chat(messages, *, provider="", model="",
                              temperature=0.7, tools=None, tool_choice=None):
            captured["provider"] = provider
            yield _chunk(done=True)

        gw.stream_chat = stream_chat

        async def _go():
            async for _ in _call_llm(gw, "stream_chat", [],
                                     {"provider": "ollama", "model": "q"},
                                     temperature=0.3):
                pass
            return captured
        got = asyncio.run(_go())
        assert got["provider"] == "ollama"


# ======================================================================
# 13. render_all 纯 Python 渲染测试
# ======================================================================

class TestRenderAll:
    """render_all.py 纯 Python 渲染（无 LLM 依赖）。"""

    def test_render_jingyesi_exit_0(self):
        import subprocess
        base = _SRC / "aidraft" / "agenthub" / "yuwen"
        script = base / "scripts" / "render_all.py"
        json_path = base / "references" / "examples" / "jingyesi.json"
        out_dir = _PROJECT_ROOT / "outputs" / "test_yuwen" / "jingyesi"

        result = subprocess.run(
            [sys.executable, str(script), str(json_path), "--out", str(out_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        files = list(out_dir.glob("*"))
        exts = [f.suffix for f in files]
        assert ".pptx" in exts, f"missing pptx in {exts}"
        assert ".html" in exts, f"missing html in {exts}"
        assert ".docx" in exts, f"missing docx in {exts}"

        import shutil
        shutil.rmtree(out_dir.parent, ignore_errors=True)

    def test_render_zuojing_exit_0(self):
        import subprocess
        base = _SRC / "aidraft" / "agenthub" / "yuwen"
        script = base / "scripts" / "render_all.py"
        json_path = base / "references" / "examples" / "zuojing-guantian.json"
        out_dir = _PROJECT_ROOT / "outputs" / "test_yuwen" / "zuojing"

        result = subprocess.run(
            [sys.executable, str(script), str(json_path), "--out", str(out_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        files = list(out_dir.glob("*"))
        exts = [f.suffix for f in files]
        assert ".pptx" in exts
        assert ".html" in exts
        assert ".docx" in exts

        import shutil
        shutil.rmtree(out_dir.parent, ignore_errors=True)

    def test_render_check_deps(self):
        import subprocess
        script = str(_SRC / "aidraft" / "agenthub" / "yuwen" / "scripts" / "check_deps.py")
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_render_nonexistent_json(self):
        import subprocess
        script = str(_SRC / "aidraft" / "agenthub" / "yuwen" / "scripts" / "render_all.py")
        result = subprocess.run(
            [sys.executable, script, "/nonexistent/path.json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        assert result.returncode == 2


# ======================================================================
# 14. 共通 schema 测试（沿用阶段 1）
# ======================================================================

class TestSchema:
    """common.schema 校验。"""

    def test_validate_valid_doc(self):
        from aidraft.agenthub.yuwen.scripts.common.schema import validate

        doc = {
            "meta": {"title": "静夜思", "grade": 1, "lessonType": "古诗词"},
            "slides": [{"id": "s01", "kind": "cover", "title": "静夜思",
                        "period": 1,
                        "elements": [{"type": "heading", "content": "静夜思",
                                      "size": "h1"}]}],
        }
        result = validate(doc)
        assert result["meta"]["stage"] == "低段"
        assert result["meta"]["periods"] == 2  # 古诗词默认 2 课时

    def test_validate_missing_meta_raises(self):
        from aidraft.agenthub.yuwen.scripts.common.schema import validate, SchemaError
        with pytest.raises(SchemaError):
            validate({})

    def test_validate_empty_handout_raises(self):
        """handout={} 过不了校验（levels 必须数组）——gen_slides 占位的依据。"""
        from aidraft.agenthub.yuwen.scripts.common.schema import validate, SchemaError
        with pytest.raises(SchemaError):
            validate({"meta": {"title": "t", "grade": 1, "lessonType": "精读"},
                      "slides": [{"id": "s01", "elements": []}],
                      "handout": {}})

    def test_validate_invalid_lesson_type(self):
        from aidraft.agenthub.yuwen.scripts.common.schema import validate, SchemaError
        with pytest.raises(SchemaError):
            validate({
                "meta": {"title": "静夜思", "grade": 1, "lessonType": "非法课型"},
                "slides": [{"id": "s01", "elements": [{"type": "heading", "content": "x", "size": "h1"}]}],
            })

    def test_validate_unknown_element_type(self):
        from aidraft.agenthub.yuwen.scripts.common.schema import validate, SchemaError
        with pytest.raises(SchemaError):
            validate({
                "meta": {"title": "静夜思", "grade": 1, "lessonType": "古诗词"},
                "slides": [{"id": "s01", "elements": [{"type": "unknown_type"}]}],
            })

    def test_normalize_real_failure_doc(self):
        """线上失败案例：《静夜思》模型输出偏差经 normalize 后通过校验。"""
        from aidraft.agenthub.yuwen.scripts.common.schema import normalize, validate

        doc = {
            "version": "1.0.0",
            "meta": {
                "title": "静夜思", "grade": 2, "subject": "语文",
                "textbook": "部编版二年级上册", "lessonType": "古诗词",
                "period": 1, "totalPeriods": 1,
                "coreCompetencies": ["语言建构与运用", "思维发展与提升"],
            },
            "slides": [
                {
                    "id": 1, "type": "cover", "title": "静夜思", "subtitle": "——李白",
                    "layout": "center",
                    "elements": [
                        {"type": "text", "content": "静夜思", "style": {"fontSize": 48}},
                        {"type": "text", "content": "唐·李白", "style": {"fontSize": 24}},
                        {"type": "image", "src": "moon_night", "alt": "明月夜图"},
                    ],
                },
                {
                    "id": 5, "type": "word-learning", "title": "生字学习（一）",
                    "elements": [
                        {"type": "word-card", "content": "静", "pinyin": "jìng",
                         "stroke": 14, "structure": "左右结构", "example": "安静、宁静"},
                        {"type": "word-card", "content": "夜", "pinyin": "yè",
                         "stroke": 8, "structure": "上下结构", "example": "夜晚、黑夜"},
                    ],
                },
                {
                    "id": 11, "type": "discussion", "title": "互动讨论",
                    "elements": [
                        {"type": "question", "content": "诗人为什么看到月亮就会想起故乡？"},
                        {"type": "text", "content": "提示：月亮代表团圆和思念。"},
                    ],
                },
                {
                    "id": 4, "type": "poem-reading", "title": "初读古诗",
                    "elements": [
                        {"type": "text", "content": "床前明月光，疑是地上霜。"},
                        {"type": "audio", "src": "poem_recitation", "alt": "古诗朗读"},
                    ],
                },
            ],
            "lessonPlan": {"teachingObjectives": [], "teachingProcess": []},
            "handout": {
                "title": "学习单",
                "content": [
                    {"section": "我会读", "items": ["静夜思", "床前"]},
                    {"section": "我会背", "items": ["床前明月光，疑是地上霜。"]},
                ],
            },
        }
        doc = normalize(doc)
        validate(doc)  # 不抛即通过

        types = [el["type"] for s in doc["slides"] for el in s["elements"]]
        assert "text" not in types and "question" not in types and "audio" not in types
        cards = [el for s in doc["slides"] for el in s["elements"]
                 if el["type"] == "word-card"]
        assert len(cards) == 1
        assert [c["char"] for c in cards[0]["cards"]] == ["静", "夜"]
        assert all("kind" in s for s in doc["slides"])
        assert [l["level"] for l in doc["handout"]["levels"]] == ["我会读", "我会背"]

    def test_normalize_meta_value_variants(self):
        """meta 值域归一化：grade 中文数字、lessonType 变体、competency 旧名。"""
        from aidraft.agenthub.yuwen.scripts.common.schema import normalize, validate

        doc = {
            "meta": {
                "title": "静夜思",
                "grade": "一",
                "lessonType": "古诗",
                "objectives": [
                    "认识9个生字",
                    {"content": "发展思维，学会比较", "competency": "思维发展与提升"},
                    {"content": "感受古诗的韵律美"},
                ],
            },
            "slides": [{"id": "s01", "kind": "cover", "title": "页", "period": 1,
                        "elements": [{"type": "heading", "content": "静", "size": "h1"}]}],
        }
        doc = normalize(doc)
        validate(doc)
        assert doc["meta"]["grade"] == 1
        assert doc["meta"]["lessonType"] == "古诗词"
        objs = doc["meta"]["objectives"]
        assert objs[0]["competency"] == "语言运用"
        assert objs[1]["competency"] == "思维能力"
        assert objs[2]["competency"] == "审美创造"

    def test_pinyin_split(self):
        from aidraft.agenthub.yuwen.scripts.common.pinyin import split_syllables

        pairs = split_syllables("静夜思", "jìng yè sī")
        assert len(pairs) == 3
        assert pairs[0][0] == "静"
        assert pairs[0][2] == 4
        assert pairs[2][2] == 1

    def test_tone_color_mapping(self):
        from aidraft.agenthub.yuwen.scripts.common.pinyin import tone_color
        assert tone_color(1) == "D9534F"
        assert tone_color(4) == "5B8AB5"
        assert tone_color(0) == "9AA0A6"


# ======================================================================
# 15. 图集成测试（mock LLM，跨轮状态机全流程）
# ======================================================================

class TestGraphIntegration:
    """盘上已确认 → astream 全链（extract→route→slides→plan→review→images→render→report）。"""

    def _drive(self, mock_gw, user_msg="确认"):
        from aidraft.agenthub.yuwen.graph import build_graph
        frames = []
        graph = build_graph(gateway=mock_gw, registry=MagicMock(),
                            emitter=lambda f: frames.append(f))
        final = {}

        async def _run():
            async for chunk in graph.astream({
                "task": user_msg, "user_message": user_msg, "messages": [],
                "yuwen_params": PARAMS, "yuwen_params_ready": True}):
                for _nid, update in chunk.items():
                    if isinstance(update, dict):
                        final.update(update)
        asyncio.run(_run())
        return final, frames

    def _mock_confirmed_full_chain(self, pages: int = 2):
        """chat：按 system 标记分发（extract/plan/review/revise）；stream：逐页。"""
        outline = {**SAMPLE_OUTLINE,
                   "pages": SAMPLE_OUTLINE["pages"][:pages]}
        mock_gw = MagicMock()

        def _chat(msgs, **kw):
            system = msgs[0].content
            if "参数提取助手" in system:
                return _chat_response(json.dumps({
                    "title": PARAMS["title"], "grade": PARAMS["grade"],
                    "lesson_type": PARAMS["lesson_type"],
                    "textbook": PARAMS["textbook"],
                    "image_style": "绘本", "image_count": "minimal",
                    "params_ready": True,
                    "question": "", "chips": []}, ensure_ascii=False))
            if "教研助手" in system:
                return _chat_response(json.dumps(PLAN_JSON, ensure_ascii=False))
            if "审查专家" in system:
                return _chat_response(json.dumps(REVIEW_PASS, ensure_ascii=False))
            # revise（修订助手）兜底：返回替换页内容
            return _chat_response(json.dumps(_page_json(3), ensure_ascii=False))

        def _stream(msgs, **kw):
            # user_prompt 里"本页条目"带 {"id": "s0X"…}，按页号产对应 mock 页
            user = msgs[1].content
            i = user.find('"id": "s0')
            idx = int(user[i + 9]) if i >= 0 else 1
            return _AsyncIter([_chunk(delta=json.dumps(_page_json(idx))),
                               _chunk(done=True)])

        mock_gw.chat.side_effect = _chat
        mock_gw.stream_chat.side_effect = _stream
        return mock_gw, outline

    def test_full_chain_confirmed(self, outputs_tmp):
        from aidraft.agenthub.yuwen.state import _save_state
        mock_gw, outline = self._mock_confirmed_full_chain()
        _save_state(PARAMS, yuwen_outline=outline,
                    yuwen_outline_confirmed=True, yuwen_params=PARAMS)
        # mock render 子进程（真渲染由 TestRenderAll 覆盖）
        fake = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("aidraft.agenthub.yuwen.nodes.render.subprocess.run",
                   return_value=fake):
            final, frames = self._drive(mock_gw)

        assert len(final["yuwen_content"]["slides"]) == 2
        assert final["yuwen_review"]["pass"] is True
        assert final["yuwen_content"]["lessonPlan"]["title"] == "静夜思"
        # done 帧由 report 发（单 done）
        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1
        visited = final["nodes_visited"]
        for n in ("extract_params", "gen_slides", "gen_plan", "review",
                  "gen_images", "render", "report"):
            assert n in visited, f"missing {n} in {visited}"

    def test_first_round_stops_at_outline(self, outputs_tmp):
        """首轮（盘上无大纲）：extract → gen_outline → END，出 outline 帧。"""
        from aidraft.agenthub.yuwen.graph import build_graph
        mock_gw = MagicMock()

        def _chat(msgs, **kw):
            system = msgs[0].content
            if "参数提取助手" in system:
                return _chat_response(json.dumps({
                    "title": PARAMS["title"], "grade": PARAMS["grade"],
                    "lesson_type": PARAMS["lesson_type"],
                    "textbook": PARAMS["textbook"],
                    "image_style": "绘本", "image_count": "minimal",
                    "params_ready": True,
                    "question": "", "chips": []}, ensure_ascii=False))
            return _chat_response(json.dumps(SAMPLE_OUTLINE, ensure_ascii=False))

        mock_gw.chat.side_effect = _chat
        frames = []
        graph = build_graph(gateway=mock_gw, registry=MagicMock(),
                            emitter=lambda f: frames.append(f))
        final = {}

        async def _run():
            async for chunk in graph.astream({
                "task": "静夜思 一年级 古诗词", "user_message": "静夜思 一年级 古诗词",
                "messages": [], "yuwen_params": PARAMS,
                "yuwen_params_ready": True}):
                for _nid, update in chunk.items():
                    if isinstance(update, dict):
                        final.update(update)
        asyncio.run(_run())

        assert any(f.get("type") == "outline" for f in frames)
        assert final["yuwen_outline_confirmed"] is False
        # stream_chat（逐页）没被调用——本轮在大纲处 END
        mock_gw.stream_chat.assert_not_called()

    def test_confirm_round_then_slides(self, outputs_tmp):
        """第二轮"确认"：extract → confirm（放行）→ gen_slides → … → report。"""
        from aidraft.agenthub.yuwen.state import _save_state
        mock_gw, outline = self._mock_confirmed_full_chain()
        _save_state(PARAMS, yuwen_outline=outline,
                    yuwen_outline_confirmed=False, yuwen_params=PARAMS)
        fake = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("aidraft.agenthub.yuwen.nodes.render.subprocess.run",
                   return_value=fake):
            final, frames = self._drive(mock_gw, user_msg="确认")
        assert final["yuwen_outline_confirmed"] is True
        assert len(final["yuwen_content"]["slides"]) == 2

    def test_review_revise_loop_second_round_pass(self, outputs_tmp):
        """图级闭环：首轮 review 不 pass → revise 修页 → 二轮 pass → 放行。"""
        from aidraft.agenthub.yuwen.graph import build_graph
        from aidraft.agenthub.yuwen.state import _save_state
        mock_gw, outline = self._mock_confirmed_full_chain()
        _save_state(PARAMS, yuwen_outline=outline,
                    yuwen_outline_confirmed=True, yuwen_params=PARAMS)

        issue_review = {"scores": {"structure": 4, "pedagogy": 4, "content": 2,
                                   "stage_fit": 4},
                        "issues": [{"page_id": "s01", "problems": ["内容空泛"]}],
                        "pass": False}
        orig_chat = mock_gw.chat.side_effect
        review_calls = []

        def _chat(msgs, **kw):
            # 注意区分：revise 的 system 里也提到"质量审查"，用"审查专家"精确匹配
            if "审查专家" in msgs[0].content:
                review_calls.append(1)
                payload = issue_review if len(review_calls) == 1 else REVIEW_PASS
                return _chat_response(json.dumps(payload, ensure_ascii=False))
            return orig_chat(msgs, **kw)

        mock_gw.chat.side_effect = _chat
        fake = MagicMock(returncode=0, stdout="ok", stderr="")
        frames = []
        graph = build_graph(gateway=mock_gw, registry=MagicMock(),
                            emitter=lambda f: frames.append(f))
        final = {}

        async def _run():
            async for chunk in graph.astream({
                "task": "确认", "user_message": "确认", "messages": [],
                "yuwen_params": PARAMS, "yuwen_params_ready": True}):
                for _nid, update in chunk.items():
                    if isinstance(update, dict):
                        final.update(update)
        with patch("aidraft.agenthub.yuwen.nodes.render.subprocess.run",
                   return_value=fake):
            asyncio.run(_run())

        assert len(review_calls) == 2, "review 应跑两轮"
        assert final["yuwen_revise_rounds"] == 1
        assert final["yuwen_review"]["pass"] is True
        assert any(f.get("id") == "revise" for f in frames if f.get("type") == "step")
        # 修订后的 s01 是 revise mock 返回的第 3 页内容
        s01 = next(s for s in final["yuwen_content"]["slides"] if s["id"] == "s01")
        assert s01["elements"][0]["content"] == "页3"
