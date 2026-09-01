"""extract_params 节点：对话追问收集参数（LLM 提取 title/grade/lesson_type）。"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_EXTRACT
from ..state import YuwenState, _emit, _step


def _normalize_grade(raw: Any) -> int:
    """把 LLM 返回的年级归一化为 int。

    DeepSeek/Qwen json_mode 常返回 2.0(float) 或 "2"(string)，统一归一化：
    int(float(str(raw))) 兼容 int / float / 字符串数字。解析失败返回 0。
    """
    if isinstance(raw, bool):
        return 0
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _image_prefs(parsed: dict) -> dict:
    """从 LLM 抽取结果提取可选配图偏好（image_style/image_count）。

    用户没提或值非法就不写该键——gen_images 侧缺省走"绘本 + minimal"。
    """
    from .gen_images import IMAGE_COUNTS, IMAGE_STYLES

    prefs: dict = {}
    style = str(parsed.get("image_style") or "").strip()
    if style in IMAGE_STYLES:
        prefs["image_style"] = style
    count = str(parsed.get("image_count") or "").strip()
    if count in IMAGE_COUNTS:
        prefs["image_count"] = count
    return prefs


def _make_extract_params_node(gateway: Any, emitter: Callable[[dict], None] | None):
    """extract_params 节点工厂：对话追问收集参数。"""

    async def extract_params(state: YuwenState) -> dict:
        _step(emitter, "extract_params", "解析参数", "running")

        visited = list(state.get("nodes_visited") or [])
        if "extract_params" not in visited:
            visited.append("extract_params")

        # 从 state 取消息
        msgs = list(state.get("messages") or [])
        user_msg = state.get("user_message") or state.get("task", "")

        # 构建 LLM 消息：system + 历史 + 当前用户输入
        llm_msgs = [ChatMessage("system", SYSTEM_EXTRACT)]
        # 历史消息（排除 system 和当前 user 的最后一条）
        if msgs:
            for m in msgs:
                if isinstance(m, dict):
                    llm_msgs.append(ChatMessage(m.get("role", "user"), str(m.get("content", ""))))
                elif hasattr(m, "role"):
                    llm_msgs.append(ChatMessage(m.role, m.content))
        # 当前用户消息（如果不在历史中）
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

        # 调 LLM 解析参数
        try:
            resp = gateway.chat(llm_msgs, temperature=0.1, json_mode=True)
            parsed = json.loads(resp.content)
        except Exception as exc:
            # LLM 调用失败时的降级
            _step(emitter, "extract_params", "解析参数", "error", str(exc))
            return {
                "yuwen_params": {},
                "yuwen_params_ready": False,
                "final_answer": f"参数解析失败：{exc}，请重试。",
                "nodes_visited": visited,
            }

        title = (parsed.get("title") or "").strip()
        grade_raw = parsed.get("grade", 0)
        grade = _normalize_grade(grade_raw)
        lesson_type = (parsed.get("lesson_type") or "").strip()
        textbook = (parsed.get("textbook") or "").strip()
        question = (parsed.get("question") or "").strip()
        chips = parsed.get("chips") or []

        params_ready = bool(title and 1 <= grade <= 6 and lesson_type)
        prefs = _image_prefs(parsed)  # 用户提到了才有键，缺省走 gen_images 默认

        if not params_ready:
            # 参数缺失，返回追问。
            # 只发 content 帧（后端 final_answer 对 content 与 token 都累加，
            # 同时发两种会重复累加追问文本；通用对话 call_model 用 content，
            # 追问轮沿用 content 保持一致）。
            if not question:
                question = "请提供课文名和年级，例如：《静夜思》 一年级 古诗词"
            # 追问轮 content 帧携带 chips（LLM 返回的快捷选项），字段名不可变。
            # 前端按 {"type":"content","chips":[...]} 消费。
            content_frame: dict = {"type": "content", "delta": question, "step_id": "extract_params"}
            if isinstance(chips, list) and chips:
                content_frame["chips"] = [str(c) for c in chips]
            _emit(emitter, content_frame)
            _step(emitter, "extract_params", "解析参数", "done", "追问参数")
            ask_params = {
                "title": title,
                "grade": grade if isinstance(grade, int) else 0,
                "lesson_type": lesson_type or "",
                "textbook": textbook or "",
            }
            ask_params.update(prefs)
            return {
                "yuwen_params": ask_params,
                "yuwen_params_ready": False,
                "final_answer": question,
                "nodes_visited": visited,
            }

        # 参数齐备
        params = {
            "title": title,
            "grade": grade,
            "lesson_type": lesson_type,
            "textbook": textbook or f"部编版{grade}年级",
        }
        params.update(prefs)
        detail = f"《{title}》· {grade}年级 · {lesson_type}"
        if prefs.get("image_style"):
            detail += f" · 配图{prefs['image_style']}"
        if prefs.get("image_count"):
            detail += f" · {prefs['image_count']}"
        _step(emitter, "extract_params", "解析参数", "done", detail)
        return {
            "yuwen_params": params,
            "yuwen_params_ready": True,
            "nodes_visited": visited,
        }

    return extract_params
