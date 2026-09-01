"""主题包：课件三渲染器的唯一视觉真相。

每个主题是一个 JSON 文件，完整覆盖 pptx/html/docx 需要的全部视觉量：
- pal         配色（含 default.json 扩展键：卡面色/斑马行/危险绿等）
- font_scale  学段字号阶梯（含 d_* 教案文档专用档）
- layout      间距 token（英寸）

load_theme(name) 读同目录 JSON；未知名/文件缺失打 warning 到 stderr
并回退 default——保证任何主题漂移都不阻断渲染。

渲染器不直接 import 本模块取色，而是经 design_tokens 的 PAL/L 动态代理
读取 ACTIVE_THEME，从而 set_theme 一次全链路生效。
"""
from __future__ import annotations
import sys
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
DEFAULT_NAME = "default"


class Theme:
    """主题命名空间：pal / font_scale / layout 三张字典，附色值快捷访问。"""

    def __init__(self, data: dict):
        self.name = data.get("name", DEFAULT_NAME)
        self.pal = dict(data.get("pal", {}))
        # TONE 的 JSON 键是字符串 "1".."0"，统一转成 int 键方便 tone_color 查表
        tone = self.pal.get("TONE")
        if isinstance(tone, dict):
            self.pal["TONE"] = {int(k): v for k, v in tone.items()}
        # HIGHLIGHTS 转成不可变元组，避免运行期被就地改坏
        if isinstance(self.pal.get("HIGHLIGHTS"), list):
            self.pal["HIGHLIGHTS"] = tuple(self.pal["HIGHLIGHTS"])
        self.font_scale = data.get("font_scale", {})
        self.layout = dict(data.get("layout", {}))

    @classmethod
    def from_dict(cls, data: dict) -> "Theme":
        return cls(data)

    def font_for(self, stage_short: str, role: str) -> int:
        """学段简称(低/中/高) + 角色 → 字号(磅)。逻辑同 design_tokens.font_for。"""
        return self.font_scale.get(stage_short, self.font_scale.get("中", {})).get(role, 24)

    def color(self, key: str, fallback: str = "") -> str:
        """取配色，缺失回退 fallback（渲染器读扩展键时容错）。"""
        return self.pal.get(key, fallback)


def load_theme(name: str | None) -> Theme:
    """按名加载主题；未知名 / 文件缺失 → warning 到 stderr + 回退 default。"""
    key = (name or DEFAULT_NAME).strip() or DEFAULT_NAME
    fp = _DIR / f"{key}.json"
    if not fp.is_file():
        if key != DEFAULT_NAME:
            print(f"[themes] ⚠ 未知主题 {key!r}（无 {fp.name}），回退 default",
                  file=sys.stderr)
        fp = _DIR / f"{DEFAULT_NAME}.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[themes] ⚠ 主题 {key!r} 读取失败（{e}），回退 default", file=sys.stderr)
        if fp.name != f"{DEFAULT_NAME}.json":
            data = json.loads((_DIR / f"{DEFAULT_NAME}.json").read_text(encoding="utf-8"))
        else:
            raise
    return Theme.from_dict(data)


def available_themes() -> list[str]:
    """列出可用主题名（按文件名，default 优先）。"""
    names = sorted(p.stem for p in _DIR.glob("*.json"))
    if DEFAULT_NAME in names:
        names.remove(DEFAULT_NAME)
        names.insert(0, DEFAULT_NAME)
    return names
