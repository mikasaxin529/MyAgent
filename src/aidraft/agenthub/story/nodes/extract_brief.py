"""extract_brief 节点：对话收集故事创意参数（与 yuwen/extract_params 同构）。"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_EXTRACT_BRIEF
from ..state import StoryState, _emit_content, _step


def _normalize_duration(raw: Any) -> int:
    """时长归一化为 int 分钟（LLM 常返回 8.0/"8"）。解析失败返回 0。"""
    if isinstance(raw, bool):
        return 0
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _make_extract_brief_node(gateway: Any, emitter: Callable[[dict], None] | None):
    """extract_brief 节点工厂。"""

    async def extract_brief(state: StoryState) -> dict:
        _step(emitter, "extract_brief", "解析创意", "running")

        visited = list(state.get("nodes_visited") or [])
        if "extract_brief" not in visited:
            visited.append("extract_brief")

        msgs = list(state.get("messages") or [])
        user_msg = state.get("user_message") or state.get("task", "")

        # 创意收集阶段的完整用户原话：history 里的 user 消息 + 当轮
        # （分几轮说清设定时，brief 不能只有最后一句）。保序去重——
        # 当轮消息可能已被前端塞进 history 末尾，重复会稀释 brief
        _seen: set[str] = set()
        _parts: list[str] = []
        for m in msgs:
            if isinstance(m, dict):
                role, content = m.get("role", ""), str(m.get("content", ""))
            elif hasattr(m, "role"):
                role, content = m.role, str(m.content)
            else:
                continue
            if role == "user" and content.strip():
                c = content.strip()
                if c not in _seen:
                    _seen.add(c)
                    _parts.append(c)
        cur = user_msg.strip()
        if cur and cur not in _seen:
            _parts.append(cur)
        brief_full = "\n".join(_parts)

        llm_msgs = [ChatMessage("system", SYSTEM_EXTRACT_BRIEF)]
        if msgs:
            for m in msgs:
                if isinstance(m, dict):
                    llm_msgs.append(ChatMessage(m.get("role", "user"), str(m.get("content", ""))))
                elif hasattr(m, "role"):
                    llm_msgs.append(ChatMessage(m.role, m.content))
        if user_msg:
            last_content = ""
            if msgs:
                last = msgs[-1]
                if isinstance(last, dict):
                    last_content = last.get("content", "")
                elif hasattr(last, "content"):
                    last_content = last.content
            if last_content != user_msg:
                llm_msgs.append(ChatMessage("user", user_msg))

        try:
            resp = gateway.chat(llm_msgs, temperature=0.1, json_mode=True)
            parsed = json.loads(resp.content)
        except Exception as exc:
            _step(emitter, "extract_brief", "解析创意", "error", str(exc))
            return {
                "story_params": {},
                "story_params_ready": False,
                "final_answer": f"创意解析失败：{exc}，请重试。",
                "nodes_visited": visited,
            }

        title = (parsed.get("title") or "").strip()
        audience = (parsed.get("audience") or "").strip()
        genre = (parsed.get("genre") or "").strip()
        style = (parsed.get("style") or "").strip()
        duration = _normalize_duration(parsed.get("duration_min", 0))
        question = (parsed.get("question") or "").strip()
        chips = parsed.get("chips") or []

        # 创意有实质内容（标题非空）即 ready；受众/题材空时给默认值
        params_ready = bool(title)
        # 前端会话短码：_session_name 据此隔离 state.json（同片名新会话
        # 不被旧会话盘上状态劫持）。不进 LLM 输出，只落盘。
        session_short = str(state.get("session_id") or "")[-8:]
        if not audience:
            audience = "全年龄"
        if not genre:
            genre = "冒险"
        if not style:
            style = "温暖手绘风"
        if duration <= 0:
            duration = 8

        if not params_ready:
            if not question:
                question = "请描述你的故事创意：主角是谁？遇到什么事？"
            _emit_content(emitter, question, "extract_brief",
                          [str(c) for c in chips] if isinstance(chips, list) and chips else None)
            _step(emitter, "extract_brief", "解析创意", "done", "追问创意")
            return {
                "story_params": {},
                "story_params_ready": False,
                "final_answer": question,
                "nodes_visited": visited,
            }

        params = {
            "title": title,
            "audience": audience,
            "genre": genre,
            "duration_min": duration,
            "style": style,
            # 用户原话逐字直通（不经 LLM 转写）：extract 的 5 字段骨架会丢掉
            # 修仙背景、人物关系等一切细节——线上实测用户设定全灭、被套幼儿
            # 模板。brief 随 params 落盘，供 gen_synopsis/confirm_synopsis 做
            # 设定锚点。
            "brief": brief_full,
        }
        if session_short:
            params["_session"] = session_short
        detail = f"《{title}》· {audience} · {genre} · {duration}分钟 · {style}"
        _step(emitter, "extract_brief", "解析创意", "done", detail)
        return {
            "story_params": params,
            "story_params_ready": True,
            "nodes_visited": visited,
        }

    return extract_brief
