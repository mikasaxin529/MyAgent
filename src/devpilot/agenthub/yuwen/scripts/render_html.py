"""JSON → 互动 HTML 课件渲染器（Jinja2）。

特性：
- ruby 原生注音 + 声调标色（CSS class t1..t4/t0）
- HanziWriter（CDN 惰性加载）笔顺动画
- 翻页 / 点读 / 答案显隐 / 计时器
- 按课时分文件输出
- CSS/JS 内联，单文件可拷走上课

退出码：0 成功 / 1 异常 / 2 前置缺失
"""
from __future__ import annotations
import os
import sys
import html
import json
from pathlib import Path

# Windows 控制台默认 GBK，输出 ✓/✗ 等 Unicode 会崩；强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from common import pinyin as py
from common import design_tokens as T
from common.schema import stage_short

import re

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def _rgb_triplet(hexstr: str) -> str:
    """'3D2B1F' → '61,43,31'（CSS rgba 用）。"""
    return ",".join(str(int(hexstr[i:i + 2], 16)) for i in (0, 2, 4))


def _theme_css_vars() -> str:
    """由当前主题生成 :root CSS 变量块。

    变量名 ↔ 主题 JSON 键映射唯一在此定义——消灭 yuwen.css 手抄漂移。
    """
    pal = T.ACTIVE_THEME.pal
    tone = pal.get("TONE", {})
    hls = pal.get("HIGHLIGHTS", [])
    decls = [
        ("--bg", pal["BG"]),
        ("--bg-card", pal["BG_CARD"]),
        ("--title-bar", pal["ACCENT"]),
        ("--text", pal["TEXT"]),
        ("--title-text", pal["TITLE_TEXT"]),
        ("--text-light", pal["TEXT_LIGHT"]),
        ("--accent", pal["ACCENT"]),
        ("--accent2", pal["ACCENT2"]),
        ("--accent3", pal["ACCENT3"]),
        ("--divider", pal["DIVIDER"]),
    ]
    for i, c in enumerate(hls[:4]):
        decls.append((f"--hl{i + 1}", c))
    for k in (1, 2, 3, 4, 0):
        decls.append((f"--tone{k}", tone.get(k, pal["TEXT_LIGHT"])))
    # 派生量：导航底色 = 标题色 85% 透明；其余取主题扩展键
    decls.append(("--note-bg", pal.get("NOTE_BG", "FFF7EE")))
    decls.append(("--nav-hover", pal.get("NAV_HOVER", pal["ACCENT_DK"])))
    # 闯关卡正确项/警示：语义色（商业课件版式新增，缺失回退绿/红）
    decls.append(("--success", pal.get("SUCCESS", "3E8E5A")))
    decls.append(("--danger", pal.get("DANGER", "E2574C")))
    lines = "".join(f"  {n}: #{v};\n" for n, v in decls)
    return (":root {\n" + lines +
            f"  --nav-bg: rgba({_rgb_triplet(pal['TITLE_TEXT'])},0.85);\n}}")


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _answer_index(it: dict, options: list) -> int:
    """challenge 项正确答案下标：'A'/'B' 字母或直接文本命中，未匹配 -1。"""
    ans = str(it.get("answer", "")).strip()
    if not ans or not options:
        return -1
    if len(ans) == 1 and ans.upper() in "ABCD" and ord(ans.upper()) - ord("A") < len(options):
        return ord(ans.upper()) - ord("A")
    for k, o in enumerate(options):
        if o == ans or ans in o:
            return k
    return -1


def _page_header(sj: dict, num: int) -> str:
    """内容页头部：mint 主题（numbered_header）出编号章节头，其余经典短横头。"""
    if T.L.get("numbered_header", False) and num:
        return (f'<div class="num-header"><div class="nh-num">{num:02d}</div>'
                f'<div class="nh-title">{_esc(sj.get("title",""))}</div></div>')
    return ""


