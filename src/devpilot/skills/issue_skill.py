"""Issue Skill：项目管理（Jira REST API）。

把项目管理系统封装为标准化 AI Skill。

设计要点：
1. Skill 抽象如何对应 MCP Server 的 tools/resources/prompts：
   - get_issue/search_issues/update_issue 即 MCP Server 暴露的三个 tools。
   - get_issue 的输出（issue 字段）可同时作为 MCP resource（按 issue key 索引）暴露。
2. 低代码理念：把 Jira 换成 GitHub Issues，只需新写一个 IssueSkill 子类覆盖方法，
   registry/Agent 完全复用——新系统接入零侵入，这就是"低代码 Skill 框架"。
3. 高危动作对接 governance 审批门：update_issue 改 issue 状态可能影响发布流程/合规，
   属高危，应在执行前调 governance.ApprovalGate.request() 走人工审批。

实现要求：用 requests（惰性导入）调 Jira REST API。无凭证时优雅降级，所有方法返回 str。
"""
from __future__ import annotations

import os
from typing import Any

from .registry import SkillSpec


class IssueSkill:
    """项目管理 Issue Skill：把 Jira 封装为标准化 AI 能力。

    把项目管理系统封装为标准化 AI Skill。
    """

    name = "issue"

    def __init__(self, base_url: str = "", token: str = "") -> None:
        """初始化 Issue Skill。

        Args:
            base_url: Jira 根 URL，如 "https://yourdomain.atlassian.net"。
                缺省从环境变量 JIRA_URL 读，缺失则方法返回降级提示。
            token: Jira API Token（Atlassian 账户设置里生成）。需配合
                JIRA_USER（邮箱）做 HTTP Basic Auth。缺省从 JIRA_TOKEN 读。
        """
        # 凭证优先环境变量：硬性要求"凭证/Token 一律从环境变量读"。
        self._base_url: str = (base_url or os.getenv("JIRA_URL", "")).rstrip("/")
        self._token: str = token or os.getenv("JIRA_TOKEN", "")
        self._user: str = os.getenv("JIRA_USER", "")

    # ------------------------------------------------------------------
    # 能力清单：对应 MCP Server tools 列表。
    # ------------------------------------------------------------------
    def specs(self) -> list[SkillSpec]:
        """返回本 Skill 暴露的能力清单（对应 MCP Server tools）。"""
        return [
            SkillSpec(
                name="get_issue",
                description="读取指定 issue 详情（只读，对应 MCP resource）",
                func=self.get_issue,
                schema={"issue_id": {"type": "string"}},
            ),
            SkillSpec(
                name="search_issues",
                description="按 JQL 条件搜索 issue 列表（只读）",
                func=self.search_issues,
                schema={"jql": {"type": "string"}},
            ),
            SkillSpec(
                name="update_issue",
                description="更新 issue 状态/评论（高危，需 Human-on-the-Loop 审批）",
                func=self.update_issue,
                schema={"issue_id": {"type": "string"}, "status": {"type": "string"}},
            ),
        ]

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _check_creds(self) -> str | None:
        """校验凭证是否齐全。返回 None 表示通过，否则返回降级提示字符串。"""
        if not self._base_url:
            return "[issue] 未配置 JIRA_URL，无法调用 Jira API。"
        if not self._token or not self._user:
            return "[issue] 未配置 JIRA_USER/JIRA_TOKEN，无法鉴权。"
        return None

    def _import_requests(self) -> Any:
        """惰性导入 requests，保证顶层 import 本模块无需装 requests。"""
        try:
            import requests  # type: ignore
        except ImportError as exc:
            raise RuntimeError("requests 未安装（pip install requests 后可用）") from exc
        return requests

    def _auth(self) -> tuple[str, str]:
        """返回 HTTP Basic Auth 元组（邮箱 + API Token）。"""
        return (self._user, self._token)

    def _rest_api_url(self, path: str) -> str:
        """拼 Jira REST API v2 的完整 URL。"""
        return f"{self._base_url}/rest/api/2/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # 能力实现
    # ------------------------------------------------------------------
    def get_issue(self, issue_id: str) -> str:
        """读取指定 issue 详情（只读）。

        调 `GET /rest/api/2/issue/{id}`。返回关键字段摘要，便于 Agent 直接读进上下文。
        """
        creds_err = self._check_creds()
        if creds_err:
            return creds_err
        if not issue_id:
            return "[issue] get_issue: empty issue_id"

        try:
            requests = self._import_requests()
            url = self._rest_api_url(f"issue/{issue_id}")
            resp = requests.get(url, auth=self._auth(), timeout=15)
            if resp.status_code != 200:
                return f"[issue] get {issue_id}: HTTP {resp.status_code} {resp.text[:200]}"
            data = resp.json()
            fields = data.get("fields", {})
            # 提取 Agent 最关心的几个字段：状态、摘要、报告人、指派人、优先级。
            status = (fields.get("status") or {}).get("name", "?")
            summary = fields.get("summary", "")
            reporter = ((fields.get("reporter") or {}).get("displayName"))
            assignee_obj = fields.get("assignee") or {}
            assignee = assignee_obj.get("displayName", "未指派")
            priority = ((fields.get("priority") or {}).get("name", "无"))
            return (
                f"[issue] {issue_id}: {summary}\n"
                f"  status={status}, priority={priority}\n"
                f"  reporter={reporter}, assignee={assignee}"
            )
        except Exception as exc:  # noqa: BLE001
            return f"[issue] get_issue error: {exc}"

    def search_issues(self, jql: str) -> str:
        """按 JQL 条件搜索 issue 列表（只读）。

        调 `POST /rest/api/2/search`，body 带 jql。返回 issue 摘要列表（截断），
        便于 Agent 在上下文里看到候选 issue。
        """
        creds_err = self._check_creds()
        if creds_err:
            return creds_err
        if not jql:
            return "[issue] search_issues: empty jql"

        try:
            requests = self._import_requests()
            url = self._rest_api_url("search")
            # 限制返回字段与条数，避免上下文爆炸。
            payload = {"jql": jql, "maxResults": 20, "fields": ["summary", "status"]}
            resp = requests.post(
                url, json=payload, auth=self._auth(),
                headers={"Content-Type": "application/json"}, timeout=15,
            )
            if resp.status_code != 200:
                return f"[issue] search: HTTP {resp.status_code} {resp.text[:200]}"
            data = resp.json()
            total = data.get("total", 0)
            issues = data.get("issues", [])
            lines = [f"[issue] search '{jql}': total={total}, returned={len(issues)}"]
            for it in issues:
                key = it.get("key", "?")
                fields = it.get("fields", {})
                summary = fields.get("summary", "")
                status = ((fields.get("status") or {}).get("name", "?"))
                lines.append(f"  {key} [{status}] {summary}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"[issue] search_issues error: {exc}"

    def update_issue(self, issue_id: str, status: str) -> str:
        """更新 issue 状态（高危写操作）。

        ===== 高危动作 =====
        此方法应在执行前调 governance.ApprovalGate.request() 触发人工审批
        （Human-on-the-Loop）：改 issue 状态可能影响发布流程/合规审计，必须人工确认。
        本实现以注释标注接入点；生产应在调度层统一拦截高危 specs 强制走审批门。
        ===================

        实现采用"状态流转"API（POST transitions）而非直接 set status：
        Jira 的状态机要求按 transition id 流转，因此先 GET transitions 找到目标状态
        对应的 transition id，再 POST transition。
        """
        creds_err = self._check_creds()
        if creds_err:
            return creds_err
        if not issue_id or not status:
            return "[issue] update_issue: issue_id and status are required"

        # ---- governance 接入点（伪代码）-----------------------------------
        # from ..governance.approval import ApprovalGate
        # ApprovalGate.request(
        #     action="issue.update_issue",
        #     summary=f"transition {issue_id} -> {status}",
        #     risk="high",  # 影响发布流程/合规
        # ).require_approved()
        # ------------------------------------------------------------------

        try:
            requests = self._import_requests()
            # 1) 先查可用 transitions，匹配目标状态名 -> transition id。
            transitions_url = self._rest_api_url(f"issue/{issue_id}/transitions")
            t_resp = requests.get(transitions_url, auth=self._auth(), timeout=15)
            if t_resp.status_code != 200:
                return f"[issue] get transitions {issue_id}: HTTP {t_resp.status_code}"
            transitions = t_resp.json().get("transitions", [])
            # 按目标状态名（to.name）匹配，大小写不敏感。
            transition_id = None
            for t in transitions:
                to_name = ((t.get("to") or {}).get("name", ""))
                if to_name.lower() == status.lower():
                    transition_id = t.get("id")
                    break
            if not transition_id:
                avail = ", ".join((t.get("to") or {}).get("name", "") for t in transitions)
                return (
                    f"[issue] {issue_id} 无法流转到状态 '{status}'，"
                    f"可用状态: {avail}"
                )
            # 2) POST transition 完成状态流转。
            payload = {"transition": {"id": transition_id}}
            up_url = self._rest_api_url(f"issue/{issue_id}/transitions")
            resp = requests.post(
                up_url, json=payload, auth=self._auth(),
                headers={"Content-Type": "application/json"}, timeout=15,
            )
            # 204 No Content 表示成功。
            if resp.status_code in (200, 204):
                return f"[issue] {issue_id} 已流转到状态 '{status}'"
            return (
                f"[issue] update {issue_id} failed: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        except Exception as exc:  # noqa: BLE001
            return f"[issue] update_issue error: {exc}"
