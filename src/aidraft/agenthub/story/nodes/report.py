"""report 节点：汇总 story 交付结果，推 files / done 终帧。"""
from __future__ import annotations

from typing import Callable

from ..state import StoryState, _emit, _session_name, _step


def _make_report_node(emitter: Callable[[dict], None] | None):
    """report 节点工厂。"""

    async def report(state: StoryState) -> dict:
        _step(emitter, "report", "交付报告", "running")

        visited = list(state.get("nodes_visited") or [])
        if "report" not in visited:
            visited.append("report")

        files = state.get("story_files", [])
        error = state.get("story_error", "")
        params = state.get("story_params", {})
        session = _session_name(params)

        if error and files:
            _emit(emitter, {"type": "files", "files": files})
            file_names = " / ".join(f["name"] for f in files)
            answer = (f"交付物部分导出成功（{len(files)} 个文件：{file_names}），"
                      f"但 {error}")
            _step(emitter, "report", "交付报告", "done", f"部分成功：{error[:60]}")
            _emit(emitter, {
                "type": "done",
                "answer": answer,
                "meta": {"nodes_visited": visited, "audit_total": len(visited)},
            })
            return {"final_answer": answer, "nodes_visited": visited}

        if error:
            answer = f"剧本项目生成失败：{error}"
            _emit(emitter, {"type": "content", "delta": answer, "step_id": "report"})
            _step(emitter, "report", "交付报告", "error", error[:80])
            return {"final_answer": answer, "nodes_visited": visited}

        if files:
            _emit(emitter, {"type": "files", "files": files})
            file_names = " / ".join(f["name"] for f in files)
            answer = f"剧本项目已导出，共 {len(files)} 个文件：{file_names}"
            detail = f"已写入 outputs/story/{session}/"
        else:
            answer = "剧本内容已生成，但导出未产出文件。"
            detail = "无产出文件"

        _step(emitter, "report", "交付报告", "done", detail)
        _emit(emitter, {
            "type": "done",
            "answer": answer,
            "meta": {"nodes_visited": visited, "audit_total": len(visited)},
        })
        return {"final_answer": answer, "nodes_visited": visited}

    return report