def render_element(el: dict) -> str:
    """单元素 → HTML 片段。"""
    t = el.get("type", "")
    if t == "heading":
        size = el.get("size", "h2")
        tag = {"h1": "div", "h2": "div", "h3": "div"}.get(size, "div")
        cls = {"h1": "elem-h1", "h2": "elem-h2", "h3": "elem-h3"}[size]
        return f'<{tag} class="{cls}">{_esc(el.get("content",""))}</{tag}>'

    if t == "paragraph":
        content = el.get("content", "")
        ems = el.get("emphasize", [])
        if ems:
            # 高亮区间
            parts = []
            last = 0
            for em in ems:
                s, e = em.get("start", 0), em.get("end", 0)
                parts.append(_esc(content[last:s]))
                parts.append(f'<span class="emph">{_esc(content[s:e])}</span>')
                last = e
            parts.append(_esc(content[last:]))
            body = "".join(parts)
        else:
            body = _esc(content)
        return f'<div class="elem-paragraph">{body}</div>'

    if t == "list":
        items = el.get("items", [])
        ordered = el.get("ordered", False)
        tag = "ol" if ordered else "ul"
        lis = "".join(f"<li>{_esc(it)}</li>" for it in items)
        return f'<{tag} class="elem-list">{lis}</{tag}>'

    if t == "quote":
        c = _esc(el.get("content", ""))
        src = el.get("source")
        s = f'<span class="quote-src">— {_esc(src)}</span>' if src else ""
        return f'<div class="elem-quote">「{c}」{s}</div>'

    if t == "table":
        headers = el.get("headers", [])
        rows = el.get("rows", [])
        th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
        trs = []
        for row in rows:
            tds = "".join(f"<td>{_esc(c)}</td>" for c in row)
            trs.append(f"<tr>{tds}</tr>")
        return f'<table class="elem-table"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'

    if t == "ruby-line":
        ruby = py.html_ruby(el.get("text", ""), el.get("ruby", ""), colorize=True)
        # 给 rt 加 tone class
        ruby = re.sub(r'<rt style="color:#([0-9A-Fa-f]{6})">',
                      lambda m: '<rt class="t' + _tone_class(m.group(1)) + '">',
                      ruby)
        return f'<div class="ruby-line" data-read="{_esc(el.get("text",""))}">{ruby}</div>'

    if t == "poem":
        title = el.get("title", "")
        author = el.get("author", "")
        lines_html = []
        for stanza in el.get("stanzas", []):
            for line in stanza.get("lines", []):
                ruby = py.html_ruby(line.get("text", ""), line.get("ruby", ""), colorize=True)
                ruby = re.sub(r'<rt style="color:#([0-9A-Fa-f]{6})">',
                              lambda m: '<rt class="t' + _tone_class(m.group(1)) + '">',
                              ruby)
                lines_html.append(f'<div class="poem-line" data-read="{_esc(line.get("text",""))}">{ruby}</div>')
        t_html = f'<div class="poem-title">{_esc(title)} { _esc(author)}</div>' if title else ""
        return f'<div class="poem">{t_html}{"".join(lines_html)}</div>'

    if t == "word-card":
        cards_html = []
        for c in el.get("cards", []):
            tone = py.tone_of(c.get("pinyin", ""))
            tcls = "t" + str(tone)
            groups = " ".join(c.get("groups", []))
            sent = c.get("sentence", "")
            ch = c.get("char", "")
            py_s = c.get("pinyin", "")
            cards_html.append(
                f'<div class="word-card">'
                f'<div class="wc-pinyin {tcls}">{_esc(py_s)}</div>'
                f'<div class="wc-char" data-read="{_esc(ch)}" data-hanzi="{_esc(ch)}">{_esc(ch)}</div>'
                f'<div class="wc-groups">{_esc(groups)}</div>'
                f'<div class="wc-sentence">{_esc(sent)}</div>'
                f'</div>'
            )
        return f'<div class="word-cards">{"".join(cards_html)}</div>'

    if t == "strokes":
        ch = el.get("char", "")
        order = " → ".join(el.get("strokeOrder", []))
        return (f'<div class="revision-card"><div class="tianzi"><div class="big-char" data-hanzi="{_esc(ch)}">{_esc(ch)}</div></div>'
                f'<div class="revision-note">笔顺：{_esc(order)}</div></div>')

    if t == "revision":
        cards_html = []
        for c in el.get("chars", []):
            ch = c.get("char", "")
            cards_html.append(
                f'<div class="revision-card">'
                f'<div class="tianzi"><div class="big-char" data-hanzi="{_esc(ch)}">{_esc(ch)}</div></div>'
                f'<div class="revision-note"><b>易错：</b>{_esc(c.get("易错点",""))}<br><b>运笔：</b>{_esc(c.get("运笔要点",""))}</div>'
                f'</div>'
            )
        return f'<div class="revision">{"".join(cards_html)}</div>'

    if t == "board":
        title = el.get("title", "")
        nodes = []
        for node in el.get("structure", []):
            nodes.append(f'<div class="board-node">● {_esc(node.get("node",""))}</div>')
            for child in node.get("children", []):
                nodes.append(f'<div class="board-child">○ {_esc(child.get("node",""))}</div>')
        return f'<div class="board"><div class="board-title">{_esc(title)}</div>{"".join(nodes)}</div>'

    if t == "discussion":
        q = _esc(el.get("question", ""))
        hint = f'<div class="hint">💡 {_esc(el.get("hint",""))}</div>' if el.get("hint") else ""
        form = f'<span class="form">{_esc(el.get("form",""))}</span>' if el.get("form") else ""
        return f'<div class="discussion"><div class="q">❓ {q}</div>{hint}{form}</div>'

    if t == "evaluation":
        rubric = el.get("rubric", [])
        headers = ["评价维度"] + [f"{i+1}★" for i in range(max((len(r.get("levels",[])) for r in rubric), default=1))]
        rows = []
        for r in rubric:
            row = [r.get("criterion", "")]
            for lv in r.get("levels", []):
                row.append(lv.get("desc", ""))
            rows.append(row)
        th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
        trs = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows)
        return f'<table class="elem-table"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>'

    if t == "note":
        return f'<div class="note">{_esc(el.get("content",""))}</div>'

    if t == "challenge":
        # 闯关练习卡：徽章 + 大字题 + 选项子卡（正确项高亮）+ hint 小字条
        cards = []
        for i, it in enumerate(el.get("items", [])):
            if not isinstance(it, dict):
                continue
            head = " · ".join(x for x in (str(it.get("stage", "")).strip(),
                                          str(it.get("title", "")).strip()) if x)
            head = head or f"第{'一二三四五'[i]}关"
            opts = [str(o) for o in (it.get("options") or []) if str(o).strip()][:4]
            ci = _answer_index(it, opts)
            opts_html = ""
            if opts:
                cells = []
                for k, o in enumerate(opts):
                    cls = "ch-opt correct" if k == ci else "ch-opt"
                    star = '<span class="ch-star">★</span>' if k == ci else ""
                    cells.append(f'<div class="{cls}"><b>{"ABCD"[k]}.</b> '
                                 f'{_esc(o)}{star}</div>')
                opts_html = f'<div class="ch-opts">{"".join(cells)}</div>'
            hint = (f'<div class="ch-hint">💡 {_esc(it["hint"])}</div>'
                    if it.get("hint") else "")
            cards.append(
                f'<div class="ch-card">'
                f'<div class="ch-badge">{_esc(head)}</div>'
                f'<div class="ch-q" data-read="{_esc(it.get("question",""))}">'
                f'{_esc(it.get("question",""))}</div>'
                f'{opts_html}{hint}</div>')
        return f'<div class="elem-challenge">{"".join(cards)}</div>'

    if t == "scene-strip":
        # 四格图解：大图 + CSS 十字分格线 + 四行 caption（圆点轮换高亮色）
        src = el.get("src", "")
        if src and Path(src).is_file():
            src = _rel_to_out(src)
        else:
            src = ""
        img_html = (f'<div class="ss-img"><img src="{_esc(src)}" alt=""></div>'
                    if src else
                    '<div class="ss-img ss-ph">🖼 四格情景图（待生图回填）</div>')
        caps = []
        for i, sc in enumerate(el.get("scenes", [])[:4]):
            cap = sc.get("caption", "") if isinstance(sc, dict) else str(sc)
            caps.append(f'<li class="hl{i % 4 + 1}"><i>{i + 1}</i>'
                        f'<span>{_esc(cap)}</span></li>')
        return f'<div class="elem-scene">{img_html}<ol class="ss-caps">{"".join(caps)}</ol></div>'

    if t == "image":
        if el.get("background"):
            return ""   # 全出血底图由页面模板层处理
        src = el.get("src", "")
        cap = el.get("caption")
        if src and Path(src).is_file():
            # render_all 已把 src 解析为绝对路径；HTML 与图片同在输出目录，
            # 相对路径引用才能单目录拷走可用
            src = _rel_to_out(src)
        else:
            src = ""   # 无效/缺失文件 → 不输出破图，走占位
        img = f'<img src="{_esc(src)}">' if src else '<div style="height:30vh"></div>'
        cap_html = f'<div class="caption">{_esc(cap)}</div>' if cap else ""
        return f'<div class="elem-image">{img}{cap_html}</div>'

    if t == "divider":
        return '<hr style="border:none;border-top:2px solid var(--divider);margin:1em 0">'

    return ""


