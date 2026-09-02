"""M3 story 剧本分镜智能体测试。

覆盖：
1. manifest 注册：agenthub 扫描发现 story
2. extract_brief：创意解析 / 追问 / 默认值补齐
3. 三确认点状态机：_stage_of 阶段推断、_route_after_brief 路由分流
4. gen_synopsis / gen_characters / gen_storyboard：LLM 产物校验与落盘
5. confirm_*：确认放行 / 自然语言修改 / 盘上找回
6. export：docx/xlsx/html 三件套真实生成（python-docx/openpyxl 可用）
7. 端到端冒烟：mock 网关跑通 梗概→确认→角色→确认→分镜→确认→导出

运行：
    PYTHONIOENCODING=utf-8 pytest tests/test_story_agent.py -q
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

PARAMS = {"title": "小北极熊回家", "audience": "儿童（6-8岁）", "genre": "冒险",
          "duration_min": 8, "style": "温暖手绘风"}

SYNOPSIS = {
    "title": "小北极熊回家", "logline": "迷路的小北极熊波波穿越冰原回家",
    "themes": ["勇气", "家庭"],
    "synopsis": "波波追蝴蝶迷路，遇到海雀奇奇，一起跨过冰裂缝……",
    "acts": [
        {"act": "第一幕·建置", "summary": "波波迷路"},
        {"act": "第二幕·对抗", "summary": "跨冰裂缝"},
        {"act": "第三幕·解决", "summary": "回家团圆"},
    ],
    "characters_brief": [
        {"name": "波波", "desc": "圆滚滚的小北极熊"},
        {"name": "奇奇", "desc": "话痨海雀"},
    ],
    "scene_count": 6,
}

CHARACTERS = {
    "characters": [
        {"id": "c1", "name": "波波", "role": "主角",
         "description": "圆滚滚的白色小北极熊，黑葡萄眼睛，围红色围巾",
         "ref_prompt": "小北极熊正面全身像，白色绒毛，红围巾，温暖手绘风，无文字，无水印"},
        {"id": "c2", "name": "奇奇", "role": "配角",
         "description": "黑白海雀，橙色喙，戴飞行员风镜",
         "ref_prompt": "海雀正面全身像，橙色喙，风镜，温暖手绘风，无文字，无水印"},
    ]
}

STORYBOARD = {
    "scenes": [
        {"scene_no": 1, "slug": "外景·北极冰原·清晨",
         "synopsis": "波波追蝴蝶离开妈妈视线",
         "shots": [
             {"id": "s1-01", "shot_size": "大远景", "camera": "固定",
              "subject": "冰原全景", "action": "波波小小的身影在雪地里追蝴蝶",
              "dialogue": "", "sfx": "风声",
              "image_prompt": "白色小北极熊红围巾在冰原追蝴蝶，温暖手绘风，无文字，无水印"},
             {"id": "s1-02", "shot_size": "特写", "camera": "推",
              "subject": "波波的脸", "action": "波波回头发现妈妈不见了",
              "dialogue": "妈妈？", "sfx": "",
              "image_prompt": "小北极熊惊讶回头特写，红围巾，温暖手绘风，无文字，无水印"},
         ]},
        {"scene_no": 2, "slug": "外景·冰裂缝·正午",
         "synopsis": "波波遇到奇奇，一起想办法",
         "shots": [
             {"id": "s2-01", "shot_size": "全景", "camera": "移",
              "subject": "冰裂缝", "action": "波波望着宽宽的裂缝发呆",
              "dialogue": "过不去呀", "sfx": "",
              "image_prompt": "小北极熊与海雀站在冰裂缝前，温暖手绘风，无文字，无水印"},
         ]},
    ]
}


@pytest.fixture
def outputs_tmp(tmp_path, monkeypatch):
    """把 story.state._OUTPUTS_DIR patch 到 tmp_path。

    DASHSCOPE_API_KEY 置空串而非 delenv：节点 import 链会经
    aidraft.config 触发 load_dotenv()，不存在的键会从 .env 复活，
    置空串则 dotenv 不覆盖（同 test_agenthub_yuwen_visual 的坑）。
    """
    from aidraft.agenthub.story import state as st
    monkeypatch.setattr(st, "_OUTPUTS_DIR", tmp_path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    return tmp_path


def _gw(response_json):
    """Mock 网关：chat 返回固定 JSON。"""
    gw = MagicMock()
    gw.chat.return_value = MagicMock(content=json.dumps(response_json,
                                                        ensure_ascii=False))
    return gw


# ---------------------------------------------------------------- 1. 注册

class TestManifest:
    def test_agenthub_discovers_story(self):
        from aidraft.agenthub import reset_cache, list_agents
        reset_cache()
        agents = {a.agent_id: a for a in list_agents()}
        assert "story" in agents
        m = agents["story"]
        assert m.display_name == "剧本分镜创作"
        assert m.graph_fn is not None
        assert m.managed_system is False


# ---------------------------------------------------------------- 2. extract_brief

class TestExtractBrief:
    def test_ready_with_defaults(self):
        """创意有实质内容即 ready，空受众/时长补默认。"""
        from aidraft.agenthub.story.nodes.extract_brief import _make_extract_brief_node
        gw = _gw({"title": "小北极熊回家", "audience": "", "genre": "",
                  "duration_min": 0, "style": "", "params_ready": True,
                  "question": "", "chips": []})
        node = _make_extract_brief_node(gw, None)
        result = asyncio.run(node({
            "task": "小北极熊想回家", "user_message": "小北极熊想回家",
            "messages": []}))
        assert result["story_params_ready"] is True
        p = result["story_params"]
        assert p["title"] == "小北极熊回家"
        assert p["audience"] == "全年龄"   # 空值默认
        assert p["duration_min"] == 8
        assert p["style"] == "温暖手绘风"

    def test_ask_when_empty(self):
        """无实质创意 → 追问 + chips。"""
        from aidraft.agenthub.story.nodes.extract_brief import _make_extract_brief_node
        gw = _gw({"title": "", "params_ready": False,
                  "question": "说说你的故事创意？", "chips": ["森林冒险", "太空旅行"]})
        node = _make_extract_brief_node(gw, None)
        result = asyncio.run(node({
            "task": "帮我写剧本", "user_message": "帮我写剧本", "messages": []}))
        assert result["story_params_ready"] is False
        assert "故事创意" in result["final_answer"]

    def test_duration_normalization(self):
        """duration 8.0(float)/"8"(str) 归一化。"""
        from aidraft.agenthub.story.nodes.extract_brief import _normalize_duration
        assert _normalize_duration(8.0) == 8
        assert _normalize_duration("8") == 8
        assert _normalize_duration("abc") == 0


# ---------------------------------------------------------------- 3. 状态机

class TestStateMachine:
    def test_stage_of(self, outputs_tmp):
        """阶段推断：确认点推进逐级升。"""
        from aidraft.agenthub.story.state import _stage_of
        assert _stage_of({}) == "brief"
        assert _stage_of({"story_synopsis": SYNOPSIS}) == "synopsis"
        assert _stage_of({"story_synopsis": SYNOPSIS,
                          "story_synopsis_confirmed": True}) == "characters"
        assert _stage_of({"story_synopsis_confirmed": True,
                          "story_characters_confirmed": True}) == "storyboard"
        assert _stage_of({"story_storyboard_confirmed": True}) == "export"

    def test_route_brief_first_round(self, outputs_tmp):
        """首轮：params ready + 盘空 → gen_synopsis。"""
        from aidraft.agenthub.story.graph import _route_after_brief
        got = _route_after_brief({"story_params_ready": True,
                                  "story_params": PARAMS})
        assert got == "gen_synopsis"

    def test_route_by_stage(self, outputs_tmp):
        """按盘上阶段路由到对应确认点。"""
        from aidraft.agenthub.story.graph import _route_after_brief
        from aidraft.agenthub.story.state import _save_state
        # synopsis 未确认
        _save_state(PARAMS, story_synopsis=SYNOPSIS,
                    story_params=PARAMS, story_synopsis_confirmed=False)
        assert _route_after_brief({"story_params_ready": True,
                                   "story_params": PARAMS}) == "confirm_synopsis"
        # characters 未确认
        _save_state(PARAMS, story_synopsis_confirmed=True,
                    story_characters=CHARACTERS, story_characters_confirmed=False)
        assert _route_after_brief({"story_params_ready": True,
                                   "story_params": PARAMS}) == "confirm_characters"
        # storyboard 未确认
        _save_state(PARAMS, story_characters_confirmed=True,
                    story_storyboard=STORYBOARD, story_storyboard_confirmed=False)
        assert _route_after_brief({"story_params_ready": True,
                                   "story_params": PARAMS}) == "confirm_storyboard"
        # 全确认 → export（续跑）
        _save_state(PARAMS, story_storyboard_confirmed=True)
        assert _route_after_brief({"story_params_ready": True,
                                   "story_params": PARAMS}) == "export"

    def test_route_chip_fallback(self, outputs_tmp):
        """params 不齐但消息像阶段应答 → 查盘兜底路由。"""
        from aidraft.agenthub.story.graph import _route_after_brief
        from aidraft.agenthub.story.state import _save_state
        _save_state(PARAMS, story_synopsis=SYNOPSIS,
                    story_params=PARAMS, story_synopsis_confirmed=False)
        got = _route_after_brief({"story_params_ready": False,
                                  "story_params": {},
                                  "user_message": "确认"})
        assert got == "confirm_synopsis"


# ---------------------------------------------------------------- 4. 生成节点

class TestGenNodes:
    def test_gen_synopsis_ok(self, outputs_tmp):
        from aidraft.agenthub.story.nodes.gen_synopsis import _make_gen_synopsis_node
        gw = _gw(SYNOPSIS)
        node = _make_gen_synopsis_node(gw, None)
        result = asyncio.run(node({"story_params": PARAMS}))
        assert result["story_synopsis"]["logline"] == SYNOPSIS["logline"]
        assert result["story_synopsis_confirmed"] is False
        from aidraft.agenthub.story.state import _load_state
        disk = _load_state(PARAMS)
        assert disk["story_synopsis"]["title"] == "小北极熊回家"

    def test_gen_synopsis_retry_on_bad_json(self, outputs_tmp):
        """JSON 解析失败 → 带反馈重试一次成功。"""
        from aidraft.agenthub.story.nodes.gen_synopsis import _make_gen_synopsis_node
        gw = MagicMock()
        gw.chat.side_effect = [
            MagicMock(content="这不是JSON"),
            MagicMock(content=json.dumps(SYNOPSIS, ensure_ascii=False)),
        ]
        node = _make_gen_synopsis_node(gw, None)
        result = asyncio.run(node({"story_params": PARAMS}))
        assert result["story_synopsis"]["logline"]
        assert gw.chat.call_count == 2

    def test_gen_characters_ok(self, outputs_tmp):
        from aidraft.agenthub.story.nodes.gen_characters import _make_gen_characters_node
        from aidraft.agenthub.story.state import _save_state
        _save_state(PARAMS, story_synopsis=SYNOPSIS, story_params=PARAMS)
        gw = _gw(CHARACTERS)
        node = _make_gen_characters_node(gw, None)
        result = asyncio.run(node({"story_params": PARAMS}))
        chars = result["story_characters"]["characters"]
        assert len(chars) == 2
        assert chars[0]["ref_prompt"]  # 视觉锚点字段在

    def test_gen_storyboard_ok(self, outputs_tmp):
        from aidraft.agenthub.story.nodes.gen_storyboard import _make_gen_storyboard_node
        gw = _gw(STORYBOARD)
        node = _make_gen_storyboard_node(gw, None)
        result = asyncio.run(node({"story_params": PARAMS,
                                   "story_synopsis": SYNOPSIS,
                                   "story_characters": CHARACTERS}))
        scenes = result["story_storyboard"]["scenes"]
        assert len(scenes) == 2
        assert scenes[0]["shots"][0]["id"] == "s1-01"

    def test_gen_storyboard_injects_character_anchor(self, outputs_tmp):
        """分镜 prompt 注入角色卡（image_prompt 复用视觉特征的原料）。"""
        from aidraft.agenthub.story.nodes.gen_storyboard import _make_gen_storyboard_node
        gw = _gw(STORYBOARD)
        node = _make_gen_storyboard_node(gw, None)
        asyncio.run(node({"story_params": PARAMS,
                          "story_synopsis": SYNOPSIS,
                          "story_characters": CHARACTERS}))
        system_prompt = gw.chat.call_args[0][0][0].content
        # 角色描述拼进了 prompt（双层锚点的前置条件）
        assert "红围巾" in system_prompt
        assert "风镜" in system_prompt


# ---------------------------------------------------------------- 5. 确认节点

class TestConfirmNodes:
    def _disk_with_synopsis(self, outputs_tmp):
        from aidraft.agenthub.story.state import _save_state
        _save_state(PARAMS, story_synopsis=SYNOPSIS, story_params=PARAMS,
                    story_synopsis_confirmed=False)

    def test_confirm_synopsis_confirm(self, outputs_tmp):
        self._disk_with_synopsis(outputs_tmp)
        from aidraft.agenthub.story.nodes.confirm_synopsis import _make_confirm_synopsis_node
        node = _make_confirm_synopsis_node(MagicMock(), None)
        result = asyncio.run(node({"story_params": PARAMS,
                                   "user_message": "确认"}))
        assert result["story_synopsis_confirmed"] is True

    def test_confirm_synopsis_edit(self, outputs_tmp):
        """修改指令 → LLM 改稿 → 未确认。"""
        self._disk_with_synopsis(outputs_tmp)
        edited = dict(SYNOPSIS)
        edited["logline"] = "改过的 logline"
        gw = _gw(edited)
        from aidraft.agenthub.story.nodes.confirm_synopsis import _make_confirm_synopsis_node
        node = _make_confirm_synopsis_node(gw, None)
        result = asyncio.run(node({"story_params": PARAMS,
                                   "user_message": "主角改成小企鹅"}))
        assert result["story_synopsis_confirmed"] is False
        assert result["story_synopsis"]["logline"] == "改过的 logline"

    def test_confirm_synopsis_edit_fail_keeps_original(self, outputs_tmp):
        """改稿失败 → 保留原稿不阻断。"""
        self._disk_with_synopsis(outputs_tmp)
        gw = MagicMock()
        gw.chat.side_effect = RuntimeError("llm down")
        from aidraft.agenthub.story.nodes.confirm_synopsis import _make_confirm_synopsis_node
        node = _make_confirm_synopsis_node(gw, None)
        result = asyncio.run(node({"story_params": PARAMS,
                                   "user_message": "主角改成小企鹅"}))
        assert result["story_synopsis_confirmed"] is False
        assert result["story_synopsis"]["logline"] == SYNOPSIS["logline"]

    def test_confirm_characters_confirm(self, outputs_tmp):
        from aidraft.agenthub.story.state import _save_state
        _save_state(PARAMS, story_synopsis=SYNOPSIS, story_params=PARAMS,
                    story_synopsis_confirmed=True,
                    story_characters=CHARACTERS, story_characters_confirmed=False)
        from aidraft.agenthub.story.nodes.confirm_characters import _make_confirm_characters_node
        node = _make_confirm_characters_node(MagicMock(), None)
        result = asyncio.run(node({"story_params": PARAMS,
                                   "user_message": "可以"}))
        assert result["story_characters_confirmed"] is True

    def test_confirm_storyboard_confirm(self, outputs_tmp):
        from aidraft.agenthub.story.state import _save_state
        _save_state(PARAMS, story_params=PARAMS,
                    story_storyboard=STORYBOARD, story_storyboard_confirmed=False)
        from aidraft.agenthub.story.nodes.confirm_storyboard import _make_confirm_storyboard_node
        node = _make_confirm_storyboard_node(MagicMock(), None)
        result = asyncio.run(node({"story_params": PARAMS,
                                   "user_message": "确认分镜，导出吧"}))
        assert result["story_storyboard_confirmed"] is True


# ---------------------------------------------------------------- 6. export

class TestExport:
    def test_export_three_files(self, outputs_tmp):
        """导出 docx/xlsx/html 三件套（真实文件生成）。"""
        docx = pytest.importorskip("docx")
        openpyxl = pytest.importorskip("openpyxl")
        from aidraft.agenthub.story.nodes.export import _make_export_node
        node = _make_export_node(None)
        result = asyncio.run(node({
            "story_params": PARAMS,
            "story_synopsis": SYNOPSIS,
            "story_characters": CHARACTERS,
            "story_storyboard": STORYBOARD,
        }))
        files = result["story_files"]
        names = {f["name"] for f in files}
        assert any(n.endswith("_剧本.docx") for n in names)
        assert any(n.endswith("_分镜表.xlsx") for n in names)
        assert any(n.endswith("_预览.html") for n in names)
        assert result["story_error"] == ""

        # xlsx 内容抽查：表头 + 一行一镜
        xlsx_path = outputs_tmp / "story" / "小北极熊回家" / "小北极熊回家_分镜表.xlsx"
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb.active
        assert ws.cell(1, 1).value == "场号"
        assert ws.cell(2, 3).value == "s1-01"
        assert ws.cell(3, 3).value == "s1-02"
        assert ws.cell(4, 3).value == "s2-01"

        # docx 内容抽查：标题 + 场次
        d = docx.Document(str(outputs_tmp / "story" / "小北极熊回家"
                              / "小北极熊回家_剧本.docx"))
        full_text = "\n".join(p.text for p in d.paragraphs)
        assert "小北极熊回家" in full_text
        assert "第1场" in full_text
        assert "妈妈？" in full_text

        # html 抽查
        html_text = (outputs_tmp / "story" / "小北极熊回家"
                     / "小北极熊回家_预览.html").read_text(encoding="utf-8")
        assert "分镜预览" in html_text
        assert "s1-01" in html_text
        assert "红围巾" in html_text  # 角色描述进预览

    def test_export_missing_storyboard(self, outputs_tmp):
        from aidraft.agenthub.story.nodes.export import _make_export_node
        node = _make_export_node(None)
        result = asyncio.run(node({"story_params": PARAMS}))
        assert result["story_error"]

    def test_export_html_escapes(self, outputs_tmp):
        """HTML 转义：台词含 <script> 不注入。"""
        from aidraft.agenthub.story.nodes.export import _make_export_node
        bad_board = json.loads(json.dumps(STORYBOARD, ensure_ascii=False))
        bad_board["scenes"][0]["shots"][0]["dialogue"] = "<script>alert(1)</script>"
        node = _make_export_node(None)
        asyncio.run(node({"story_params": PARAMS,
                          "story_synopsis": SYNOPSIS,
                          "story_characters": CHARACTERS,
                          "story_storyboard": bad_board}))
        html_path = outputs_tmp / "story" / "小北极熊回家" / "小北极熊回家_预览.html"
        text = html_path.read_text(encoding="utf-8")
        assert "<script>alert" not in text
        assert "&lt;script&gt;" in text


# ---------------------------------------------------------------- 7. 端到端

class TestEndToEnd:
    def test_full_pipeline_multiround(self, outputs_tmp):
        """多轮冒烟：梗概→确认→角色→确认→分镜→确认→导出（mock 网关）。

        每轮 astream 新 state（模拟跨轮）——落盘 state.json 是唯一记忆。
        """
        import pytest_asyncio  # noqa: F401  # 确认依赖存在
        from aidraft.agenthub.story.graph import build_graph
        from aidraft.agenthub.story.state import _load_state

        # 网关按 system prompt 关键词分流返回对应产物
        def fake_chat(msgs, **kwargs):
            system = msgs[0].content
            if "参数提取助手" in system:
                return MagicMock(content=json.dumps({
                    "title": "小北极熊回家", "audience": "儿童（6-8岁）",
                    "genre": "冒险", "duration_min": 8, "style": "温暖手绘风",
                    "params_ready": True, "question": "", "chips": []},
                    ensure_ascii=False))
            if "剧本策划" in system:
                return MagicMock(content=json.dumps(SYNOPSIS, ensure_ascii=False))
            if "角色设计师" in system:
                return MagicMock(content=json.dumps(CHARACTERS, ensure_ascii=False))
            if "分镜师" in system:
                return MagicMock(content=json.dumps(STORYBOARD, ensure_ascii=False))
            return MagicMock(content=json.dumps({}, ensure_ascii=False))

        gw = MagicMock()
        gw.chat.side_effect = fake_chat

        async def run_round(user_msg):
            g = build_graph(gw, MagicMock())
            state = {"user_message": user_msg, "task": user_msg,
                     "messages": []}
            final = {}
            async for chunk in g.astream(state, stream_mode="updates"):
                for node_result in chunk.values():
                    if isinstance(node_result, dict):
                        final.update(node_result)
            return final

        # 轮1：创意 → 梗概（END 等确认）
        r1 = asyncio.run(run_round("写一个小北极熊回家的儿童短片"))
        assert r1.get("story_synopsis", {}).get("logline")

        # 轮2：确认梗概 → 角色卡（END 等确认）
        r2 = asyncio.run(run_round("确认"))
        assert r2.get("story_characters", {}).get("characters")

        # 轮3：确认角色 → 立绘（无 key 跳过）→ 分镜（END 等确认）
        r3 = asyncio.run(run_round("确认"))
        assert r3.get("story_storyboard", {}).get("scenes")

        # 轮4：确认分镜 → 导出 + 报告
        r4 = asyncio.run(run_round("确认"))
        assert r4.get("story_files")
        assert len(r4["story_files"]) >= 3

        # 盘上终态
        disk = _load_state(PARAMS)
        assert disk.get("story_storyboard_confirmed") is True
