"""confirm_synopsis 节点：梗概确认点（第一确认点，与 yuwen/confirm 同构）。

查盘恢复梗概 → 确认放行 gen_characters / 自然语言改梗概（LLM）。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_EDIT_SYNOPSIS
from ..state import (
    StoryState,
    _emit_synopsis,
    _find_pending_session,
    _load_state,
    _parse_llm_json,
    _save_state,
    _step,
)
from .gen_synopsis import _validate_synopsis

# 确认意图词表（整句等于或前缀命中）
_EXACT_CONFIRM = {"确认", "确认梗概", "可以", "可以的", "没问题", "就这样",
                  "同意", "好的", "好", "行", "ok", "OK", "继续", "生成角色",
                  "开始角色设计", "下一步"}
_PREFIX_CONFIRM = ("确认", "可以", "好的", "没问题", "同意", "ok", "OK",
                   "继续", "下一步", "生成角色")


def _detect_confirm(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if s in _EXACT_CONFIRM:
        return True
    return any(s.startswith(w) for w in _PREFIX_CONFIRM)


def _make_confirm_synopsis_node(gateway: Any, emitter: Callable[[dict], None] | None,
                                model_kwargs: dict | None = None):
    """confirm_synopsis 节点工厂。"""
    model_kwargs = model_kwargs or {}

    async def confirm_synopsis(state: StoryState) -> dict:
        _step(emitter, "confirm_synopsis", "梗概确认", "running")

        visited = list(state.get("nodes_visited") or [])
        if "confirm_synopsis" not in visited:
            visited.append("confirm_synopsis")

        params = dict(state.get("story_params") or {})
        user_msg = state.get("user_message") or state.get("task", "")

        # 查盘恢复梗概
        disk = _load_state(params) if params.get("title") else {}
        synopsis = disk.get("story_synopsis") or {}
        if not synopsis.get("logline"):
            pending = _find_pending_session()
            if pending:
                params, disk = pending
                synopsis = disk.get("story_synopsis") or {}
        if not synopsis.get("logline"):
            _step(emitter, "confirm_synopsis", "梗概确认", "done", "盘上无梗概")
            return {"story_synopsis_confirmed": False, "nodes_visited": visited}

        already_confirmed = bool(disk.get("story_synopsis_confirmed"))
        is_confirm = already_confirmed or _detect_confirm(user_msg)

        # 路径 A：确认 → 放行 gen_characters
        if is_confirm:
            _save_state(params, story_synopsis=synopsis,
                        story_params=params,
                        story_synopsis_confirmed=True)
            _step(emitter, "confirm_synopsis", "梗概确认", "done", "梗概已确认")
            return {"story_params": params,
                    "story_synopsis": synopsis,
                    "story_synopsis_confirmed": True,
                    "nodes_visited": visited}

        # 路径 B：自然语言修改 → LLM 改梗概
        system_prompt = SYSTEM_EDIT_SYNOPSIS.format(
            synopsis_json=json.dumps(synopsis, ensure_ascii=False, indent=1))
        edited: dict | None = None
        err = ""
        try:
            resp = gateway.chat(
                [ChatMessage("system", system_prompt),
                 ChatMessage("user", user_msg)],
                temperature=0.3, json_mode=True, **model_kwargs)
            parsed = _parse_llm_json(resp.content)
            problems = _validate_synopsis(parsed)
            if problems:
                err = "；".join(problems[:3])
            else:
                edited = parsed
        except Exception as exc:  # noqa: BLE001 - 改稿失败降级为提示
            err = str(exc)

        if edited is None:
            _emit_synopsis(emitter, synopsis)
            answer = (f"梗概修改指令未能生效（{err[:80]}），已保留原稿。"
                      f"可重新描述修改点，或回复\"确认\"进入角色设计。")
            _step(emitter, "confirm_synopsis", "梗概确认", "done", "改稿失败，保留原稿")
            return {"story_params": params,
                    "story_synopsis": synopsis,
                    "story_synopsis_confirmed": False,
                    "final_answer": answer,
                    "nodes_visited": visited}

        _save_state(params, story_synopsis=edited, story_params=params)
        _emit_synopsis(emitter, edited)
        _step(emitter, "confirm_synopsis", "梗概确认", "done", "梗概已按意见修改")
        answer = "梗概已按你的意见修改。如无其他修改，回复\"确认\"进入角色设计。"
        return {"story_params": params,
                "story_synopsis": edited,
                "story_synopsis_confirmed": False,
                "final_answer": answer,
                "nodes_visited": visited}

    return confirm_synopsis