# 当前渲染输出所在目录（image 相对路径基准；render() 开头设置）
_OUT_DIR: Path | None = None


def _rel_to_out(abs_src: str) -> str:
    """绝对图片路径 → 相对当前 HTML 文件的路径（正斜杠，浏览器友好）。

    非绝对路径原样返回（单独运行 render_html 时 src 可能本就是相对输出目录的，
    浏览器直接可用，无需再转换）。
    """
    global _OUT_DIR
    if _OUT_DIR is None or not Path(abs_src).is_absolute():
        return abs_src
    try:
        return Path(os.path.relpath(abs_src, _OUT_DIR)).as_posix()
    except ValueError:
        return abs_src   # 跨盘符（Windows）无法相对，退回绝对路径


def _tone_class(hexcolor: str) -> str:
    """hex 颜色 → 声调 class 数字。遍历当前主题 TONE 动态反查。"""
    tone = T.ACTIVE_THEME.pal.get("TONE", {})
    up = hexcolor.upper()
    for k, v in tone.items():
        if str(v).upper() == up:
            return str(k)
    return "0"


def _toc_entries(sj: dict) -> list:
    """目录页条目：第一个 list 元素 items，兜底 heading.content。"""
    for el in sj.get("elements", []):
        if el.get("type") == "list":
            return [str(x) for x in el.get("items", [])]
    return [str(el.get("content", "")) for el in sj.get("elements", [])
            if el.get("type") == "heading" and el.get("content")]


