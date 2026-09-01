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


# ---- 归一化：模型常见偏差 → 合法 schema ---------------------------------
# LLM 生成接近但非严格合规的结构（text→paragraph、question→discussion、
# 散装 word-card→聚合 cards 等）。直接判失败会浪费一次完整生成，
# 先尽力转换，转换不了的留给 validate 报精确错误。

_TYPE_ALIASES = {
    "text": "paragraph",        # 最常见：裸文本元素
    "title": "heading",
    "question": "discussion",
    "audio": "note",            # schema 无 audio，语义最近的教师备注
    "cover": "divider",
    "ending": "divider",
}

# 命名风格变体 → 规范连字符名。模型写 word_card / wordCard / WordCard /
# ruby_line 等都是同一元素，只是命名风格漂移——统一切换成连字符小写即可，
# 无需丢弃重新生成。
_TYPE_CANONICAL = {t: t for t in ELEMENT_TYPES}
for _t in ELEMENT_TYPES:
    if "-" in _t:
        _first, _rest = _t.split("-", 1)
        # 下划线形态 word_card / 首段拼接 camelCase wordCard / 大写开头 WordCard
        for _variant in (
            _t.replace("-", "_"),
            _first + _rest[:1].upper() + _rest[1:],
            _t.replace("-", "_").title().replace("_", "").replace(" ", "_"),
        ):
            _TYPE_CANONICAL.setdefault(_variant, _t)
# 全小写 / 全大写等大小写漂移：小写化匹配兜底
for _k in list(_TYPE_CANONICAL):
    _TYPE_CANONICAL.setdefault(_k.lower(), _TYPE_CANONICAL[_k])


def _canonical_type(t):
    """元素类型归一化：别名 → 规范名；命名变体 → 规范名；未知原样返回。"""
    if not isinstance(t, str):
        return t
    if t in _TYPE_ALIASES:
        return _TYPE_ALIASES[t]
    if t in _TYPE_CANONICAL:
        return _TYPE_CANONICAL[t]
    # 兜底：小写 + 下划线→连字符（WORD-CARD / Word_Card 等混合形态）
    return _TYPE_CANONICAL.get(t.lower().replace("_", "-"), t)


# ---- 值域归一化：模型常见取值偏差 → 合法枚举值 ---------------------------
# validate 的枚举分支（grade/lessonType/competency/dimension）对字符串
# 形态零容忍，而模型输出"古诗"/"识字与写字"/"语言建构与运用"（旧课标名）
# 是高频偏差——映射表转换零风险，整轮生成报废代价太高。

# 旧课标（2017/2020 版）四素养 → 新课标（2022 版）四素养
_COMPETENCY_ALIASES = {
    "语言建构与运用": "语言运用",
    "思维发展与提升": "思维能力",
    "审美鉴赏与创造": "审美创造",
    "文化传承与理解": "文化自信",
}

_LESSON_TYPE_ALIASES = {
    "古诗": "古诗词",
    "诗词": "古诗词",
    "识字与写字": "识字写字",
    "识字": "识字写字",
    "写字": "识字写字",
    "精读课": "精读",
    "阅读": "精读",
    "阅读课": "精读",
    "口语交际": "口语交际习作",
    "口语交际课": "口语交际习作",
    "习作": "口语交际习作",
    "写作": "口语交际习作",
    "表达": "口语交际习作",
}

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def _normalize_grade_value(raw):
    """meta.grade 归一化：'1'/'一'/'1年级'/2.0 → int（1-6），失败原样返回。"""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        s = raw.strip()
        # 中文数字："二"/"二年级"/"二年级上册"
        for cn, n in _CN_NUM.items():
            if s.startswith(cn):
                return n
        # 阿拉伯数字："1"/"1年级"/"一年级下册（2024）"里的数字提取
        import re
        m = re.search(r"[1-6]", s)
        if m:
            return int(m.group())
    return raw


def _canonical_competency(raw):
    """competency 归一化：旧课标名 → 新课标名；未知原样返回。"""
    if isinstance(raw, str):
        return _COMPETENCY_ALIASES.get(raw.strip(), raw)
    return raw


