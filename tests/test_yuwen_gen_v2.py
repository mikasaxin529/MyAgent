"""生成层 v2 约束测试：页数收敛 + 版式化栏目（toc/challenge/scene-strip）。

覆盖（任务 #23 的生成层改造验收）：
1. stages.md / lesson-types.md 参考文件含新页数指引与栏目关键词
2. SYSTEM_GEN_OUTLINE 格式化后含页数收敛与栏目要求段
3. SYSTEM_GEN_SLIDE 含新元素（challenge/scene-strip/toc/background image）教学段
4. jingyesi.json 示例通过 schema 校验，且三种新元素各至少一页
5. review._structure_report 对版式栏目页不误报、页数超标加提示

运行：
    PYTHONIOENCODING=utf-8 pytest tests/test_yuwen_gen_v2.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_SRC))

YUWEN = _SRC / "devpilot" / "agenthub" / "yuwen"
REFS = YUWEN / "references"
JINGYESI = REFS / "examples" / "jingyesi.json"


@pytest.fixture
def scripts_path():
    """把 scripts 目录加入 sys.path 并还原（common 相对导入依赖脚本模式）。"""
    had = str(YUWEN / "scripts") in sys.path
    if not had:
        sys.path.insert(0, str(YUWEN / "scripts"))
    yield
    if not had:
        sys.path.remove(str(YUWEN / "scripts"))


# ---------------------------------------------------------------- 1. 参考文件

class TestReferenceGuidance:
    """stages.md / lesson-types.md 的页数收敛与新栏目指引。"""

    def test_stages_page_range_converged(self):
        text = (REFS / "stages.md").read_text(encoding="utf-8")
        # 新指引：每课时 10-14 页（低段 12-14、中/高段 10-12）
        assert "10-14" in text
        assert "12-14" in text and "10-12" in text
        # 旧口径必须清除：低段 20-55 页、"优质课件低段常达 30-50 页"
        assert "20-55" not in text
        assert "30-50 页" not in text
        # 密度新指引
        assert "宁精不滥" in text

    def test_stages_lesson_type_ranges(self):
        text = (REFS / "stages.md").read_text(encoding="utf-8")
        assert "12-16" in text   # 识字课
        assert "8-12" in text    # 口语交际

    def test_lesson_types_new_columns(self):
        text = (REFS / "lesson-types.md").read_text(encoding="utf-8")
        # 三大版式栏目进入栏目表
        assert "toc" in text
        assert "challenge" in text
        assert "scene-strip" in text
        # 闯关练习替代旧"课堂练习 list/table 当堂检测"口径
        assert "闯关练习" in text
        # 目录行说明正式版式（左图栏 + 两列条目）
        assert "两列" in text
        # 旧页数口径清除
        assert "20-30 页" not in text and "15-30 页" not in text

    def test_lesson_types_per_period_range(self):
        text = (REFS / "lesson-types.md").read_text(encoding="utf-8")
        assert "每课时 10-14 页" in text


# ---------------------------------------------------------------- 2. 大纲 prompt

class TestOutlinePrompt:
    """SYSTEM_GEN_OUTLINE：页数收敛 + 栏目要求段。"""

    @pytest.fixture
    def formatted(self):
        from devpilot.agenthub.yuwen.prompts import (
            META_CONTRACT, SYSTEM_GEN_OUTLINE, _read_ref)
        return SYSTEM_GEN_OUTLINE.format(
            stages=_read_ref("stages.md"),
            lesson_types=_read_ref("lesson-types.md"),
            meta_contract=META_CONTRACT,
        )

    def test_page_guidance(self, formatted):
        assert "每课时 10-14 页" in formatted
        assert "每课时 12-14 页" in formatted  # 低段
        # 旧"共 15-20 页"总量口径清除
        assert "共 15-20 页" not in formatted

    def test_column_requirements(self, formatted):
        assert "## 栏目要求" in formatted
        assert "kind=toc" in formatted
        assert "challenge" in formatted
        assert "scene-strip" in formatted
        assert "全出血意境背景图" in formatted

    def test_example_pages_include_new_kinds(self, formatted):
        # 输出格式示例本身示范 toc/scene-strip/challenge 三种新 kind
        assert '"kind": "toc"' in formatted
        assert '"kind": "scene-strip"' in formatted
        assert '"kind": "challenge"' in formatted


# ---------------------------------------------------------------- 3. 逐页 prompt

class TestSlidePrompt:
    """SYSTEM_GEN_SLIDE：新元素 JSON 写法教学。"""

    @pytest.fixture
    def formatted(self):
        from devpilot.agenthub.yuwen.prompts import SYSTEM_GEN_SLIDE, _read_ref
        return SYSTEM_GEN_SLIDE.format(
            stages=_read_ref("stages.md"),
            schema=_read_ref("schema.md"),
            example=_read_ref("examples/jingyesi.json"),
            outline_ctx="（大纲上下文）",
        )

    def test_challenge_teaching(self, formatted):
        assert "第一关" in formatted and "第二关" in formatted
        assert '"type":"challenge"' in formatted
        assert "options" in formatted and "hint" in formatted

    def test_scene_strip_teaching(self, formatted):
        assert "scene-strip" in formatted
        assert "scenes" in formatted
        assert '"caption":"床前明月光' in formatted

    def test_toc_teaching(self, formatted):
        assert "kind=toc" in formatted
        assert "左图栏" in formatted

    def test_cover_background_teaching(self, formatted):
        assert '"background":true' in formatted

    def test_one_layout_rule(self, formatted):
        assert "每页一个主版式" in formatted
        # 版式栏目页 1-2 元素不视为密度问题
        assert "不要为凑数堆元素" in formatted


# ---------------------------------------------------------------- 4. 示例 JSON

class TestExampleDoc:
    """jingyesi.json：schema 合规 + 三种新元素各至少一页（few-shot 标杆）。"""

    @pytest.fixture
    def doc(self):
        return json.loads(JINGYESI.read_text(encoding="utf-8"))

    def test_schema_valid(self, doc, scripts_path):
        from common.schema import normalize, validate
        validate(normalize(json.loads(json.dumps(doc))))  # 不抛 SchemaError

    def test_new_elements_present(self, doc):
        all_elems = [el["type"] for s in doc["slides"] for el in s["elements"]]
        assert "challenge" in all_elems
        assert "scene-strip" in all_elems
        # toc 是 kind 不是元素类型：目录页 = image + list
        toc_pages = [s for s in doc["slides"] if s.get("kind") == "toc"]
        assert toc_pages, "示例缺 toc 目录页"

    def test_challenge_fields_complete(self, doc):
        """challenge items 含 stage/title/question 必备字段，第二关含 options/answer。"""
        ch = [el for s in doc["slides"] for el in s["elements"]
              if el["type"] == "challenge"]
        assert ch
        items = ch[0]["items"]
        assert 1 <= len(items) <= 2
        for it in items:
            assert it.get("stage") and it.get("title") and it.get("question")
        pick = next((it for it in items if "options" in it), None)
        assert pick and pick.get("answer") and pick.get("hint")

    def test_scene_strip_exactly_four(self, doc):
        ss = [el for s in doc["slides"] for el in s["elements"]
              if el["type"] == "scene-strip"]
        assert ss and len(ss[0]["scenes"]) == 4

    def test_appended_pages_last_and_period1(self, doc):
        """新增页放最后且 period 沿用既有（不动既有页结构）。"""
        slides = doc["slides"]
        new_kinds = [s["kind"] for s in slides if
                     s["kind"] in ("toc", "scene-strip", "challenge")]
        tail = [s["kind"] for s in slides[-3:]]
        assert sorted(new_kinds) == sorted(tail)
        assert all(s["period"] == 1 for s in slides[-3:])

    def test_existing_pages_untouched(self, doc):
        """既有 9 页 s01-s09 结构不变（只追加，不修改）。"""
        head = doc["slides"][:9]
        assert [s["id"] for s in head] == [f"s{i:02d}" for i in range(1, 10)]
        assert head[0]["kind"] == "cover" and head[-1]["kind"] == "board"


# ---------------------------------------------------------------- 5. review 预检

class TestStructureReport:
    """_structure_report：版式栏目页豁免 + 页数超标提示。"""

    def _doc(self, slides, periods=1, stage="低段"):
        return {"meta": {"stage": stage, "periods": periods}, "slides": slides}

    def test_toc_page_not_empty_warning(self):
        from devpilot.agenthub.yuwen.nodes.review import _structure_report
        toc = {"id": "s02", "kind": "toc", "title": "目录", "period": 1,
               "elements": [{"type": "image", "src": "", "caption": "配图"},
                            {"type": "list", "items": ["导入", "识字"]}]}
        report = _structure_report(self._doc([toc]))
        assert "空元素页" not in report
        assert "版式栏目页" in report
        assert "s02(toc,2元素)" in report

    def test_challenge_single_element_no_density_warning(self):
        from devpilot.agenthub.yuwen.nodes.review import _structure_report
        ch = {"id": "s12", "kind": "challenge", "title": "闯关", "period": 1,
              "elements": [{"type": "challenge", "items": [
                  {"stage": "第一关", "title": "填一填", "question": "床前明月□"}]}]}
        report = _structure_report(self._doc([ch]))
        assert "超密度上限" not in report
        assert "版式栏目页" in report

    def test_normal_page_still_warns(self):
        """普通页元素超限仍要告警（豁免只针对版式栏目 kind）。"""
        from devpilot.agenthub.yuwen.nodes.review import _structure_report
        elems = [{"type": "paragraph", "content": f"句{i}"} for i in range(6)]
        page = {"id": "s03", "kind": "analysis", "title": "品析",
                "period": 1, "elements": elems}
        report = _structure_report(self._doc([page]))
        assert "超密度上限" in report

    def test_empty_page_still_warns_even_formatted(self):
        """版式栏目 kind 但 elements 为空 → 仍按空元素页告警。"""
        from devpilot.agenthub.yuwen.nodes.review import _structure_report
        page = {"id": "s02", "kind": "toc", "title": "目录", "period": 1,
                "elements": []}
        report = _structure_report(self._doc([page]))
        assert "空元素页" in report

    def test_page_target_line_and_overage_warning(self):
        from devpilot.agenthub.yuwen.nodes.review import _structure_report
        slides = [{"id": f"s{i:02d}", "kind": "content", "title": "t",
                   "period": 1,
                   "elements": [{"type": "heading", "content": "x", "size": "h1"}]}
                  for i in range(18)]
        report = _structure_report(self._doc(slides))
        assert "对标指引：每课时 10-14 页" in report
        assert "明显超每课时 10-14 页指引" in report

    def test_within_target_no_overage_warning(self):
        from devpilot.agenthub.yuwen.nodes.review import _structure_report
        slides = [{"id": f"s{i:02d}", "kind": "content", "title": "t",
                   "period": 1,
                   "elements": [{"type": "heading", "content": "x", "size": "h1"}]}
                  for i in range(14)]
        report = _structure_report(self._doc(slides))
        assert "明显超每课时" not in report

    def test_jingyesi_example_report_clean(self):
        """真实示例整课跑预检：无密度告警、无空页、三种版式栏目页被标注。"""
        from devpilot.agenthub.yuwen.nodes.review import _structure_report
        doc = json.loads(JINGYESI.read_text(encoding="utf-8"))
        report = _structure_report(doc)
        assert "超密度上限" not in report
        assert "空元素页" not in report
        for k in ("toc", "scene-strip", "challenge"):
            assert f"({k}," in report
