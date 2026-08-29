"""中文字体探测与 fallback。

pptx/docx 在指定字体名时，仅写入名称字符串，不嵌入字体文件；
运行时由查看者系统渲染。因此按"系统大概率有 → 备选"顺序给字体名栈。
"""
from __future__ import annotations
import sys

# 字体优先级栈：标题/正文用黑体类，拼音用等宽，古诗文用楷体类
FONT_STACK_HEI = ["微软雅黑", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "黑体", "SimHei"]
FONT_STACK_KAI = ["楷体", "KaiTi", "STKaiti", "Kaiti SC", "Noto Serif CJK SC", "宋体", "SimSun"]
FONT_STACK_SONG = ["宋体", "SimSun", "Noto Serif CJK SC", "Times New Roman"]
FONT_STACK_MONO = ["Consolas", "Menlo", "DejaVu Sans Mono"]


def pick(stack: list[str]) -> str:
    """从栈中返回第一个（不在此处验证系统是否真装——pptx 只写名称，
    由渲染端决定。保留函数以便未来扩展为系统探测）。"""
    return stack[0]


HEI = pick(FONT_STACK_HEI)   # 标题/正文默认
KAI = pick(FONT_STACK_KAI)   # 古诗文/范读
SONG = pick(FONT_STACK_SONG)
MONO = pick(FONT_STACK_MONO) # 拼音（等宽对齐）


def font_for_kind(kind: str) -> str:
    """按元素用途选字体。kind 粗粒度即可。"""
    if kind in ("poem", "quote"):
        return KAI
    if kind in ("pinyin", "ruby-line", "word-card", "strokes", "revision"):
        return HEI   # 大字识字用黑体更醒目
    return HEI


def main():
    """打印当前选定的字体，供调试。"""
    print(f"HEI={HEI}\nKAI={KAI}\nSONG={SONG}\nMONO={MONO}")


if __name__ == "__main__":
    main()
