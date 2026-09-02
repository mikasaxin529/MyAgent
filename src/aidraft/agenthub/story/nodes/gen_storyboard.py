"""gen_storyboard 节点：梗概+角色卡 → 分镜脚本（第三确认点产物）。"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_GEN_STORYBOARD
from ..state import (
    StoryState,
    _emit_storyboard,
    _load_state,
    _parse_llm_json,
    _save_state,
    _step,
)

_SHOT_SIZES = {"大远景", "远景", "全景", "中景", "近景", "特写", "大特写"}


def _validate_storyboard(parsed) -> list[str]:
    """分镜轻校验：场非空、每场有镜头、镜头有 id/景别/画面描述。"""
    errors: list[str] = []
    if not isinstance(parsed, dict):
        return ["分镜顶层必须是对象"]
    scenes = parsed.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return ["scenes 缺失或为空"]
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            errors.append(f"scenes[{i}] 不是对象")
            continue
        shots = sc.get("shots")
        if not isinstance(shots, list) or not shots:
            errors.append(f"scenes[{i}] 缺 shots")
            continue
        for j, sh in enumerate(shots):
            if not isinstance(sh, dict):
                errors.append(f"scenes[{i}].shots[{j}] 不是对象")
                continue
            for k in ("id", "shot_size", "image_prompt"):
                if not str(sh.get(k) or "").strip():
                    errors.append(f"scenes[{i}].shots[{j}] 缺 {k}")
    return errors


def _make_gen_storyboard_node(gateway: Any, emitter: Callable[[dict], None] | None,
                              model_kwargs: dict | None = None):
    """gen_storyboard 节点工厂。"""
    model_kwargs = model_kwargs or {}

    async def gen_storyboard(state: StoryState) -> dict:
        _step(emitter, "gen_storyboard", "创作分镜", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_storyboard" not in visited:
            visited.append("gen_storyboard")

        params = state.get("story_params", {})
        synopsis = state.get("story_synopsis") or {}
        characters = state.get("story_characters") or {}
        disk = _load_state(params)
        if not synopsis.get("logline"):
            synopsis = disk.get("story_synopsis") or {}
        if not characters.get("characters"):
            characters = disk.get("story_characters") or {}
        if not synopsis.get("logline"):
            _step(emitter, "gen_storyboard", "创作分镜", "error", "梗概缺失")
            return {"story_error": "梗概缺失，无法创作分镜",
                    "final_answer": "梗概缺失，请重新开始。",
                    "nodes_visited": visited}

        system_prompt = SYSTEM_GEN_STORYBOARD.format(
            synopsis_json=json.dumps(synopsis, ensure_ascii=False, indent=1),
            characters_json=json.dumps(characters, ensure_ascii=False, indent=1),
            style=params.get("style", "温暖手绘风"))
        user_prompt = "直接输出分镜 JSON。"

        storyboard: dict | None = None
        last_error = ""
        feedback_msgs: list[ChatMessage] = []
        for attempt in range(2):
            try:
                resp = gateway.chat(
                    [ChatMessage("system", system_prompt),
                     ChatMessage("user", user_prompt)] + feedback_msgs,
                    temperature=0.4, json_mode=True, **model_kwargs)
            except Exception as exc:  # noqa: BLE001
                _step(emitter, "gen_storyboard", "创作分镜", "error", str(exc))
                return {"story_error": f"分镜创作失败：{exc}",
                        "final_answer": f"分镜创作失败：{exc}，请重试。",
                        "nodes_visited": visited}

            try:
                parsed = _parse_llm_json(resp.content)
            except ValueError as exc:
                last_error = str(exc)
                feedback_msgs = [
                    ChatMessage("assistant", resp.content[-2000:]),
                    ChatMessage("user", "请只输出分镜 JSON 对象。"),
                ]
                continue

            errors = _validate_storyboard(parsed)
            if not errors:
                storyboard = parsed
                break
            last_error = "；".join(errors[:3])
            feedback_msgs = [
                ChatMessage("assistant", resp.content[-2000:]),
                ChatMessage("user",
                            f"校验失败：{last_error}。请修正后重新输出完整分镜 JSON。"),
            ]

        if storyboard is None:
            _step(emitter, "gen_storyboard", "创作分镜", "error", last_error)
            return {"story_error": f"分镜创作失败：{last_error}",
                    "final_answer": f"分镜创作失败：{last_error}，请重试。",
                    "nodes_visited": visited}

        _save_state(params, story_storyboard=storyboard,
                    story_params=params, story_storyboard_confirmed=False)
        _emit_storyboard(emitter, storyboard)

        n_scenes = len(storyboard.get("scenes", []))
        n_shots = sum(len(sc.get("shots") or [])
                      for sc in storyboard.get("scenes", []))
        _step(emitter, "gen_storyboard", "创作分镜", "done",
              f"{n_scenes} 场 / {n_shots} 镜分镜已生成，等待确认")
        answer = (f"分镜已生成：{n_scenes} 场 / {n_shots} 镜。"
                  f"回复\"确认\"导出全部交付物，或直接说修改意见。")
        return {"story_storyboard": storyboard,
                "story_storyboard_confirmed": False,
                "story_error": "",
                "final_answer": answer,
                "nodes_visited": visited}

    return gen_storyboard