def _toc_image(sj: dict) -> str:
    """目录页左栏图：第一个有效 src 的 image 元素（相对输出目录）。"""
    for el in sj.get("elements", []):
        if el.get("type") == "image" and not el.get("background"):
            src = el.get("src", "")
            if src and Path(src).is_file():
                return _rel_to_out(src)
    return ""


def _bg_image(sj: dict) -> str:
    """封面全出血底图：background:True 且 src 有效的 image，无则空。"""
    for el in sj.get("elements", []):
        if el.get("type") == "image" and el.get("background"):
            src = el.get("src", "")
            if src and Path(src).is_file():
                return _rel_to_out(src)
    return ""


def _annotate_slides(slides: list) -> list:
    """给模板预处理每页渲染上下文：编号章节头 / 全出血底图 / toc 条目。"""
    num = 0
    for sj in slides:
        sj.setdefault("_bg_src", "")
        sj.setdefault("_num_header", "")
        kind = sj.get("kind", "")
        if kind == "cover":
            sj["_bg_src"] = _bg_image(sj)
        elif kind == "toc":
            sj["_toc_entries"] = _toc_entries(sj)
            sj["_toc_img"] = _toc_image(sj)
        elif kind not in ("end",):
            num += 1
            sj["_num_header"] = _page_header(sj, num)
    return slides


def render(doc: dict, out_path: str) -> str:
    """渲染 doc → HTML 文件（按课时分文件）。返回主文件路径。"""
    from jinja2 import Environment, FileSystemLoader

    global _OUT_DIR
    out_path = Path(out_path)
    _OUT_DIR = out_path.parent

    meta = doc["meta"]
    total = meta.get("periods", 1)
    periods = sorted(set(s.get("period", 1) for s in doc["slides"]))

    # 读 CSS/JS 内联；:root 变量块由主题生成，拼在基础规则之前
    css = _theme_css_vars() + "\n" + (ASSETS / "css" / "yuwen-base.css").read_text(encoding="utf-8")
    js = (ASSETS / "js" / "interactivity.js").read_text(encoding="utf-8")

    env = Environment(loader=FileSystemLoader(str(ASSETS / "templates")),
                      autoescape=False)
    env.globals.update({
        "render_element": render_element,
        "include_css": lambda: css,
        "include_js": lambda: js,
    })
    tmpl = env.get_template("slides.html.j2")

    out_path = Path(out_path)
    main_path = str(out_path)
    paths = []
    for pi, per in enumerate(periods):
        per_slides = _annotate_slides(
            [s for s in doc["slides"] if s.get("period", 1) == per])
        html_str = tmpl.render(meta=meta, slides=per_slides, period=per,
                               numbered_header=bool(T.L.get("numbered_header", False)))
        if pi == 0:
            fp = main_path
        else:
            fp = str(out_path.with_suffix(f".p{per}.html"))
        Path(fp).write_text(html_str, encoding="utf-8")
        paths.append(fp)
    return paths[0]


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="JSON → HTML 互动课件渲染")
    p.add_argument("input", help="课程 JSON 文件")
    p.add_argument("-o", "--output", help="输出 .html 路径")
    args = p.parse_args(argv)
    try:
        with open(args.input, encoding="utf-8") as f:
            doc = json.load(f)
        from common.schema import validate
        validate(doc)
    except Exception as e:
        print(f"[html] ✗ 输入无效：{e}", file=sys.stderr)
        return 2
    out = args.output or str(Path(args.input).with_suffix(".html"))
    try:
        main_path = render(doc, out)
        print(f"[html] ✓ 已生成：{main_path}")
        return 0
    except Exception as e:
        print(f"[html] ✗ 渲染失败：{e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
