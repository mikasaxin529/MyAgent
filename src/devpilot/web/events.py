"""可观测审计：把 AuditLog.record() 的事件实时广播给订阅者。

设计要点（为什么这么写）：
- Orchestrator/Worker 在每一步本就调用 self.audit.record(...)，这天然是一条
  "事件流"。只要让 AuditLog 在 record 时同步通知订阅者，就能把执行过程实时
  推给 WebSocket 前端——无需改动 Orchestrator/agents 任何一行业务逻辑。
- 用"订阅者列表"而非单一回调：支持同时多个订阅者（如 WS 推流 + Langfuse 上报）。
- 订阅者可能在另一个线程被调用（Orchestrator 跑在后台线程），因此订阅者自身
  必须线程安全（推荐用 queue.Queue 把事件投递回事件循环所在线程）。
"""
from __future__ import annotations

from typing import Callable

from ..governance.audit import AuditLog, AuditEntry


class ObservableAuditLog(AuditLog):
    """可观测审计日志：record 时既写入父类存储，又广播给所有订阅者。

    零侵入原理：父类 AuditLog.record 已实现写入与字段填充；本类只在其基础上
    追加"广播"一步。Orchestrator 拿到的是 ObservableAuditLog 实例，但它只调
    audit.record(...)——对编排器而言与普通 AuditLog 无差异，完全透明。
    """

    def __init__(self) -> None:
        super().__init__()
        # 订阅者：Callable[[AuditEntry], None]。list 便于动态增删。
        self._subscribers: list[Callable[[AuditEntry], None]] = []

    def subscribe(self, subscriber: Callable[[AuditEntry], None]) -> None:
        """注册一个订阅者，每条 audit 事件都会回调它。"""
        self._subscribers.append(subscriber)

    def record(self, event: str, actor: str, detail: dict, trace_id: str = "") -> None:
        """覆写 record：先走父类写入，再广播。"""
        # 先写父类：保证审计存储与普通 AuditLog 完全一致（不影响 to_summary/export）。
        super().record(event, actor, detail, trace_id=trace_id)
        # 取刚写入的那条（父类 record 已 append 到 entries 末尾）。
        entry = self._entries[-1]
        # 广播给所有订阅者。捕获异常避免某个订阅者出错影响主流程。
        for sub in self._subscribers:
            try:
                sub(entry)
            except Exception:  # noqa: BLE001 - 订阅者失败不该影响审计主链路
                # 静默吞掉：订阅者是"旁路"，不能拖垮核心流程。
                continue
