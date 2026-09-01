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
from common.schema import stage_short

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


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
        import re
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
                import re
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

    if t == "image":
        src = el.get("src", "")
        cap = el.get("caption")
        img = f'<img src="{_esc(src)}">' if src else '<div style="height:30vh"></div>'
        cap_html = f'<div class="caption">{_esc(cap)}</div>' if cap else ""
        return f'<div class="elem-image">{img}{cap_html}</div>'

    if t == "divider":
        return '<hr style="border:none;border-top:2px solid var(--divider);margin:1em 0">'

    return ""


def _tone_class(hexcolor: str) -> str:
    """hex 颜色 → 声调 class 数字。与 pinyin.tone_color 反查。"""
    m = {
        "D9534F": "1", "E8A33C": "2", "5BA88A": "3", "5B8AB5": "4", "9AA0A6": "0",
    }
    return m.get(hexcolor.upper(), "0")


def render(doc: dict, out_path: str) -> str:
    """渲染 doc → HTML 文件（按课时分文件）。返回主文件路径。"""
    from jinja2 import Environment, FileSystemLoader

    meta = doc["meta"]
    total = meta.get("periods", 1)
    periods = sorted(set(s.get("period", 1) for s in doc["slides"]))

    # 读 CSS/JS 内联
    css = (ASSETS / "css" / "yuwen.css").read_text(encoding="utf-8")
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
        per_slides = [s for s in doc["slides"] if s.get("period", 1) == per]
        html_str = tmpl.render(meta=meta, slides=per_slides, period=per)
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
