"""gen_content 节点：LLM 按 references/schema.md 生成课件 JSON（自检 + 反思重试）。"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_GEN_CONTENT, _read_ref
from ..state import (
    YuwenState,
    _OUTPUTS_DIR,
    _emit,
    _session_name,
    _step,
)


def _make_gen_content_node(gateway: Any, emitter: Callable[[dict], None] | None):
    """gen_content 节点工厂：LLM 按 schema 生成课件 JSON。"""

    async def gen_content(state: YuwenState) -> dict:
        _step(emitter, "gen_content", "生成课件 JSON", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_content" not in visited:
            visited.append("gen_content")

        params = state.get("yuwen_params", {})

        # 读取参考文件
        schema_text = _read_ref("schema.md")
        lesson_types_text = _read_ref("lesson-types.md")
        stages_text = _read_ref("stages.md")
        curriculum_text = _read_ref("curriculum.md")
        # few-shot：完整合法示例（结构标杆）。模型模仿现成结构远比读
        # schema 描述可靠——线上三连失败（type='text'/'word_card'/elements
        # 非数组）全是"格式理解偏差"，示例直接消除这类偏差。
        example_text = _read_ref("examples/jingyesi.json")

        system_prompt = SYSTEM_GEN_CONTENT.format(
            stages=stages_text,
            lesson_types=lesson_types_text,
            schema=schema_text,
            curriculum=curriculum_text,
            example=example_text,
        )

        user_prompt = (
            f"请为以下课文生成课件 JSON：\n"
            f"课文名：{params.get('title', '')}\n"
            f"年级：{params.get('grade', '')}\n"
            f"课型：{params.get('lesson_type', '')}\n"
            f"教材版本：{params.get('textbook', '')}\n\n"
            f"直接输出合法的 JSON 对象。"
        )

        # 反思重试：失败时把"错误 + 上轮输出片段"以 assistant+user 消息
        # 反馈给模型重新生成（而非原样重掷骰子——温度相同的两次采样
        # 大概率复现同一类结构偏差）。反馈保留在 messages 里跨轮累积：
        # 第 2 次失败时模型能看到全部历史错误，越改越准。
        max_attempts = 3
        feedback_msgs: list[ChatMessage] = []
        content = ""
        doc: dict | None = None
        last_error = ""
        for attempt in range(max_attempts):
            content = ""
            finish_reason = ""
            try:
                llm_msgs = [ChatMessage("system", system_prompt),
                            ChatMessage("user", user_prompt)] + feedback_msgs
                async for chunk in gateway.stream_chat(
                    llm_msgs,
                    temperature=0.3 + 0.2 * attempt,
                ):
                    if chunk.delta:
                        content += chunk.delta
                        _emit(emitter, {
                            "type": "token",
                            "delta": chunk.delta,
                            "step_id": "gen_content",
                        })
                    if chunk.reasoning:
                        _emit(emitter, {
                            "type": "thinking",
                            "node": "gen_content",
                            "phase": "reasoning",
                            "delta": chunk.reasoning,
                        })
                    if chunk.done and chunk.finish_reason:
                        finish_reason = chunk.finish_reason
            except Exception as exc:
                if attempt < max_attempts - 1:
                    _emit(emitter, {
                        "type": "token",
                        "delta": f"\n[重试] 生成失败：{exc}，正在重试...\n",
                        "step_id": "gen_content",
                    })
                    continue
                _step(emitter, "gen_content", "生成课件 JSON", "error", str(exc))
                return {
                    "yuwen_content": {},
                    "yuwen_content_path": "",
                    "yuwen_error": f"课件生成失败：{exc}",
                    "final_answer": f"课件生成失败：{exc}",
                    "nodes_visited": visited,
                }

            # 尝试解析 JSON
            doc = None
            parse_error = ""
            try:
                # 尝试直接解析
                doc = json.loads(content)
            except json.JSONDecodeError as e:
                parse_error = str(e)
                # 尝试提取 markdown 代码块中的 JSON
                import re
                m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
                if m:
                    try:
                        doc = json.loads(m.group(1))
                    except json.JSONDecodeError as e2:
                        parse_error = str(e2)
            if doc is None:
                # 尝试从第一个 { 到最后一个 }
                start = content.find("{")
                end = content.rfind("}")
                if start >= 0 and end > start:
                    try:
                        doc = json.loads(content[start:end + 1])
                    except json.JSONDecodeError as e3:
                        parse_error = str(e3)

            if doc is None:
                if attempt < max_attempts - 1:
                    # 截断是 JSON 解析失败的高频根因：finish_reason=="length"
                    # 说明输出被 max_tokens 硬截断，残缺 JSON 无法修复，
                    # 反馈要带针对性指令（压缩篇幅），而非只报语法错误。
                    truncated = finish_reason == "length"
                    _emit(emitter, {
                        "type": "token",
                        "delta": "\n[输出被截断，正在精简重新生成...]\n"
                                 if truncated else
                                 "\n[JSON 解析失败，正在带着错误反馈重新生成...]\n",
                        "step_id": "gen_content",
                    })
                    if truncated:
                        retry_hint = (
                            "上一轮输出因超出长度上限被截断，JSON 不完整。"
                            "请重新生成：压缩每页文字密度（每页 2-4 个元素），"
                            "减少 slides 数量，确保输出完整的 JSON 对象。"
                        )
                    else:
                        retry_hint = (
                            f"上一轮输出不是合法 JSON（错误：{parse_error}）。"
                            f"请严格检查：不要用 markdown 代码块包裹，不要输出任何"
                            f"JSON 以外的文字，确保引号/括号完整配对。重新生成完整的 JSON 对象。"
                        )
                    feedback_msgs.append(ChatMessage("assistant", content[-2000:]))
                    feedback_msgs.append(ChatMessage("user", retry_hint))
                    continue
                _step(emitter, "gen_content", "生成课件 JSON", "error", "JSON 解析失败")
                return {
                    "yuwen_content": {},
                    "yuwen_content_path": "",
                    "yuwen_error": "课件 JSON 生成失败：无法解析 LLM 输出为合法 JSON。",
                    "final_answer": "课件 JSON 生成失败：无法解析 LLM 输出为合法 JSON。",
                    "nodes_visited": visited,
                }

            # 校验 schema（先归一化：text/question/散装 word-card 等常见
            # 模型偏差自动转换，转换不了的才报错重试）
            from ..scripts.common.schema import validate, normalize, SchemaError
            try:
                doc = validate(normalize(doc))
                # 校验通过
                break
            except SchemaError as e:
                last_error = str(e)
                if attempt < max_attempts - 1:
                    _emit(emitter, {
                        "type": "token",
                        "delta": f"\n[schema 校验失败：{e}，正在带着错误反馈重新生成...]\n",
                        "step_id": "gen_content",
                    })
                    feedback_msgs.append(ChatMessage("assistant", content[-3000:]))
                    feedback_msgs.append(ChatMessage(
                        "user",
                        f"上一轮输出未通过 schema 校验，错误：{e}\n"
                        f"请修正上述错误重新生成。重点检查：\n"
                        f"- elements[].type 必须用合法枚举值（word-card 连字符，"
                        f"不是 word_card）\n"
                        f"- slides 用 kind 字段（不是 type）\n"
                        f"- elements 必须是数组\n"
                        f"严格模仿 system 提示中《静夜思》示例的结构。"
                    ))
                    continue
                _step(emitter, "gen_content", "生成课件 JSON", "error", last_error)
                return {
                    "yuwen_content": {},
                    "yuwen_content_path": "",
                    "yuwen_error": f"课件 JSON schema 校验失败：{last_error}",
                    "final_answer": f"课件 JSON schema 校验失败：{last_error}",
                    "nodes_visited": visited,
                }
        else:
            # for 循环自然耗尽（无 break）——理论上不可达：三种失败路径
            # 均在末次尝试直接 return。防御性兜底。
            _step(emitter, "gen_content", "生成课件 JSON", "error", last_error or "生成失败")
            return {
                "yuwen_content": {},
                "yuwen_content_path": "",
                "yuwen_error": f"课件 JSON schema 校验失败：{last_error}",
                "final_answer": f"课件 JSON schema 校验失败：{last_error}",
                "nodes_visited": visited,
            }

        # 写入临时 JSON 文件
        session = _session_name(params)
        session_dir = _OUTPUTS_DIR / "yuwen" / session
        session_dir.mkdir(parents=True, exist_ok=True)

        tmp_path = session_dir / "tmp_content.json"
        tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        n_slides = len(doc.get("slides", []))
        meta = doc.get("meta", {})
        detail = f"{n_slides} slides · {meta.get('periods', '?')} 课时"
        _step(emitter, "gen_content", "生成课件 JSON", "done", detail)

        return {
            "yuwen_content": doc,
            "yuwen_content_path": str(tmp_path),
            "nodes_visited": visited,
        }

    return gen_content
