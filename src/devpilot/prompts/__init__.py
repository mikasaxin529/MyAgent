"""提示词加载：从 prompts/nodes/{name}.md 读模板，替换 {{var}} 占位。

占位用 {{key}} 双花括号（避免与 md 内 JSON 的单花括号冲突），load_prompt
做字符串替换而非 str.format，保证 schema JSON 里的 {} 不被误解析。
对齐 ChatFlow prompts/__init__.py 的 load_prompt 机制。
"""
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent / "nodes"


def load_prompt(name: str, **vars: object) -> str:
    """读 prompts/nodes/{name}.md，把 {{key}} 占位替换为 vars[key]。"""
    p = _DIR / f"{name}.md"
    text = p.read_text(encoding="utf-8")
    for k, v in vars.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text
