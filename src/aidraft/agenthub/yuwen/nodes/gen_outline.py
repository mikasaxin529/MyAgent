"""gen_outline 节点：轻量大纲生成（每页 kind/title/一句话要点），产出后 END 等确认。

跨轮状态机第一步：大纲落盘 state.json + 发 outline 帧 → 本轮 END。
用户下一轮消息由 confirm 节点查盘恢复大纲，确认或修改。
"""
from __future__ import annotations

from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_GEN_OUTLINE, META_CONTRACT, _read_ref, _themes_hint
from ..state import (
    YuwenState,
    _emit_outline,
    _load_state,
    _outline_summary,
    _parse_llm_json,
    _save_state,
    _step,
)
from ._page import _call_llm, _validate_outline


def _make_gen_outline_node(gateway: Any, emitter: Callable[[dict], None] | None,
                           model_kwargs: dict | None = None):
    """gen_outline 节点工厂：LLM 生成页面大纲（非流式，json_mode）。

    大纲短（每页一行），不值得流式；json_mode 让 DeepSeek/Qwen 直接产合法
    JSON，解析失败带错误反馈重试 1 次即可。
    """
    model_kwargs = model_kwargs or {}

    async def gen_outline(state: YuwenState) -> dict:
        _step(emitter, "gen_outline", "生成大纲", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_outline" not in visited:
            visited.append("gen_outline")

        params = state.get("yuwen_params", {})

        # 联网参考资料（M2）：research 节点写入 state；路由直跳本节点的
        # 场景（盘上已确认后重跑等）state 里没有——查盘兜底
        research = state.get("yuwen_research") or {}
        if not research.get("content"):
            research = _load_state(params).get("yuwen_research") or {}

        system_prompt = SYSTEM_GEN_OUTLINE.format(
            stages=_read_ref("stages.md"),
            lesson_types=_read_ref("lesson-types.md"),
            meta_contract=META_CONTRACT.format(themes=_themes_hint()),
            themes=_themes_hint(),
        )
        research_seg = ""
        if research.get("content"):
            research_seg = (
                f"\n## 联网参考资料（真实网络搜索结果，供参考——"
                f"教学设计要原创，只借结构思路；课文原文以资料为准）\n"
                f"{research['content']}\n"
            )
        user_prompt = (
            f"请为以下课文设计课件大纲：\n"
            f"课文名：{params.get('title', '')}\n"
            f"年级：{params.get('grade', '')}\n"
            f"课型：{params.get('lesson_type', '')}\n"
            f"教材版本：{params.get('textbook', '')}\n"
            f"{research_seg}\n"
            f"直接输出大纲 JSON。"
        )

        outline: dict | None = None
        last_error = ""
        feedback_msgs: list[ChatMessage] = []
        # 轻校验失败重试 1 次（共 2 次尝试）：带错误反馈重生成，
        # 与原样重掷不同——模型看到缺什么字段才能补什么。
        for attempt in range(2):
            try:
                llm_msgs = [ChatMessage("system", system_prompt),
                            ChatMessage("user", user_prompt)] + feedback_msgs
                resp = _call_llm(gateway, "chat", llm_msgs, model_kwargs,
                                 temperature=0.3, json_mode=True)
            except Exception as exc:  # noqa: BLE001 - 网关失败反馈给用户
                _step(emitter, "gen_outline", "生成大纲", "error", str(exc))
                return {
                    "yuwen_outline": {},
                    "yuwen_outline_confirmed": False,
                    "yuwen_error": f"大纲生成失败：{exc}",
                    "final_answer": f"大纲生成失败：{exc}，请重试。",
                    "nodes_visited": visited,
                }

            try:
                parsed = _parse_llm_json(resp.content)
            except ValueError as exc:
                last_error = str(exc)
                feedback_msgs = [
                    ChatMessage("assistant", resp.content[-2000:]),
                    ChatMessage("user",
                                "上一轮输出不是合法 JSON。请只输出大纲 JSON 对象"
                                "（含 pages 和 meta），不要任何解释文字。"),
                ]
                continue

            errors = _validate_outline(parsed)
            if not errors:
                outline = parsed
                break
            last_error = "；".join(errors[:5])
            feedback_msgs = [
                ChatMessage("assistant", resp.content[-2000:]),
                ChatMessage("user",
                            f"上一轮大纲校验失败：{last_error}。"
                            "请修正后重新输出完整大纲 JSON。"),
            ]

        if outline is None:
            _step(emitter, "gen_outline", "生成大纲", "error", last_error)
            return {
                "yuwen_outline": {},
                "yuwen_outline_confirmed": False,
                "yuwen_error": f"大纲生成失败：{last_error}",
                "final_answer": f"大纲生成失败：{last_error}，请重试。",
                "nodes_visited": visited,
            }

        # 落盘 state.json + 发 outline 帧（跨轮状态机的交接点：
        # 本轮 END 后图销毁，confirm 下轮从盘上找回大纲）
        _save_state(params,
                    yuwen_outline=outline,
                    yuwen_outline_confirmed=False,
                    yuwen_params=params)
        _emit_outline(emitter, outline)

        n_pages = len(outline.get("pages", []))
        _step(emitter, "gen_outline", "生成大纲", "done",
              f"{n_pages} 页大纲已生成，等待确认")
        return {
            "yuwen_outline": outline,
            "yuwen_outline_confirmed": False,
            "yuwen_error": "",
            "final_answer": _outline_summary(outline),
            "nodes_visited": visited,
        }

    return gen_outline
