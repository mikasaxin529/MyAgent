"""confirm_storyboard 节点：分镜确认点（第三确认点 = 终确认）。

确认 → export（导出四件套）。修改 → LLM 改分镜。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_EDIT_STORYBOARD
from ..state import (
    StoryState,
    _emit_storyboard,
    _find_pending_session,
    _load_state,
    _parse_llm_json,
    _save_state,
    _step,
)
from .gen_storyboard import _validate_storyboard

_EXACT_CONFIRM = {"确认", "确认分镜", "可以", "可以的", "没问题", "就这样",
                  "同意", "好的", "好", "行", "ok", "OK", "继续", "导出",
                  "开始导出", "导出吧"}
_PREFIX_CONFIRM = ("确认", "可以", "好的", "没问题", "同意", "ok", "OK",
                   "继续", "导出")


def _detect_confirm(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if s in _EXACT_CONFIRM:
        return True
    return any(s.startswith(w) for w in _PREFIX_CONFIRM)


def _make_confirm_storyboard_node(gateway: Any, emitter: Callable[[dict], None] | None,
                                  model_kwargs: dict | None = None):
    """confirm_storyboard 节点工厂。"""
    model_kwargs = model_kwargs or {}

    async def confirm_storyboard(state: StoryState) -> dict:
        _step(emitter, "confirm_storyboard", "分镜确认", "running")

        visited = list(state.get("nodes_visited") or [])
        if "confirm_storyboard" not in visited:
            visited.append("confirm_storyboard")

        params = dict(state.get("story_params") or {})
        user_msg = state.get("user_message") or state.get("task", "")

        disk = _load_state(params) if params.get("title") else {}
        storyboard = disk.get("story_storyboard") or {}
        if not storyboard.get("scenes"):
            pending = _find_pending_session(
                "storyboard", session_short=str(state.get("session_id") or "")[-8:])
            if pending:
                params, disk = pending
                storyboard = disk.get("story_storyboard") or {}
        if not storyboard.get("scenes"):
            _step(emitter, "confirm_storyboard", "分镜确认", "done", "盘上无分镜")
            return {"story_storyboard_confirmed": False, "nodes_visited": visited}

        already_confirmed = bool(disk.get("story_storyboard_confirmed"))
        is_confirm = already_confirmed or _detect_confirm(user_msg)

        if is_confirm:
            _save_state(params, story_storyboard=storyboard,
                        story_params=params,
                        story_storyboard_confirmed=True)
            _step(emitter, "confirm_storyboard", "分镜确认", "done", "分镜已确认")
            return {"story_params": params,
                    "story_storyboard": storyboard,
                    "story_storyboard_confirmed": True,
                    "nodes_visited": visited}

        # 自然语言修改分镜
        system_prompt = SYSTEM_EDIT_STORYBOARD.format(
            storyboard_json=json.dumps(storyboard, ensure_ascii=False, indent=1))
        edited: dict | None = None
        err = ""
        try:
            resp = gateway.chat(
                [ChatMessage("system", system_prompt),
                 ChatMessage("user", user_msg)],
                temperature=0.3, json_mode=True, **model_kwargs)
            parsed = _parse_llm_json(resp.content)
            problems = _validate_storyboard(parsed)
            if problems:
                err = "；".join(problems[:3])
            else:
                edited = parsed
        except Exception as exc:  # noqa: BLE001
            err = str(exc)

        if edited is None:
            _emit_storyboard(emitter, storyboard)
            answer = (f"分镜修改指令未能生效（{err[:80]}），已保留原稿。"
                      f"可重新描述修改点，或回复\"确认\"导出。")
            _step(emitter, "confirm_storyboard", "分镜确认", "done", "改分镜失败，保留原稿")
            return {"story_params": params,
                    "story_storyboard": storyboard,
                    "story_storyboard_confirmed": False,
                    "final_answer": answer,
                    "nodes_visited": visited}

        _save_state(params, story_storyboard=edited, story_params=params)
        _emit_storyboard(emitter, edited)
        _step(emitter, "confirm_storyboard", "分镜确认", "done", "分镜已按意见修改")
        answer = "分镜已按你的意见修改。如无其他修改，回复\"确认\"导出全部交付物。"
        return {"story_params": params,
                "story_storyboard": edited,
                "story_storyboard_confirmed": False,
                "final_answer": answer,
                "nodes_visited": visited}

    return confirm_storyboard
