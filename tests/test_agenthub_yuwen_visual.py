"""visual_review 节点测试：渲染后视觉审查（降级路径 + 真实 pdf 抽查路径）。

覆盖：
1. 无 DASHSCOPE_API_KEY → available=false 降级帧
2. 无 soffice（mock which=None）→ 降级 reason 含 LibreOffice
3. session 无 pptx → 降级
4. 正常路径：PyMuPDF 真实生成空白 pdf + mock soffice 转换与 VLM →
   visual 帧契约、score 平均、issues 合并带 page_id、越界枚举归一化
5. 单页 VLM 输出解析失败 → 该页跳过，其余正常
6. 抽查页全部失败 → available=false
7. VLMReview env 开关与默认模型
8. _visual_note（report 摘要片段）三态
9. visual_fix 修复闭环：路由放行 / 修复回写 / 降分回滚 / 复查透传 /
   成本护栏（severity、页数上限）/ 图接线

运行：
    pytest tests/test_agenthub_yuwen_visual.py -x -v
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


PARAMS = {"title": "静夜思", "grade": 1, "lesson_type": "古诗词",
          "textbook": "部编版一年级下册"}


def _doc(n_slides: int = 3) -> dict:
    return {
        "version": "1.0",
        "meta": {"title": "静夜思", "stage": "低段", "grade": 1,
                 "lessonType": "古诗词"},
        "slides": [{"id": f"s0{i+1}", "kind": "cover", "title": f"标题{i+1}",
                    "period": 1,
                    "elements": [{"type": "heading", "content": f"页{i+1}",
                                  "size": "h1"}]}
                   for i in range(n_slides)],
        "lessonPlan": {}, "handout": {"levels": []},
    }


@pytest.fixture
def outputs_tmp(tmp_path, monkeypatch):
    """state._OUTPUTS_DIR 隔离到 tmp + 清空 DASHSCOPE env（防本机 .env 泄漏）。

    注意用 setenv("") 而非 delenv：节点 import 链会经 aidraft.config 触发
    load_dotenv()，不存在的键会从 .env 复活；置空串则 dotenv 不覆盖。
    """
    from aidraft.agenthub.yuwen import state as st
    monkeypatch.setattr(st, "_OUTPUTS_DIR", tmp_path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.delenv("DASHSCOPE_VL_MODEL", raising=False)
    # base env 也置空：本机 .env 可能配了专用端点，断言默认值需稳定
    monkeypatch.setenv("DASHSCOPE_IMAGE_BASE", "")
    return tmp_path


def _run(state: dict) -> tuple[dict, list]:
    from aidraft.agenthub.yuwen.nodes.visual_review import _make_visual_review_node
    frames: list = []
    node = _make_visual_review_node(lambda f: frames.append(f))
    result = asyncio.run(node(state))
    return result, frames


def _visual_frames(frames: list) -> list:
    return [f for f in frames if f.get("type") == "visual"]


def _fake_vlm(payloads: list[str]):
    """构造 VLMReview 替身：available=True，review_page 按序返回 payloads。"""
    inst = MagicMock()
    inst.available = True
    inst.review_page = AsyncMock(side_effect=payloads)
    cls = MagicMock(return_value=inst)
    return cls, inst


def _seed_session(tmp_path, n_pdf_pages: int = 3) -> Path:
    """预置 session 目录：假 pptx + 真实空白 pdf（PyMuPDF 生成）。

    返回 pdf 路径——测试 patch 掉 _convert_pptx_to_pdf 直接指向它，
    绕开 soffice 子进程（soffice 转换本身由真机手工验收覆盖）。
    """
    import pymupdf as fitz
    from aidraft.agenthub.yuwen import state as st
    session_dir = st._session_dir(PARAMS)
    (session_dir / "review").mkdir(parents=True, exist_ok=True)
    (session_dir / "静夜思.pptx").write_bytes(b"fake-pptx-bytes")
    doc = fitz.open()
    for _ in range(n_pdf_pages):
        doc.new_page()
    pdf_path = session_dir / "review" / "静夜思.pdf"
    doc.save(pdf_path)
    doc.close()
    return pdf_path


# ======================================================================
# 降级路径
# ======================================================================

class TestVisualDegraded:
    """三种缺席原因都发 available=false 帧且 step 正常收尾，绝不 raise。"""

    def test_no_api_key(self, outputs_tmp):
        result, frames = _run({"yuwen_params": PARAMS, "yuwen_content": _doc()})
        assert result["yuwen_visual"]["available"] is False
        assert "DASHSCOPE_API_KEY" in result["yuwen_visual"]["reason"]
        vf = _visual_frames(frames)
        assert len(vf) == 1
        assert vf[0]["visual"]["available"] is False
        # step 有 running 与 done（前端时间线不留"运行中"）
        steps = [f for f in frames if f.get("type") == "step"]
        assert {s["status"] for s in steps} == {"running", "done"}
        assert "visual_review" in result["nodes_visited"]

    def test_no_soffice(self, outputs_tmp, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        _seed_session(outputs_tmp)
        with patch("aidraft.agenthub.yuwen.nodes.visual_review.shutil.which",
                   return_value=None):
            result, frames = _run({"yuwen_params": PARAMS,
                                   "yuwen_content": _doc()})
        assert result["yuwen_visual"]["available"] is False
        assert "LibreOffice" in result["yuwen_visual"]["reason"]

    def test_no_pptx(self, outputs_tmp, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        # session 目录存在但无 pptx
        (outputs_tmp / "yuwen" / "静夜思-古诗词").mkdir(parents=True,
                                                        exist_ok=True)
        result, _ = _run({"yuwen_params": PARAMS, "yuwen_content": _doc()})
        assert result["yuwen_visual"]["available"] is False
        assert "PPTX" in result["yuwen_visual"]["reason"]

    def test_convert_failure(self, outputs_tmp, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        _seed_session(outputs_tmp)
        with patch("aidraft.agenthub.yuwen.nodes.visual_review.shutil.which",
                   return_value="soffice"), \
             patch("aidraft.agenthub.yuwen.nodes.visual_review._convert_pptx_to_pdf",
                   return_value=None):
            result, _ = _run({"yuwen_params": PARAMS, "yuwen_content": _doc()})
        assert result["yuwen_visual"]["available"] is False
        assert "转换 PDF 失败" in result["yuwen_visual"]["reason"]

    def test_all_pages_unparseable(self, outputs_tmp, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        pdf = _seed_session(outputs_tmp, n_pdf_pages=2)
        cls, _ = _fake_vlm(["彻底不是 JSON"] * 2)
        with patch("aidraft.agenthub.yuwen.nodes.visual_review.shutil.which",
                   return_value="soffice"), \
             patch("aidraft.agenthub.yuwen.nodes.visual_review._convert_pptx_to_pdf",
                   return_value=pdf), \
             patch("aidraft.agenthub.yuwen.vlm.VLMReview", cls):
            result, frames = _run({"yuwen_params": PARAMS,
                                   "yuwen_content": _doc(2)})
        assert result["yuwen_visual"]["available"] is False
        assert len(_visual_frames(frames)) == 1


# ======================================================================
# 正常路径（真实 pdf + mock VLM）
# ======================================================================

class TestVisualHappyPath:
    """帧契约、平均分、issues 合并归一化。"""

    def _happy(self, outputs_tmp, monkeypatch, payloads):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        pdf = _seed_session(outputs_tmp, n_pdf_pages=len(payloads))
        cls, inst = _fake_vlm(payloads)
        with patch("aidraft.agenthub.yuwen.nodes.visual_review.shutil.which",
                   return_value="soffice"), \
             patch("aidraft.agenthub.yuwen.nodes.visual_review._convert_pptx_to_pdf",
                   return_value=pdf), \
             patch("aidraft.agenthub.yuwen.vlm.VLMReview", cls):
            result, frames = _run({"yuwen_params": PARAMS,
                                   "yuwen_content": _doc(len(payloads))})
        return result, frames, inst

    def test_frame_contract_and_average(self, outputs_tmp, monkeypatch):
        p1 = {"score": 90, "issues": [
            {"type": "text_too_small", "severity": "medium",
             "bbox": [10, 20, 300, 200], "suggestion": "增大正文到24pt"}]}
        p2 = {"score": 70, "issues": []}
        p3 = {"score": 95, "issues": [
            {"type": "bogus_type", "severity": "weird"}]}  # 越界归一化
        result, frames, inst = self._happy(
            outputs_tmp, monkeypatch,
            [json.dumps(p1), json.dumps(p2), json.dumps(p3)])

        vf = _visual_frames(frames)
        assert len(vf) == 1
        v = vf[0]["visual"]
        # 契约键齐全
        assert set(v.keys()) == {"available", "reason", "score", "pages",
                                 "issues"}
        assert v["available"] is True and v["reason"] == ""
        assert v["score"] == round((90 + 70 + 95) / 3)  # 85
        assert [p["page_id"] for p in v["pages"]] == ["s01", "s02", "s03"]
        assert v["pages"][0]["image"] == \
            "/files/yuwen/静夜思-古诗词/review/s00.png"
        assert v["pages"][1]["image"] == \
            "/files/yuwen/静夜思-古诗词/review/s01.png"
        # png 真实落盘
        review_dir = outputs_tmp / "yuwen" / "静夜思-古诗词" / "review"
        assert (review_dir / "s00.png").exists()
        # issues 合并带 page_id，越界枚举归一化
        issues = v["issues"]
        assert len(issues) == 2
        assert issues[0] == {"page_id": "s01", "type": "text_too_small",
                             "severity": "medium",
                             "bbox": [10, 20, 300, 200],
                             "suggestion": "增大正文到24pt"}
        assert issues[1]["page_id"] == "s03"
        assert issues[1]["type"] == "other" and issues[1]["severity"] == "low"
        assert issues[1]["bbox"] == []
        # prompt 带课文/页上下文；图片 bytes 传给了 VLM
        assert inst.review_page.call_count == 3
        first_prompt = inst.review_page.call_args_list[0][0][1]
        assert "《静夜思》" in first_prompt and "标题1" in first_prompt
        assert "图片是否与课文主题匹配" in first_prompt
        assert inst.review_page.call_args_list[0][0][0][:4] == b"\x89PNG"

    def test_single_page_failure_skipped(self, outputs_tmp, monkeypatch):
        """第 2 页返回坏 JSON → 跳过该页，其余正常汇总。"""
        good = json.dumps({"score": 80, "issues": []})
        result, frames, _ = self._happy(
            outputs_tmp, monkeypatch, [good, "不是JSON", good])
        v = _visual_frames(frames)[0]["visual"]
        assert v["available"] is True
        assert [p["page_id"] for p in v["pages"]] == ["s01", "s03"]
        assert v["score"] == 80
        done = [f for f in frames if f.get("type") == "step"
                and f["status"] == "done"][-1]
        assert "跳过 1 页" in done["detail"]


# ======================================================================
# 抽查上限（确定性）
# ======================================================================

class TestSampling:
    def test_under_limit_all_pages(self):
        from aidraft.agenthub.yuwen.nodes.visual_review import _sample_page_indices
        assert _sample_page_indices(3, 8) == [0, 1, 2]

    def test_over_limit_deterministic(self):
        from aidraft.agenthub.yuwen.nodes.visual_review import _sample_page_indices
        a, b = _sample_page_indices(12, 8), _sample_page_indices(12, 8)
        assert a == b, "固定种子抽查必须可重现"
        assert len(a) == 8
        assert a[:2] == [0, 1], "前 2 页必查"

    def test_env_limit_override(self, outputs_tmp, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_VL_MAX_PAGES", "3")
        from aidraft.agenthub.yuwen.nodes.visual_review import _max_pages
        assert _max_pages() == 3
        # 非法值回退默认（默认 14：线上一课 13-14 页，默认即全查）
        monkeypatch.setenv("DASHSCOPE_VL_MAX_PAGES", "abc")
        assert _max_pages() == 14
        monkeypatch.delenv("DASHSCOPE_VL_MAX_PAGES")
        assert _max_pages() == 14


# ======================================================================
# soffice → pdf 转换子进程（命令形状与失败兜底）
# ======================================================================

class TestSofficeConversion:
    """不真调 soffice：断言命令行形状（profile 隔离 / headless / outdir）。"""

    def test_command_shape_and_pdf_found(self, tmp_path):
        from aidraft.agenthub.yuwen.nodes.visual_review import _convert_pptx_to_pdf
        pptx = tmp_path / "静夜思.pptx"
        pptx.write_bytes(b"x")
        review_dir = tmp_path / "review"
        review_dir.mkdir()

        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["timeout"] = kw.get("timeout")
            # 模拟 soffice 行为：按输入 stem 在 outdir 落 pdf
            (review_dir / "静夜思.pdf").write_bytes(b"%PDF")
            return MagicMock(returncode=0)

        with patch("aidraft.agenthub.yuwen.nodes.visual_review.subprocess.run",
                   side_effect=fake_run):
            got = _convert_pptx_to_pdf("soffice", pptx, review_dir)
        assert got == review_dir / "静夜思.pdf"
        assert captured["timeout"] == 90
        cmd = captured["cmd"]
        assert cmd[0] == "soffice"
        assert cmd[1].startswith("-env:UserInstallation=file:///")
        assert "--headless" in cmd and "--convert-to" in cmd
        assert "pdf" in cmd
        i = cmd.index("--outdir")
        assert Path(cmd[i + 1]) == review_dir and cmd[i + 2] == str(pptx)

    def test_timeout_returns_none(self, tmp_path):
        import subprocess as sp
        from aidraft.agenthub.yuwen.nodes.visual_review import _convert_pptx_to_pdf
        pptx = tmp_path / "a.pptx"
        pptx.write_bytes(b"x")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        with patch("aidraft.agenthub.yuwen.nodes.visual_review.subprocess.run",
                   side_effect=sp.TimeoutExpired(cmd="soffice", timeout=90)):
            assert _convert_pptx_to_pdf("soffice", pptx, review_dir) is None

    def test_no_pdf_output_returns_none(self, tmp_path):
        from aidraft.agenthub.yuwen.nodes.visual_review import _convert_pptx_to_pdf
        pptx = tmp_path / "a.pptx"
        pptx.write_bytes(b"x")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        with patch("aidraft.agenthub.yuwen.nodes.visual_review.subprocess.run",
                   return_value=MagicMock(returncode=0)):
            assert _convert_pptx_to_pdf("soffice", pptx, review_dir) is None


# ======================================================================
# 图接线：render → visual_review → report
# ======================================================================

class TestGraphWiring:
    def test_node_registered_and_edges(self):
        from aidraft.agenthub.yuwen.graph import build_graph
        graph = build_graph(gateway=MagicMock(), registry=MagicMock())
        assert "visual_review" in graph.nodes
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        assert ("render", "visual_review") in edges
        assert ("visual_review", "report") in edges
        assert ("render", "report") not in edges, "旧边应被替换"


# ======================================================================
# vlm 客户端
# ======================================================================

class TestVLMClient:
    def test_unavailable_without_env(self, monkeypatch):
        from aidraft.agenthub.yuwen import vlm as v
        # 置空串防 load_dotenv 从 .env 复活真 key / 真 base
        monkeypatch.setenv("DASHSCOPE_API_KEY", "")
        monkeypatch.setenv("DASHSCOPE_IMAGE_BASE", "")
        assert v.VLMReview().available is False
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        c = v.VLMReview()
        assert c.available is True and c._model
        # DASHSCOPE_IMAGE_BASE 置空（fixture）→ 回退公共百炼端点
        assert c._base == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_review_page_sends_data_uri(self, monkeypatch):
        """消息结构：image_url data URI + text，走 chat.completions。"""
        from aidraft.agenthub.yuwen import vlm as v
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        monkeypatch.setenv("DASHSCOPE_VL_MODEL", "qwen-vl-max")
        # base 由 DASHSCOPE_IMAGE_BASE 覆盖（与生图共用同一 env）
        monkeypatch.setenv("DASHSCOPE_IMAGE_BASE",
                           "https://token-plan.example.com/compatible-mode/v1")
        captured: dict = {}

        class _Completions:
            async def create(self, **kw):
                captured.update(kw)
                resp = MagicMock()
                resp.choices = [MagicMock()]
                resp.choices[0].message.content = " {\"score\": 90} "
                return resp

        class _Client:
            def __init__(self, **kw):
                captured["client_kw"] = kw
                self.chat = MagicMock()
                self.chat.completions = _Completions()

        import openai
        monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
        got = asyncio.run(v.VLMReview().review_page(b"PNGDATA", "检查这一页"))
        assert got == '{"score": 90}'
        # base 由 DASHSCOPE_IMAGE_BASE 覆盖（与生图共用同一 env）
        assert captured["client_kw"]["base_url"] == \
            "https://token-plan.example.com/compatible-mode/v1"
        assert captured["model"] == "qwen-vl-max"
        content = captured["messages"][0]["content"]
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"].startswith(
            "data:image/png;base64,")
        assert content[1] == {"type": "text", "text": "检查这一页"}

    def test_empty_response_raises(self, monkeypatch):
        from aidraft.agenthub.yuwen import vlm as v
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")

        class _Completions:
            async def create(self, **kw):
                resp = MagicMock()
                resp.choices = [MagicMock()]
                resp.choices[0].message.content = "  "
                return resp

        class _Client:
            def __init__(self, **kw):
                self.chat = MagicMock()
                self.chat.completions = _Completions()

        import openai
        monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
        with pytest.raises(RuntimeError):
            asyncio.run(v.VLMReview().review_page(b"x", "p"))


# ======================================================================
# report 摘要片段
# ======================================================================

class TestVisualNote:
    def test_note_states(self):
        from aidraft.agenthub.yuwen.nodes.report import _visual_note
        assert _visual_note({}) == ""
        assert _visual_note({"yuwen_visual": {
            "available": False, "reason": "未配置 DASHSCOPE_API_KEY",
            "score": 0, "pages": [], "issues": []}}) \
            == "视觉审查未启用（未配置 DASHSCOPE_API_KEY）"
        note = _visual_note({"yuwen_visual": {
            "available": True, "reason": "", "score": 86, "pages": [],
            "issues": [{"severity": "high"}, {"severity": "medium"},
                       {"severity": "medium"}]}})
        assert note == "视觉审查 86 分，3 个问题（高 1 / 中 2）"
        # 零问题不带计数
        assert _visual_note({"yuwen_visual": {
            "available": True, "score": 100, "issues": []}}) \
            == "视觉审查 100 分，0 个问题"


class TestReportWithVisual:
    """report 的 done answer 拼入视觉摘要（真实 report 节点级验证）。"""

    def _report(self, state):
        from aidraft.agenthub.yuwen.nodes.report import _make_report_node
        frames: list = []
        result = asyncio.run(
            _make_report_node(lambda f: frames.append(f))(state))
        done = [f for f in frames if f.get("type") == "done"]
        return result, done

    def test_answer_includes_visual(self, outputs_tmp):
        state = {"yuwen_params": PARAMS,
                 "yuwen_files": [{"name": "a.pptx", "path": "/files/x",
                                  "size": 1, "mime": "m"}],
                 "yuwen_visual": {"available": True, "reason": "",
                                  "score": 86, "pages": [],
                                  "issues": [{"severity": "high"}]}}
        result, done = self._report(state)
        assert "视觉审查 86 分，1 个问题（高 1）" in result["final_answer"]
        assert "视觉审查 86 分" in done[0]["answer"]

    def test_answer_includes_unavailable(self, outputs_tmp):
        state = {"yuwen_params": PARAMS,
                 "yuwen_files": [{"name": "a.pptx", "path": "/files/x",
                                  "size": 1, "mime": "m"}],
                 "yuwen_visual": {"available": False,
                                  "reason": "未安装 LibreOffice，无法转页面图",
                                  "score": 0, "pages": [], "issues": []}}
        result, _ = self._report(state)
        assert "视觉审查未启用（未安装 LibreOffice，无法转页面图）" \
            in result["final_answer"]


# ======================================================================
# visual_fix 修复闭环
# ======================================================================

def _visual(score: int, issues: list, n_pages: int = 3) -> dict:
    """构造 visual_review 输出帧（available=True）。"""
    return {"available": True, "reason": "", "score": score,
            "pages": [{"page_id": f"s0{i+1}", "score": score}
                      for i in range(n_pages)],
            "issues": issues}


def _issue(page_id: str, typ: str = "text_too_small",
           sev: str = "medium") -> dict:
    return {"page_id": page_id, "type": typ, "severity": sev,
            "bbox": [], "suggestion": "增大正文字号到 24pt"}


def _fixed_page(i: int) -> dict:
    """visual_fix LLM 重生成返回的合法单页（title 带"修复"便于断言）。"""
    return {"id": f"s0{i}", "kind": "cover", "title": f"修复{i}", "period": 1,
            "elements": [{"type": "heading", "content": f"新内容{i}",
                          "size": "h1"}]}


def _resp(page: dict):
    """gateway.chat 返回值替身（_call_llm 只消费 .content）。"""
    return MagicMock(content=json.dumps(page, ensure_ascii=False))


def _run_fix(state: dict, gateway: MagicMock) -> tuple[dict, list]:
    from aidraft.agenthub.yuwen.nodes.visual_fix import _make_visual_fix_node
    frames: list = []
    node = _make_visual_fix_node(gateway, lambda f: frames.append(f))
    result = asyncio.run(node(state))
    return result, frames


def _tmp_doc(tmp_path) -> dict:
    """读回 tmp_content.json（visual_fix 回写的盘上 doc）。"""
    from aidraft.agenthub.yuwen import state as st
    p = st._content_path(PARAMS)
    return json.loads(p.read_text(encoding="utf-8"))


class TestRouteAfterVisual:
    """闭环总闸路由：放行条件与进入修复条件。"""

    def _route(self, state):
        from aidraft.agenthub.yuwen.graph import _route_after_visual
        return _route_after_visual(state)

    def test_unavailable_to_report(self):
        assert self._route({"yuwen_visual": {"available": False, "reason": "无key",
                                             "score": 0, "pages": [],
                                             "issues": []}}) == "report"

    def test_no_issues_to_report(self):
        assert self._route({"yuwen_visual": _visual(95, [])}) == "report"

    def test_low_only_to_report(self):
        # low 只统计不修（成本护栏）
        assert self._route({"yuwen_visual": _visual(80, [_issue("s01", sev="low")])}) \
            == "report"

    def test_render_layer_only_to_report(self):
        # color/theme 属渲染层，重生成内容无意义
        v = _visual(80, [_issue("s01", "color_mismatch", "high"),
                         _issue("s02", "theme_mismatch", "high")])
        assert self._route({"yuwen_visual": v}) == "report"

    def test_medium_small_deck_to_fix(self):
        assert self._route({"yuwen_visual": _visual(80, [_issue("s01")],
                                                   n_pages=3)}) == "visual_fix"

    def test_medium_large_deck_too_to_fix(self):
        """medium 的 deck 页数门槛已放开：抽查 14 页的 medium 同样进修复。

        原 _MEDIUM_MAX_PAGES=4 门槛在默认抽查上限提到 14 后等于永久
        封死 medium——用户实测缺陷（初读节奏不可读）正是 medium 级。
        """
        v = _visual(80, [_issue("s05", "text_too_small", "medium")],
                    n_pages=14)
        assert self._route({"yuwen_visual": v}) == "visual_fix"

    def test_high_always_to_fix(self):
        # 高严重度不受页数门槛限制
        v = _visual(60, [_issue("s01", sev="high")], n_pages=8)
        assert self._route({"yuwen_visual": v}) == "visual_fix"

    def test_rounds_exhausted_to_report(self):
        v = _visual(80, [_issue("s01", sev="high")])
        assert self._route({"yuwen_visual": v,
                            "yuwen_visual_fix_rounds": 1}) == "report"

    def test_pending_to_compare(self):
        # 修复做完、复查回来 → 再进 visual_fix 走对比阶段
        assert self._route({"yuwen_visual": _visual(80, []),
                            "yuwen_visual_fix_rounds": 1,
                            "yuwen_visual_fix_pending": True}) == "visual_fix"

    def test_route_after_fix(self):
        from aidraft.agenthub.yuwen.graph import _route_after_fix
        assert _route_after_fix({"yuwen_visual_fix_pending": True}) == "render"
        assert _route_after_fix({"yuwen_visual_fix_rollback": True}) == "render"
        assert _route_after_fix({}) == "report"


class TestVisualFixRepair:
    """修复阶段：挑页重生成、回写 doc + 盘、备份与计数。"""

    def test_medium_issue_regenerates_page(self, outputs_tmp):
        gw = MagicMock()
        gw.chat.return_value = _resp(_fixed_page(1))
        state = {"yuwen_params": PARAMS, "yuwen_content": _doc(3),
                 "yuwen_visual": _visual(70, [_issue("s01")])}
        result, frames = _run_fix(state, gw)

        # 页内容已换成重生成版（title 被锁定逻辑保留原标题）
        s01 = result["yuwen_content"]["slides"][0]
        assert s01["title"] == "标题1"          # 身份三键锁定
        assert s01["elements"][0]["content"] == "新内容1"  # 内容已更新
        # 盘上 tmp_content.json 同步回写（render 读盘）
        assert _tmp_doc(outputs_tmp)["slides"][0]["elements"][0]["content"] \
            == "新内容1"
        # 闭环状态：轮数置位、待复查标记、备份与基线分
        assert result["yuwen_visual_fix_rounds"] == 1
        assert result["yuwen_visual_fix_pending"] is True
        assert result["yuwen_visual_fix_prev_score"] == 70
        assert result["yuwen_visual_fix_backup"]["slides"][0]["title"] == "标题1"
        assert result["yuwen_visual_fix_backup"]["slides"][0]["elements"][0]["content"] \
            == "页1"  # 备份是修复前原页
        # step 帧 running/done 成对
        steps = [f for f in frames if f.get("type") == "step"
                 and f["id"] == "visual_fix"]
        assert {s["status"] for s in steps} == {"running", "done"}
        # 提示词带问题中文释义与 suggestion 原文
        sys_prompt = gw.chat.call_args[0][0][0].content
        assert "字体过小" in sys_prompt and "增大正文字号到 24pt" in sys_prompt

    def test_llm_failure_keeps_original(self, outputs_tmp):
        """LLM 炸 / 输出过不了校验 → 保留原版直接放行（不置 pending）。"""
        gw = MagicMock()
        gw.chat.return_value = MagicMock(content="彻底不是 JSON")
        state = {"yuwen_params": PARAMS, "yuwen_content": _doc(3),
                 "yuwen_visual": _visual(70, [_issue("s01", sev="high")])}
        result, frames = _run_fix(state, gw)
        assert "yuwen_content" not in result          # doc 未动
        assert result["yuwen_visual_fix_pending"] is False
        assert result["yuwen_visual_fix_rounds"] == 1  # 轮数仍消耗，不再重试
        assert "保留原版" in result["yuwen_visual_fix_note"]
        assert len(_visual_frames(frames)) == 0

    def test_ghost_page_skipped(self, outputs_tmp):
        gw = MagicMock()
        gw.chat.return_value = _resp(_fixed_page(1))
        state = {"yuwen_params": PARAMS, "yuwen_content": _doc(3),
                 "yuwen_visual": _visual(70, [_issue("s99", sev="high")])}
        result, _ = _run_fix(state, gw)
        assert result["yuwen_visual_fix_pending"] is False
        gw.chat.assert_not_called()

    def test_max_three_pages_by_severity(self, outputs_tmp):
        """页数上限 3：6 页 high issue 只重生成排序前 3 页（确定性取前 3）。"""
        gw = MagicMock()
        gw.chat.return_value = _resp(_fixed_page(1))
        issues = [_issue(f"s0{i}", sev="high") for i in range(1, 7)]
        state = {"yuwen_params": PARAMS, "yuwen_content": _doc(6),
                 "yuwen_visual": _visual(50, issues, n_pages=6)}
        result, _ = _run_fix(state, gw)
        assert gw.chat.call_count == 3
        assert result["yuwen_visual_fix_pending"] is True
        # page_id 升序确定性取前 3：s01-s03 换成修复内容，s04+ 保持原样
        slides = result["yuwen_content"]["slides"]
        updated = [s["id"] for s in slides
                   if s["elements"][0]["content"] == "新内容1"]
        assert updated == ["s01", "s02", "s03"]


class TestVisualFixCompare:
    """对比阶段：升/平保留，降分或复查降级回滚。"""

    def _base(self, prev_score, new_visual):
        return {"yuwen_params": PARAMS, "yuwen_content": _doc(3),
                "yuwen_visual": new_visual,
                "yuwen_visual_fix_rounds": 1,
                "yuwen_visual_fix_pending": True,
                "yuwen_visual_fix_prev_score": prev_score,
                "yuwen_visual_fix_backup": _doc(3),
                "yuwen_visual_fix_prev_visual": _visual(prev_score, [])}

    def test_score_up_keeps_fix(self, outputs_tmp):
        gw = MagicMock()
        result, _ = _run_fix(self._base(80, _visual(90, [])), gw)
        assert result["yuwen_visual_fix_rollback"] is False
        assert result["yuwen_visual_fix_pending"] is False
        assert "保留修复版" in result["yuwen_visual_fix_note"]
        gw.chat.assert_not_called()  # 对比阶段不调 LLM

    def test_score_flat_keeps_fix(self, outputs_tmp):
        result, _ = _run_fix(self._base(80, _visual(80, [])), MagicMock())
        assert result["yuwen_visual_fix_rollback"] is False

    def test_score_down_rolls_back(self, outputs_tmp):
        result, frames = _run_fix(self._base(80, _visual(60, [])),
                                  MagicMock())
        assert result["yuwen_visual_fix_rollback"] is True
        assert result["yuwen_visual_fix_pending"] is False
        # doc 与盘都回到备份版（"页1" 而非修复版内容）
        assert result["yuwen_content"]["slides"][0]["elements"][0]["content"] \
            == "页1"
        assert _tmp_doc(outputs_tmp)["slides"][0]["elements"][0]["content"] \
            == "页1"
        assert "80 → 60" in result["yuwen_visual_fix_note"]
        assert "已回滚原版" in result["yuwen_visual_fix_note"]
        steps = [f for f in frames if f.get("type") == "step"
                 and f["status"] == "running"]
        assert steps[0]["label"] == "视觉修复复查"

    def test_recheck_degraded_rolls_back(self, outputs_tmp):
        """复查降级（无从对比）→ 质量棘轮：回滚交付经 V1 验证过的原版。"""
        degraded = {"available": False, "reason": "未安装 LibreOffice",
                    "score": 0, "pages": [], "issues": []}
        result, _ = _run_fix(self._base(80, degraded), MagicMock())
        assert result["yuwen_visual_fix_rollback"] is True
        assert "已回滚原版" in result["yuwen_visual_fix_note"]

    def test_missing_backup_keeps_status(self, outputs_tmp):
        state = self._base(80, _visual(60, []))
        state["yuwen_visual_fix_backup"] = {}
        result, _ = _run_fix(state, MagicMock())
        assert result["yuwen_visual_fix_rollback"] is False
        assert "备份缺失" in result["yuwen_visual_fix_note"]


class TestVisualReviewRollbackPassthrough:
    """回滚重渲染后 visual_review 跳过复查：透传修复前帧，不重调 VLM。"""

    def test_passthrough_without_vlm(self, outputs_tmp):
        prev = _visual(85, [_issue("s01", sev="high")])
        state = {"yuwen_params": PARAMS, "yuwen_content": _doc(3),
                 "yuwen_visual": _visual(60, []),   # 修复后复查的旧结果
                 "yuwen_visual_fix_rollback": True,
                 "yuwen_visual_fix_prev_visual": prev}
        # env 无 key：若透传逻辑未命中会走 available=false 降级——断言等价
        # 于 prev 即证明走了透传分支
        result, frames = _run(state)
        assert result["yuwen_visual"] == prev
        vf = _visual_frames(frames)
        assert len(vf) == 1 and vf[0]["visual"] == prev
        done = [f for f in frames if f.get("type") == "step"
                and f["status"] == "done"][-1]
        assert "未重跑 VLM" in done["detail"]

    def test_no_passthrough_without_flag(self, outputs_tmp):
        """无 rollback 标记：即使 prev_visual 在也不透传（正常重审降级路径）。"""
        state = {"yuwen_params": PARAMS, "yuwen_content": _doc(3),
                 "yuwen_visual_fix_prev_visual": _visual(85, [])}
        result, _ = _run(state)
        assert result["yuwen_visual"]["available"] is False  # 无 key 正常降级


class TestVisualFixWiring:
    """图接线：visual_review/visual_fix 条件边展开后目标齐全。"""

    def test_nodes_and_edges(self):
        from aidraft.agenthub.yuwen.graph import build_graph
        graph = build_graph(gateway=MagicMock(), registry=MagicMock())
        assert "visual_fix" in graph.nodes
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        assert ("visual_review", "visual_fix") in edges
        assert ("visual_review", "report") in edges   # 放行分支仍在
        assert ("visual_fix", "render") in edges      # 复查/回滚重渲染
        assert ("visual_fix", "report") in edges      # 保留/没修成放行
        assert ("render", "report") not in edges

    def test_report_includes_fix_note(self, outputs_tmp):
        """report 汇总拼入修复结论（回滚场景全貌可见）。"""
        from aidraft.agenthub.yuwen.nodes.report import _make_report_node
        frames: list = []
        state = {"yuwen_params": PARAMS,
                 "yuwen_files": [{"name": "a.pptx", "path": "/files/x",
                                  "size": 1, "mime": "m"}],
                 "yuwen_visual": _visual(80, [_issue("s01", sev="high")]),
                 "yuwen_visual_fix_note":
                     "视觉修复未提升（90 → 80 分），已回滚原版"}
        result = asyncio.run(
            _make_report_node(lambda f: frames.append(f))(state))
        assert "视觉审查 80 分" in result["final_answer"]
        assert "已回滚原版" in result["final_answer"]