def _canonical_lesson_type(raw):
    """lessonType 归一化：常见变体 → 四课型之一；未知原样返回。"""
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    if s in LESSON_TYPES:
        return s
    if s in _LESSON_TYPE_ALIASES:
        return _LESSON_TYPE_ALIASES[s]
    # 包含式兜底："口语交际与习作"/"古诗二首"→按关键词归类
    for kw, lt in (("口语", "口语交际习作"), ("习作", "口语交际习作"),
                   ("古诗", "古诗词"), ("诗词", "古诗词"), ("识字", "识字写字"),
                   ("写字", "识字写字"), ("精读", "精读")):
        if kw in s:
            return lt
    return s


def _guess_competency(text: str) -> str:
    """按目标内容关键词推断核心素养（objectives 缺 competency 时兜底）。

    推断错了只影响素养标签，内容本身无损——比整轮生成报废划算得多。
    """
    t = text or ""
    for kw, comp in (("文化", "文化自信"), ("传统", "文化自信"), ("爱国", "文化自信"),
                     ("感受", "审美创造"), ("欣赏", "审美创造"), ("美", "审美创造"),
                     ("想象", "思维能力"), ("思维", "思维能力"), ("判断", "思维能力"),
                     ("比较", "思维能力")):
        if kw in t:
            return comp
    return "语言运用"  # 识字/朗读/写字/表达等默认语言运用



