"""visual_review 节点：渲染后视觉审查（PPTX → 逐页图 → 百炼 qwen-vl 评分）。

流程：session 目录找最新 .pptx → soffice 无头转 PDF（UserInstallation
临时 profile 隔离并发锁）→ PyMuPDF 逐页导出 PNG → 每页发 VLM 做 8 项
视觉检查 → 汇总发 visual 帧（契约见 graph.py docstring）。

降级原则（与 gen_images 同一思路）：审查是提质不是阻断——无 key / 无
soffice / 转换失败 / 整体异常一律发 available=false 降级帧注明原因，
绝不 raise、绝不阻断 report。单页失败只跳该页。

抽查策略沿用 review.py：页数超 DASHSCOPE_VL_MAX_PAGES（默认 14——线上一课
约 13-14 页，默认即全查；缺陷页漏检的直接教训）时取前 2 页 +
random.Random(42) 随机补齐，确定性可重现。审查结果落盘 state.json
（yuwen_visual）供跨轮诊断与复盘。
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from ..state import (
    YuwenState,
    _emit,
    _parse_llm_json,
    _save_state,
    _session_dir,
    _session_name,
    _step,
)

# 视觉检查项（用户原始需求，照抄进 prompt，不要改写）
_CHECKS = """1. 标题是否清晰
2. 是否存在文字遮挡
3. 图片是否被裁切
4. 图片与文字是否重叠
5. 字体是否过小
6. 页面是否留白过多
7. 配色是否适合小学课堂
8. 图片是否与课文主题匹配"""

# issue.type 枚举（帧契约，前端据此图标/文案）
_ISSUE_TYPES = {"title_unclear", "text_overlap", "image_cropped",
                "image_text_overlap", "text_too_small", "too_much_whitespace",
                "color_mismatch", "theme_mismatch", "other"}
_SEVERITIES = {"low", "medium", "high"}


def _review_prompt(page_id: str, page_title: str, meta: dict) -> str:
    """单页视觉审查提示词：课文/页上下文 + 8 项检查 + 严格 JSON 输出格式。"""
    return f"""你是小学语文课件视觉质检专家。下面是课件《{meta.get('title', '')}》{meta.get('stage', '')}的一页渲染截图，页 id：{page_id}，页面标题：{page_title}。
请对这一页做视觉检查，逐项判断：
{_CHECKS}

