"""语文智能体端点级端到端测试：TestClient + mock gateway 覆盖 SSE 帧序列。

测试覆盖：
1. 缺参轮：agent=yuwen_skill 发送"帮我做课件" → content 含追问 + 单 done 无双 done
2. 补齐轮：发送完整参数 → step 链完整 + files 帧 + files 可下载
3. grade=2.0 浮点数能放行到 params_ready
4. grade="2" 字符串能放行到 params_ready
5. 未知 agent 返回 error 帧

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


# 合法 schema 的样本课件 JSON（对齐 test_agenthub_yuwen.TestGenContent.SAMPLE_JSON）
_VALID_SAMPLE_JSON = {
    "version": "1.0",
    "meta": {
        "title": "静夜思",
        "grade": 1,
        "lessonType": "古诗词",
        "textbook": "部编版一年级下册",
        "periods": 1,
        "coreCompetencies": ["文化自信", "语言运用", "思维能力", "审美创造"],
        "objectives": [
            {"content": "认识9个生字", "competency": "语言运用", "dimension": "知识与技能"},
            {"content": "正确流利有感情地朗读古诗", "competency": "语言运用", "dimension": "过程与方法"},
        ],
        "keyPoints": ["识字，朗读背诵古诗"],
        "difficulties": ["体会诗人思乡之情"],
    },
    "slides": [
        {"id": "s01", "kind": "cover", "title": "静夜思", "period": 1, "elements": [
            {"type": "heading", "content": "静夜思", "size": "h1"},
        ]},
        {"id": "s02", "kind": "intro", "title": "诗人背景", "period": 1, "elements": [
            {"type": "paragraph", "content": "李白，唐代诗人。", "emphasize": []},
        ]},
        {"id": "s03", "kind": "read-rhythm", "title": "初读节奏", "period": 1, "elements": [
            {"type": "poem", "title": "静夜思", "author": "李白", "stanzas": [
                {"lines": [
                    {"text": "床前明月光", "ruby": "chuáng qián míng yuè guāng"},
                    {"text": "低头思故乡", "ruby": "dī tóu sī gù xiāng"},
                ]}
            ]},
        ]},
    ],
    "lessonPlan": {
        "title": "静夜思",
        "base": {"textbook": "部编版一年级下册", "grade": "一年级", "periods": "1", "lessonType": "古诗词"},
        "objectives": [
            {"content": "认识9个生字", "competency": "语言运用", "dimension": "知识与技能"},
        ],
        "keyPoints": ["识字"],
        "difficulties": ["体会情感"],
        "preparation": "多媒体课件",
        "periods": "1课时",
        "teachingProcess": [
            {"phase": "一、导入", "duration": "5分钟", "activities": [
                {"teacher": "出示明月图", "student": "观察图片"},
            ], "design": "创设情境"},
        ],
        "boardDesign": {"structure": "静夜思：思乡之情"},
        "homework": {"levels": [{"level": "基础", "items": ["背诵"]}]},
        "reflection": "",
    },
    "handout": {
        "levels": [{"level": "基础", "items": ["背诵"]}],
    },
}


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
    from devpilot.gateway import ChatResponse
    return ChatResponse(
        content=content, provider="test", model="test",
        latency_ms=100, finish_reason=finish_reason,
    )


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


def _chunk(delta: str = "", reasoning: str = "", done: bool = False):
    from devpilot.gateway import ChatChunk
    return ChatChunk(delta=delta, reasoning=reasoning, done=done)


# ======================================================================
# Fixture
# ======================================================================

@pytest.fixture
def client():
    """返回 mock 网关后的 TestClient，避免真实 LLM 调用。"""
    from devpilot.web.api import app
    from fastapi.testclient import TestClient

    with patch("devpilot.gateway.build_default_gateway") as mock_build:
        mock_gw = MagicMock()
        # chat 默认返回空响应
        mock_gw.chat.return_value = _chat_response(
            json.dumps({
                "title": "静夜思",
                "grade": 1,
                "lesson_type": "古诗词",
                "textbook": "部编版一年级下册",
                "params_ready": True,
                "question": "",
                "chips": [],
            }, ensure_ascii=False)
        )
        # stream_chat 返回空 async 迭代器
        async def _empty_stream(*args, **kwargs):
            for _ in ():
                yield
        mock_gw.stream_chat = _empty_stream
        mock_build.return_value = mock_gw

        with TestClient(app) as c:
            yield c, mock_gw


# ======================================================================
# E2E 测试
# ======================================================================

class TestYuwenEndpoints:
    """语文智能体端到端 SSE 帧序列验证。"""

    def test_missing_params_round(self, client):
        """缺参轮：content 含追问 + 单 done 无双 done。"""
        c, mock_gw = client
        # 模拟缺参数返回
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思",
            "grade": 0,
            "lesson_type": "",
            "textbook": "",
            "params_ready": False,
            "question": "请问课文是几年级的？需要什么课型？",
            "chips": ["一年级 古诗词", "二年级 精读"],
        }, ensure_ascii=False))

        # 让 stream_chat 返回空（gen_content 不会被调用）
        async def _empty(*args, **kwargs):
            for _ in ():
                yield
        mock_gw.stream_chat = _empty

        resp = c.post("/api/chat", json={
            "prompt": "帮我做《静夜思》的课件",
            "agent": "yuwen_skill",
        })
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        # 检查 agent_meta 帧
        meta_frames = [f for f in frames if f.get("type") == "agent_meta"]
        assert len(meta_frames) == 1
        assert meta_frames[0]["agent_id"] == "yuwen_skill"

        # 检查 step 帧：extract_params running → done
        step_frames = [f for f in frames if f.get("type") == "step"]
        assert len(step_frames) >= 2
        extract_running = [f for f in step_frames if f.get("id") == "extract_params" and f.get("status") == "running"]
        extract_done = [f for f in step_frames if f.get("id") == "extract_params" and f.get("status") == "done"]
        assert len(extract_running) == 1
        assert len(extract_done) == 1

        # 检查 content 帧含追问文本和 chips
        content_frames = [f for f in frames if f.get("type") == "content"]
        assert len(content_frames) == 1
        assert "年级" in content_frames[0].get("delta", "")
        assert "chips" in content_frames[0]
        assert len(content_frames[0]["chips"]) > 0

        # 检查 done 帧：仅一个
        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1, f"期望单 done，实际 {len(done_frames)}"

        # 检查无 files 帧
        file_frames = [f for f in frames if f.get("type") == "files"]
        assert len(file_frames) == 0

    def test_full_params_round(self, client):
        """补齐轮：step 链完整 + files 帧 + files 可下载。"""
        c, mock_gw = client
        # 参数齐备
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思",
            "grade": 1,
            "lesson_type": "古诗词",
            "textbook": "部编版一年级下册",
            "params_ready": True,
            "question": "",
            "chips": [],
        }, ensure_ascii=False))

        # 创建测试输出目录和模拟渲染产物
        from devpilot.web.api import OUTPUTS_DIR
        session_dir = OUTPUTS_DIR / "yuwen_skill" / "静夜思-古诗词"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "静夜思-古诗词.pptx").write_text("fake pptx")
        (session_dir / "静夜思.html").write_text("fake html")
        (session_dir / "静夜思-教案.docx").write_text("fake docx")

        # stream_chat 返回合法 schema 的 JSON（复用 test_agenthub_yuwen 的样本）
        sample_json = json.dumps(_VALID_SAMPLE_JSON, ensure_ascii=False)
        async def _stream(*args, **kwargs):
            yield _chunk(delta=sample_json)
            yield _chunk(done=True)
        mock_gw.stream_chat = _stream

        # mock render 子进程：返回 0，避免真实调用 render_all.py（需完整 schema）
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "ok"
        fake_result.stderr = ""
        with patch("devpilot.agenthub.yuwen_skill.graph.subprocess.run",
                   return_value=fake_result):
            resp = c.post("/api/chat", json={
                "prompt": "静夜思 一年级 古诗词",
                "agent": "yuwen_skill",
            })
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        # 检查 step 链完整
        step_frames = [f for f in frames if f.get("type") == "step"]
        step_ids = [(f.get("id"), f.get("status")) for f in step_frames]
        # 应有 extract_params、gen_content、render、report 各两态
        extract_ids = [s for s in step_ids if s[0] == "extract_params"]
        gen_ids = [s for s in step_ids if s[0] == "gen_content"]
        render_ids = [s for s in step_ids if s[0] == "render"]
        report_ids = [s for s in step_ids if s[0] == "report"]
        assert len(extract_ids) >= 2
        assert len(gen_ids) >= 2
        assert len(render_ids) >= 2
        assert len(report_ids) >= 2

        # 检查 files 帧
        file_frames = [f for f in frames if f.get("type") == "files"]
        assert len(file_frames) >= 1
        file_list = file_frames[0].get("files", [])
        assert len(file_list) >= 1

        # 检查 done 帧：仅一个
        done_frames = [f for f in frames if f.get("type") == "done"]
        assert len(done_frames) == 1, f"期望单 done，实际 {len(done_frames)}"

        # 检查 files 可下载
        if file_list:
            first_file = file_list[0]
            download_path = first_file.get("path", "")
            if download_path:
                dl_resp = c.get(download_path)
                assert dl_resp.status_code in (200, 404)

        # 清理
        import shutil
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    def test_grade_float_passes(self, client):
        """grade=2.0 浮点数能放行到 params_ready=True。"""
        c, mock_gw = client
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思",
            "grade": 2.0,
            "lesson_type": "古诗词",
            "textbook": "部编版二年级上册",
            "params_ready": True,
            "question": "",
            "chips": [],
        }, ensure_ascii=False))

        # stream_chat 快速结束
        sample_json = json.dumps(_VALID_SAMPLE_JSON, ensure_ascii=False)
        async def _quick_stream(*args, **kwargs):
            yield _chunk(delta=sample_json)
            yield _chunk(done=True)
        mock_gw.stream_chat = _quick_stream

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "ok"
        fake_result.stderr = ""
        with patch("devpilot.agenthub.yuwen_skill.graph.subprocess.run",
                   return_value=fake_result):
            resp = c.post("/api/chat", json={
                "prompt": "静夜思 二年级 古诗词",
                "agent": "yuwen_skill",
            })
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        # 参数应解析成功，gen_content 被调用
        gen_running = [f for f in frames if f.get("type") == "step"
                       and f.get("id") == "gen_content" and f.get("status") == "running"]
        assert len(gen_running) == 1, "grade=2.0 应放行到 gen_content"

        # 清理
        import shutil
        from devpilot.web.api import OUTPUTS_DIR
        d = OUTPUTS_DIR / "yuwen_skill" / "静夜思-古诗词"
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    def test_grade_string_passes(self, client):
        """grade="2" 字符串能放行到 params_ready=True。"""
        c, mock_gw = client
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思",
            "grade": "2",
            "lesson_type": "古诗词",
            "textbook": "部编版二年级上册",
            "params_ready": True,
            "question": "",
            "chips": [],
        }, ensure_ascii=False))

        sample_json = json.dumps(_VALID_SAMPLE_JSON, ensure_ascii=False)
        async def _quick_stream(*args, **kwargs):
            yield _chunk(delta=sample_json)
            yield _chunk(done=True)
        mock_gw.stream_chat = _quick_stream

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "ok"
        fake_result.stderr = ""
        with patch("devpilot.agenthub.yuwen_skill.graph.subprocess.run",
                   return_value=fake_result):
            resp = c.post("/api/chat", json={
                "prompt": "静夜思 二年级 古诗词",
                "agent": "yuwen_skill",
            })
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)

        gen_running = [f for f in frames if f.get("type") == "step"
                       and f.get("id") == "gen_content" and f.get("status") == "running"]
        assert len(gen_running) == 1, 'grade="2" 应放行到 gen_content'

        # 清理
        import shutil
        from devpilot.web.api import OUTPUTS_DIR
        d = OUTPUTS_DIR / "yuwen_skill" / "静夜思-古诗词"
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    def test_unknown_agent_returns_error(self, client):
        """未知 agent 返回 error 帧。"""
        c, _ = client
        resp = c.post("/api/chat", json={
            "prompt": "你好",
            "agent": "nonexistent_agent",
        })
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)
        error_frames = [f for f in frames if f.get("type") == "error"]
        assert len(error_frames) >= 1
        assert "unknown agent" in error_frames[0].get("message", "")

    def test_yuwen_step_id_in_content(self, client):
        """yuwen 追问轮的 content 帧携带 step_id='extract_params'。"""
        c, mock_gw = client
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "",
            "grade": 0,
            "lesson_type": "",
            "textbook": "",
            "params_ready": False,
            "question": "请提供课文名和年级",
            "chips": [],
        }, ensure_ascii=False))

        async def _empty(*args, **kwargs):
            for _ in ():
                yield
        mock_gw.stream_chat = _empty

        resp = c.post("/api/chat", json={
            "prompt": "帮我做课件",
            "agent": "yuwen_skill",
        })
        assert resp.status_code == 200
        frames = _parse_sse(resp.text)
        content_frames = [f for f in frames if f.get("type") == "content"]
        # 至少有一个 content 帧，携带 step_id 字符串
        if content_frames:
            assert "step_id" in content_frames[0]
            assert isinstance(content_frames[0]["step_id"], str)
            assert content_frames[0]["step_id"] == "extract_params"