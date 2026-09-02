"""extract_params 节点：对话追问收集参数（LLM 提取 title/grade/lesson_type）。"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_EXTRACT
from ..state import YuwenState, _emit, _load_state, _save_state, _step


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

    风格开放透传：任意非空串都收（预置档 / 自由风格如"赛博朋克"），
    gen_images 侧统一拼 prompt。数量是三档枚举，值域外丢弃。
    用户没提就不写该键——gen_images 侧缺省走"绘本 + minimal"。
    """
    from .gen_images import IMAGE_COUNTS

    prefs: dict = {}
    style = str(parsed.get("image_style") or "").strip()
    if style:
        prefs["image_style"] = style[:20]  # 防失控长串，风格词不会超过这个长度
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
        # 前端会话短码（不进 prefs——prefs 会拼进 prompt；params 落盘
        # state.json 供 _session_name 隔离，同课名新会话不被旧状态劫持）
        session_short = str(state.get("session_id") or "")[-8:]

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
            if session_short:
                ask_params["_session"] = session_short
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
        if session_short:
            params["_session"] = session_short

        # 配图偏好主动询问（用户拍板：收参数时多问一轮，缺省不再静默放行）。
        # 首轮：参数齐但没提到配图 → 落盘 params + yuwen_image_asked 标记
        # （防循环：用户回"都行"没抽到偏好也只代表问过，第二轮直接放行），
        # 追问一轮后进 END 等回复。第二轮：从本轮抽取或盘上旧值合并偏好，
        # 齐了才真正放行。
        disk = _load_state(params)
        asked = bool(disk.get("yuwen_image_asked"))
        if not (params.get("image_style") or params.get("image_count")):
            # 本轮没抽到偏好时兜底捡盘上旧值（询问轮落过盘的 image_*）
            disk_params = disk.get("yuwen_params") or {}
            for key in ("image_style", "image_count"):
                if disk_params.get(key):
                    params[key] = disk_params[key]
        if not (params.get("image_style") or params.get("image_count")) \
                and not asked:
            question = (f"参数已就绪：《{title}》· {grade}年级 · {lesson_type}。"
                        "配图有什么偏好？\n"
                        "· 风格：绘本 / 水彩 / 剪纸 / 国风 / 卡通，也可以说任意你喜欢的风格\n"
                        "· 数量：少量（每课时几张，默认）/ 全配（每页都配）/ 不配\n"
                        "例如\"水彩，每页都配\"；不想挑就回复\"默认\"。")
            _save_state(params, yuwen_params=params, yuwen_image_asked=True)
            _emit(emitter, {"type": "content", "delta": question,
                            "step_id": "extract_params",
                            "chips": ["默认（绘本+少量）", "水彩，每页都配",
                                      "不要配图"]})
            _step(emitter, "extract_params", "解析参数", "done", "询问配图偏好")
            return {
                "yuwen_params": params,
                "yuwen_params_ready": False,
                "final_answer": question,
                "nodes_visited": visited,
            }

        detail = f"《{title}》· {grade}年级 · {lesson_type}"
        if params.get("image_style"):
            detail += f" · 配图{params['image_style']}"
        if params.get("image_count"):
            detail += f" · {params['image_count']}"
        _step(emitter, "extract_params", "解析参数", "done", detail)
        # 询问轮之后盘上的 yuwen_params 是旧值（无偏好）——放行前同步，
        # 防中断续跑时从盘读到缺省偏好。
        if asked:
            _save_state(params, yuwen_params=params)
        return {
            "yuwen_params": params,
            "yuwen_params_ready": True,
            "nodes_visited": visited,
        }

    return extract_params