只输出严格 JSON（不要代码块、不要解释）：
{{"score": 0-100 整数, "issues": [{{"type": "...", "severity": "low|medium|high", "bbox": [x1, y1, x2, y2], "suggestion": "..."}}]}}
规则：
1. type 只能取：title_unclear / text_overlap / image_cropped / image_text_overlap / text_too_small / too_much_whitespace / color_mismatch / theme_mismatch / other
2. bbox 是问题区域坐标，按页面宽高归一化到 0-1000
3. suggestion 用中文给可执行的修改建议
4. 无问题输出 "issues": []"""


def _sample_page_indices(total: int, limit: int) -> list[int]:
    """页数超限时的确定性抽查索引（0 基）：前 2 页 + 固定种子随机补齐。"""
    if total <= limit:
        return list(range(total))
    idx = list(range(min(2, total, limit)))
    pool = [i for i in range(2, total)]
    need = max(0, min(limit, total) - len(idx))
    if need and pool:
        idx += sorted(random.Random(42).sample(pool, min(need, len(pool))))
    return sorted(dict.fromkeys(idx))


def _max_pages() -> int:
    """抽查上限（env DASHSCOPE_VL_MAX_PAGES，默认 14；非法值回退 14）。

    原默认 8：线上一课 13-14 页的课件只查 8 页，用户实测的缺陷页
    （初读节奏诗句不可读）恰好没被抽中——上调到覆盖整课的规模。
    """
    try:
        return max(1, int(os.environ.get("DASHSCOPE_VL_MAX_PAGES", "").strip() or 14))
    except ValueError:
        return 14


def _normalize_page_result(parsed, page_id: str):
    """单页 VLM JSON → (score, issues)；结构不合法返回 None（该页跳过）。

    type/severity 越界值归入 other/low，bbox 校验为 4 个数（0-1000
    取整），缺省给 []——前端按固定键消费，宁可空值不可缺键。
    """
    if not isinstance(parsed, dict):
        return None
    raw_score = parsed.get("score")
    if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
        return None
    score = max(0, min(100, int(round(raw_score))))
    issues: list[dict] = []
    for it in parsed.get("issues") or []:
        if not isinstance(it, dict):
            continue
        typ = str(it.get("type") or "other")
        if typ not in _ISSUE_TYPES:
            typ = "other"
        sev = str(it.get("severity") or "low")
        if sev not in _SEVERITIES:
            sev = "low"
        bbox = it.get("bbox")
        norm_bbox: list[int] = []
        if (isinstance(bbox, (list, tuple)) and len(bbox) == 4
                and all(isinstance(v, (int, float))
                        and not isinstance(v, bool) for v in bbox)):
            norm_bbox = [max(0, min(1000, int(round(v)))) for v in bbox]
        issues.append({
            "page_id": page_id,
            "type": typ,
            "severity": sev,
            "bbox": norm_bbox,
            "suggestion": str(it.get("suggestion") or ""),
        })
    return score, issues


def _convert_pptx_to_pdf(soffice: str, pptx: Path, review_dir: Path) -> Path | None:
    """soffice 无头转 PDF；成功返回 PDF 路径，失败返回 None。

    -env:UserInstallation 指向独立临时 profile——soffice 并发共享默认
    profile 会锁冲突（本机/容器里其他 office 进程也吃这把锁）。
    Windows 路径 as_uri() 得 file:///C:/... 三斜杠正斜杠，soffice 认这个写法。
    """
    profile_dir = Path(tempfile.mkdtemp(prefix="dp_soffice_"))
    # Windows 下 as_uri() 得 file:///C:/... （三斜杠 + 正斜杠），soffice 认这个写法
    profile_uri = profile_dir.as_uri()
    try:
        # 不判退出码——soffice 偶有转换成功仍非零，以 PDF 是否落盘为准
        subprocess.run(
            [soffice, f"-env:UserInstallation={profile_uri}",
             "--headless", "--convert-to", "pdf",
             "--outdir", str(review_dir), str(pptx)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    pdf_path = review_dir / f"{pptx.stem}.pdf"
    return pdf_path if pdf_path.exists() else None


def _make_visual_review_node(emitter: Callable[[dict], None] | None):
    """visual_review 节点工厂：渲染产物逐页视觉审查，发 visual 帧。"""

    def _degraded(visited: list, reason: str, params: dict) -> dict:
        """统一降级出口：available=false + 原因，step done（不阻断管线）。

        降级结果同样落盘——复盘时"为什么没修"要看得到"审查为什么没跑"。
        """
        frame = {"available": False, "reason": reason,
                 "score": 0, "pages": [], "issues": []}
        _step(emitter, "visual_review", "视觉审查", "done", reason)
        _emit(emitter, {"type": "visual", "visual": frame})
        _save_state(params, yuwen_visual=frame)
        return {"yuwen_visual": frame, "nodes_visited": visited}

    async def visual_review(state: YuwenState) -> dict:
        _step(emitter, "visual_review", "视觉审查", "running")

        visited = list(state.get("nodes_visited") or [])
        if "visual_review" not in visited:
            visited.append("visual_review")

        # 回滚后重渲染透传：doc 已回到修复前版本，再转 PDF 调 VLM 只会
        # 得到与 prev_visual 相同的结果——直接透传修复前审查帧，省一次
        # 完整审查的钱（回滚链路见 nodes/visual_fix.py）。
        if state.get("yuwen_visual_fix_rollback"):
            prev = state.get("yuwen_visual_fix_prev_visual") or {}
            if prev.get("available"):
                _step(emitter, "visual_review", "视觉审查", "done",
                      "修复已回滚，透传修复前审查结果（未重跑 VLM）")
                _emit(emitter, {"type": "visual", "visual": prev})
                return {"yuwen_visual": prev, "nodes_visited": visited}

        try:
            return await _review(state, visited)
        except Exception as exc:  # noqa: BLE001 - 任何整体异常降级，绝不阻断
            return _degraded(visited, f"视觉审查异常：{str(exc)[:80]}",
                             state.get("yuwen_params") or {})

    async def _review(state: YuwenState, visited: list) -> dict:
        doc = state.get("yuwen_content") or {}
        params = state.get("yuwen_params", {})

        from ..vlm import VLMReview
        vlm = VLMReview()
        if not vlm.available:
            return _degraded(visited, "未配置 DASHSCOPE_API_KEY", params)

        session = _session_name(params)
        session_dir = _session_dir(params)
        pptx_files = sorted(session_dir.glob("*.pptx"),
                            key=lambda p: p.stat().st_mtime)
        if not pptx_files:
            return _degraded(visited, "未找到 PPTX 渲染产物", params)
        pptx = pptx_files[-1]  # 重渲染会同名覆盖，取 mtime 最新

        soffice = shutil.which("soffice")
        if not soffice:
            return _degraded(visited, "未安装 LibreOffice，无法转页面图", params)

        review_dir = session_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = _convert_pptx_to_pdf(soffice, pptx, review_dir)
        if pdf_path is None:
            return _degraded(visited, "LibreOffice 转换 PDF 失败", params)

        try:
            import pymupdf as fitz
        except ImportError:  # 旧版包名
            import fitz  # type: ignore[no-redef]
        slides = doc.get("slides") or []
        meta = doc.get("meta") or {}
        with fitz.open(pdf_path) as pdf:
            total = pdf.page_count
            if total == 0:
                return _degraded(visited, "PDF 无页面", params)
            shots: dict[int, tuple[str, bytes]] = {}  # 页序 → (png 路径名, bytes)
            for i in range(total):
                pix = pdf[i].get_pixmap(dpi=110)
                name = f"s{i:02d}.png"
                data = pix.tobytes("png")
                (review_dir / name).write_bytes(data)
                shots[i] = (name, data)

        indices = _sample_page_indices(total, _max_pages())
        pages_out: list[dict] = []
        issues_out: list[dict] = []
        skipped = 0
        for i in indices:
            name, data = shots[i]
            slide = slides[i] if i < len(slides) else {}
            page_id = str(slide.get("id") or f"s{i + 1:02d}")
            try:
                raw = await vlm.review_page(
                    data, _review_prompt(page_id, str(slide.get("title") or ""),
                                         meta))
                norm = _normalize_page_result(_parse_llm_json(raw), page_id)
            except Exception:  # noqa: BLE001 - 单页失败只跳该页
                norm = None
            if norm is None:
                skipped += 1
                continue
            score, issues = norm
            pages_out.append({
                "page_id": page_id,
                "score": score,
                "image": f"/files/yuwen/{session}/review/{name}",
            })
            issues_out.extend(issues)

        if not pages_out:
            return _degraded(visited, "所有页面视觉审查均失败", params)

        score = round(sum(p["score"] for p in pages_out) / len(pages_out))
        payload = {"available": True, "reason": "", "score": score,
                   "pages": pages_out, "issues": issues_out}
        _step(emitter, "visual_review", "视觉审查", "done",
              f"{score} 分，{len(issues_out)} 个问题，检查 {len(pages_out)} 页"
              + (f"，跳过 {skipped} 页" if skipped else ""))
        _emit(emitter, {"type": "visual", "visual": payload})
        _save_state(params, yuwen_visual=payload)
        return {"yuwen_visual": payload, "nodes_visited": visited}

    return visual_review
