"""语文智能体图测试：extract_params / gen_content / render / report 节点。

测试覆盖：
1. manifest.py 字段完整性
2. graph.py 编译正确（build_graph 返回 CompiledGraph）
3. extract_params 条件边双出口
4. gen_content 节点 mock LLM 调用
5. render_all.py 纯 Python 渲染（无 LLM 依赖）
6. 图集成：astream 状态流转

运行：
    pytest tests/test_agenthub_yuwen.py -x -v
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


def _chunk(delta: str = "", reasoning: str = "", done: bool = False):
    from devpilot.gateway import ChatChunk
    return ChatChunk(delta=delta, reasoning=reasoning, done=done)


def _chat_response(content: str, finish_reason: str = "stop"):
    from devpilot.gateway import ChatResponse
    return ChatResponse(content=content, provider="test", model="test",
                        latency_ms=100, finish_reason=finish_reason)


# ======================================================================
# 1. manifest 测试
# ======================================================================

class TestYuwenManifest:
    """语文智能体 manifest 字段完整性与注册。"""

    def test_manifest_fields(self):
        """manifest.py 导出所有必填字段。"""
        from devpilot.agenthub.yuwen_skill import manifest as m
        assert m.AGENT_ID == "yuwen_skill"
        assert m.DISPLAY_NAME == "语文课件生成"
        assert m.DESCRIPTION
        assert m.IDENTITY_COLOR
        assert m.PLACEHOLDER

    def test_registry_discovers_yuwen(self):
        """注册中心能发现 yuwen_skill。"""
        from devpilot.agenthub import list_agents, reset_cache

        reset_cache()
        agents = list_agents()
        ids = [a.agent_id for a in agents]
        assert "yuwen_skill" in ids, f"expected 'yuwen_skill' in {ids}"

    def test_manifest_to_dict_format(self):
        """to_dict() 字段名对齐契约。"""
        from devpilot.agenthub import get_agent, reset_cache

        reset_cache()
        agent = get_agent("yuwen_skill")
        assert agent is not None
        d = agent.to_dict()
        assert d["id"] == "yuwen_skill"
        assert d["display_name"] == "语文课件生成"
        assert "description" in d
        assert "identity_color" in d
        assert "placeholder" in d


# ======================================================================
# 2. graph 编译测试
# ======================================================================

class TestYuwenGraph:
    """图编译与结构。"""

    def test_graph_compiles(self):
        """build_graph 返回可调用的编译图。"""
        from devpilot.agenthub.yuwen_skill.graph import build_graph

        mock_gw = MagicMock()
        mock_registry = MagicMock()
        graph = build_graph(gateway=mock_gw, registry=mock_registry,
                            emitter=lambda f: None)
        assert hasattr(graph, "astream"), "build_graph must return a compiled graph"

    def test_graph_has_four_nodes(self):
        """图有 4 个节点：extract_params / gen_content / render / report。"""
        from devpilot.agenthub.yuwen_skill.graph import build_graph

        mock_gw = MagicMock()
        mock_registry = MagicMock()
        graph = build_graph(gateway=mock_gw, registry=mock_registry)
        # 编译后的图 nodes 字典包含所有节点
        assert "extract_params" in graph.nodes
        assert "gen_content" in graph.nodes
        assert "render" in graph.nodes
        assert "report" in graph.nodes

    def test_entry_point_is_extract_params(self):
        """入口是 extract_params。"""
        from devpilot.agenthub.yuwen_skill.graph import build_graph

        mock_gw = MagicMock()
        mock_registry = MagicMock()
        graph = build_graph(gateway=mock_gw, registry=mock_registry)
        # 编译图通过 get_graph() 拿到内部结构；首节点是 extract_params
        g = graph.get_graph()
        assert g.nodes and "extract_params" in g.nodes


# ======================================================================
# 3. extract_params 节点测试
# ======================================================================

class TestExtractParams:
    """extract_params 节点：LLM 参数提取与追问。"""

    def test_params_ready_goes_to_gen_content(self):
        """参数齐备时条件边走向 gen_content。"""
        from devpilot.agenthub.yuwen_skill.graph import _params_ready

        state = {"yuwen_params_ready": True}
        assert _params_ready(state) == "gen_content"

    def test_params_not_ready_ends(self):
        """参数缺失时条件边走向 __end__。"""
        from devpilot.agenthub.yuwen_skill.graph import _params_ready

        state = {"yuwen_params_ready": False}
        assert _params_ready(state) == "__end__"

    def test_params_ready_defaults_false(self):
        """state 无 yuwen_params_ready 时默认走向 __end__。"""
        from devpilot.agenthub.yuwen_skill.graph import _params_ready

        state = {}
        assert _params_ready(state) == "__end__"

    def test_extract_params_full_parsed(self):
        """LLM 返回完整参数时，节点返回 params_ready=True。"""
        from devpilot.agenthub.yuwen_skill.graph import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思",
            "grade": 1,
            "lesson_type": "古诗词",
            "textbook": "部编版一年级下册",
            "params_ready": True,
            "question": "",
            "chips": [],
        }, ensure_ascii=False))

        frames = []
        node = _make_extract_params_node(mock_gw, lambda f: frames.append(f))

        import asyncio
        result = asyncio.run(node({
            "task": "帮我做《静夜思》的课件",
            "user_message": "帮我做《静夜思》的课件",
            "messages": [],
        }))

        assert result["yuwen_params_ready"] is True
        assert result["yuwen_params"]["title"] == "静夜思"
        assert result["yuwen_params"]["grade"] == 1
        assert result["yuwen_params"]["lesson_type"] == "古诗词"
        # 应有 step 帧
        step_frames = [f for f in frames if f.get("type") == "step"]
        assert len(step_frames) == 2  # running + done

    def test_extract_params_missing_grade(self):
        """缺年级时 params_ready=False，发出追问。"""
        from devpilot.agenthub.yuwen_skill.graph import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思",
            "grade": 0,
            "lesson_type": "",
            "textbook": "",
            "params_ready": False,
            "question": "请提供年级和课型，例如：一年级 古诗词",
            "chips": ["一年级 古诗词", "二年级 精读"],
        }, ensure_ascii=False))

        frames = []
        node = _make_extract_params_node(mock_gw, lambda f: frames.append(f))

        import asyncio
        result = asyncio.run(node({
            "task": "做《静夜思》",
            "user_message": "做《静夜思》",
            "messages": [],
        }))

        assert result["yuwen_params_ready"] is False
        # 应有追问文本
        content_frames = [f for f in frames if f.get("type") == "content"]
        assert len(content_frames) > 0
        assert "年级" in content_frames[0].get("delta", "")

    def test_extract_params_llm_failure(self):
        """LLM 调用异常时降级返回。"""
        from devpilot.agenthub.yuwen_skill.graph import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = RuntimeError("API 不可用")

        node = _make_extract_params_node(mock_gw, None)

        import asyncio
        result = asyncio.run(node({
            "task": "帮我做课件",
            "user_message": "帮我做课件",
            "messages": [],
        }))

        assert result["yuwen_params_ready"] is False

    def test_extract_params_gateway_called_with_json_mode(self):
        """gateway.chat 被调用且 json_mode=True。"""
        from devpilot.agenthub.yuwen_skill.graph import _make_extract_params_node

        mock_gw = MagicMock()
        mock_gw.chat.return_value = _chat_response(json.dumps({
            "title": "静夜思", "grade": 1, "lesson_type": "古诗词",
            "textbook": "部编版", "params_ready": True, "question": "", "chips": [],
        }))

        node = _make_extract_params_node(mock_gw, None)

        import asyncio
        asyncio.run(node({
            "task": "静夜思 一年级",
            "user_message": "静夜思 一年级",
            "messages": [],
        }))

        # 验证 json_mode=True
        _, kwargs = mock_gw.chat.call_args
        assert kwargs.get("json_mode") is True


# ======================================================================
# 4. gen_content 节点测试
# ======================================================================

class TestGenContent:
    """gen_content 节点：LLM 生成课件 JSON。"""

    SAMPLE_JSON = {
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
                        {"text": "疑是地上霜", "ruby": "yí shì dì shàng shuāng"},
                        {"text": "举头望明月", "ruby": "jǔ tóu wàng míng yuè"},
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

    def test_gen_content_returns_json_path(self):
        """gen_content 产出 JSON 文件路径。"""
        from devpilot.agenthub.yuwen_skill.graph import _make_gen_content_node

        import json as _json
        sample_json_str = _json.dumps(self.SAMPLE_JSON, ensure_ascii=False)

        mock_gw = MagicMock()
        mock_gw.stream_chat.return_value = _AsyncIter([
            _chunk(delta=sample_json_str),
            _chunk(done=True),
        ])

        frames = []
        node = _make_gen_content_node(mock_gw, lambda f: frames.append(f))

        import asyncio
        result = asyncio.run(node({
            "yuwen_params": {
                "title": "静夜思",
                "grade": 1,
                "lesson_type": "古诗词",
                "textbook": "部编版一年级下册",
            },
        }))

        assert result["yuwen_content_path"], "应返回 JSON 文件路径"
        path = Path(result["yuwen_content_path"])
        assert path.exists(), f"JSON 文件应存在：{path}"
        # 验证内容
        loaded = _json.loads(path.read_text(encoding="utf-8"))
        assert loaded["meta"]["title"] == "静夜思"
        # 清理
        path.unlink()

    def test_gen_content_with_markdown_code_block(self):
        """LLM 返回 markdown 代码块包裹的 JSON 也能解析。"""
        import json as _json
        wrapped = f"```json\n{_json.dumps(self.SAMPLE_JSON, ensure_ascii=False)}\n```"

        from devpilot.agenthub.yuwen_skill.graph import _make_gen_content_node

        mock_gw = MagicMock()
        mock_gw.stream_chat.return_value = _AsyncIter([
            _chunk(delta=wrapped),
            _chunk(done=True),
        ])

        node = _make_gen_content_node(mock_gw, None)

        import asyncio
        result = asyncio.run(node({
            "yuwen_params": {
                "title": "静夜思",
                "grade": 1,
                "lesson_type": "古诗词",
                "textbook": "部编版一年级下册",
            },
        }))

        assert result["yuwen_content_path"]
        Path(result["yuwen_content_path"]).unlink()  # cleanup

    def test_gen_content_invalid_json_retry(self):
        """JSON 解析失败时重试一次。"""
        from devpilot.agenthub.yuwen_skill.graph import _make_gen_content_node

        import json as _json
        sample_str = _json.dumps(self.SAMPLE_JSON, ensure_ascii=False)

        # 第一次返回无效 JSON，第二次返回有效
        mock_gw = MagicMock()
        mock_gw.stream_chat.side_effect = [
            _AsyncIter([_chunk(delta="not valid json at all"), _chunk(done=True)]),
            _AsyncIter([_chunk(delta=sample_str), _chunk(done=True)]),
        ]

        node = _make_gen_content_node(mock_gw, None)

        import asyncio
        result = asyncio.run(node({
            "yuwen_params": {
                "title": "静夜思",
                "grade": 1,
                "lesson_type": "古诗词",
                "textbook": "部编版一年级下册",
            },
        }))

        assert result["yuwen_content_path"]
        assert mock_gw.stream_chat.call_count == 2
        Path(result["yuwen_content_path"]).unlink()  # cleanup

    def test_gen_content_both_attempts_fail(self):
        """两次都失败时返回空路径。"""
        from devpilot.agenthub.yuwen_skill.graph import _make_gen_content_node

        mock_gw = MagicMock()
        mock_gw.stream_chat.return_value = _AsyncIter([
            _chunk(delta="garbage"),
            _chunk(done=True),
        ])

        node = _make_gen_content_node(mock_gw, None)

        import asyncio
        result = asyncio.run(node({
            "yuwen_params": {
                "title": "静夜思",
                "grade": 1,
                "lesson_type": "古诗词",
                "textbook": "部编版一年级下册",
            },
        }))

        assert result["yuwen_content_path"] == ""


# ======================================================================
# 5. render_all 纯 Python 渲染测试
# ======================================================================

class TestRenderAll:
    """render_all.py 纯 Python 渲染（无 LLM 依赖）。"""

    def test_render_jingyesi_exit_0(self):
        """静夜思 JSON → 退出码 0。"""
        import subprocess, sys
        base = _SRC / "devpilot" / "agenthub" / "yuwen_skill"
        script = base / "scripts" / "render_all.py"
        json_path = base / "references" / "examples" / "jingyesi.json"
        out_dir = _PROJECT_ROOT / "outputs" / "test_yuwen" / "jingyesi"

        result = subprocess.run(
            [sys.executable, str(script), str(json_path), "--out", str(out_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        # 验证 3 个文件
        files = list(out_dir.glob("*"))
        exts = [f.suffix for f in files]
        assert ".pptx" in exts, f"missing pptx in {exts}"
        assert ".html" in exts, f"missing html in {exts}"
        assert ".docx" in exts, f"missing docx in {exts}"

        # 清理
        import shutil
        shutil.rmtree(out_dir.parent, ignore_errors=True)

    def test_render_zuojing_exit_0(self):
        """坐井观天 JSON（2 课时）→ 退出码 0。"""
        import subprocess, sys
        base = _SRC / "devpilot" / "agenthub" / "yuwen_skill"
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
        """check_deps.py 返回 0（全部就绪）。"""
        import subprocess, sys
        script = str(_SRC / "devpilot" / "agenthub" / "yuwen_skill" / "scripts" / "check_deps.py")

        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_render_nonexistent_json(self):
        """不存在的 JSON → 退出码 2。"""
        import subprocess, sys
        script = str(_SRC / "devpilot" / "agenthub" / "yuwen_skill" / "scripts" / "render_all.py")

        result = subprocess.run(
            [sys.executable, script, "/nonexistent/path.json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        assert result.returncode == 2


# ======================================================================
# 6. 条件边单元测试
# ======================================================================

class TestConditionalEdge:
    """图条件边函数。"""

    def test_params_ready_function(self):
        """_params_ready 条件边直接返回字符串。"""
        from devpilot.agenthub.yuwen_skill.graph import _params_ready

        assert _params_ready({"yuwen_params_ready": True}) == "gen_content"
        assert _params_ready({"yuwen_params_ready": False}) == "__end__"
        assert _params_ready({}) == "__end__"


# ======================================================================
# 7. 图集成测试（mock LLM）
# ======================================================================

class TestGraphIntegration:
    """图 astream 集成测试（全部 mock）。"""

    SAMPLE_JSON = TestGenContent.SAMPLE_JSON

    async def _run_graph(self, user_input: str, mock_gw) -> dict:
        """驱动图执行并返回最终 state。"""
        from devpilot.agenthub.yuwen_skill.graph import build_graph

        registry = MagicMock()
        frames = []

        graph = build_graph(gateway=mock_gw, registry=registry,
                            emitter=lambda f: frames.append(f))

        final_state = {}
        async for chunk in graph.astream({
            "task": user_input,
            "user_message": user_input,
            "messages": [],
        }):
            for node_id, update in chunk.items():
                if isinstance(update, dict):
                    final_state.update(update)

        return final_state, frames

    def test_full_graph_params_ready(self):
        """完整流程：参数齐备 → gen_content → render → report。"""
        import json as _json

        mock_gw = MagicMock()
        # extract_params 返回齐备参数
        mock_gw.chat.return_value = _chat_response(_json.dumps({
            "title": "静夜思",
            "grade": 1,
            "lesson_type": "古诗词",
            "textbook": "部编版一年级下册",
            "params_ready": True,
            "question": "",
            "chips": [],
        }, ensure_ascii=False))

        # gen_content 流式返回 JSON
        sample_str = _json.dumps(self.SAMPLE_JSON, ensure_ascii=False)
        mock_gw.stream_chat.return_value = _AsyncIter([
            _chunk(delta=sample_str),
            _chunk(done=True),
        ])

        import asyncio
        state, frames = asyncio.run(self._run_graph("静夜思 一年级 古诗词", mock_gw))

        # 验证 params 解析
        assert state.get("yuwen_params", {}).get("title") == "静夜思"
        # 验证 content 生成
        assert state.get("yuwen_content_path", "")
        # 验证渲染结果
        assert "yuwen_files" in state
        assert "yuwen_render_error" not in state or not state["yuwen_render_error"]

        # 清理（跳过清理渲染产生的大文件）
        content_path = state.get("yuwen_content_path", "")
        if content_path and Path(content_path).exists():
            # 注：渲染产生的 pptx/html/docx 已被 test_render 清理，这里只清 tmp JSON
            pass

    def test_full_graph_params_missing_then_ready_two_rounds(self):
        """两轮对话：第一轮缺参数 → END，第二轮补齐 → 完整流程。"""
        import json as _json

        # 第一轮：缺参数
        mock_gw1 = MagicMock()
        mock_gw1.chat.return_value = _chat_response(_json.dumps({
            "title": "静夜思",
            "grade": 0,
            "lesson_type": "",
            "textbook": "",
            "params_ready": False,
            "question": "请提供年级和课型",
            "chips": ["一年级 古诗词"],
        }, ensure_ascii=False))

        registry = MagicMock()
        from devpilot.agenthub.yuwen_skill.graph import build_graph

        import asyncio

        frames1 = []
        graph = build_graph(gateway=mock_gw1, registry=registry,
                            emitter=lambda f: frames1.append(f))

        async def _run_round(graph, user_input, history):
            st = {}
            async for chunk in graph.astream({
                "task": user_input,
                "user_message": user_input,
                "messages": history,
            }):
                for _node_id, update in chunk.items():
                    if isinstance(update, dict):
                        st.update(update)
            return st

        state1 = asyncio.run(_run_round(
            graph, "帮我做《静夜思》的课件", []))

        assert state1.get("yuwen_params_ready") is False
        assert "yuwen_content_path" not in state1 or not state1.get("yuwen_content_path")

        # 第二轮：补齐参数
        mock_gw2 = MagicMock()
        mock_gw2.chat.return_value = _chat_response(_json.dumps({
            "title": "静夜思",
            "grade": 1,
            "lesson_type": "古诗词",
            "textbook": "部编版一年级下册",
            "params_ready": True,
            "question": "",
            "chips": [],
        }, ensure_ascii=False))

        sample_str = _json.dumps(self.SAMPLE_JSON, ensure_ascii=False)
        mock_gw2.stream_chat.return_value = _AsyncIter([
            _chunk(delta=sample_str),
            _chunk(done=True),
        ])

        frames2 = []
        graph2 = build_graph(gateway=mock_gw2, registry=registry,
                             emitter=lambda f: frames2.append(f))

        async def _run_round2(graph, user_input, history):
            st = {}
            async for chunk in graph.astream({
                "task": user_input,
                "user_message": user_input,
                "messages": history,
            }):
                for _node_id, update in chunk.items():
                    if isinstance(update, dict):
                        st.update(update)
            return st

        state2 = asyncio.run(_run_round2(
            graph2, "一年级 古诗词",
            [
                {"role": "user", "content": "帮我做《静夜思》的课件"},
                {"role": "assistant", "content": "请提供年级和课型"},
            ],
        ))

        # 验证第二轮参数齐备
        assert state2.get("yuwen_params_ready") is True
        # 验证渲染
        assert "yuwen_files" in state2


# ======================================================================
# 8. 共通 schema 测试
# ======================================================================

class TestSchema:
    """common.schema 校验。"""

    def test_validate_valid_doc(self):
        """合法文档通过校验。"""
        from devpilot.agenthub.yuwen_skill.scripts.common.schema import validate

        doc = {
            "meta": {
                "title": "静夜思",
                "grade": 1,
                "lessonType": "古诗词",
            },
            "slides": [
                {
                    "id": "s01",
                    "kind": "cover",
                    "title": "静夜思",
                    "period": 1,
                    "elements": [
                        {"type": "heading", "content": "静夜思", "size": "h1"},
                    ],
                },
            ],
        }
        result = validate(doc)
        assert result["meta"]["stage"] == "低段"
        assert result["meta"]["periods"] == 2  # 古诗词默认 2 课时

    def test_validate_missing_meta_raises(self):
        """缺 meta 抛 SchemaError。"""
        from devpilot.agenthub.yuwen_skill.scripts.common.schema import validate, SchemaError

        with pytest.raises(SchemaError):
            validate({})

    def test_validate_invalid_lesson_type(self):
        """非法课型抛 SchemaError。"""
        from devpilot.agenthub.yuwen_skill.scripts.common.schema import validate, SchemaError

        with pytest.raises(SchemaError):
            validate({
                "meta": {"title": "静夜思", "grade": 1, "lessonType": "非法课型"},
                "slides": [{"id": "s01", "elements": [{"type": "heading", "content": "x", "size": "h1"}]}],
            })

    def test_validate_unknown_element_type(self):
        """未知元素类型抛 SchemaError。"""
        from devpilot.agenthub.yuwen_skill.scripts.common.schema import validate, SchemaError

        with pytest.raises(SchemaError):
            validate({
                "meta": {"title": "静夜思", "grade": 1, "lessonType": "古诗词"},
                "slides": [{"id": "s01", "elements": [{"type": "unknown_type"}]}],
            })

    def test_pinyin_split(self):
        """拼音拆分正确。"""
        from devpilot.agenthub.yuwen_skill.scripts.common.pinyin import split_syllables, tone_of

        pairs = split_syllables("静夜思", "jìng yè sī")
        assert len(pairs) == 3
        assert pairs[0][0] == "静"
        assert pairs[0][1] == "jìng"
        assert pairs[0][2] == 4  # 四声
        assert pairs[1][2] == 4
        assert pairs[2][2] == 1  # 一声

    def test_tone_color_mapping(self):
        """声调标色映射完整。"""
        from devpilot.agenthub.yuwen_skill.scripts.common.pinyin import tone_color

        assert tone_color(1) == "D9534F"
        assert tone_color(2) == "E8A33C"
        assert tone_color(3) == "5BA88A"
        assert tone_color(4) == "5B8AB5"
        assert tone_color(0) == "9AA0A6"