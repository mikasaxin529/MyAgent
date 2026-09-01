"""gen_images 节点：AI 生图回填 image 元素的空 src（可选增强，不阻断）。

无 IMAGE_API_KEY → 直接跳过。有 key → asyncio.Semaphore(3) 并发生图，
成功落盘 assets/{page_id}_{i}.png 并把 el["src"] 写成**相对 session 目录**
路径（渲染器由 renderer agent 适配解析——帧/路径契约见 graph.py docstring）。
任何失败都降级为占位（src 留空），绝不 raise。
改过 doc 后必须重写 tmp_content.json——render 读的是盘。
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Callable

from ..state import (
    YuwenState,
    _content_path,
    _session_dir,
    _step,
)


def _image_prompt(el: dict, page: dict, meta: dict) -> str:
    """元素 caption + 页标题 + 课文名拼中文生图提示词。

    课件插图要的是"符合小学课堂语境的插画"，明确排除文字/水印——
    模型爱在图里写字，中文渲染又尤其崩坏。
    """
    caption = str(el.get("caption") or "").strip()
    parts = [
        f"小学语文课件插画：《{meta.get('title', '')}》",
        f"页面主题：{page.get('title', '')}",
        f"画面内容：{caption}" if caption else "画面贴合课文情境",
        "儿童绘本风格，色彩明亮，构图简洁，无文字，无水印",
    ]
    return "；".join(p for p in parts if p)


def _make_gen_images_node(emitter: Callable[[dict], None] | None):
    """gen_images 节点工厂：扫描空 src 的 image 元素，并发 AI 生图回填。"""

    async def gen_images(state: YuwenState) -> dict:
        visited = list(state.get("nodes_visited") or [])
        if "gen_images" not in visited:
            visited.append("gen_images")

        doc = state.get("yuwen_content") or {}
        slides = doc.get("slides") or []
        meta = doc.get("meta") or {}
        params = state.get("yuwen_params", {})

        # 收集待配图元素：(page, element, 元素在该页的序号)
        targets: list[tuple[dict, dict, int]] = []
        for page in slides:
            for i, el in enumerate(page.get("elements") or []):
                if (isinstance(el, dict) and el.get("type") == "image"
                        and not str(el.get("src") or "").strip()):
                    targets.append((page, el, i))

        if not targets:
            _step(emitter, "gen_images", "AI 配图", "done", "无需配图，跳过")
            return {"yuwen_content": doc, "nodes_visited": visited}

        from ..imagegen import ImageGen
        gen = ImageGen()
        if not gen.available:
            _step(emitter, "gen_images", "AI 配图", "done",
                  f"未配置 IMAGE_API_KEY，跳过 AI 配图（{len(targets)} 张走占位）")
            return {"yuwen_content": doc, "nodes_visited": visited}

        _step(emitter, "gen_images", "AI 配图", "running",
              f"{len(targets)} 张配图生成中")

        session_dir = _session_dir(params)
        assets_dir = session_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        sem = asyncio.Semaphore(3)  # 并发上限：生图网关普遍限流，3 是经验值

        ok = 0
        fail = 0

        async def _one(page: dict, el: dict, i: int) -> None:
            nonlocal ok, fail
            page_id = str(page.get("id", "s00"))
            async with sem:
                try:
                    data = await gen.generate(_image_prompt(el, page, meta))
                    fname = f"{page_id}_{i}.png"
                    (assets_dir / fname).write_bytes(data)
                    # src = 相对 session 目录路径（正斜杠，跨平台一致）；
                    # 渲染器据此拼绝对路径（契约同步 renderer agent）
                    el["src"] = f"assets/{fname}"
                    ok += 1
                except Exception:  # noqa: BLE001 - 单张失败走占位
                    fail += 1

        await asyncio.gather(*(_one(p, e, i) for p, e, i in targets),
                             return_exceptions=True)

        # 重写 tmp_content.json——render 子进程读盘，src 必须落进去
        doc["slides"] = slides
        try:
            tmp_path = _content_path(params)
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

        _step(emitter, "gen_images", "AI 配图", "done",
              f"成功 {ok} 张 / 失败 {fail} 张（走占位）")
        return {"yuwen_content": doc, "nodes_visited": visited}

    return gen_images
