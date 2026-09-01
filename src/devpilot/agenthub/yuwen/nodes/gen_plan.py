"""gen_plan 节点：一次调用生成 lessonPlan + handout（教案 + 分层作业），融进 doc。

失败不阻断主链路：两块留空 dict，docx 渲染容错是 renderer 的事。
输出经 _validate_full_doc 轻量整 doc 校验（不依赖 schema.py 对空 handout
的限制），只保证结构可用；严格 schema 校验在每页生成时已做过。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_GEN_PLAN, _read_ref
from ..state import (
    YuwenState,
    _content_path,
    _parse_llm_json,
    _step,
)
from ._page import _call_llm


def _make_gen_plan_node(gateway: Any, emitter: Callable[[dict], None] | None,
                        model_kwargs: dict | None = None):
    """gen_plan 节点工厂：教案 + 学习单一次生成，写回 tmp_content.json。"""
    model_kwargs = model_kwargs or {}

    async def gen_plan(state: YuwenState) -> dict:
        _step(emitter, "gen_plan", "生成教案与学习单", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_plan" not in visited:
            visited.append("gen_plan")

        params = state.get("yuwen_params", {})
        doc = state.get("yuwen_content") or {}
        slides = doc.get("slides") or []
        if not slides:
            # gen_slides 没产出任何页——教案无从谈起，静默跳过
            _step(emitter, "gen_plan", "生成教案与学习单", "done", "无课件内容，跳过")
            return {"nodes_visited": visited}

        # 各页要点摘要（不灌完整 elements——教案看教学脉络，不需要每题细节）
        outline_lines = [
            f"p{s.get('id', '')} [{s.get('kind', '')}] {s.get('title', '')}（课时{s.get('period', 1)}）"
            for s in slides]
        system_prompt = SYSTEM_GEN_PLAN.format(
            curriculum=_read_ref("curriculum.md"),
            outline_ctx=json.dumps(doc.get("meta", {}), ensure_ascii=False)
            + "\n" + "\n".join(outline_lines),
        )
        user_prompt = (
            f"请为该课件生成教案（lessonPlan）与分层学习单（handout）。\n"
            f"课文：{doc.get('meta', {}).get('title', '')}，"
            f"共 {len(slides)} 页。直接输出 JSON。"
        )

        plan = None
        err = ""
        for attempt in range(2):  # 失败重试 1 次（带反馈）
            try:
                resp = _call_llm(
                    gateway, "chat",
                    [ChatMessage("system", system_prompt),
                     ChatMessage("user", user_prompt)],
                    model_kwargs, temperature=0.4, json_mode=True)
                parsed = _parse_llm_json(resp.content)
                if (isinstance(parsed, dict)
                        and isinstance(parsed.get("lessonPlan"), dict)
                        and isinstance(parsed.get("handout"), dict)):
                    plan = parsed
                    break
                err = "输出缺 lessonPlan 或 handout 对象"
            except Exception as exc:  # noqa: BLE001 - 整体失败降级留空
                err = str(exc)
            # 反馈重试（保留上次输出片段供模型对照修正）
            user_prompt = (f"上一轮输出有问题（{err[:200]}）。"
                           f"请重新输出含 lessonPlan 和 handout 两个对象的完整 JSON。")

        if plan is None:
            # 失败不阻断：留空 dict，report 里透传提示
            _step(emitter, "gen_plan", "生成教案与学习单", "done",
                  f"生成失败（留空，docx 可能缺教案）：{err[:60]}")
            return {"nodes_visited": visited}

        doc["lessonPlan"] = plan["lessonPlan"]
        handout = plan["handout"]
        # validate 对 handout 的硬性要求：levels 必须是数组——补齐兜底
        if not isinstance(handout.get("levels"), list):
            handout["levels"] = []
        doc["handout"] = handout

        # 重写 tmp_content.json（render 读盘）
        try:
            tmp_path = _content_path(params)
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:  # noqa: BLE001 - 落盘失败时 state 里的 doc 仍可用
            pass

        _step(emitter, "gen_plan", "生成教案与学习单", "done",
              f"{len(plan['lessonPlan'].get('teachingProcess', []))} 个教学环节")
        return {"yuwen_content": doc, "nodes_visited": visited}

    return gen_plan
