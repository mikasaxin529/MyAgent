"""主题注册表：扫描 themes/*.json 目录，主进程侧的主题唯一真相。

设计（M1 主题即插即用）：
- 新增主题 = 往 scripts/common/themes/ 放一个 JSON（含 meta 自描述段），
  不改任何 Python 代码——schema 枚举 / confirm 词表 / prompt 清单 /
  outline 帧 chips / 前端显示名全部从本注册表派生。
- 目录扫描带 mtime 缓存：每次调用先比对目录 fingerprint（文件名集合 +
  各文件 mtime），变了才重扫——运行期热加主题无需重启进程。
- 主进程（图节点）与渲染子进程（scripts/common/themes）读同一批 JSON：
  渲染端 load_theme 不感知 meta（Theme 只取 pal/font_scale/layout），
  本模块读 meta——同一文件两视角，互不侵入。

meta 段契约（theme JSON 顶层可选字段）：
  "meta": {
    "display": "青蓝",            # 中文名（前端徽章 / confirm 回复文案）
    "keywords": ["青蓝", "蓝色"],  # confirm 切换词表的匹配词（含英文名自动追加）
    "swatch": ["2E7BB5", ...],    # 前端色卡（3 色：强调/底/深）
    "tags": ["清新", "通用"]       # 语义标签（prompt 里给 LLM 的选型提示）
  }
缺 meta 的主题仍可用（渲染不依赖 meta），注册表给 display=name、
keywords=[name] 兜底——但建议都配齐。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

# themes 目录（scripts/common/themes/，渲染端 load_theme 同目录）
_THEMES_DIR = Path(__file__).resolve().parent / "scripts" / "common" / "themes"
DEFAULT_THEME = "default"

_lock = threading.Lock()
# 缓存：目录 fingerprint → [主题记录 dict]
_cache: dict = {"fingerprint": None, "themes": []}


def _dir_fingerprint() -> tuple | None:
    """目录指纹：(文件名, mtime) 集合；目录不可读返回 None。"""
    try:
        entries = sorted((p.name, p.stat().st_mtime)
                         for p in _THEMES_DIR.glob("*.json"))
    except OSError:
        return None
    return tuple(entries)


def _scan() -> list[dict]:
    """全量扫描 themes 目录，返回主题记录列表（default 永远排首位）。

    单个文件损坏/缺 meta 不炸全局：跳过该文件（default.json 除外——
    它坏了整个注册表没意义，仍返回其最小记录）。
    """
    records: list[dict] = []
    names: set[str] = set()
    for fp in sorted(_THEMES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or fp.stem).strip() or fp.stem
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        display = str(meta.get("display") or name).strip() or name
        # keywords：meta 里的词 + 主题英文名（"换成 fresh-blue" 也能命中）
        kws = [str(k).strip() for k in (meta.get("keywords") or [])
               if str(k).strip()]
        if name not in kws:
            kws.append(name)
        swatch = [str(c) for c in (meta.get("swatch") or [])][:4]
        tags = [str(t) for t in (meta.get("tags") or [])]
        records.append({"name": name, "display": display,
                        "keywords": kws, "swatch": swatch, "tags": tags})
        names.add(name)
    # default 排首位（chips / 前端列表的稳定首项）；缺失时兜底最小记录
    records.sort(key=lambda r: (r["name"] != DEFAULT_THEME, r["name"]))
    if DEFAULT_THEME not in names:
        records.insert(0, {"name": DEFAULT_THEME, "display": "默认",
                           "keywords": [DEFAULT_THEME], "swatch": [], "tags": []})
    return records


def list_themes() -> list[dict]:
    """列出全部主题记录（带 mtime 缓存，目录变了自动重扫）。

    返回 [{"name", "display", "keywords", "swatch", "tags"}]，
    default 排首位。扫描失败返回 [default 最小记录]——注册表永远非空。
    """
    with _lock:
        fp = _dir_fingerprint()
        if fp is None or fp != _cache["fingerprint"]:
            _cache["fingerprint"] = fp
            _cache["themes"] = _scan()
        return [dict(r) for r in _cache["themes"]]


def theme_names() -> list[str]:
    """主题名列表（list_themes 的名字投影，default 首位）。"""
    return [r["name"] for r in list_themes()]


def theme_display(name: str) -> str:
    """主题名 → 中文名；未知名原样返回（前端契约：未知主题显示原名）。"""
    for r in list_themes():
        if r["name"] == name:
            return r["display"]
    return name


def match_theme(text: str) -> str | None:
    """从用户消息解析主题切换意图，返回目标主题名或 None。

    匹配规则：keywords 按词长降序逐词做子串匹配（"青绿"先于"绿"，
    避免 mint-green 被 warm-green 的"绿"字误捕）。多个主题命中时取
    最长命中词所在的主题。这是 confirm 节点 _THEME_MAP 硬编码的
    注册表替代——加主题自动进词表。
    """
    s = text or ""
    if not s:
        return None
    best: tuple[int, str] | None = None  # (命中词长, 主题名)
    for r in list_themes():
        for kw in r["keywords"]:
            if kw in s and (best is None or len(kw) > best[0]):
                best = (len(kw), r["name"])
    return best[1] if best else None


def theme_chip_labels() -> list[str]:
    """outline 帧 chips 用的主题切换提示文案（"换青蓝主题"）。

    只取前 3 个非 default 主题（chips 是快捷入口不是全集——全集在
    outline 帧的 options.themes 里，前端可渲染完整选择器）。
    """
    labels = [f"换{r['display']}主题" for r in list_themes()
              if r["name"] != DEFAULT_THEME]
    return labels[:3]


def themes_hint_for_prompt() -> str:
    """给 LLM prompt 的主题清单（名字=中文名(标签)，逗号连接）。

    替代 prompts.py 里手写的 "default / fresh-blue / ..." 清单——
    gen_outline / edit_outline / META_CONTRACT 共用。
    """
    bits = []
    for r in list_themes():
        tag = f"（{'、'.join(r['tags'])}）" if r["tags"] else ""
        bits.append(f"{r['name']}={r['display']}{tag}")
    return " / ".join(bits)
