"""gen_images 节点：AI 生图回填空 src 的 image / scene-strip 元素（可选增强）。

无 DASHSCOPE_API_KEY → 直接跳过。有 key → asyncio.Semaphore(3) 并发生图，
成功落盘 assets/{page_id}_{i}.jpg（PIL 压到长边 ≤1600px，防 PPTX 体积
爆炸）并把 el["src"] 写成**相对 session 目录**路径（渲染器由 renderer
agent 适配解析——帧/路径契约见 graph.py docstring）。
任何失败都降级（src 留空），绝不 raise。收尾统一删除未生成图的空 src
image 元素——渲染器对空 src 回退"🖼 插图"灰块占位面板，用户选了不配图
或生图失败时灰块突兀。改过 doc 后必须重写 tmp_content.json——render 读的是盘。

配图风格与数量由 yuwen_params 控制（extract_params 首轮抽取 / confirm
确认轮改配图，缺省走默认）：
- image_style：IMAGE_STYLES 五档，默认"绘本"；非法值回退默认
- image_count：minimal（默认，上限 max(2, 课时数)，按优先级截断控成本）/
  all（全部空 src 元素）/ none（跳过生图）

生图目标三角色（prompt 与优先级不同）：
- 全出血背景图（image.background=true，封面页）：横构图主体居下、上方
  留呼吸空间压标题；优先级最高（观感杠杆最大）
- 四格连环画（scene-strip）：单张图田字 2×2 四格，四段 caption 依次入画，
  四格人物画风一致——单张出图天然保证风格统一还省成本
- 普通内嵌插图：既有逻辑
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

# 配图风格表：预置五档（key → 风格短语）。风格短语统一在 prompt 里
# 以"，无文字，无水印"收尾（模型爱在图里写字，中文渲染又尤其崩坏）。
# 自由风格透传：IMAGE_STYLES 之外任意非空串（如"赛博朋克""蜡笔"）不回退
# 默认，直接以 "<风格>风格" 拼进 prompt——extract_params 抽到的用户原话
# 即为风格指令，挡在外面反而违背用户意图。
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


_IMG_MAX_EDGE = 1600  # 落盘长边上限：1024px 生图原图 ~2MB/张，多张会把 PPTX 撑爆


def _compress_image(data: bytes, max_edge: int = _IMG_MAX_EDGE) -> tuple[bytes, str]:
    """生图落盘前压成 JPEG（长边 ≤max_edge，q85）。返回 (bytes, 扩展名)。

    AI 生图无透明通道，RGB 转 JPEG 目视无损且体积约降到 1/4；
    渲染器 add_picture 按文件内容嗅探格式，扩展名跟着实走。
    PIL 缺失/解码失败回退原始 bytes + png（功能优先于体积）。
    """
    try:
        import io

        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(data))
        if im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_edge:
            scale = max_edge / max(w, h)
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                           PILImage.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "jpg"
    except Exception:  # noqa: BLE001 - 压缩失败走原图
        return data, "png"


def _prune_empty_images(slides: list[dict]) -> int:
    """删除空 src 的普通 image 元素（未生图/生图失败/用户不配图），返回删除数。

    渲染器对空 src 回退"🖼 插图"灰块占位面板——不配图或失败时灰块突兀，
    删掉元素让正文自然涨满版面。豁免面：
    - background 全出血图：封面入口对无图有 on_image=False 主题底色兜底，不灰；
    - scene-strip：src 缺失时分格线与 caption 照常渲染，四段情节有教学价值；
    - 删空后无元素的页保留原样（elements 为空过不了 validate，灰块次之）。
    """
    removed = 0
    for page in slides:
        els = page.get("elements")
        if not isinstance(els, list) or len(els) < 2:
            continue
        keep = [el for el in els
                if not (isinstance(el, dict)
                        and el.get("type") == "image"
                        and not el.get("background")
                        and not str(el.get("src") or "").strip())]
        if keep and len(keep) < len(els):
            page["elements"] = keep
            removed += len(els) - len(keep)
    return removed


def _rewrite_content(doc: dict, params: dict) -> None:
    """把 doc 写回 tmp_content.json——render 子进程读盘，改动必须落进去。"""
    try:
        tmp_path = _content_path(params)
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    except Exception:  # noqa: BLE001 - 落盘失败不炸管线（渲染读旧盘仍可出片）
        pass


def _resolve_image_options(params: dict) -> tuple[str, str]:
    """从 params 解析配图风格/数量档位。

    风格：预置五档之外的非空串视为自由风格透传（不回退默认）；
    空串才走默认。数量：值域外回退默认。
    """
    style = str(params.get("image_style") or "").strip()
    if not style:
        style = DEFAULT_IMAGE_STYLE
    count = str(params.get("image_count") or "").strip()
    if count not in IMAGE_COUNTS:
        count = DEFAULT_IMAGE_COUNT
    return style, count


def _image_prompt(el: dict, page: dict, meta: dict,
                  style: str = DEFAULT_IMAGE_STYLE) -> str:
    """元素 caption + 页标题 + 课文名拼中文生图提示词（风格段参数化）。

    课件插图要的是"符合小学课堂语境的插画"，明确排除文字/水印。
    按元素角色分三种 prompt：普通内嵌图 / 全出血背景图 / 四格连环画。
    风格段：预置档查表；自由风格（表外非空串）透传为"<风格>风格"。
    """
    style_seg = IMAGE_STYLES.get(style) or f"{style}风格"

    # 全出血背景图（封面/导入页）：横构图、留出中心视觉呼吸——文字压图
    if el.get("background"):
        caption = str(el.get("caption") or "").strip()
        parts = [
            f"小学语文课件封面背景插画：《{meta.get('title', '')}》",
            f"画面内容：{caption}" if caption else "贴合课文意境的场景",
            "横幅全景构图，画面主体居下三分之一，上方留出干净的色彩空间供压标题",
            f"{style_seg}，无文字，无水印",
        ]
        return "；".join(p for p in parts if p)

    # 四格连环画（scene-strip）：单张图内 2×2 四格，四段 caption 依次入画
    scenes = el.get("scenes")
    if isinstance(scenes, list) and scenes:
        caps = [str(s.get("caption") or "").strip() for s in scenes
                if isinstance(s, dict)][:4]
        caps = [c for c in caps if c]
        seg = "；".join(f"第{i+1}格：{c}" for i, c in enumerate(caps))
        parts = [
            f"小学语文课件四格连环画：《{meta.get('title', '')}》"
            f"{page.get('title', '')}",
            f"一张图内画田字排列的四格连环画（左上→右上→左下→右下按顺序），{seg}"
            if seg else "一张图内画田字排列的四格连环画，按页面主题分四个情节",
            "四格之间用细白线分隔，人物形象与画风四格保持一致",
            f"{style_seg}，无文字，无水印",
        ]
        return "；".join(p for p in parts if p)

    # 普通内嵌插图
    caption = str(el.get("caption") or "").strip()
    parts = [
        f"小学语文课件插画：《{meta.get('title', '')}》",
        f"页面主题：{page.get('title', '')}",
        f"画面内容：{caption}" if caption else "画面贴合课文情境",
        f"{style_seg}，无文字，无水印",
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
            removed = _prune_empty_images(slides)
            if removed:
                _rewrite_content(doc, params)
            _step(emitter, "gen_images", "AI 配图", "done",
                  "用户选择不配图" + (f"（清理 {removed} 个插图占位）"
                                      if removed else ""))
            return {"yuwen_content": doc, "nodes_visited": visited}

        # 收集待配图元素：(page, element, 元素在该页的序号)。
        # image 元素看空 src；scene-strip 看顶层空 src（四格图解一页一张，
        # src 挂元素顶层，渲染层据此嵌图）
        targets: list[tuple[dict, dict, int]] = []
        for page in slides:
            for i, el in enumerate(page.get("elements") or []):
                if not isinstance(el, dict):
                    continue
                t = el.get("type")
                if (t == "image"
                        and not str(el.get("src") or "").strip()):
                    targets.append((page, el, i))
                elif (t == "scene-strip"
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
                # 全出血封面背景图是观感杠杆最大的单图，永远最优先
                if (str(page.get("kind", "")) == "cover"
                        and el.get("background")):
                    return 0
                if str(page.get("kind", "")) == "cover":
                    return 1
                # 四格图解是版式页核心，仅次于封面
                if el.get("type") == "scene-strip":
                    return 2
                if str(page.get("id", "")) in first_page_ids:
                    return 3
                if str(el.get("caption") or "").strip():
                    return 4
                return 5

            targets.sort(key=_rank)  # list.sort 稳定，同级保持原文档顺序
            skipped = max(0, len(targets) - limit)
            targets = targets[:limit]
        else:
            skipped = 0

        from ..imagegen import ImageGen
        gen = ImageGen()
        if not gen.available:
            removed = _prune_empty_images(slides)
            if removed:
                _rewrite_content(doc, params)
            detail = "未配置 DASHSCOPE_API_KEY，跳过 AI 配图"
            if removed:
                detail += f"（清理 {removed} 个插图占位）"
            _step(emitter, "gen_images", "AI 配图", "done", detail)
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
                    data, ext = _compress_image(data)
                    fname = f"{page_id}_{i}.{ext}"
                    (assets_dir / fname).write_bytes(data)
                    # src = 相对 session 目录路径（正斜杠，跨平台一致）；
                    # 渲染器据此拼绝对路径（契约同步 renderer agent）。
                    # image 与 scene-strip 都是元素顶层 src，回填逻辑一致
                    el["src"] = f"assets/{fname}"
                    ok += 1
                except Exception:  # noqa: BLE001 - 单张失败走占位
                    fail += 1

        await asyncio.gather(*(_one(p, e, i) for p, e, i in targets),
                             return_exceptions=True)

        # 未选中（minimal 截断）/ 生图失败的普通 image 元素 src 仍为空，
        # 渲染会回退灰块占位——删除后再落盘；重写 tmp_content.json，
        # render 子进程读盘，src 与删除都必须落进去。
        doc["slides"] = slides
        removed = _prune_empty_images(slides)
        _rewrite_content(doc, params)

        detail = (f"{style}风格，{_COUNT_LABELS[count]}："
                  f"{len(targets)}/{total_candidates} 张候选，"
                  f"成功 {ok} 张 / 失败 {fail} 张")
        if removed:
            detail += f"，清理 {removed} 个插图占位"
        if skipped:
            detail += f"，另有 {skipped} 张候选未生成（走占位）"
        _step(emitter, "gen_images", "AI 配图", "done", detail)
        return {"yuwen_content": doc, "nodes_visited": visited}

    return gen_images
