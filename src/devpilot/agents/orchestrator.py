"""Orchestrator：Multi-Agent 编排器。

从 0 到 1 搭建 AI Agent 架构落地。
W5 实现：任务分解、角色委派、共享黑板、结果聚合。

==============================================================================
架构选型要点：
==============================================================================
1. 拓扑：Orchestrator-Worker（也称 Hub-and-Spoke / 编排者-执行者）。
   - 选它的理由：研发流程天然是"流水线式"的（规划→改码→评审→测试），
     角色之间是"接力"关系而非"对等协商"。一个中央编排器统一拆任务、
     派活、收结果，比让 Agent 之间两两直接通信更可控、更易审计、更易扩展。
   - 对比"去中心化 / P2P Agent 网络"：那种拓扑适合开放式探索（如多 Agent
     辩论），但研发场景需要确定性与可追溯，Orchestrator 是更贴合的选择。
   - 对比"单 Agent 全干"：把一个超大 prompt 塞给一个 Agent，专业度不足且
     上下文易爆；拆成聚焦角色（Coder/Reviewer/Tester）每个 prompt 短、
     职责单一，输出质量更高，也便于独立评估每个环节。

2. 通信机制：Blackboard（黑板模式）。
   - 各 Worker 不直接调用彼此，而是读写同一个 Blackboard（共享数据对象）。
   - 好处：Worker 之间"零耦合"——Coder 不需要知道 Reviewer 的存在，只把
     code_diff 写进黑板；Reviewer 从黑板读 code_diff 即可。新增一个角色
     （如 SecAgent）只需"读 X 写 Y"，不必改动既有 Worker。
   - 与"消息总线"的区别：黑板是"共享状态 + 中央编排顺序驱动"，比纯消息
     总线更简单、顺序更可控；适合流程固定、步骤数有限的研发闭环。

3. 韧性（Resilience）：每个 Worker 用 try/except 包住，单个失败不中断整体。
   - 研发流水线中"评审失败"不该让"测试"也跑不了；失败写审计、留黑板
     空字段，下游可据此判断"上一步未完成"并降级处理。
   - 体现"宁可降级不可崩溃"的工程原则，也方便事后从审计复盘失败点。

4. 治理（Governance）接入：高危动作（如 commit_and_pr）走 ApprovalGate。
   - 编排器在 Reviewer 标记高危后，调 approval.request 触发 Human-on-the-Loop
     审批，把"是否放行不可逆动作"的决定权交还给人。
   - 全程 audit.record，形成"决策→审批→审计→回流"可追溯闭环。
==============================================================================
"""
from __future__ import annotations

# 标准库 dataclass 用于 Blackboard：用 dataclass 而非普通 class，是为了
# 免费拿到 __init__/__repr__，且字段默认值用 field(default_factory=...) 安全
# 处理可变默认值（避免经典可变默认值共享 bug）。
from dataclasses import dataclass, field


@dataclass
class Blackboard:
    """Multi-Agent 共享黑板：各角色读写中间产物，解耦 Agent 间直接通信。

    为什么用 Blackboard 而非"两两传参 / 消息队列"：
        - 两两传参会让 Worker 互相感知对方接口，耦合度高，新增角色要改多处。
        - 消息队列对"顺序敏感、步骤有限"的研发闭环过重，且异步语义增加调试难度。
        - Blackboard 是"共享状态 + 编排器顺序驱动"：简单、顺序可控、字段显式，
          谁写哪个字段一目了然，事后从一块黑板即可复盘整次运行。

    字段语义（对应研发闭环各阶段产物）：
        task:        原始需求文本，全流程只读基线。
        plan:        Planner 拆出的步骤列表（3-5 步），驱动后续 Worker。
        code_diff:   Coder 产出的代码改动（demo 阶段为描述/伪 diff，真改码走 repo Skill）。
        review:      Reviewer 的评审结论与风险点。
        test_result: Tester 拉取/解析的测试报告摘要。
        artifacts:   自由扩展字段，放各阶段附带产物（如 trace_id、PR url 等），
                     用 dict 避免 Blackboard 字段爆炸，给未来留扩展口子。
    """

    task: str = ""
    plan: list[str] = field(default_factory=list)
    code_diff: str = ""
    review: str = ""
    test_result: str = ""
    artifacts: dict = field(default_factory=dict)


