"""依赖自检：try-import + 中文安装提示。

检查 python-pptx / python-docx / Jinja2 / Pillow 是否就绪。
退出码 0 全部就绪 / 2 缺失。
"""
from __future__ import annotations
import sys

# Windows 控制台默认 GBK，输出 ✓/✗ 等 Unicode 会崩；强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DEPS = [
    ("python-pptx", "pptx", "生成 .pptx 课件"),
    ("python-docx", "docx", "生成 .docx 教案"),
    ("Jinja2", "jinja2", "渲染 HTML 互动课件"),
    ("Pillow", "PIL", "预渲染识字卡/田字格/注音行 PNG"),
]


def check_all() -> int:
    missing = []
    for pkg, mod, desc in DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append((pkg, desc))
    if missing:
        print("⚠ 检测到缺失依赖：", file=sys.stderr)
        for pkg, desc in missing:
            print(f"  · {pkg}（{desc}）", file=sys.stderr)
        print("\n请运行以下命令安装：", file=sys.stderr)
        print('  pip install -r requirements.txt',
              file=sys.stderr)
        print("  或：pip install " + " ".join(p for p, _ in missing), file=sys.stderr)
        return 2
    print("✓ 依赖全部就绪：python-pptx / python-docx / Jinja2 / Pillow")
    return 0


if __name__ == "__main__":
    sys.exit(check_all())
