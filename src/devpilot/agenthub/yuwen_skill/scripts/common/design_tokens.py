"""设计 token：暖色板 / 字号阶梯 / 间距 token / EMU 辅助。

小学语文课件视觉语言：近白暖底、大粗标题 + 强调色短横、
白色圆角卡 + 柔影、关键词彩色高亮、药丸标签。
"""
from __future__ import annotations

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


# ---- 暖色板 PAL --------------------------------------------
class PAL:
    """小学课件配色：暖白底、深褐粗字、主强调橙，
    关键词用 蓝/绿/红/紫 四色高亮（对齐真实课件的荧光词效果）。"""
    BG          = "FDF9F1"   # 暖白底（近白，衬托白卡）
    BG_CARD     = "FFFFFF"   # 卡片白
    TITLE_TEXT  = "3D2B1F"   # 页标题/正文主色（深褐近黑）
    TEXT        = "4A3B2E"   # 正文
    TEXT_LIGHT  = "9C8B78"   # 弱化灰褐
    ACCENT      = "ED7D31"   # 主强调橙（短横/药丸/页脚）
    ACCENT_DK   = "D9631B"   # 深橙（hover/描边）
    ACCENT2     = "3E8E5A"   # 辅助绿
    ACCENT3     = "3E7BB6"   # 辅助蓝
    DIVIDER     = "EAD9BE"   # 分隔虚线/卡描边（暖沙）
    FOOT        = "B7A995"   # 页脚浅灰褐
    BORDER      = "F0E4CD"   # 卡片柔描边（几乎不可见）
    HL          = "FFDF8A"   # 荧光黄（重点底色高亮用）

    # 关键词高亮轮换色（emphasize 片段按序取色，参考真实课件蓝/绿/红/紫）
    HIGHLIGHTS  = ["3E7BB6", "3E8E5A", "E2574C", "8E6BB5", "C99A2E"]

    # 声调标色（拼音注音用）
    TONE = {
        1: "D9534F",  # 一声红
        2: "E8A33C",  # 二声橙
        3: "5BA88A",  # 三声绿
        4: "5B8AB5",  # 四声蓝
        0: "9AA0A6",  # 轻声灰
    }


# ---- 字号阶梯（磅）按学段 -----------------------------------
# 低段字大图多，中段适中，高段字稍密。key: 低/中/高
FONT_SCALE = {
    "低": {  # 1-2 年级
        "cover_title": 60, "slide_title": 40, "h1": 32, "h2": 28, "h3": 24,
        "body": 24, "list": 22, "bigchar": 88, "pinyin": 20, "note": 14,
        "caption": 14, "footer": 11,
    },
    "中": {  # 3-4 年级
        "cover_title": 54, "slide_title": 36, "h1": 30, "h2": 26, "h3": 22,
        "body": 22, "list": 20, "bigchar": 80, "pinyin": 18, "note": 14,
        "caption": 13, "footer": 11,
    },
    "高": {  # 5-6 年级
        "cover_title": 48, "slide_title": 32, "h1": 28, "h2": 24, "h3": 20,
        "body": 20, "list": 18, "bigchar": 72, "pinyin": 16, "note": 13,
        "caption": 12, "footer": 11,
    },
}


def font_for(stage_short: str, role: str) -> int:
    """学段简称(低/中/高) + 角色 → 字号(磅)。"""
    return FONT_SCALE.get(stage_short, FONT_SCALE["中"]).get(role, 24)


# ---- 间距 token（英寸）--------------------------------------
class L:
    MARGIN_X   = 0.75     # 左右边距
    MARGIN_TOP  = 0.5
    TITLE_H     = 1.0      # （已弃用通栏色带，保留兼容）
    GAP         = 0.25     # 元素间间距
    CARD_GAP    = 0.3      # 卡片间间距
    CARD_PAD    = 0.2      # 卡片内边距
    CONTENT_TOP = 1.45     # 内容起始 y（短横标题体系，无通栏色带）
    MAX_Y       = 7.05     # 内容下界（防溢出，slide 高 7.5）
    LINE_SP     = 1.25     # 正文行距倍数


def size_by_role(role: str, stage_short: str = "中") -> int:
    """便捷：角色名 → 字号。"""
    return font_for(stage_short, role)
