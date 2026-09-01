"""gen_images 节点：AI 生图回填 image 元素的空 src（可选增强，不阻断）。

无 DASHSCOPE_API_KEY → 直接跳过。有 key → asyncio.Semaphore(3) 并发生图，
成功落盘 assets/{page_id}_{i}.png 并把 el["src"] 写成**相对 session 目录**
路径（渲染器由 renderer agent 适配解析——帧/路径契约见 graph.py docstring）。
任何失败都降级为占位（src 留空），绝不 raise。
改过 doc 后必须重写 tmp_content.json——render 读的是盘。

配图风格与数量由 yuwen_params 控制（extract_params 首轮抽取 / confirm
确认轮改配图，缺省走默认）：
- image_style：IMAGE_STYLES 五档，默认"绘本"；非法值回退默认
- image_count：minimal（默认，上限 max(2, 课时数)，按优先级截断控成本）/
  all（全部空 src 元素）/ none（跳过生图）
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from ..state import (
    YuwenState,
    _content_path,
    _session_dir,
    _step,
)

# 配图风格表：风格短语统一在 prompt 里以"，无文字，无水印"收尾
# （模型爱在图里写字，中文渲染又尤其崩坏）
IMAGE_STYLES = {
    "绘本": "儿童绘本风格，色彩明亮温暖，构图简洁",
    "水彩": "水彩插画风格，笔触柔和，清新淡雅",
    "剪纸": "中国传统剪纸风格，红白主色调，平面装饰感",
    "国风": "中国水墨国风，留白意境，淡雅山水",
    "卡通": "扁平卡通风格，简洁明快，几何构图",
}
DEFAULT_IMAGE_STYLE = "绘本"
DEFAULT_IMAGE_COUNT = "minimal"
IMAGE_COUNTS = ("minimal", "all", "none")
_COUNT_LABELS = {"minimal": "最少配图", "all": "全部配图", "none": "不配图"}


def _resolve_image_options(params: dict) -> tuple[str, str]:
    """从 params 解析配图风格/数量档位；非法值回退默认。"""
    style = str(params.get("image_style") or "").strip()
    if style not in IMAGE_STYLES:
        style = DEFAULT_IMAGE_STYLE
    count = str(params.get("image_count") or "").strip()
    if count not in IMAGE_COUNTS:
        count = DEFAULT_IMAGE_COUNT
    return style, count


def _image_prompt(el: dict, page: dict, meta: dict,
                  style: str = DEFAULT_IMAGE_STYLE) -> str:
    """元素 caption + 页标题 + 课文名拼中文生图提示词（风格段参数化）。

    课件插图要的是"符合小学课堂语境的插画"，明确排除文字/水印。
    """
    caption = str(el.get("caption") or "").strip()
    parts = [
        f"小学语文课件插画：《{meta.get('title', '')}》",
        f"页面主题：{page.get('title', '')}",
        f"画面内容：{caption}" if caption else "画面贴合课文情境",
        f"{IMAGE_STYLES.get(style, IMAGE_STYLES[DEFAULT_IMAGE_STYLE])}，"
        "无文字，无水印",
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
        style, count = _resolve_image_options(params)

        if count == "none":
            _step(emitter, "gen_images", "AI 配图", "done", "用户选择不配图")
            return {"yuwen_content": doc, "nodes_visited": visited}

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

        # minimal 档位：上限 = max(2, 课时数)，按优先级截断控成本。
        # 优先级：封面页 image > 每个 period 的第一页 > caption 非空 > 其余
        total_candidates = len(targets)
        if count == "minimal":
            try:
                periods = int(meta.get("periods")
                              or params.get("periods") or 1)
            except (TypeError, ValueError):
                periods = 1
            limit = max(2, periods)
            first_page_ids = set()
            seen_periods: set = set()
            for page in slides:
                p = page.get("period")
                if p is not None and p not in seen_periods:
                    seen_periods.add(p)
                    first_page_ids.add(str(page.get("id", "")))

            def _rank(t: tuple[dict, dict, int]) -> int:
                page, el, _i = t
                if str(page.get("kind", "")) == "cover":
                    return 0
                if str(page.get("id", "")) in first_page_ids:
                    return 1
                if str(el.get("caption") or "").strip():
                    return 2
                return 3

            targets.sort(key=_rank)  # list.sort 稳定，同级保持原文档顺序
            skipped = max(0, len(targets) - limit)
            targets = targets[:limit]
        else:
            skipped = 0

        from ..imagegen import ImageGen
        gen = ImageGen()
        if not gen.available:
            _step(emitter, "gen_images", "AI 配图", "done",
                  f"未配置 DASHSCOPE_API_KEY，跳过 AI 配图"
                  f"（{len(targets)} 张走占位）")
            return {"yuwen_content": doc, "nodes_visited": visited}

        _step(emitter, "gen_images", "AI 配图", "running",
              f"{style}风格，{_COUNT_LABELS[count]}："
              f"{len(targets)}/{total_candidates} 张候选，生成中")

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
                    data = await gen.generate(_image_prompt(el, page, meta,
                                                            style))
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

        detail = (f"{style}风格，{_COUNT_LABELS[count]}："
                  f"{len(targets)}/{total_candidates} 张候选，"
                  f"成功 {ok} 张 / 失败 {fail} 张（走占位）")
        if skipped:
            detail += f"，另有 {skipped} 张候选未生成（走占位）"
        _step(emitter, "gen_images", "AI 配图", "done", detail)
        return {"yuwen_content": doc, "nodes_visited": visited}

    return gen_images
