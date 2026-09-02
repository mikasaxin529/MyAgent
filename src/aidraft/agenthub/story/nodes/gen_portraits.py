"""gen_portraits 节点：角色确认后生成标准立绘（第二确认点的增强产物）。

每个角色一张：ref_prompt → 百炼生图 → assets/characters/<id>.png。
立绘是分镜阶段 image_prompt 的视觉参照（双层锚点的"锚"）——
生成失败不阻断分镜（分镜靠文字锚点 description 也能写），只降级提示。
无 DASHSCOPE_API_KEY 直接跳过。
"""
from __future__ import annotations

import asyncio
from typing import Callable

from ..state import StoryState, _session_dir, _step


async def _generate_portraits(characters: list[dict], session_dir, emitter,
                              step_id: str) -> tuple[int, int]:
    """并发生成立绘，返回 (成功数, 失败数)。图片路径写回角色卡 portrait 字段。"""
    from ...yuwen.imagegen import ImageGen

    gen = ImageGen()
    if not gen.available:
        _step(emitter, step_id, "角色立绘", "done",
              "未配置 DASHSCOPE_API_KEY，跳过立绘生成")
        return 0, 0

    assets_dir = session_dir / "assets" / "characters"
    assets_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)
    ok = fail = 0

    async def _one(c: dict) -> None:
        nonlocal ok, fail
        async with sem:
            try:
                data = await gen.generate(str(c.get("ref_prompt") or ""))
                fp = assets_dir / f"{c.get('id', 'c')}.png"
                fp.write_bytes(data)
                # 相对盘路径（HTML 预览用）+ web 路径（前端角色卡 <img> 用）
                c["portrait"] = f"assets/characters/{fp.name}"
                c["portrait_url"] = (
                    f"/files/story/{session_dir.name}/assets/characters/{fp.name}")
                ok += 1
            except Exception:  # noqa: BLE001 - 单张失败走无图
                fail += 1

    await asyncio.gather(*(_one(c) for c in characters if isinstance(c, dict)),
                         return_exceptions=True)
    return ok, fail


def _make_gen_portraits_node(emitter: Callable[[dict], None] | None):
    """gen_portraits 节点工厂：角色卡确认后逐个生成立绘。"""

    async def gen_portraits(state: StoryState) -> dict:
        _step(emitter, "gen_portraits", "角色立绘", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_portraits" not in visited:
            visited.append("gen_portraits")

        params = state.get("story_params", {})
        characters = dict(state.get("story_characters") or {})
        chars = [c for c in (characters.get("characters") or [])
                 if isinstance(c, dict) and not c.get("portrait")]
        if not chars:
            _step(emitter, "gen_portraits", "角色立绘", "done", "无需生成")
            return {"story_characters": characters, "nodes_visited": visited}

        session_dir = _session_dir(params)
        ok, fail = await _generate_portraits(chars, session_dir, emitter,
                                             "gen_portraits")

        # 立绘路径已写进角色卡 dicts（_one 里就地改），落盘
        from ..state import _save_state
        _save_state(params, story_characters=characters)

        detail = f"立绘生成：成功 {ok} 张" + (f" / 失败 {fail} 张（走文字锚点）" if fail else "")
        _step(emitter, "gen_portraits", "角色立绘", "done", detail)
        return {"story_characters": characters, "nodes_visited": visited}

    return gen_portraits
