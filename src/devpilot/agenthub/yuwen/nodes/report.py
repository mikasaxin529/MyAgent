"""report 节点：汇总文件清单，推 files / done 终帧。"""
from __future__ import annotations

from typing import Callable

from ..state import YuwenState, _emit, _session_name, _step


def _visual_note(state: YuwenState) -> str:
    """视觉审查摘要片段（追加进 final_answer）：分数 + 问题按严重度计数。

    未跑（available=false）注明原因——用户至少知道审查为什么缺席；
    state 里完全没有 visual 字段（旧会话 / 渲染前就 END）返回空串。
    """
    visual = state.get("yuwen_visual") or {}
    if not visual:
        return ""
    if not visual.get("available"):
        return f"视觉审查未启用（{visual.get('reason', '未知原因')}）"
    sev_count: dict[str, int] = {}
    for issue in visual.get("issues") or []:
        sev = str(issue.get("severity") or "low")
        sev_count[sev] = sev_count.get(sev, 0) + 1
    n_issues = sum(sev_count.values())
    note = f"视觉审查 {visual.get('score', 0)} 分，{n_issues} 个问题"
    if n_issues:
        order = {"high": "高", "medium": "中", "low": "低"}
        parts = [f"{order[s]} {sev_count[s]}"
                 for s in ("high", "medium", "low") if sev_count.get(s)]
        note += f"（{' / '.join(parts)}）"
    return note


def _make_report_node(emitter: Callable[[dict], None] | None):
    """report 节点工厂：汇总交付结果，推 files/done 帧。"""

    async def report(state: YuwenState) -> dict:
        _step(emitter, "report", "交付报告", "running")

        visited = list(state.get("nodes_visited") or [])
        if "report" not in visited:
            visited.append("report")

        files = state.get("yuwen_files", [])
        # 优先展示真实失败原因：gen_content 失败(yuwen_error)优先于 render 失败
        error = state.get("yuwen_error", "") or state.get("yuwen_render_error", "")
        params = state.get("yuwen_params", {})
        session = _session_name(params)

        # 部分成功：有文件但渲染器有失败 → files 帧照发，report 注明哪个渲染器失败
        if error and files:
            _emit(emitter, {"type": "files", "files": files})
            file_names = " / ".join(f["name"] for f in files)
            answer = (f"课件部分生成成功（{len(files)} 个文件：{file_names}），"
                      f"但 {error}{_visual_note(state)}")
            detail = f"部分成功：{error}"
            _step(emitter, "report", "交付报告", "done", detail)
            _emit(emitter, {
                "type": "done",
                "answer": answer,
                "meta": {
                    "nodes_visited": visited,
                    "audit_total": len(visited),
                },
            })
            return {
                "final_answer": answer,
                "nodes_visited": visited,
                "messages": state.get("messages", []) + [
                    {"role": "assistant", "content": answer},
                ],
            }

        if error:
            answer = f"课件生成失败：{error}"
            _emit(emitter, {"type": "content", "delta": answer, "step_id": "report"})
            # error 终态：保证每个 running 都有终态（前端时间线避免永久"运行中"）
            _step(emitter, "report", "交付报告", "error", error)
            return {
                "final_answer": answer,
                "nodes_visited": visited,
            }

        # 推 files 帧
        if files:
            _emit(emitter, {"type": "files", "files": files})

        # 构建 summary（带 AI 审查评分摘要，让用户知道质量水位）
        review = state.get("yuwen_review") or {}
        scores = review.get("scores") or {}
        review_note = ""
        if scores:
            label = {"structure": "结构", "pedagogy": "教学",
                     "content": "内容", "stage_fit": "适配"}
            parts = [f"{label.get(k, k)}{v}" for k, v in scores.items()
                     if isinstance(v, (int, float))]
            if parts:
                review_note = f"（审查评分：{'/'.join(parts)}）"
        visual_note = _visual_note(state)
        v_ok = "，" + visual_note if visual_note else ""   # 逗号分隔续句
        v_full = ("。" + visual_note) if visual_note else ""  # 句号后另起
        n_files = len(files)
        if n_files > 0:
            file_names = " / ".join(f["name"] for f in files)
            answer = (f"课件已生成，共 {n_files} 个文件：{file_names}"
                      f"{review_note}{v_ok}")
            detail = f"已写入 outputs/yuwen/{session}/"
        else:
            answer = (f"课件内容已生成，但渲染未产出文件。"
                      f"{review_note}{v_full}")
            detail = "无产出文件"

        _step(emitter, "report", "交付报告", "done", detail)

        # 推 done 帧
        _emit(emitter, {
            "type": "done",
            "answer": answer,
            "meta": {
                "nodes_visited": visited,
                "audit_total": len(visited),
            },
        })

        return {
            "final_answer": answer,
            "nodes_visited": visited,
            "messages": state.get("messages", []) + [
                {"role": "assistant", "content": answer},
            ],
        }

    return report