def normalize(doc: dict) -> dict:
    """就地归一化模型输出常见偏差，返回同一 doc。"""
    # ---- meta 值域归一化（grade/lessonType/objectives）----
    meta = doc.get("meta")
    if isinstance(meta, dict):
        g = _normalize_grade_value(meta.get("grade"))
        if isinstance(g, int):
            meta["grade"] = g
        meta["lessonType"] = _canonical_lesson_type(meta.get("lessonType"))
        # objectives 字符串数组 → 对象数组（模型高频写法）；
        # competency 旧课标名 → 新课标名；缺失时按内容关键词补默认素养
        objs = meta.get("objectives")
        if isinstance(objs, list):
            fixed = []
            for obj in objs:
                if isinstance(obj, str) and obj.strip():
                    fixed.append({"content": obj,
                                  "competency": _guess_competency(obj)})
                elif isinstance(obj, dict):
                    comp = _canonical_competency(obj.get("competency"))
                    if comp not in CORE_COMPETENCIES:
                        comp = _guess_competency(obj.get("content", ""))
                    obj["competency"] = comp
                    fixed.append(obj)
            meta["objectives"] = fixed

    # handout.content[{section,items}] → handout.levels[{level,items}]
    ho = doc.get("handout")
    if isinstance(ho, dict):
        content = ho.pop("content", None)
        if content is None:
            content = ho.pop("sections", None)
        if isinstance(content, list) and "levels" not in ho:
            ho["levels"] = [
                {"level": c.get("section") or c.get("level") or "基础",
                 "items": c.get("items") or []}
                for c in content if isinstance(c, dict)
            ]

    slides = doc.get("slides")
    if not isinstance(slides, list):
        return doc
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        # slides[].type → kind（schema 用 kind；模型爱写 type）
        if "type" in slide and "kind" not in slide:
            slide["kind"] = slide.pop("type")
        # subtitle 合并进 title（封面副标题常见偏差）
        sub = slide.pop("subtitle", None)
        if sub and isinstance(sub, str) and sub.strip():
            slide["title"] = f"{slide.get('title') or ''} {sub}".strip()
        # period/totalPeriods 混写；period 字符串 "1"/1.0 → int
        # （渲染层 sorted(set(periods)) 混型直接 TypeError，必须归一）
        if "totalPeriods" in slide:
            slide.pop("totalPeriods")
        if "period" in slide:
            try:
                slide["period"] = int(slide["period"])
            except (TypeError, ValueError):
                slide["period"] = 1

        # elements 缺失/非数组 → 尽力重建：
        # - dict（单元素对象）→ 包成数组
        # - 字符串 → 单 paragraph
        # - 缺失但 slide 顶层有散字段（content/text/title）→ 用它们拼一个
        # - 其余（None/数字等）→ 空数组（validate 允许空页）
        elems = slide.get("elements")
        if isinstance(elems, dict):
            elems = [elems]
        elif isinstance(elems, str) and elems.strip():
            elems = [{"type": "paragraph", "content": elems}]
        elif not isinstance(elems, list):
            fallback = []
            for src in ("content", "text", "body"):
                v = slide.pop(src, None)
                if isinstance(v, str) and v.strip():
                    fallback.append({"type": "paragraph", "content": v})
                elif isinstance(v, list):
                    fallback.extend(
                        {"type": "paragraph", "content": x}
                        for x in v if isinstance(x, str))
                elif isinstance(v, dict) and v:
                    fallback.append(v)
            title = slide.get("title")
            if fallback and title:
                fallback.insert(0, {"type": "heading", "content": title, "size": "h1"})
            elems = fallback
        slide["elements"] = elems
        merged_word_cards: list | None = None
        for el in elems:
            if not isinstance(el, dict):
                continue
            t = el.get("type")
            # 类型归一化：别名（text/question…）+ 命名风格变体（word_card/
            # wordCard/WordCard…）→ 规范枚举名
            canonical = _canonical_type(t)
            if canonical != t:
                el["type"] = canonical
                # 别名转换配套字段搬运：audio 的文字字段 → note.content，
                # text/title 键 → paragraph/heading 的 content（别名表只改
                # type 名不改字段，转出空元素是最常见的次生缺陷）
                if canonical == "note" and "content" not in el:
                    for k in ("text", "src", "label", "title"):
                        if isinstance(el.get(k), str) and el[k].strip():
                            el["content"] = el.pop(k)
                            break
                elif canonical == "paragraph" and "content" not in el:
                    for k in ("text", "body"):
                        if isinstance(el.get(k), str) and el[k].strip():
                            el["content"] = el.pop(k)
                            break
                elif canonical == "heading" and "content" not in el:
                    for k in ("text", "title"):
                        if isinstance(el.get(k), str) and el[k].strip():
                            el["content"] = el.pop(k)
                            break
                elif canonical == "discussion" and "question" not in el:
                    if isinstance(el.get("content"), str) and el["content"].strip():
                        el["question"] = el.pop("content")
            # heading.size 渲染层直接查表 {size: cls}，非 h1/h2/h3 → KeyError
            if el.get("type") == "heading" and el.get("size") not in ("h1", "h2", "h3"):
                el["size"] = "h1" if not el.get("size") else "h2"
            # poem: stanzas 扁平 [{"text","ruby"}] → [{"lines":[...]}]。
            # 渲染层只认 lines 层，扁平结构下整首诗静默蒸发（不崩但内容全丢）
            if el.get("type") == "poem":
                stanzas = el.get("stanzas")
                if isinstance(stanzas, list):
                    fixed = []
                    for st in stanzas:
                        if isinstance(st, dict):
                            if "lines" not in st and "text" in st:
                                st = {"lines": [st]}
                            fixed.append(st)
                        elif isinstance(st, str) and st.strip():
                            # 纯诗句字符串 → 单行 stanza
                            fixed.append({"lines": [{"text": st}]})
                    el["stanzas"] = fixed
            # board: children 里塞字符串 → {"node": s}（HTML 对字符串
            # child.get 直接 AttributeError，PPTX 有 isinstance 兜底）
            if el.get("type") == "board":
                structure = el.get("structure")
                if isinstance(structure, list):
                    for node in structure:
                        if isinstance(node, dict) and isinstance(node.get("children"), list):
                            node["children"] = [
                                {"node": c} if isinstance(c, str) else c
                                for c in node["children"]
                            ]
            # word-card: content=汉字 → char（模型常用 content）
            if el.get("type") == "word-card" and "cards" not in el:
                if "content" in el and "char" not in el:
                    el["char"] = el.pop("content")
            # 散装 word-card（每元素一张卡）→ 聚合进一个 cards[]
            if el.get("type") == "word-card" and "cards" not in el:
                card = {k: el[k] for k in
                        ("char", "pinyin", "radical", "strokes", "strokeOrder",
                         "structure", "groups", "example", "sentence", "word")
                        if k in el}
                # 卡内键别名：word→char（模型爱用 word 装汉字）
                if "word" in card and "char" not in card:
                    card["char"] = card.pop("word")
                if "structure" in card and "radical" not in card:
                    card["radical"] = card.pop("structure")
                if "example" in card:
                    card["groups"] = card.pop("example")
                if card.get("char"):
                    merged_word_cards = merged_word_cards or []
                    merged_word_cards.append(card)
        # word-card 元素只留一个聚合卡（cards[] 已被上面收集）
        if merged_word_cards:
            slide["elements"] = [
                el for el in elems
                if not (isinstance(el, dict)
                        and el.get("type") == "word-card" and "cards" not in el)
            ] + [{"type": "word-card", "cards": merged_word_cards}]
    return doc


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
