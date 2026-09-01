"""设计 token：暖色板 / 字号阶梯 / 间距 token / EMU 辅助。

本模块是 themes/ 主题包的**薄兼容层**：
- 值的唯一真相在 common/themes/*.json（default.json 逐字段等于改造前）
- PAL / L / FONT_SCALE 是**动态代理**，属性访问转发到 ACTIVE_THEME，
  故 `import design_tokens as T; T.PAL.ACCENT` 这类调用点零改动即可吃上主题
- set_theme(name) 切换 ACTIVE_THEME（子进程内一次性设置，全局生效）

未调 set_theme 时默认加载 default 主题，视觉与改造前完全一致。

小学语文课件视觉语言：近白暖底、大粗标题 + 强调色短横、
白色圆角卡 + 柔影、关键词彩色高亮、药丸标签。
"""
from __future__ import annotations

from .themes import Theme, load_theme

# ---- EMU 辅助（python-pptx 用 EMU 作单位）-------------------
# 1 inch = 914400 EMU。16:9 宽屏 13.333" x 7.5"
EMU_PER_INCH = 914400
SLIDE_W = int(round(13.333 * EMU_PER_INCH))   # 12192000
SLIDE_H = int(round(7.5 * EMU_PER_INCH))      # 6858000


def inch(v: float) -> int:
    """英寸 → EMU。"""
    return int(v * EMU_PER_INCH)


def pt(v: float) -> int:
    """磅 → EMU（1pt = 12700 EMU）。"""
    return int(v * 12700)


# ---- 当前生效主题（进程内全局，render_all 子进程隔离）--------
ACTIVE_THEME: Theme = load_theme("default")


class _DictProxy:
    """把 ACTIVE_THEME 下某张字典（pal/layout/font_scale）代理成模块级常量。

    每次属性访问都重新读 ACTIVE_THEME，因此 set_theme 之后所有
    `T.PAL.XXX` / `T.L.XXX` / `T.FONT_SCALE[...]` 调用点即时切换到新主题。
    """

    __slots__ = ("_attr",)

    def __init__(self, attr: str):
        object.__setattr__(self, "_attr", attr)

    def _target(self):
        return getattr(ACTIVE_THEME, object.__getattribute__(self, "_attr"))

    def __getattr__(self, k):
        return self._target()[k]

    def __getitem__(self, k):
        return self._target()[k]

    def __contains__(self, k):
        return k in self._target()

    def __iter__(self):
        return iter(self._target())

    def __len__(self):
        return len(self._target())

    def keys(self):
        return self._target().keys()

    def get(self, k, default=None):
        return self._target().get(k, default)


# ---- 暖色板 PAL（代理 ACTIVE_THEME.pal）---------------------
PAL = _DictProxy("pal")

# ---- 字号阶梯（代理 ACTIVE_THEME.font_scale）----------------
FONT_SCALE = _DictProxy("font_scale")


def font_for(stage_short: str, role: str) -> int:
    """学段简称(低/中/高) + 角色 → 字号(磅)。"""
    return ACTIVE_THEME.font_for(stage_short, role)


# ---- 间距 token（英寸，代理 ACTIVE_THEME.layout）-------------
L = _DictProxy("layout")


def size_by_role(role: str, stage_short: str = "中") -> int:
    """便捷：角色名 → 字号。"""
    return font_for(stage_short, role)


def set_theme(name: str | None) -> Theme:
    """切换当前生效主题，返回加载到的 Theme。

    render_all 在子进程里读到 doc.meta.theme 后调用一次即可——
    三个渲染器经 PAL/L 代理与 ACTIVE_THEME.font_for 全链路吃上主题。
    """
    global ACTIVE_THEME
    ACTIVE_THEME = load_theme(name)
    return ACTIVE_THEME
