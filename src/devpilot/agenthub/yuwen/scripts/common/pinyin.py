"""拼音处理：声调拆分 / 标色 / ruby 文本生成。

三种渲染媒介对拼音的表示不同，这里提供统一的解析层：
- split_syllables(pinyin_str) → [(char, pinyin, tone), ...]
- tone_of(syllable) → 0..4 （0 = 轻声）
- HTML ruby 片段、docx 行内文本、Pillow 注音行各取所需
"""
from __future__ import annotations
import unicodedata

# 带调元音 → (基字母, 声调)
_TONE_MAP = {
    "ā": ("a", 1), "á": ("a", 2), "ǎ": ("a", 3), "à": ("a", 4),
    "ē": ("e", 1), "é": ("e", 2), "ě": ("e", 3), "è": ("e", 4),
    "ī": ("i", 1), "í": ("i", 2), "ǐ": ("i", 3), "ì": ("i", 4),
    "ō": ("o", 1), "ó": ("o", 2), "ǒ": ("o", 3), "ò": ("o", 4),
    "ū": ("u", 1), "ú": ("u", 2), "ǔ": ("u", 3), "ù": ("u", 4),
    "ǖ": ("ü", 1), "ǘ": ("ü", 2), "ǚ": ("ü", 3), "ǜ": ("ü", 4),
}


def tone_of(syllable: str) -> int:
    """返回音节的声调 1-4，无标调（轻声/无声调）返回 0。"""
    for ch in syllable:
        if ch in _TONE_MAP:
            return _TONE_MAP[ch][1]
    return 0


def strip_tone(syllable: str) -> str:
    """去声调符号 → 基字母形式（ǎ→a）。"""
    out = []
    for ch in syllable:
        if ch in _TONE_MAP:
            out.append(_TONE_MAP[ch][0])
        else:
            out.append(ch)
    return "".join(out)


def split_syllables(text: str, pinyin: str):
    """把 text（汉字串）与 pinyin（空格分隔的拼音）对齐。

    返回 [(char, pinyin_syllable, tone), ...]。
    若两者数量不等，按可用部分对齐，缺的用占位。
    """
    chars = [c for c in text if c.strip()]
    syls = pinyin.split()
    pairs = []
    for i, c in enumerate(chars):
        s = syls[i] if i < len(syls) else ""
        # 处理 "轻声"标注如 "de" 无声调
        pairs.append((c, s, tone_of(s) if s else 0))
    return pairs


def tone_color(tone: int) -> str:
    """声调 → 颜色 hex（无 #）。需与 design_tokens.PAL.TONE 一致。"""
    return {
        1: "D9534F",  # 一声红
        2: "E8A33C",  # 二声橙
        3: "5BA88A",  # 三声绿
        4: "5B8AB5",  # 四声蓝
        0: "9AA0A6",  # 轻声灰
    }.get(tone, "3D2B1F")


def html_ruby(text: str, pinyin: str, colorize: bool = True) -> str:
    """生成 HTML <ruby> 片段：汉字 + 注音，可选声调标色。"""
    pairs = split_syllables(text, pinyin)
    parts = []
    for c, s, t in pairs:
        color = tone_color(t) if colorize else None
        rt_style = f' style="color:#{color}"' if color else ""
        rt = f'<rt{rt_style}>{s}</rt>' if s else "<rt></rt>"
        parts.append(f"<ruby><rb>{c}</rb>{rt}</ruby>")
    return "".join(parts)


def docx_inline(text: str, pinyin: str) -> str:
    """docx 行内格式：jǐng(井) 风格，逐字拼。"""
    pairs = split_syllables(text, pinyin)
    parts = []
    for c, s, t in pairs:
        parts.append(f"{s}({c})" if s else c)
    return " ".join(parts)


def main():
    """命令行自测。"""
    text = "坐井观天"
    pinyin = "zuò jǐng guān tiān"
    print("pairs:", split_syllables(text, pinyin))
    print("html :", html_ruby(text, pinyin))
    print("docx :", docx_inline(text, pinyin))


if __name__ == "__main__":
    main()
