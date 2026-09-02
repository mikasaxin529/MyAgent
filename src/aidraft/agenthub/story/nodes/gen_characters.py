"""gen_characters 节点：梗概 → 角色卡 + 标准立绘（第二确认点产物）。

角色卡是全片一致性的锚点：description（视觉特征段）+ ref_prompt
（立绘生图提示词）。本节点只产卡；立绘图由 gen_portraits 生图节点出
（确认角色后一次性生成，未确认就生图会浪费额度）。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_GEN_CHARACTERS
from ..state import (
    StoryState,
    _emit_characters,
    _load_state,
    _parse_llm_json,
    _save_state,
    _step,
)


def _validate_characters(parsed) -> list[str]:
    """角色卡轻校验。"""
    errors: list[str] = []
    if not isinstance(parsed, dict):
        return ["角色顶层必须是对象"]
    chars = parsed.get("characters")
    if not isinstance(chars, list) or not chars:
        return ["characters 缺失或为空"]
    for i, c in enumerate(chars):
        if not isinstance(c, dict):
            errors.append(f"characters[{i}] 不是对象")
            continue
        for k in ("id", "name", "description", "ref_prompt"):
            if not str(c.get(k) or "").strip():
                errors.append(f"characters[{i}] 缺 {k}")
    return errors


def _make_gen_characters_node(gateway: Any, emitter: Callable[[dict], None] | None,
                              model_kwargs: dict | None = None):
    """gen_characters 节点工厂。"""
    model_kwargs = model_kwargs or {}

    async def gen_characters(state: StoryState) -> dict:
        _step(emitter, "gen_characters", "设计角色", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_characters" not in visited:
            visited.append("gen_characters")

        params = state.get("story_params", {})
        synopsis = state.get("story_synopsis") or {}
        if not synopsis.get("logline"):
            # 路由直跳（盘上已确认）时查盘兜底
            synopsis = _load_state(params).get("story_synopsis") or {}
        if not synopsis.get("logline"):
            _step(emitter, "gen_characters", "设计角色", "error", "梗概缺失")
            return {"story_error": "梗概缺失，无法设计角色",
                    "final_answer": "梗概缺失，请重新开始。",
                    "nodes_visited": visited}

        system_prompt = SYSTEM_GEN_CHARACTERS.format(
            synopsis_json=json.dumps(synopsis, ensure_ascii=False, indent=1),
            style=params.get("style", "温暖手绘风"))
        user_prompt = "直接输出角色卡 JSON。"

        characters: dict | None = None
        last_error = ""
        feedback_msgs: list[ChatMessage] = []
        for attempt in range(2):
            try:
                resp = gateway.chat(
                    [ChatMessage("system", system_prompt),
                     ChatMessage("user", user_prompt)] + feedback_msgs,
                    temperature=0.4, json_mode=True, **model_kwargs)
            except Exception as exc:  # noqa: BLE001
                _step(emitter, "gen_characters", "设计角色", "error", str(exc))
                return {"story_error": f"角色设计失败：{exc}",
                        "final_answer": f"角色设计失败：{exc}，请重试。",
                        "nodes_visited": visited}

            try:
                parsed = _parse_llm_json(resp.content)
            except ValueError as exc:
                last_error = str(exc)
                feedback_msgs = [
                    ChatMessage("assistant", resp.content[-2000:]),
                    ChatMessage("user", "请只输出角色卡 JSON 对象。"),
                ]
                continue

            errors = _validate_characters(parsed)
            if not errors:
                characters = parsed
                break
            last_error = "；".join(errors[:3])
            feedback_msgs = [
                ChatMessage("assistant", resp.content[-2000:]),
                ChatMessage("user",
                            f"校验失败：{last_error}。请修正后重新输出完整角色卡 JSON。"),
            ]

        if characters is None:
            _step(emitter, "gen_characters", "设计角色", "error", last_error)
            return {"story_error": f"角色设计失败：{last_error}",
                    "final_answer": f"角色设计失败：{last_error}，请重试。",
                    "nodes_visited": visited}

        _save_state(params, story_characters=characters,
                    story_params=params, story_characters_confirmed=False)
        _emit_characters(emitter, characters)

        names = "、".join(c.get("name", "") for c in characters.get("characters", []))
        _step(emitter, "gen_characters", "设计角色", "done",
              f"{len(characters.get('characters', []))} 个角色已设计（{names}）")
        answer = (f"已设计 {len(characters.get('characters', []))} 个角色：{names}。"
                  f"回复\"确认\"生成角色立绘并进入分镜，或直接说修改意见。")
        return {"story_characters": characters,
                "story_characters_confirmed": False,
                "story_error": "",
                "final_answer": answer,
                "nodes_visited": visited}

    return gen_characters
