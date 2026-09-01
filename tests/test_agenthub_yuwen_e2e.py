"""语文智能体端点级端到端测试：TestClient + mock gateway 覆盖 SSE 帧序列。

测试覆盖（阶段 2a 多阶段管线）：
1. 缺参轮：agent=yuwen 发送"帮我做课件" → content 含追问 + 单 done 无双 done
2. 首轮（参数齐、盘上无大纲）：extract → gen_outline → END，outline 帧 + 单 done
3. 确认轮（盘上有未确认大纲）：extract → confirm → gen_slides → gen_plan →
   review → gen_images → render → report，files 帧 + 单 done
4. 修改轮：盘上有大纲 + 改纲指令 → 重发 outline 帧，不进入生成
5. grade=2.0 / grade="2" 变体能放行到 gen_outline
6. 未知 agent 返回 error 帧

注意：state._OUTPUTS_DIR patch 到 tmp（盘上大纲、tmp_content.json、fake
渲染产物全部隔离）；subprocess patch 点仍在 nodes/render.py（机制不变）。

运行：
    pytest tests/test_agenthub_yuwen_e2e.py -x -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))


# ======================================================================
# mock 数据
# ======================================================================

_PARAMS_JSON = {
    "title": "静夜思",
    "grade": 1,
    "lesson_type": "古诗词",
    "textbook": "部编版一年级下册",
    "params_ready": True,
    "question": "",
    "chips": [],
}

_OUTLINE_JSON = {
    "pages": [
        {"id": "s01", "kind": "cover", "title": "静夜思", "period": 1,
         "points": "配乐范读，整体感知"},
        {"id": "s02", "kind": "intro", "title": "诗人背景", "period": 1,
         "points": "李白简介"},
        {"id": "s03", "kind": "read-rhythm", "title": "初读节奏", "period": 1,
         "points": "停顿划分"},
    ],
    "meta": {"title": "静夜思", "grade": 1, "lessonType": "古诗词",
             "textbook": "部编版一年级下册", "periods": 1, "theme": "default"},
}


def _page_json(i: int, kind: str = "cover"):
    return {"id": f"s0{i}", "kind": kind, "title": f"标题{i}", "period": 1,
            "elements": [{"type": "heading", "content": f"页{i}", "size": "h1"}]}


_PLAN_JSON = {
    "lessonPlan": {"title": "静夜思",
                   "teachingProcess": [{"phase": "一、导入", "duration": "5分钟",
                                        "activities": [{"teacher": "看图",
                                                        "student": "观察"}],
                                        "design": "情境"}]},
    "handout": {"levels": [{"level": "基础", "items": ["背诵"]}]},
}

_REVIEW_PASS = {"scores": {"structure": 5, "pedagogy": 4, "content": 4,
                           "stage_fit": 5},
                "issues": [], "pass": True}


# ======================================================================
# 辅助
# ======================================================================

def _parse_sse(text: str) -> list[dict]:
    """解析 SSE 文本为帧列表。"""
    frames = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            try:
                frames.append(json.loads(line[6:]))
            except (json.JSONDecodeError, IndexError):
                continue
    return frames


def _chat_response(content: str, finish_reason: str = "stop"):
    from aidraft.gateway import ChatResponse
    return ChatResponse(content=content, provider="test", model="test",
                        latency_ms=100, finish_reason=finish_reason)


def _make_chat_dispatch():
    """按 system 提示词标记分发 chat 响应（extract/outline/plan/review/revise）。"""
    def _chat(msgs, **kw):
        system = msgs[0].content
        if "参数提取助手" in system:
            return _chat_response(json.dumps(_PARAMS_JSON, ensure_ascii=False))
        if "大纲设计助手" in system:
            return _chat_response(json.dumps(_OUTLINE_JSON, ensure_ascii=False))
        if "教研助手" in system:
            return _chat_response(json.dumps(_PLAN_JSON, ensure_ascii=False))
        if "审查专家" in system:
            return _chat_response(json.dumps(_REVIEW_PASS, ensure_ascii=False))
        # 兜底：revise/改纲等返回修订页
        return _chat_response(json.dumps(_page_json(3), ensure_ascii=False))
    return _chat


async def _page_stream(msgs, **kwargs):
    """逐页 stream：从 user prompt 解析本页 id，返回对应单页 JSON。"""
    user = msgs[1].content
    i = user.find('"id": "s0')
    idx = int(user[i + 9]) if i >= 0 else 1
    from aidraft.gateway import ChatChunk
    yield ChatChunk(delta=json.dumps(_page_json(idx), ensure_ascii=False))
    yield ChatChunk(done=True)


# ======================================================================
# Fixture
# ======================================================================

@pytest.fixture
def client(tmp_path, monkeypatch):
    """mock 网关 TestClient + state 目录隔离到 tmp。

    注意 patch 的是 state._OUTPUTS_DIR 模块属性（节点运行时查找），
    api.py 的 OUTPUTS_DIR 只在 /files 下载路径展示用，测试不断言其内容。
    """
    from aidraft.agenthub.yuwen import state as yuwen_state
    from aidraft.web.api import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(yuwen_state, "_OUTPUTS_DIR", tmp_path)

    with patch("aidraft.gateway.build_default_gateway") as mock_build:
        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _make_chat_dispatch()

        async def _empty_stream(*args, **kwargs):
            for _ in ():
                yield
        mock_gw.stream_chat = _empty_stream
        mock_build.return_value = mock_gw

        with TestClient(app) as c:
            yield c, mock_gw, tmp_path


def _seed_outline_on_disk(tmp_path, confirmed: bool):
    """盘上预置大纲（模拟上一轮 gen_outline / confirm 的结果）。

    本地自建 params（不经 _PARAMS_JSON 的 "title" 键），保证 _session_name
    派生的目录名与后续 fixture 用例里真实 extract 出的 params 一致，
    confirm/gen_slides/render 都定位到同一会话。
    """
    from aidraft.agenthub.yuwen import state as yuwen_state
    params = {"title": "静夜思", "grade": 1, "lesson_type": "古诗词",
              "textbook": "部编版一年级下册"}
    yuwen_state._save_state(params,
                            yuwen_outline=_OUTLINE_JSON,
                            yuwen_outline_confirmed=confirmed,
                            yuwen_params=params)


# ======================================================================
# E2E 测试
# ======================================================================

class TestYuwenEndpoints:
    """语文智能体端到端 SSE 帧序列验证（多阶段管线）。"""

    def test_missing_params_round(self, client):
        """缺参轮：content 含追问 + 单 done 无双 done。"""
        c, mock_gw, _ = client
        mock_gw.chat.side_effect = None
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思", "grade": 0, "lesson_type": "", "textbook": "",
            "params_ready": False,
            "question": "请问课文是几年级的？需要什么课型？",
            "chips": ["一年级 古诗词", "二年级 精读"],
        }, ensure_ascii=False))

        resp = c.post("/api/chat", json={
            "prompt": "帮我做《静夜思》的课件", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        meta_frames = [f for f in frames if f.get("type") == "agent_meta"]
        assert len(meta_frames) == 1
        assert meta_frames[0]["agent_id"] == "yuwen"

        step_frames = [f for f in frames if f.get("type") == "step"]
        extract_running = [f for f in step_frames
                           if f.get("id") == "extract_params" and f.get("status") == "running"]
        extract_done = [f for f in step_frames
                        if f.get("id") == "extract_params" and f.get("status") == "done"]
        assert len(extract_running) == 1
        assert len(extract_done) == 1

        content_frames = [f for f in frames if f.get("type") == "content"]
        assert len(content_frames) == 1
        assert "年级" in content_frames[0].get("delta", "")
        assert len(content_frames[0]["chips"]) > 0

        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1, f"期望单 done，实际 {len(done_frames)}"
        assert not [f for f in frames if f.get("type") == "files"]
        # 大纲还没生成
        assert not [f for f in frames if f.get("type") == "outline"]

    def test_first_round_outline(self, client):
        """首轮（盘上无大纲）：extract → gen_outline → END，outline 帧 + 单 done。"""
        c, mock_gw, tmp_path = client
        resp = c.post("/api/chat", json={
            "prompt": "静夜思 一年级 古诗词", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        step_ids = {(f.get("id"), f.get("status")) for f in frames
                    if f.get("type") == "step"}
        assert ("extract_params", "done") in step_ids
        assert ("gen_outline", "running") in step_ids
        assert ("gen_outline", "done") in step_ids

        outline_frames = [f for f in frames if f.get("type") == "outline"]
        assert len(outline_frames) == 1
        assert len(outline_frames[0]["outline"]["pages"]) == 3
        assert any("确认" in ch for ch in outline_frames[0]["chips"])

        # 兼容旧前端：至少一条人类可读摘要
        content_frames = [f for f in frames if f.get("type") == "content"]
        assert content_frames and "3 页" in content_frames[0]["delta"]

        # 本轮 END：没进生成/渲染
        assert not any(i == "gen_slides" for i, _ in step_ids)
        assert not [f for f in frames if f.get("type") == "files"]
        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1

        # 大纲落盘 state.json（跨轮契约）
        state_file = tmp_path / "yuwen" / "静夜思-古诗词" / "state.json"
        assert state_file.exists()
        disk = json.loads(state_file.read_text(encoding="utf-8"))
        assert disk["yuwen_outline"]["pages"][0]["id"] == "s01"
        assert disk["yuwen_outline_confirmed"] is False

    def test_confirm_round_full_pipeline(self, client):
        """确认轮：盘上有未确认大纲 + "确认" → 全链到 report（files + 单 done）。"""
        c, mock_gw, tmp_path = client
        _seed_outline_on_disk(tmp_path, confirmed=False)
        mock_gw.stream_chat = _page_stream

        # mock render 子进程（真渲染由 TestRenderAll 覆盖）。会话目录名由
        # _session_name(params) 派生 = title-lesson_type（不含年级）；
        # 文件名任意（render 按扩展名 glob），预置供 files 收集。
        session_dir = tmp_path / "yuwen" / "静夜思-古诗词"
        session_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("静夜思.pptx", "静夜思.html", "静夜思-教案.docx"):
            (session_dir / fname).write_text("fake")
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "ok"
        fake_result.stderr = ""
        with patch("aidraft.agenthub.yuwen.nodes.render.subprocess.run",
                   return_value=fake_result):
            resp = c.post("/api/chat", json={
                "prompt": "确认", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        step_frames = [f for f in frames if f.get("type") == "step"]
        ids = {f.get("id") for f in step_frames}
        for node in ("extract_params", "confirm", "gen_slides", "gen_plan",
                     "review", "gen_images", "render", "report"):
            assert node in ids, f"step 链缺 {node}：{sorted(ids)}"

        review_frames = [f for f in frames if f.get("type") == "review"]
        assert len(review_frames) == 1
        assert review_frames[0]["review"]["pass"] is True

        file_frames = [f for f in frames if f.get("type") == "files"]
        assert len(file_frames) >= 1
        assert len(file_frames[0]["files"]) >= 1

        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1, f"期望单 done，实际 {len(done_frames)}"
        # report 的 done 带审查评分摘要
        assert "审查评分" in done_frames[0]["answer"]

        # token 帧（gen_slides 流式）不应污染累加：done 的 answer 来自 state
        assert "页1" not in done_frames[0]["answer"]

    def test_edit_round_resends_outline(self, client):
        """改纲轮：盘上有大纲 + 修改指令 → 重发 outline 帧，不进入生成。"""
        c, mock_gw, tmp_path = client
        _seed_outline_on_disk(tmp_path, confirmed=False)
        edited = json.loads(json.dumps(_OUTLINE_JSON))
        edited["pages"].append({"id": "s04", "kind": "game", "title": "游戏",
                                "period": 1, "points": "摘苹果"})

        calls = {"n": 0}
        base = _make_chat_dispatch()

        def _chat(msgs, **kw):
            if "大纲编辑" in msgs[0].content:
                calls["n"] += 1
                return _chat_response(json.dumps(edited, ensure_ascii=False))
            return base(msgs, **kw)
        mock_gw.chat.side_effect = _chat

        resp = c.post("/api/chat", json={
            "prompt": "加一个识字游戏页", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        outline_frames = [f for f in frames if f.get("type") == "outline"]
        assert len(outline_frames) == 1
        assert len(outline_frames[0]["outline"]["pages"]) == 4
        assert calls["n"] == 1

        step_ids = {f.get("id") for f in frames if f.get("type") == "step"}
        assert "gen_slides" not in step_ids, "未确认不应进入生成"
        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1

    def test_chip_confirm_with_empty_params(self, client):
        """chip 点击轮："确认大纲，开始生成" 被 extract 判无参数 → confirm 盘上找回会话。"""
        c, mock_gw, tmp_path = client
        _seed_outline_on_disk(tmp_path, confirmed=False)
        # extract 抽不出参数（chip 原文不是课文描述）
        mock_gw.chat.side_effect = None
        dispatch = _make_chat_dispatch()

        def _chat(msgs, **kw):
            if "参数提取助手" in msgs[0].content:
                return _chat_response(json.dumps({
                    "title": "", "grade": 0, "lesson_type": "", "textbook": "",
                    "params_ready": False, "question": "请提供课文名和年级",
                    "chips": []}, ensure_ascii=False))
            return dispatch(msgs, **kw)
        mock_gw.chat.side_effect = _chat
        mock_gw.stream_chat = _page_stream

        # 盘上找回的 params 会话 = 静夜思-古诗词；预置渲染产物供 files 收集
        session_dir = tmp_path / "yuwen" / "静夜思-古诗词"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "静夜思.pptx").write_text("fake")
        fake_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("aidraft.agenthub.yuwen.nodes.render.subprocess.run",
                   return_value=fake_result):
            resp = c.post("/api/chat", json={
                "prompt": "确认大纲，开始生成", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        # 走 confirm 兜底 → 进入生成（出现 gen_slides step）
        step_ids = {f.get("id") for f in frames if f.get("type") == "step"}
        assert "gen_slides" in step_ids
        # extract_params 的追问 content 帧照发（节点行为不抑制），但它是
        # 噪声：done 必须是 report 的交付确认，不是追问——链路兜底成功
        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1
        assert "请提供课文名" not in done_frames[0]["answer"]
        assert [f for f in frames if f.get("type") == "files"], "应产出 files 帧"

    def test_grade_float_passes(self, client):
        """grade=2.0 浮点数能放行到 params_ready → gen_outline。"""
        c, mock_gw, _ = client
        mock_gw.chat.side_effect = None
        float_params = dict(_PARAMS_JSON, grade=2.0)
        dispatch = _make_chat_dispatch()

        def _chat(msgs, **kw):
            if "参数提取助手" in msgs[0].content:
                return _chat_response(json.dumps(float_params, ensure_ascii=False))
            return dispatch(msgs, **kw)
        mock_gw.chat.side_effect = _chat

        resp = c.post("/api/chat", json={
            "prompt": "静夜思 二年级 古诗词", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        outline_running = [f for f in frames if f.get("type") == "step"
                           and f.get("id") == "gen_outline" and f.get("status") == "running"]
        assert len(outline_running) == 1, "grade=2.0 应放行到 gen_outline"

    def test_grade_string_passes(self, client):
        """grade="2" 字符串能放行到 params_ready → gen_outline。"""
        c, mock_gw, _ = client
        mock_gw.chat.side_effect = None
        str_params = dict(_PARAMS_JSON, grade="2")
        dispatch = _make_chat_dispatch()

        def _chat(msgs, **kw):
            if "参数提取助手" in msgs[0].content:
                return _chat_response(json.dumps(str_params, ensure_ascii=False))
            return dispatch(msgs, **kw)
        mock_gw.chat.side_effect = _chat

        resp = c.post("/api/chat", json={
            "prompt": "静夜思 二年级 古诗词", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        outline_running = [f for f in frames if f.get("type") == "step"
                           and f.get("id") == "gen_outline" and f.get("status") == "running"]
        assert len(outline_running) == 1, 'grade="2" 应放行到 gen_outline'

    def test_unknown_agent_returns_error(self, client):
        """未知 agent 返回 error 帧。"""
        c, _, _ = client
        resp = c.post("/api/chat", json={
            "prompt": "你好", "agent": "nonexistent_agent"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)
        error_frames = [f for f in frames if f.get("type") == "error"]
        assert len(error_frames) >= 1
        assert "unknown agent" in error_frames[0].get("message", "")

    def test_yuwen_step_id_in_content(self, client):
        """yuwen 追问轮的 content 帧携带 step_id='extract_params'。"""
        c, mock_gw, _ = client
        mock_gw.chat.side_effect = None
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "", "grade": 0, "lesson_type": "", "textbook": "",
            "params_ready": False, "question": "请提供课文名和年级",
            "chips": [],
        }, ensure_ascii=False))

        resp = c.post("/api/chat", json={
            "prompt": "帮我做课件", "agent": "yuwen"})
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)
        content_frames = [f for f in frames if f.get("type") == "content"]
        if content_frames:
            assert "step_id" in content_frames[0]
            assert isinstance(content_frames[0]["step_id"], str)
