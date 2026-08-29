"""JSON schema 校验与缺省值补齐。

课程 JSON 顶层结构：
{
  "version": "1.0",
  "meta": { "title","grade","stage","lessonType","textbook",
            "coreCompetencies","objectives","keyPoints","difficulties","periods" },
  "slides": [ { id, kind, title, layout, period, elements:[...] } ],
  "lessonPlan": { ...教案结构... },
  "handout": { "levels":[ {level, items} ] }   # 可选
}

每页 frame.elements[].type 取值（元素类型全集）。
未知元素类型 → 校验失败，退出码 2。
"""
from __future__ import annotations
import sys

# ---- 枚举定义（内容层与渲染层共享的契约）----------------------------

LESSON_TYPES = {"精读", "识字写字", "古诗词", "口语交际习作"}

CORE_COMPETENCIES = {"文化自信", "语言运用", "思维能力", "审美创造"}

# 传统三维目标（可选标签）
DIMENSIONS = {"知识与技能", "过程与方法", "情感态度与价值观", "情感态度价值观"}

# 元素类型全集
ELEMENT_TYPES = {
    # 通用文本
    "heading",        # { content, size: h1|h2|h3 }
    "paragraph",      # { content, emphasize:[{start,end}] }
    "list",           # { items:[...], ordered:bool }
    "quote",          # { content, source? }
    "table",          # { headers:[...], rows:[[...]] }
    # 语文专用
    "word-card",      # { cards:[{char,pinyin,radical,strokes,strokeOrder,groups,sentence}] }
    "ruby-line",      # { text, ruby }   整行注音
    "poem",           # { stanzas:[{lines:[{text,ruby}]}], title?, author? }
    "strokes",        # { char, strokeOrder:[...] }   笔顺
    "revision",       # { chars:[{char,pinyin,易错点,运笔要点}] }   写字指导/田字格
    "board",          # { title, structure:[{node, children?[...]}] }   板书
    "discussion",     # { question, hint?, form:同桌互说|小组讨论|开火车|全班交流 }
    "evaluation",     # { rubric:[{criterion, levels:[{star,desc}]}] }   评价量表
    # 媒体/辅助
    "image",          # { src, caption? }
    "note",           # { content }   教师备注，HTML 默认隐藏
    "divider",        # {}            分隔
}

# 课型 → 默认课时数
DEFAULT_PERIODS = {
    "精读": 2,
    "识字写字": 2,
    "古诗词": 2,
    "口语交际习作": 1,
}

REQUIRED_META = ("title", "grade", "lessonType")


def stage_from_grade(grade: int) -> str:
    """年级(1-6) → 学段（低段/中段/高段）。"""
    if grade <= 2:
        return "低段"
    if grade <= 4:
        return "中段"
    return "高段"


def stage_short(stage: str) -> str:
    """学段 → 简称（低/中/高），用于字号档位 key。"""
    return {"低段": "低", "中段": "中", "高段": "高"}.get(stage, "中")


class SchemaError(Exception):
    """schema 校验失败。退出码 2。"""


def _err(msg: str):
    raise SchemaError(msg)


def validate(doc: dict) -> dict:
    """校验文档并补齐缺省值，返回规范化后的 doc。校验失败抛 SchemaError。"""
    if not isinstance(doc, dict):
        _err("顶层必须是对象")

    # version
    doc.setdefault("version", "1.0")

    # ---- meta ----
    meta = doc.get("meta")
    if not isinstance(meta, dict):
        _err("meta 缺失或不是对象")
    for k in REQUIRED_META:
        if k not in meta:
            _err(f"meta.{k} 缺失（必填：title/grade/lessonType）")

    grade = meta["grade"]
    if not isinstance(grade, int) or not (1 <= grade <= 6):
        _err(f"meta.grade 必须是 1-6 的整数，当前 {grade!r}")

    lt = meta["lessonType"]
    if lt not in LESSON_TYPES:
        _err(f"meta.lessonType 必须是 {LESSON_TYPES} 之一，当前 {lt!r}")

    # stage 自动派生
    meta["stage"] = stage_from_grade(grade)

    # periods 缺省
    meta.setdefault("periods", DEFAULT_PERIODS.get(lt, 1))
    meta.setdefault("textbook", f"部编版{grade}年级")
    meta.setdefault("coreCompetencies", list(CORE_COMPETENCIES))
    meta.setdefault("objectives", [])
    meta.setdefault("keyPoints", [])
    meta.setdefault("difficulties", [])

    # 目标强制标注 competency
    for i, obj in enumerate(meta["objectives"]):
        if not isinstance(obj, dict):
            _err(f"objectives[{i}] 必须是对象")
        comp = obj.get("competency")
        if comp not in CORE_COMPETENCIES:
            _err(f"objectives[{i}].competency 必须是四素养之一 {CORE_COMPETENCIES}，当前 {comp!r}")
        # dimension 可选校验
        dim = obj.get("dimension")
        if dim is not None and dim not in DIMENSIONS:
            _err(f"objectives[{i}].dimension 若填须为三维目标之一 {DIMENSIONS}，当前 {dim!r}")

    # ---- slides ----
    slides = doc.get("slides")
    if not isinstance(slides, list) or not slides:
        _err("slides 缺失或为空")
    seen_ids = set()
    for i, slide in enumerate(slides):
        if not isinstance(slide, dict):
            _err(f"slides[{i}] 必须是对象")
        slide.setdefault("id", f"s{i+1}")
        sid = slide["id"]
        if sid in seen_ids:
            _err(f"slide id 重复：{sid}")
        seen_ids.add(sid)
        slide.setdefault("kind", "content")
        slide.setdefault("title", "")
        slide.setdefault("layout", "")
        slide.setdefault("period", 1)
        elems = slide.get("elements")
        if not isinstance(elems, list):
            _err(f"slides[{i}].elements 必须是数组")
        for j, el in enumerate(elems):
            if not isinstance(el, dict):
                _err(f"slides[{i}].elements[{j}] 必须是对象")
            t = el.get("type")
            if t not in ELEMENT_TYPES:
                _err(f"slides[{i}].elements[{j}].type={t!r} 未知，合法值：{sorted(ELEMENT_TYPES)}")

    # ---- lessonPlan（docx 用，可选但建议有）----
    lp = doc.get("lessonPlan")
    if lp is not None and not isinstance(lp, dict):
        _err("lessonPlan 必须是对象")

    # ---- handout（可选）----
    ho = doc.get("handout")
    if ho is not None:
        if not isinstance(ho, dict):
            _err("handout 必须是对象")
        levels = ho.get("levels")
        if not isinstance(levels, list):
            _err("handout.levels 必须是数组")

    return doc


def main(argv=None):
    """命令行入口：校验一个 JSON 文件。退出码 0/1/2。"""
    import json, argparse
    p = argparse.ArgumentParser(description="校验课程 JSON schema")
    p.add_argument("file", help="JSON 文件路径")
    args = p.parse_args(argv)
    try:
        with open(args.file, "r", encoding="utf-8") as f:
            doc = json.load(f)
        validate(doc)
    except SchemaError as e:
        print(f"[schema] ✗ 校验失败：{e}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f"[schema] ✗ 文件不存在：{args.file}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"[schema] ✗ JSON 解析失败：{e}", file=sys.stderr)
        return 2
    n = len(doc.get("slides", []))
    print(f"[schema] ✓ 通过：{doc['meta']['title']} · {doc['meta']['lessonType']} · {n} 页")
    return 0


if __name__ == "__main__":
    sys.exit(main())
