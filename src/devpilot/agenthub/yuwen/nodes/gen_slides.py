"""gen_slides 节点：逐页生成课件内容（每页一次 LLM 调用 + 页级反思重试）。

由 gen_content 重写而来。核心变化：一次性全量生成 → 单页粒度——
- 单次输出短，截断（finish_reason=length）与格式漂移概率大幅下降；
- 页级重试只重掷失败页，不报废整课；
- token 帧带 step_id="gen_slides"，前端能看到逐页进度。
并发是后续优化项（gateway 内部已有主备重试，先保正确再谈速度）。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_GEN_SLIDE, _read_ref
from ..state import (
    YuwenState,
    _content_path,
    _emit,
    _load_state,
    _parse_llm_json,
    _step,
)
from ._page import _call_llm, _merge_meta, _validate_page_slide

_MAX_ATTEMPTS = 3  # 每页最多 3 次尝试（1 次正常 + 2 次反思重试）


def _outline_ctx(pages: list[dict], idx: int) -> str:
    """当前页在完整大纲中的位置上下文（前后页标题 + 本页要点）。"""
    lines = []
    for i, p in enumerate(pages):
        mark = "▶" if i == idx else " "
        prev_t = pages[i - 1].get("title", "") if i > 0 else ""
        next_t = pages[i + 1].get("title", "") if i + 1 < len(pages) else ""
        line = (f"{mark} {p.get('id', f's{i+1}')} [{p.get('kind', '')}] "
                f"{p.get('title', '')}（课时{p.get('period', 1)}）"
                f"—— {p.get('points', '')}")
        if i == idx:
            line += f"\n   （上一页：{prev_t or '无'}｜下一页：{next_t or '无'}）"
        lines.append(line)
    return "\n".join(lines)


def _make_gen_slides_node(gateway: Any, emitter: Callable[[dict], None] | None,
                          model_kwargs: dict | None = None):
    """gen_slides 节点工厂：按已确认大纲逐页生成，合成完整课程 doc 落盘。"""
    model_kwargs = model_kwargs or {}

    async def gen_slides(state: YuwenState) -> dict:
        _step(emitter, "gen_slides", "逐页生成", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_slides" not in visited:
            visited.append("gen_slides")

        params = state.get("yuwen_params", {})
        outline = state.get("yuwen_outline") or {}
        if not outline.get("pages"):
            # 路由可从盘直跳 gen_slides（盘上已确认），本轮 state 里没有
            # outline——查盘兜底（跨轮状态机的节点也要幂等）。
            outline = _load_state(params).get("yuwen_outline") or {}
        pages = outline.get("pages") or []
        meta = _merge_meta(outline, params)
        if not pages:
            _step(emitter, "gen_slides", "逐页生成", "error", "大纲为空")
            return {"yuwen_error": "大纲为空，无法逐页生成",
                    "nodes_visited": visited}

        # 参考文本一次读入，system prompt 每页只换 outline_ctx 段
        ref_stages = _read_ref("stages.md")
        ref_schema = _read_ref("schema.md")
        ref_example = _read_ref("examples/jingyesi.json")

        slides: list[dict] = []
        failed_pages: list[str] = []
        for i, entry in enumerate(pages):
            page_id = str(entry.get("id", f"s{i+1}"))
            title = str(entry.get("title", ""))
            _step(emitter, "gen_slides", "逐页生成", "running",
                  f"{i+1}/{len(pages)} 页：{title}")

            system_prompt = SYSTEM_GEN_SLIDE.format(
                stages=ref_stages,
                schema=ref_schema,
                example=ref_example,
                outline_ctx=_outline_ctx(pages, i),
            )
            user_prompt = (
                f"课文：《{meta.get('title', '')}》 {meta.get('grade', '')}年级 "
                f"{meta.get('lessonType', '')}\n"
                f"本页条目：{json.dumps(entry, ensure_ascii=False)}\n"
                f"只生成 ▶ 标记的本页，输出单页 JSON。"
            )

            slide = await _gen_one_page(
                gateway, emitter, system_prompt, user_prompt, meta, model_kwargs,
                expect_id=page_id)
            if slide is None:
                failed_pages.append(f"{page_id}（{title}）")
                continue
            # 单页校验只保证该页合法，不保证 id 是大纲约定值——逐页生成
            # 时模型偶发自增/复用 id（如把 s02 写回 s01），合成 doc 后
            # validate 会报"slide id 重复"。以大纲 id 为准强制回写。
            slide["id"] = page_id
            slides.append(slide)

        if not slides:
            err = f"全部 {len(pages)} 页生成失败"
            _step(emitter, "gen_slides", "逐页生成", "error", err)
            return {"yuwen_content": {}, "yuwen_content_path": "",
                    "yuwen_error": err, "final_answer": err,
                    "nodes_visited": visited}

        # 合成完整 doc：lessonPlan/handout 占位由 gen_plan 填充。
        # handout 必须是 {"levels": []} 而非 {}——render_all.py 落盘的
        # validate 对 handout.levels 硬性要求数组，{} 直接退出码 2。
        doc = {
            "version": "1.0",
            "meta": meta,
            "slides": slides,
            "lessonPlan": {},
            "handout": {"levels": []},
        }
        tmp_path = _content_path(params)
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")

        detail = f"{len(slides)}/{len(pages)} 页完成"
        if failed_pages:
            detail += f"，失败：{', '.join(failed_pages)}"
        _step(emitter, "gen_slides", "逐页生成", "done", detail)

        out: dict = {
            "yuwen_content": doc,
            "yuwen_content_path": str(tmp_path),
            "nodes_visited": visited,
        }
        if failed_pages:
            out["yuwen_error"] = f"第 {', '.join(failed_pages)} 页生成失败"
        return out

    return gen_slides


async def _gen_one_page(gateway, emitter, system_prompt: str, user_prompt: str,
                        meta: dict, model_kwargs: dict,
                        expect_id: str = "") -> dict | None:
    """单页生成 + 页级反思重试。彻底失败返回 None（调用方记录并继续）。

    expect_id：大纲约定的本页 id。模型返回的 id 与之不符时直接纠正
    （不浪费一轮重试——id 漂移不是内容错误，回写即可）。
    """
    feedback_msgs: list[ChatMessage] = []
    last_content = ""
    for attempt in range(_MAX_ATTEMPTS):
        content = ""
        finish_reason = ""
        try:
            llm_msgs = [ChatMessage("system", system_prompt),
                        ChatMessage("user", user_prompt)] + feedback_msgs
            stream = _call_llm(gateway, "stream_chat", llm_msgs, model_kwargs,
                               temperature=0.3 + 0.2 * attempt)
            async for chunk in stream:
                if chunk.delta:
                    content += chunk.delta
                    _emit(emitter, {"type": "token", "delta": chunk.delta,
                                    "step_id": "gen_slides"})
                if chunk.reasoning:
                    _emit(emitter, {"type": "thinking", "node": "gen_slides",
                                    "phase": "reasoning", "delta": chunk.reasoning})
                if chunk.done and chunk.finish_reason:
                    finish_reason = chunk.finish_reason
        except Exception:  # noqa: BLE001 - 网络/网关失败：进入重试反馈
            if attempt == _MAX_ATTEMPTS - 1:
                return None
            feedback_msgs = [ChatMessage("assistant", content[-2000:]),
                             ChatMessage("user", "上一轮调用失败，请重新输出本页完整 JSON。")]
            continue

        # 解析单页 JSON
        try:
            slide = _parse_llm_json(content)
        except ValueError:
            slide = None
        if isinstance(slide, dict) and "slides" in slide and "elements" not in slide:
            # 模型误输出整 doc：拆第一页用（常见偏差抢救，比报废重生成划算）
            inner = slide.get("slides")
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                slide = inner[0]

        if isinstance(slide, dict):
            try:
                page = _validate_page_slide(slide, meta)
                if expect_id and page.get("id") != expect_id:
                    page["id"] = expect_id
                return page
            except Exception as exc:  # noqa: BLE001 - SchemaError 等，带反馈重试
                err = str(exc)
        else:
            err = "输出不是合法 JSON 对象"
        last_content = content

        if attempt < _MAX_ATTEMPTS - 1:
            truncated = finish_reason == "length"
            hint = ("上一轮输出被长度上限截断。请压缩本页文字（每页 2-4 个元素），"
                    "确保输出完整 JSON。" if truncated else
                    f"上一轮输出未通过校验（{err[:200]}）。重点检查："
                    "elements[].type 用连字符枚举名（word-card 不是 word_card）、"
                    "elements 必须是数组、只输出单页 JSON 对象。")
            _emit(emitter, {"type": "token",
                            "delta": f"\n[本页重试：{err[:60]}]\n",
                            "step_id": "gen_slides"})
            feedback_msgs = [ChatMessage("assistant", content[-3000:]),
                             ChatMessage("user", hint)]

    _ = last_content  # 末次失败：调用方统计失败页，内容不再回灌
    return None
