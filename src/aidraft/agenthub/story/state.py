"""剧本智能体共享状态与基础设施（与 yuwen/state.py 同构，命名前缀 story_）。

StoryState：langgraph 图状态（增量合并，节点只返回自己改的字段）
路径常量：_OUTPUTS_DIR 与 yuwen 同根（outputs/story/<会话名>/）
跨轮落盘：state.json（三确认点状态机的载体）
帧辅助：_emit / _step / _emit_synopsis / _emit_characters / _emit_storyboard

三确认点状态机（会话在 state.json 里的阶段字段 story_stage）：
  brief     → synopsis（梗概确认点）
  synopsis  → characters（角色形象确认点）
  characters → storyboard（分镜确认点，终确认）
  storyboard → export（导出交付，不再回头）
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from typing_extensions import TypedDict


class StoryState(TypedDict, total=False):
    """剧本智能体图状态（增量合并，只返回自己改的字段）。"""
    # 通用（与 /api/chat 兼容）
    task: str
    user_message: str
    messages: list
    final_answer: str
    nodes_visited: list
    session_id: str  # 前端会话 id（/api/chat 传入）：state.json 会话键的隔离维度
    # 剧本专用
    story_params: dict         # {title, audience, genre, duration_min, style}
    story_params_ready: bool
    story_research: dict       # 联网参考资料 {content, sources, ts}（M2 复用）
    story_synopsis: dict       # 梗概 {logline, themes, synopsis, acts, characters_brief}
    story_synopsis_confirmed: bool
    story_characters: dict     # {characters: [{id, name, role, description, lock, ref_prompt}]}
    story_characters_confirmed: bool
    story_storyboard: dict     # {scenes: [{scene_no, slug, synopsis, shots: [{id, shot_size,
                               #   camera, subject, action, dialogue, sfx, image_prompt}]}]}
    story_storyboard_confirmed: bool
    story_error: str
    story_files: list


# ---------------------------------------------------------------------------
# 路径定位
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parents[3]
_OUTPUTS_DIR = Path(os.environ.get("AIDRAFT_OUTPUTS_DIR")
                    or _PROJECT_ROOT / "outputs")


def _session_name(params: dict) -> str:
    """从参数生成会话目录名（安全文件名，与 yuwen 同规则）。

    params 带 "_session" 短码（extract_brief 从前端 session_id 取后 8 位
    写入）时拼进目录名——同片名的新会话不被旧会话 state.json 劫持；
    缺省保持纯片名（历史兼容）。
    """
    title = params.get("title", "untitled")
    safe_title = "".join(c for c in str(title) if c not in '\\/:*?"<>|')
    name = safe_title or "untitled"
    short = str(params.get("_session") or "").strip()
    if short:
        name += f"-{short}"
    return name


def _state_path(params: dict) -> Path:
    return _OUTPUTS_DIR / "story" / _session_name(params) / "state.json"


def _session_dir(params: dict) -> Path:
    return _OUTPUTS_DIR / "story" / _session_name(params)


# ---------------------------------------------------------------------------
# 跨轮状态落盘（state.json）——三确认点状态机的载体
# ---------------------------------------------------------------------------

def _save_state(params: dict, **fields) -> None:
    """读-改-写 state.json：只更新传入字段。异常吞掉不阻断主流程。"""
    try:
        path = _state_path(params)
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                data = {}
        data.update(fields)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001 - 落盘失败不阻断管线
        pass


def _load_state(params: dict) -> dict:
    """读 state.json；不存在 / 损坏返回 {}（防御式）。"""
    try:
        path = _state_path(params)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _parse_llm_json(content: str):
    """从 LLM 输出提取 JSON（与 yuwen/state 同逻辑三级降级）。"""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', str(content), re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    s = str(content)
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = s.find(open_ch), s.rfind(close_ch)
        if start >= 0 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("LLM 输出无法解析为 JSON")


# ---------------------------------------------------------------------------
# 阶段推进与会话找回
# ---------------------------------------------------------------------------

def _stage_of(disk: dict) -> str:
    """从盘上状态推断当前所处确认点阶段。

    brief（什么都没有）→ synopsis → characters → storyboard → export。
    """
    if disk.get("story_storyboard_confirmed"):
        return "export"
    if disk.get("story_characters_confirmed"):
        return "storyboard"
    if disk.get("story_synopsis_confirmed"):
        return "characters"
    if disk.get("story_synopsis"):
        return "synopsis"
    return "brief"


def _find_pending_session(min_stage: str = "synopsis",
                          session_short: str = "") -> tuple[dict, dict] | None:
    """扫盘找回最近的未完成会话（阶段 ≥ min_stage 且未到 export）。

    与 yuwen._find_pending_session 同思路：chip 点击轮 params 可能被
    extract 判空，路由层兜底进 confirm 阶段节点时从盘上找回。
    带 session 短码时只找回本会话的目录——不劫持其他前端会话的状态。
    返回 (params, disk_state)；无命中返回 None。
    """
    order = {"brief": 0, "synopsis": 1, "characters": 2, "storyboard": 3,
             "export": 4}
    try:
        root = _OUTPUTS_DIR / "story"
        if not root.exists():
            return None
        best: tuple[float, dict, dict] | None = None
        for sub in root.iterdir():
            sp = sub / "state.json"
            if not sp.exists():
                continue
            if session_short and not sub.name.endswith(f"-{session_short}"):
                continue
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            if order.get(_stage_of(data), 0) < order.get(min_stage, 1):
                continue
            if _stage_of(data) == "export":
                continue
            mtime = sp.stat().st_mtime
            if best is None or mtime > best[0]:
                params = data.get("story_params") or {}
                best = (mtime, params, data)
        if best is None:
            return None
        return best[1], best[2]
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 应答意图判定（路由兜底用，与 yuwen 同思路但词表更剧本向）
# ---------------------------------------------------------------------------

_CONFIRM_WORDS = ("确认", "可以", "没问题", "就这样", "同意", "ok", "OK",
                  "好", "行", "继续", "开始", "生成")
# 剧本三确认点的常见修改指令信号（用户想改而非确认）
_EDIT_WORDS = ("改成", "换成", "调整", "修改", "删掉", "增加", "加一个",
               "不要", "重来", "重写")


def _looks_like_stage_command(msg: str) -> bool:
    """消息是否像对当前确认点的应答（确认/修改）。"""
    s = (msg or "").strip()
    if not s or len(s) > 60:
        return False
    return (any(w in s for w in _CONFIRM_WORDS)
            or any(w in s for w in _EDIT_WORDS))


# ---------------------------------------------------------------------------
# 帧辅助
# ---------------------------------------------------------------------------

def _emit(emitter: Callable[[dict], None] | None, frame: dict) -> None:
    if emitter is None:
        return
    try:
        emitter(frame)
    except Exception:
        pass


def _step(emitter: Callable[[dict], None] | None,
          step_id: str, label: str, status: str, detail: str = "") -> None:
    _emit(emitter, {
        "type": "step",
        "id": step_id,
        "label": label,
        "status": status,
        "ts": time.time(),
        "detail": detail,
    })


def _emit_content(emitter: Callable[[dict], None] | None, delta: str,
                  step_id: str, chips: list | None = None) -> None:
    """content 帧（追问/摘要文本）。chips 可选携带快捷选项。"""
    frame: dict = {"type": "content", "delta": delta, "step_id": step_id}
    if chips:
        frame["chips"] = chips
    _emit(emitter, frame)


def _emit_synopsis(emitter: Callable[[dict], None] | None, synopsis: dict) -> None:
    """梗概帧（story 帧契约，见 graph.py docstring）。"""
    _emit(emitter, {
        "type": "story_synopsis",
        "synopsis": synopsis,
        "chips": ["确认梗概，生成角色", "主角改成…", "结局改成…"],
    })


def _emit_characters(emitter: Callable[[dict], None] | None, characters: dict) -> None:
    """角色帧。"""
    _emit(emitter, {
        "type": "story_characters",
        "characters": characters,
        "chips": ["确认角色，生成分镜", "角色形象重新生成"],
    })


def _emit_storyboard(emitter: Callable[[dict], None] | None, storyboard: dict) -> None:
    """分镜帧。n_shots 冗余带一份，前端免二次遍历。"""
    n_shots = sum(len(sc.get("shots") or []) for sc in storyboard.get("scenes") or [])
    _emit(emitter, {
        "type": "story_storyboard",
        "storyboard": storyboard,
        "n_shots": n_shots,
        "chips": ["确认分镜，开始导出", "第2场改成…"],
    })
