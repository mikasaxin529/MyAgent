"""revise 节点：按 review 问题清单单页重生成（≤2 轮，审查是提质不是阻断）。

只重做 review.issues 点名的页；校验失败保留原页（改坏不如不改）。
改完重写 tmp_content.json 并重发 review 帧（更新后评分由下一轮 review 给）。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_REVISE, _read_ref
from ..state import (
    YuwenState,
    _content_path,
    _emit,
    _parse_llm_json,
    _step,
)
from ._page import _call_llm, _validate_page_slide


def _make_revise_node(gateway: Any, emitter: Callable[[dict], None] | None,
                      model_kwargs: dict | None = None):
    """revise 节点工厂：逐问题页带反馈重生成，回写 doc。"""
    model_kwargs = model_kwargs or {}

    async def revise(state: YuwenState) -> dict:
        visited = list(state.get("nodes_visited") or [])
        if "revise" not in visited:
            visited.append("revise")

        rounds = int(state.get("yuwen_revise_rounds") or 0) + 1
        doc = state.get("yuwen_content") or {}
        review = state.get("yuwen_review") or {}
        issues = review.get("issues") or []
        slides = doc.get("slides") or []
        meta = doc.get("meta") or {}
        params = state.get("yuwen_params", {})

        by_id = {str(s.get("id", "")): s for s in slides}
        _step(emitter, "revise", "按审查意见修订", "running",
              f"第 {rounds} 轮，{len(issues)} 页待修订")

        fixed = 0
        for issue in issues:
            page_id = str(issue.get("page_id", ""))
            problems = issue.get("problems") or []
            slide = by_id.get(page_id)
            if slide is None or not problems:
                continue  # 幽灵页 ID（LLM 幻觉）：跳过
            system_prompt = SYSTEM_REVISE.format(
                schema=_read_ref("schema.md"),
                slide_json=json.dumps(slide, ensure_ascii=False, indent=1),
                problems="\n".join(f"- {p}" for p in problems))
            revised: dict | None = None
            try:
                resp = _call_llm(
                    gateway, "chat",
                    [ChatMessage("system", system_prompt),
                     ChatMessage("user", "请输出修复后的完整单页 JSON。")],
                    model_kwargs, temperature=0.4, json_mode=True)
                parsed = _parse_llm_json(resp.content)
                revised = _validate_page_slide(parsed, meta)
            except Exception:  # noqa: BLE001 - 修订失败保留原页
                revised = None
            if revised is not None:
                # 保持原 id（validate 补的默认 id 不能覆盖 doc 内对应关系）
                revised["id"] = slide.get("id", revised.get("id"))
                idx = slides.index(slide)
                slides[idx] = revised
                by_id[page_id] = revised
                fixed += 1
                _emit(emitter, {"type": "content",
                                "delta": f"已按审查意见修订 {page_id}\n",
                                "step_id": "revise"})

        # 重写 tmp_content.json（下游 gen_images/render 读盘）
        doc["slides"] = slides
        try:
            tmp_path = _content_path(params)
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

        _step(emitter, "revise", "按审查意见修订", "done",
              f"第 {rounds} 轮：{fixed}/{len(issues)} 页修订成功")
        return {"yuwen_content": doc, "yuwen_revise_rounds": rounds,
                "nodes_visited": visited}

    return revise