class Orchestrator:
    """编排器：协调 Planner→Coder→Reviewer→Tester 流转。

    职责定位：
        - 编排器只负责"拆任务、派活、收结果、过审批、记审计"，本身不做具体研发动作。
        - 具体动作由各 Worker（Coder/Reviewer/Tester）承担，编排器不重复实现其逻辑，
          体现"单一职责"与"对扩展开放、对修改关闭"。
        - gateway 是与具体 LLM 的唯一边界；registry 是 Skill 生态的统一发现入口；
          audit/approval 是治理闭环的两条腿。编排器把它们串成一条可控流水线。
    """

    def __init__(self, gateway, registry, audit=None, approval=None, emitter=None) -> None:
        """初始化编排器。

        参数：
            gateway:  模型网关（devpilot.gateway.Gateway），提供 chat_text/chat。
            registry: Skill 注册中心（devpilot.skills.registry.SkillRegistry），
                      用来发现 repo/cicd 等可复用 Skill。
            audit:    审计日志（devpilot.governance.audit.AuditLog），可选注入。
                      默认 new 一个，保证即使调用方不传也有审计可写——
                      体现"审计是底线能力，不该因忘传而缺失"。
            approval: 审批门（devpilot.governance.approval.ApprovalGate），可选注入。
                      默认 new 一个。注入式设计便于测试用 mock 替换，
                      也便于将来换不同审批策略（如接外部 OA 审批系统）。
            emitter:  可选的 Blackboard 快照回调，签名 Callable[[dict], None]。
                      每个 worker 完成后调用一次，把当前黑板状态投递给订阅方
                      （如 Web API 层实时推给前端）。默认 None——CLI 路径完全无影响。

        为什么用"构造注入"而非"全局单例"：
            - 注入显式声明依赖，可测试性强（测试时传 mock 即可隔离副作用）。
            - 单例会让审计/审批变成全局隐式状态，难以并行跑多个任务、难测试。
        """
        self.gateway = gateway
        self.registry = registry  # SkillRegistry
        self.emitter = emitter  # 可选：Blackboard 快照回调，None 则不推送。

        # 审计：可选注入，默认自建。惰性 import 避免顶层依赖循环/重复实例化。
        # AuditLog 与 ApprovalGate 都是 governance 下的标准件，自建零成本。
        if audit is None:
            # 局部 import：保证本模块顶层 import 不触发 governance 子模块加载，
            # 也避免循环 import 风险（governance 将来若反向引用 agents 会出问题）。
            from ..governance.audit import AuditLog
            audit = AuditLog()
        self.audit = audit

        # 审批门：同理可选注入、默认自建。
        if approval is None:
            from ..governance.approval import ApprovalGate
            approval = ApprovalGate()
        self.approval = approval

    def _emit_bb(self, bb: "Blackboard") -> None:
        """把当前 Blackboard 快照推给 emitter（若有）。

        Web 层用它实现"Run 页 Blackboard 面板实时填充"：每完成一个 worker，
        前端就能看到 plan/code_diff/review/test_result 的最新状态，而非等全部跑完。
        emitter 为 None（CLI 路径）时直接返回，零开销。
        """
        if self.emitter is None:
            return
        try:
            from dataclasses import asdict
            self.emitter(asdict(bb))
        except Exception:  # noqa: BLE001 - 推送失败不该影响主流程
            return

    # ------------------------------------------------------------------
    # 内部：trace_id 生成。一次 run 一个 id，串起本次所有审计记录。
    # ------------------------------------------------------------------
    @staticmethod
    def _new_trace_id() -> str:
        """生成轻量追踪 ID，把一次 run 的所有审计事件串成一条链路。

        为什么不用 uuid4：uuid 太长且对 demo 无意义；这里用时间戳+随机后缀
        既能保证唯一性，又便于人眼从 id 看出大致时间，调试友好。
        生产可替换为 uuid4 或分布式 trace 体系（如 OpenTelemetry trace_id）。
        """
        import time
        import random
        # 时间戳秒级 + 随机 4 位，冲突概率在单机 demo 场景可忽略。
        return f"run-{int(time.time())}-{random.randint(1000, 9999)}"

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, task: str) -> Blackboard:
        """跑通一个研发任务的全流程：规划→改码→评审→测试→（高危则审批）。

        流程（每步都写审计，每步都包 try/except 保证韧性）：
            1. 初始化 Blackboard(task)，并记一条 run_started 审计。
            2. Planner 分解：用 gateway.chat_text 让 LLM 拆 3-5 步，写 bb.plan。
               （简化版：直接用网关对话拆解，不强制依赖 runtime.Planner，
                 降低耦合；注释里说明可替换为 ReActRuntime 以获得工具调用能力。）
            3. CoderAgent.act()：让 LLM 据 plan 产出"要做的代码改动描述/diff"，
               写 bb.code_diff。真实改码应调 registry 的 repo Skill。
            4. ReviewerAgent.act()：让 LLM 评审 bb.code_diff，标风险，写 bb.review；
               若 review 标记高危，调 approval.request("commit_and_pr", ...)。
            5. TesterAgent.act()：调 registry 的 cicd Skill 触发流水线；
               若 Skill 抛 NotImplementedError/未配置则捕获并记录降级提示，
               写 bb.test_result。
            6. 全程 audit.record 每步；任一 Worker 失败不中断整体，失败写审计。
            7. 返回 Blackboard，调用方可从黑板读取所有阶段产物与状态。

        参数：
            task: 用户的研发需求文本，如"给 FastAPI 加一个 /health 健康检查接口"。
        返回：
            填充完各阶段产物的 Blackboard。
        """
        # --- 步骤 1：初始化黑板 + trace_id + 起始审计 ---
        # 黑板是这次运行的唯一共享状态，所有 Worker 共享同一个实例。
        bb = Blackboard(task=task)
        # 一次 run 一个 trace_id，串起本次所有审计事件，事后可按 id 切片回放。
        trace_id = self._new_trace_id()
        bb.artifacts["trace_id"] = trace_id
        self.audit.record(
            event="agent_step",
            actor="orchestrator",
            detail={"step": "run_started", "task": task},
            trace_id=trace_id,
        )

        # --- 步骤 2：Planner 分解任务 ---
        # 用 try/except 包住：即使 LLM 拆解失败，也要让流程继续（下游可据空 plan 降级）。
        try:
            bb.plan = self._plan(task, trace_id)
        except Exception as exc:  # noqa: BLE001 - 编排层兜底，保证不崩
            # 失败写审计：记下失败点与异常，便于事后复盘。
            self.audit.record(
                event="agent_step",
                actor="orchestrator",
                detail={"step": "plan_failed", "error": repr(exc)},
                trace_id=trace_id,
            )
            # plan 留空，下游 Worker 会据此降级（如 Coder 直接对 task 兜底）。
            bb.plan = []
        self._emit_bb(bb)

        # --- 步骤 3：Coder 改码 ---
        # 这里通过 Worker 实例注入 gateway/registry/bb/approval/audit，
        # Worker 自己决定怎么用这些依赖，编排器不越俎代庖。
        try:
            self._run_worker("coder", trace_id, bb)
        except Exception as exc:  # noqa: BLE001
            # Coder 失败不中断：Reviewer 可能仍能对"空 diff"做评审（如提示需求不清晰）。
            self.audit.record(
                event="agent_step",
                actor="orchestrator",
                detail={"step": "coder_failed", "error": repr(exc)},
                trace_id=trace_id,
            )
        self._emit_bb(bb)

        # --- 步骤 4：Reviewer 评审 ---
        # 同理包住：评审失败不让测试跑不了。
        try:
            self._run_worker("reviewer", trace_id, bb)
        except Exception as exc:  # noqa: BLE001
            self.audit.record(
                event="agent_step",
                actor="orchestrator",
                detail={"step": "reviewer_failed", "error": repr(exc)},
                trace_id=trace_id,
            )
        self._emit_bb(bb)

        # --- 步骤 5：Tester 测试 ---
        try:
            self._run_worker("tester", trace_id, bb)
        except Exception as exc:  # noqa: BLE001
            self.audit.record(
                event="agent_step",
                actor="orchestrator",
                detail={"step": "tester_failed", "error": repr(exc)},
                trace_id=trace_id,
            )
        self._emit_bb(bb)

        # --- 步骤 6：收尾审计 + 返回黑板 ---
        self.audit.record(
            event="agent_step",
            actor="orchestrator",
            detail={
                "step": "run_finished",
                # 用 bool 摘要各阶段是否产出，便于一眼看出哪步空了。
                "has_plan": bool(bb.plan),
                "has_code_diff": bool(bb.code_diff),
                "has_review": bool(bb.review),
                "has_test_result": bool(bb.test_result),
            },
            trace_id=trace_id,
        )
        return bb

    # ------------------------------------------------------------------
    # 子步骤实现
    # ------------------------------------------------------------------
    def _plan(self, task: str, trace_id: str) -> list[str]:
        """Planner：让 LLM 把需求拆成 3-5 步可执行步骤。

        简化版策略（为什么这么简化）：
            - 直接用 gateway.chat_text 让 LLM 输出"行列表"的步骤，而非引入
              runtime.Planner/ReActRuntime。理由：Planner 阶段只需"思考分解"，
              不需要工具调用；用 ReAct 会带来 tool 注册/解析的额外复杂度与耦合，
              对 demo 是过度设计。注释标明可替换：若要 Planner 也能调工具
              （如先 search_code 摸清现状再拆步骤），可改为构造
              ReActRuntime(gateway).run(task, tools=registry.all_specs())。
            - 用 system prompt 约束输出格式为"每步一行、3-5 步、中文"，便于
              直接 splitlines 得到 list；不强制 JSON 以降低对模型指令遵循的
              依赖（小模型 JSON 模式不一定稳）。

        参数：
            task: 原始需求。
            trace_id: 追踪 ID，写审计用。
        返回：
            步骤列表；LLM 调用失败时抛异常，由上层 run() 的 try/except 捕获。
        """
        # system prompt：给 LLM 一个"研发流程拆解专家"的角色与硬性输出格式约束。
        # 明确"3-5 步""每步一行""不要编号/前缀"是为了让 splitlines 后即可用。
        system = (
            "你是一位资深研发流程拆解专家。把用户需求拆成 3-5 个可执行步骤，"
            "覆盖：定位代码→改代码→自测→提 PR。"
            "输出要求：每步一行纯文本，不要编号、不要 markdown 前缀、不要多余空行。"
        )
        prompt = f"需求：{task}\n请拆解为 3-5 个可执行步骤。"

        # 记一条 LLM 调用审计：把 prompt 摘要写进去（不写全文防止敏感信息泄露/超长）。
        self.audit.record(
            event="llm_call",
            actor="planner",
            detail={"prompt_preview": prompt[:200]},
            trace_id=trace_id,
        )

        # 真正调模型：chat_text 返回纯字符串。temperature 用默认即可（拆解要稳不要发散）。
        raw = self.gateway.chat_text(prompt, system=system)

        # 解析输出：按行切，去掉首尾空白与空行，得到步骤列表。
        # 用列表推导 + strip 过滤空行，比 for 循环简洁且不改变语义。
        steps = [line.strip() for line in raw.splitlines() if line.strip()]

        # 防御：若模型输出不符合格式（如输出空或一大段），保留原样兜底，
        # 至少让下游有"非空 plan"可读，避免 plan 为空导致 Coder 完全无输入。
        if not steps:
            steps = [raw.strip()] if raw.strip() else [task]

        # 记拆解结果审计：步骤数与首步预览，便于看板统计"平均拆几步"。
        self.audit.record(
            event="agent_step",
            actor="planner",
            detail={"step": "plan_done", "num_steps": len(steps),
                    "first_step_preview": steps[0][:100]},
            trace_id=trace_id,
        )
        return steps

    def _run_worker(self, role: str, trace_id: str, bb: Blackboard) -> None:
        """构造并运行指定角色的 Worker。

        为什么把 Worker 构造也放编排器里：
            - Worker 是"无状态工具"（每次 run 都新建），其生命周期与一次 run 绑定，
              由编排器统一构造可保证所有 Worker 共享同一份 gateway/registry/bb/
              approval/audit，避免不一致。
            - 用 role 字符串映射到类，避免 if-else 链散落在 run() 里，保持主流程线性可读。

        参数：
            role: 角色名 "coder" | "reviewer" | "tester"。
            trace_id: 追踪 ID。
            bb: 共享黑板。
        """
        # 局部 import Worker 类：避免顶层循环 import（orchestrator 与 agents 互相引用）。
        from .agents import CoderAgent, ReviewerAgent, TesterAgent

        # role -> 类 的映射表。用 dict 而非 if-elif，新增角色只改一处（开闭原则）。
        workers = {
            "coder": CoderAgent,
            "reviewer": ReviewerAgent,
            "tester": TesterAgent,
        }
        cls = workers.get(role)
        if cls is None:
            # 防御：未知角色不应让流程崩，记审计后直接返回。
            self.audit.record(
                event="agent_step",
                actor="orchestrator",
                detail={"step": "unknown_role", "role": role},
                trace_id=trace_id,
            )
            return

        # 把 trace_id 挂到黑板 artifacts，供 Worker 在 act() 内记审计时取用，
        # 避免 trace_id 在 Worker 里还要层层透传。
        bb.artifacts["trace_id"] = trace_id

        # 统一构造：所有 Worker 共享同一份依赖，approval/audit 注入保证治理一致。
        worker = cls(self.gateway, self.registry, bb, approval=self.approval, audit=self.audit)

        # 记一条 Worker 启动审计，便于看板"每个角色何时开工"。
        self.audit.record(
            event="agent_step",
            actor=role,
            detail={"step": "act_start"},
            trace_id=trace_id,
        )

        # 真正执行 Worker 动作。Worker 内部也会记审计，这里不再重复记成功细节。
        worker.act()

        # 记 Worker 完成审计。
        self.audit.record(
            event="agent_step",
            actor=role,
            detail={"step": "act_done"},
            trace_id=trace_id,
        )
