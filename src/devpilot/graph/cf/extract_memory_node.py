"""ExtractMemory 节点：从对话抽事实存 JSONL（对齐 ChatFlow extract_memory，裁 DB/向量库）。

LLM 抽取值得长期记住的用户事实/偏好（姓名、约束、偏好），追加到
.devpilot/memory/facts.jsonl。无 DB——单机单进程，重启可读回。
"""
from __future__ import annotations

import json
from pathlib import Path

from ...config import load_agent_models
from ...gateway import ChatMessage
from ..state import AgentGraphState
from .base import done, emit, visit

_MEM_DIR = Path(".devpilot/memory")
_FACTS_FILE = _MEM_DIR / "facts.jsonl"


def make_extract_memory_node(gateway, audit=None, emitter=None):
    async def extract_node(state: AgentGraphState) -> dict:
        visited = visit(state, "extract_memory", emitter)
        task = state.get("user_message") or state.get("task", "")
        full = state.get("full_response", "")
        models = load_agent_models()
        provider, model = models.get(
            "extractor", models.get("coder", ("deepseek", "deepseek-chat")))
        system = (
            "从以下对话抽取值得长期记住的用户事实/偏好（如姓名、偏好、约束、"
            "正在做的项目）。只输出 JSON 数组 [{\"fact\":\"...\"}]，无可抽则 []。"
        )
        prompt = f"用户：{task}\n助手：{full[:1500]}"
        raw = ""
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", prompt)],
            provider=provider, model=model, temperature=0.0,
        ):
            if chunk.delta:
                raw += chunk.delta
        facts = _parse_facts(raw)
        if facts:
            _append_jsonl(_FACTS_FILE, {"facts": facts})
            emit(emitter, {"type": "memory", "kind": "extract", "count": len(facts)})
        done(emitter, "extract_memory")
        return {"nodes_visited": visited}

    return extract_node


def _parse_facts(raw: str) -> list[str]:
    t = (raw or "").strip().strip("`").strip()
    try:
        data = json.loads(t)
        if isinstance(data, list):
            return [str(item.get("fact", item) if isinstance(item, dict) else item)
                    for item in data][:20]
    except Exception:  # noqa: BLE001
        pass
    return []


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
