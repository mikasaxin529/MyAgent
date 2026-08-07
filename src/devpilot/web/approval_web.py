"""Web 审批门：非阻塞式人工审批，对接前端。

设计要点（为什么需要单独的 Web 审批门）：
- CLI 的 ApprovalGate.request() 用 input() 阻塞终端等待人输入。但 Web 场景下，
  Orchestrator 跑在后台线程，"人"在浏览器另一端——不能用 input()。
- 解法：WebApprovalGate 把"审批请求"投递到一个队列（req_q），自身阻塞在另一个
  队列（res_q）上等"裁决结果"；Web API 层从 req_q 取出请求推给前端，前端用户
  决策后，API 层把结果放回 res_q，gate 解阻塞继续。这是跨线程的"请求-应答"握手。
- 继承 ApprovalGate 的 requires_approval 与高危动作集合，只覆写 request()。
"""
from __future__ import annotations

import queue as _queue

from ..governance.approval import ApprovalGate, ApprovalRequest, ApprovalResult


class WebApprovalGate(ApprovalGate):
    """面向前端的审批门：把阻塞等待从 stdin 转为"队列握手"。

    使用方式（由 api.py 的 WS 处理器编排）：
        req_q, res_q = queue.Queue(), queue.Queue()
        gate = WebApprovalGate(req_q, res_q)
        # 把 gate 注入 Orchestrator，orchestrator.run 跑在后台线程
        # WS 处理器在主事件循环里轮询 req_q：取到请求→推前端→等用户回填→放 res_q
    """

    def __init__(self, req_q: "_queue.Queue", res_q: "_queue.Queue") -> None:
        self._req_q = req_q
        self._res_q = res_q

    def request(self, action: str, args: dict, reason: str = "") -> ApprovalResult:
        """覆写：不读 stdin，改为队列握手。

        流程：
            1. 构造 ApprovalRequest，投递 req_q（Web 层会推给前端弹审批框）。
            2. 阻塞 res_q.get() 等前端回填的 ApprovalResult。
            3. 返回结果，Orchestrator 据此决定放行/终止/用改写参数。

        非交互安全：本类不再做 tty 判定——是否需要审批由 requires_approval 决定，
        而既然走到了 request()，说明动作高危，必须等前端明确裁决。为防前端断连
        导致 Orchestrator 永久挂起，res_q.get 用一个较长 timeout 循环等待，
        超时则默认拒绝（安全合规底线：宁可不办事也不在无人裁决时执行不可逆动作）。
        """
        req = ApprovalRequest(action=action, args=args, reason=reason)
        # 投递请求给 Web 层（非阻塞 put，队列无界，立即返回）。
        self._req_q.put(req)

        # 阻塞等待裁决，但带超时循环：避免前端断连后 Worker 线程永久卡死。
        # 超时时间取 5 分钟：足够人审批，又不会让流程无限挂起。
        while True:
            try:
                result = self._res_q.get(timeout=300)
            except _queue.Empty:
                # 超时无人裁决：默认拒绝，安全合规。
                return ApprovalResult(
                    approved=False,
                    comment="Web 审批超时（5 分钟无人裁决），默认拒绝以保安全",
                )
            # 拿到结果即返回（result 已是 ApprovalResult）。
            return result
