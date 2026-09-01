"""总入口：课程 JSON → .pptx + .html + .docx 三份成品。

用法：
  python render_all.py <input.json> [--out DIR]

- 校验 schema（不合法退出码 2）
- 检查依赖（缺失退出码 2，给出中文安装提示）
- 调用三个渲染器，按课时分文件输出
- 默认输出到 ~/Desktop/语文课件/<课文名>-<课型>/

退出码：0 成功 / 1 异常 / 2 前置缺失
"""
from __future__ import annotations
import sys
import os
import json
from pathlib import Path

# Windows 控制台默认 GBK，输出 ✓/✗ 等 Unicode 会崩；强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 确保能 import common 与各渲染器
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from common.schema import validate, SchemaError


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="课程 JSON → 课件+教案 三件套")
    p.add_argument("input", help="课程 JSON 文件路径")
    p.add_argument("-o", "--out", dest="out",
                   help="输出目录（默认 ~/Desktop/语文课件/<课文名>-<课型>/）")
    args = p.parse_args(argv)

    # ---- 1. 读 & 校验 ----
    try:
        with open(args.input, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        print(f"[render_all] ✗ 文件不存在：{args.input}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"[render_all] ✗ JSON 解析失败：{e}", file=sys.stderr)
        return 2
    try:
        validate(doc)
    except SchemaError as e:
        print(f"[render_all] ✗ schema 校验失败：{e}", file=sys.stderr)
        return 2

    meta = doc["meta"]
    title = meta["title"]
    ltype = meta["lessonType"]

    # ---- 2. 依赖检查 ----
    from check_deps import check_all
    if check_all() != 0:
        return 2

    # ---- 3. 输出目录 ----
    if args.out:
        out_dir = Path(args.out)
    else:
        home = Path.home()
        desktop = home / "Desktop"
        out_dir = desktop / "语文课件" / f"{title}-{ltype}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 安全文件名
    safe_title = "".join(c for c in title if c not in '\\/:*?"<>|')
    print(f"\n=== 开始渲染：{title} · {ltype} ===")
    print(f"输出目录：{out_dir}\n")

    results = {}
    code = 0

    # ---- 4. PPTX ----
    try:
        import render_pptx
        pptx_path = out_dir / f"{safe_title}.pptx"
        render_pptx.render(doc, str(pptx_path))
        # 列出所有课时文件
        for fp in sorted(out_dir.glob(f"{safe_title}*.pptx")):
            results[fp.name] = str(fp)
            print(f"  ✓ PPTX: {fp.name} ({fp.stat().st_size//1024} KB)")
    except Exception as e:
        print(f"  ✗ PPTX 渲染失败：{e}", file=sys.stderr)
        code = 1

    # ---- 5. HTML ----
    try:
        import render_html
        html_path = out_dir / f"{safe_title}.html"
        render_html.render(doc, str(html_path))
        for fp in sorted(out_dir.glob(f"{safe_title}*.html")):
            results[fp.name] = str(fp)
            print(f"  ✓ HTML: {fp.name} ({fp.stat().st_size//1024} KB)")
    except Exception as e:
        print(f"  ✗ HTML 渲染失败：{e}", file=sys.stderr)
        code = 1

    # ---- 6. DOCX ----
    try:
        import render_docx
        docx_path = out_dir / f"{safe_title}-教案.docx"
        render_docx.render(doc, str(docx_path))
        results[docx_path.name] = str(docx_path)
        print(f"  ✓ DOCX: {docx_path.name} ({docx_path.stat().st_size//1024} KB)")
    except Exception as e:
        print(f"  ✗ DOCX 渲染失败：{e}", file=sys.stderr)
        code = 1

    # ---- 7. 报告 ----
    n_periods = meta.get("periods", 1)
    print(f"\n=== 完成 ===")
    print(f"共生成 {len(results)} 个文件")
    print(f"课时建议：本课 {n_periods} 课时")
    print(f"\n上课提示：")
    print(f"  · HTML 课件用浏览器全屏（按 F）上课，支持翻页/点读/笔顺动画")
    print(f"  · PPTX 可二次编辑，按需调整")
    print(f"  · DOCX 教案可直接交学校存档")
    return code


if __name__ == "__main__":
    sys.exit(main())
