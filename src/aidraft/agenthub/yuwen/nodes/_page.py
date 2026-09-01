"""页级工具：单页 schema 校验 / meta 合成（gen_slides、revise、review 共用）。

schema.py 的 validate 只接受完整 doc（meta + slides 非空），不支持单页——
本模块把单页拼进最小骨架 doc 再校验，拆出校验并补完默认值后的页面返回。
scripts/ 是 renderer 的地盘，只 import 不修改。
"""
from __future__ import annotations

import copy
import inspect

# 主题枚举（与 outline prompt / confirm 主题切换词表保持一致）
THEMES = ("default", "fresh-blue", "warm-green", "mint-green")


def _call_llm(gateway, method: str, msgs, model_kwargs: dict, **kw):
    """统一 LLM 调用入口：按网关方法签名过滤 provider/model。

    真实 Gateway.stream_chat 支持 provider/model 参数，但 Gateway.chat
    不支持（走 _pick_chain 默认链）——规格假设两者都支持，与现实有出入。
    这里探测签名：方法能接就传绑定，不能接就静默降级默认链。
    MagicMock 签名探测失败时按"能接"处理（测试 mock 接受任意 kwargs）。
    """
    fn = getattr(gateway, method)
    kwargs = dict(kw)
    try:
        params = inspect.signature(fn).parameters
        accepts_any = any(p.kind == p.VAR_KEYWORD for p in params.values())
        for key in ("provider", "model"):
            if key in model_kwargs and (accepts_any or key in params):
                kwargs[key] = model_kwargs[key]
    except (TypeError, ValueError):
        kwargs.update(model_kwargs)
    return fn(msgs, **kwargs)


def _meta_from_params(params: dict) -> dict:
    """从对话参数生成 meta 基线（outline 缺字段时的兜底）。"""
    grade = params.get("grade", 1)
    try:
        grade = int(grade)
    except (TypeError, ValueError):
        grade = 1
    return {
        "title": params.get("title", "未命名课文"),
        "grade": grade,
        "lessonType": params.get("lesson_type", "精读"),
        "textbook": params.get("textbook", f"部编版{grade}年级"),
        "theme": "default",
    }


def _merge_meta(outline: dict, params: dict) -> dict:
    """outline.meta 与 params 基线合并：params 值优先（用户对话是权威来源），
    LLM 大纲补 periods/theme/objectives 等扩展字段。theme 值域外归 default。"""
    meta = dict(outline.get("meta") or {})
    base = _meta_from_params(params)
    for k in ("title", "grade", "lessonType", "textbook"):
        if base.get(k):
            meta[k] = base[k]
    meta.setdefault("theme", "default")
    if meta["theme"] not in THEMES:
        meta["theme"] = "default"
    return meta


def _validate_page_slide(slide: dict, meta: dict) -> dict:
    """单页 schema 校验：拼最小骨架 doc → normalize → validate → 拆页返回。

    校验失败抛 SchemaError / 其他异常（调用方按页失败处理）。
    返回的页面经 validate 补完默认值（id/kind/layout/period），
    元素类型经 normalize 纠偏（text→paragraph 等模型高频偏差）。
    """
    from ..scripts.common.schema import validate, normalize, SchemaError

    if not isinstance(slide, dict):
        raise SchemaError("单页必须是 JSON 对象")
    skeleton = {
        "version": "1.0",
        "meta": copy.deepcopy(meta),
        "slides": [copy.deepcopy(slide)],
    }
    doc = validate(normalize(skeleton))
    return doc["slides"][0]


def _validate_outline(outline) -> list[str]:
    """大纲轻校验：返回错误列表（空 = 通过）。

    只查结构底线（pages 非空、每页有 id/kind/title），教学合理性交 review。
    """
    errors: list[str] = []
    if not isinstance(outline, dict):
        return ["大纲顶层必须是对象"]
    pages = outline.get("pages")
    if not isinstance(pages, list) or not pages:
        return ["pages 缺失或为空"]
    for i, p in enumerate(pages):
        if not isinstance(p, dict):
            errors.append(f"pages[{i}] 不是对象")
            continue
        for k in ("id", "kind", "title"):
            if not str(p.get(k, "")).strip():
                errors.append(f"pages[{i}] 缺 {k}")
    return errors
