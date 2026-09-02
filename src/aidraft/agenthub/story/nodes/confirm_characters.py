"""confirm_characters 节点：角色确认点（第二确认点）。

确认 → 先生成立绘（gen_portraits，立绘图是分镜 image_prompt 的视觉
参照），然后进 gen_storyboard。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_EDIT_CHARACTERS
from ..state import (
    StoryState,
    _emit_characters,
    _find_pending_session,
    _load_state,
    _parse_llm_json,
    _save_state,
    _step,
)
from .gen_characters import _validate_characters

_EXACT_CONFIRM = {"确认", "确认角色", "可以", "可以的", "没问题", "就这样",
                  "同意", "好的", "好", "行", "ok", "OK", "继续", "生成分镜",
                  "开始分镜", "下一步"}
_PREFIX_CONFIRM = ("确认", "可以", "好的", "没问题", "同意", "ok", "OK",
                   "继续", "下一步", "生成分镜")


def _detect_confirm(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if s in _EXACT_CONFIRM:
        return True
    return any(s.startswith(w) for w in _PREFIX_CONFIRM)


def _make_confirm_characters_node(gateway: Any, emitter: Callable[[dict], None] | None,
                                  model_kwargs: dict | None = None):
    """confirm_characters 节点工厂。"""
    model_kwargs = model_kwargs or {}

    async def confirm_characters(state: StoryState) -> dict:
        _step(emitter, "confirm_characters", "角色确认", "running")

        visited = list(state.get("nodes_visited") or [])
        if "confirm_characters" not in visited:
            visited.append("confirm_characters")

        params = dict(state.get("story_params") or {})
        user_msg = state.get("user_message") or state.get("task", "")

        disk = _load_state(params) if params.get("title") else {}
        characters = disk.get("story_characters") or {}
        if not characters.get("characters"):
            pending = _find_pending_session("characters")
            if pending:
                params, disk = pending
                characters = disk.get("story_characters") or {}
        if not characters.get("characters"):
            _step(emitter, "confirm_characters", "角色确认", "done", "盘上无角色卡")
            return {"story_characters_confirmed": False, "nodes_visited": visited}

        already_confirmed = bool(disk.get("story_characters_confirmed"))
        is_confirm = already_confirmed or _detect_confirm(user_msg)

        if is_confirm:
            _save_state(params, story_characters=characters,
                        story_params=params,
                        story_characters_confirmed=True)
            _step(emitter, "confirm_characters", "角色确认", "done", "角色已确认")
            return {"story_params": params,
                    "story_characters": characters,
                    "story_characters_confirmed": True,
                    "nodes_visited": visited}

        # 自然语言修改角色卡
        system_prompt = SYSTEM_EDIT_CHARACTERS.format(
            characters_json=json.dumps(characters, ensure_ascii=False, indent=1))
        edited: dict | None = None
        err = ""
        try:
            resp = gateway.chat(
                [ChatMessage("system", system_prompt),
                 ChatMessage("user", user_msg)],
                temperature=0.3, json_mode=True, **model_kwargs)
            parsed = _parse_llm_json(resp.content)
            problems = _validate_characters(parsed)
            if problems:
                err = "；".join(problems[:3])
            else:
                edited = parsed
        except Exception as exc:  # noqa: BLE001
            err = str(exc)

        if edited is None:
            _emit_characters(emitter, characters)
            answer = (f"角色修改指令未能生效（{err[:80]}），已保留原卡。"
                      f"可重新描述修改点，或回复\"确认\"进入分镜。")
            _step(emitter, "confirm_characters", "角色确认", "done", "改卡失败，保留原卡")
            return {"story_params": params,
                    "story_characters": characters,
                    "story_characters_confirmed": False,
                    "final_answer": answer,
                    "nodes_visited": visited}

        _save_state(params, story_characters=edited, story_params=params)
        _emit_characters(emitter, edited)
        _step(emitter, "confirm_characters", "角色确认", "done", "角色卡已按意见修改")
        answer = "角色卡已按你的意见修改。如无其他修改，回复\"确认\"进入分镜。"
        return {"story_params": params,
                "story_characters": edited,
                "story_characters_confirmed": False,
                "final_answer": answer,
                "nodes_visited": visited}

    return confirm_characters
