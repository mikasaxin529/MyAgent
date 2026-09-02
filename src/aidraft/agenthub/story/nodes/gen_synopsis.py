"""gen_synopsis 节点：创意 → 故事梗概（第一确认点产物）。"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_GEN_SYNOPSIS
from ..state import (
    StoryState,
    _emit_synopsis,
    _parse_llm_json,
    _save_state,
    _step,
)


def _validate_synopsis(parsed) -> list[str]:
    """梗概轻校验：结构底线（logline/三幕/synopsis）。"""
    errors: list[str] = []
    if not isinstance(parsed, dict):
        return ["梗概顶层必须是对象"]
    if not str(parsed.get("logline") or "").strip():
        errors.append("缺 logline")
    if not str(parsed.get("synopsis") or "").strip():
        errors.append("缺 synopsis")
    acts = parsed.get("acts")
    if not isinstance(acts, list) or len(acts) < 3:
        errors.append("acts 至少三幕")
    return errors


def _make_gen_synopsis_node(gateway: Any, emitter: Callable[[dict], None] | None,
                            model_kwargs: dict | None = None):
    """gen_synopsis 节点工厂：LLM 生成梗概（json_mode + 失败反馈重试 1 次）。"""
    model_kwargs = model_kwargs or {}

    async def gen_synopsis(state: StoryState) -> dict:
        _step(emitter, "gen_synopsis", "生成梗概", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_synopsis" not in visited:
            visited.append("gen_synopsis")

        params = state.get("story_params", {})
        # _session 是会话隔离用的内部字段，不进 prompt
        prompt_params = {k: v for k, v in params.items() if not k.startswith("_")}

        system_prompt = SYSTEM_GEN_SYNOPSIS.format(
            params_json=json.dumps(prompt_params, ensure_ascii=False, indent=1))
        user_prompt = "直接输出梗概 JSON。"

        synopsis: dict | None = None
        last_error = ""
        feedback_msgs: list[ChatMessage] = []
        for attempt in range(2):
            try:
                llm_msgs = [ChatMessage("system", system_prompt),
                            ChatMessage("user", user_prompt)] + feedback_msgs
                resp = gateway.chat(llm_msgs, temperature=0.4, json_mode=True,
                                    **model_kwargs)
            except Exception as exc:  # noqa: BLE001
                _step(emitter, "gen_synopsis", "生成梗概", "error", str(exc))
                return {
                    "story_error": f"梗概生成失败：{exc}",
                    "final_answer": f"梗概生成失败：{exc}，请重试。",
                    "nodes_visited": visited,
                }

            try:
                parsed = _parse_llm_json(resp.content)
            except ValueError as exc:
                last_error = str(exc)
                feedback_msgs = [
                    ChatMessage("assistant", resp.content[-2000:]),
                    ChatMessage("user",
                                "上一轮输出不是合法 JSON。请只输出梗概 JSON 对象，"
                                "不要任何解释文字。"),
                ]
                continue

            errors = _validate_synopsis(parsed)
            if not errors:
                synopsis = parsed
                break
            last_error = "；".join(errors[:3])
            feedback_msgs = [
                ChatMessage("assistant", resp.content[-2000:]),
                ChatMessage("user",
                            f"上一轮梗概校验失败：{last_error}。"
                            "请修正后重新输出完整梗概 JSON。"),
            ]

        if synopsis is None:
            _step(emitter, "gen_synopsis", "生成梗概", "error", last_error)
            return {
                "story_error": f"梗概生成失败：{last_error}",
                "final_answer": f"梗概生成失败：{last_error}，请重试。",
                "nodes_visited": visited,
            }

        # 落盘 + 发帧（第一确认点交接：本轮 END，confirm_synopsis 下轮查盘）
        _save_state(params,
                    story_synopsis=synopsis,
                    story_params=params,
                    story_synopsis_confirmed=False)
        _emit_synopsis(emitter, synopsis)

        n_scenes = synopsis.get("scene_count",
                                sum(len(a.get("shots") or []) for a in synopsis.get("acts") or []))
        _step(emitter, "gen_synopsis", "生成梗概", "done",
              f"梗概已生成（约 {n_scenes} 场），等待确认")
        answer = (f"《{synopsis.get('title', params.get('title', ''))}》梗概已生成。"
                  f"回复\"确认\"进入角色设计，或直接说修改意见。")
        return {
            "story_synopsis": synopsis,
            "story_synopsis_confirmed": False,
            "story_error": "",
            "final_answer": answer,
            "nodes_visited": visited,
        }

    return gen_synopsis
