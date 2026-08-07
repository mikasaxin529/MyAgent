"""手写的 ReAct 运行时（W3 完整实现）。

为什么手写而不依赖 OpenAI function-calling / LangChain Agent：
    这是项目最核心的设计决策——手写实现能完全掌控运行时行为，不依赖框架黑盒。
    手写一个基于文本解析的 ReAct 循环，能逐行讲清"提示怎么组、输出怎么解析、
    工具怎么调度、何时终止"，真正掌握内部机制而非只会调用封装好的 API。

    三个具体理由：
    1. 完全掌控内部机制——Thought/Action/Observation 三段式是 ReAct 论文的本体，
       手写一遍等于把论文实现了一遍，可逐行审查与讲解。
    2. 模型无关——不依赖 OpenAI function-calling（DeepSeek/Qwen 的兼容模式对
       tools 参数支持参差不齐），纯文本输入输出在任何模型上都稳定可跑。
    3. 可审计可评估——每一步的 Thought/Action/Observation 都落到 AgentState.steps，
       后续可做轨迹回放、错误归因、评估打分，这是生产级 Agent 的硬需求。

ReAct 循环（Reasoning + Acting）：
    思考(Thought) → 行动(Action/ToolCall) → 观察(Observation) → 思考 → ... → FinalAnswer

    每一轮：
    - Thought：模型对当前状态推理，决定下一步该干什么
    - Action：模型选择一个工具（或直接给出 FinalAnswer）
    - Action Input：工具的入参（JSON）或最终答案文本
    - Observation：工具执行结果，拼回 prompt 进入下一轮
"""
from __future__ import annotations

import json
import re

from ..gateway import ChatMessage
from .types import AgentState, AgentStep, Tool, ToolCall


# ---- ReAct 输出格式约定 ----
# 模型必须严格按以下三行格式输出，我们用正则解析。这套格式来自 ReAct 论文
# （Yao et al., 2022）的 prompt 模板，是最经典的文本协议。
#
#   Thought: <对当前状态的推理过程>
#   Action: <工具名，或 FinalAnswer 表示直接给最终答案>
#   Action Input: <工具入参 JSON，或最终答案文本>
#
# 用文本协议而非 JSON 整体包裹，是因为：
# - 模型在"自由推理 + 结构化指令"混合场景下，纯文本比纯 JSON 更稳；
# - Thought 是自然语言，塞进 JSON 字符串要转义，模型容易写错引号；
# - 三行前缀格式可读性强，便于人工审计和单元测试断言。
_THOUGHT_RE = re.compile(r"Thought:\s*(.*?)\s*(?=Action:|$)", re.DOTALL)
_ACTION_RE = re.compile(r"Action:\s*(.*?)\s*(?=Action Input:|$)", re.DOTALL)
_ACTION_INPUT_RE = re.compile(r"Action Input:\s*(.*)$", re.DOTALL)

# 最终答案的哨兵值：当 Action 取这个值时，循环终止，Action Input 即为最终答案。
# 用一个常量而非魔法字符串，便于全局统一修改与测试断言。
FINAL_ANSWER = "FinalAnswer"


class ReActRuntime:
    """从零手写的 ReAct Agent 运行时。

    职责（推理决策 + 工具调用）：
    - 接收一个自然语言 task 和一组 Tool
    - 反复"推理 → 选工具 → 执行 → 观察"，直到模型给出 FinalAnswer 或达到步数上限
    - 全过程记录到 AgentState.steps，供审计 / 评估 / 回放

    设计要点：
    - 只依赖 gateway 抽象，不感知具体模型（DeepSeek/Qwen/OpenAI 都行）
    - 工具调度失败不崩，把异常包成 Observation: <error> 喂回模型，让它自己纠错
    - 步数硬上限 max_steps，防止模型陷入"工具调用死循环"烧 token
    """

    def __init__(self, gateway) -> None:  # gateway: devpilot.gateway.Gateway
        """构造函数。

        Args:
            gateway: 模型网关。只通过 gateway.chat() 与 LLM 交互，
                这样运行时不绑定任何具体模型，模型可热切换（保证稳定性）。
        """
        self.gateway = gateway

    def run(self, task: str, tools: list[Tool] | None = None) -> tuple[str, AgentState]:
        """ReAct 主循环：推理决策 + 工具调用。

        算法（见模块 docstring 的设计决策）：
            1. _build_prompt 组装本轮 ReAct 提示
            2. gateway.chat 拿到 LLM 输出
            3. _parse_action 解析出 Thought/Action/Action Input
            4. 若 Action == FinalAnswer：标记完成，Action Input 即最终答案
            5. 否则 _execute_tool 执行工具，结果作为 Observation 拼回 state
            6. 循环 1-5，直到 finished 或 step_count >= max_steps

        终止条件（双保险）：
            - 正常终止：模型输出 Action: FinalAnswer
            - 兜底终止：步数达 max_steps 仍未完成，返回"未能在步数内完成"提示
              （这是生产 Agent 必须有的保护，防止模型陷死循环烧光 token）

        Args:
            task: 用户原始任务（自然语言）
            tools: 可用工具列表。为空或 None 时模型只能直接给 FinalAnswer。

        Returns:
            (final_answer_text, state)：最终答案文本 + 完整执行轨迹状态。
            final_answer_text 可能是模型的 FinalAnswer，也可能是超步数的兜底提示。
        """
        # 初始化运行状态。max_steps=10 是经验值：多数 ReAct 任务 3-6 步即可完成，
        # 留 10 步既给模型纠错空间，又能硬性兜底防死循环。
        state = AgentState(max_steps=10)
        tools = tools or []  # 统一成 list，避免后续到处判 None

        # ---- 主循环 ----
        # 终止条件：state.finished（模型主动给出 FinalAnswer）
        #          或 step_count >= max_steps（步数兜底）
        while not state.finished and state.step_count() < state.max_steps:
            # 1) 组装本轮 ReAct 提示：系统提示 + 工具清单 + 历史轨迹 + 当前任务
            prompt = self._build_prompt(task, state, tools)

            # 2) 调 LLM。这里只发一条 system（格式约束）+ 一条 user（组装好的 prompt）。
            #    temperature 偏低（0.2）：ReAct 需要模型严格按格式输出，太发散会破坏三行协议。
            messages = [
                ChatMessage(
                    "system",
                    "你是一个严格遵循 ReAct 范式的 Agent。"
                    "必须按如下三行格式输出，不得增减行、不得加多余解释：\n"
                    "Thought: <你的推理>\n"
                    "Action: <工具名 或 FinalAnswer>\n"
                    "Action Input: <工具入参JSON 或 最终答案文本>",
                ),
                ChatMessage("user", prompt),
            ]
            try:
                resp = self.gateway.chat(messages, temperature=0.2)
                llm_output = resp.content
            except Exception as e:
                # 网关全部 fallback 失败的兜底：把错误作为 Observation 喂回，让循环有机会重试。
                # 若已经无可用模型，则直接终止并返回错误提示，避免无限重试。
                state.steps.append(
                    AgentStep(kind="observation", content=f"[gateway error] {e!r}")
                )
                # 连模型都调不通，继续循环也没意义，直接跳出返回兜底。
                return (
                    f"Agent 执行失败：模型网关不可用（{e!r}）。",
                    state,
                )

            # 3) 把模型本轮的原始输出作为一条 thought 记录，便于审计回放。
            state.steps.append(AgentStep(kind="thought", content=llm_output))

            # 4) 解析 LLM 输出为结构化的 ToolCall（含 thought / action / arguments）。
            tool_call = self._parse_action(llm_output)

            # 5) 判定是否终局：Action == FinalAnswer 表示模型决定直接给答案。
            if tool_call.name == FINAL_ANSWER or not tool_call.name:
                # 模型给出最终答案，Action Input 字段即答案文本。
                state.finished = True
                final_answer = tool_call.arguments.get("__answer__", "") if tool_call.arguments else ""
                # 兜底：若解析没拿到答案文本，直接用 Action Input 原文。
                if not final_answer and tool_call.thought:
                    final_answer = tool_call.thought
                return final_answer, state

            # 6) 非终局：执行工具，拿到 Observation。
            observation = self._execute_tool(tool_call, tools)
            # 把 Action 和 Observation 都记入轨迹，下一轮 _build_prompt 会拼回 prompt。
            state.steps.append(
                AgentStep(kind="action", content=tool_call.name, tool=tool_call.name)
            )
            state.steps.append(AgentStep(kind="observation", content=observation))

        # ---- 步数兜底 ----
        # 走到这里说明 max_steps 用完仍未 FinalAnswer。生产 Agent 必须显式处理这种情况，
        # 否则会给用户一个"卡死无响应"的体验。这里返回明确的兜底提示，附带已有轨迹摘要，
        # 便于上层（如 Planner 重规划 / 人工介入）做后续处理。
        last_obs = next(
            (s.content for s in reversed(state.steps) if s.kind == "observation"),
            "（无观察记录）",
        )
        return (
            f"未能在 {state.max_steps} 步内完成任务。最后一次观察：{last_obs}",
            state,
        )

    def _build_prompt(self, task: str, state: AgentState, tools: list[Tool] | None) -> str:
        """组装 ReAct 提示：提示工程 + 上下文管理。

        提示由五部分拼成，顺序经过设计：工具清单在前让模型知道"手头有什么"，
        历史轨迹在中让模型看到"已经做了什么"，当前任务在后让模型聚焦"现在要干什么"。

        五部分：
            1. 工具清单：每个 Tool 的 name + description + 参数 schema，让模型知道可调什么
            2. 已知上下文/记忆：预留给 Memory 注入（当前从 state 派生，未来可扩展）
            3. 历史轨迹：把 state.steps 里的 Thought/Action/Observation 串成文本
            4. 当前任务
            5. 输出格式约定（再强调一次，降低格式跑偏概率）

        Args:
            task: 用户原始任务
            state: 当前运行状态（主要用 state.steps 拼历史轨迹）
            tools: 可用工具

        Returns:
            组装好的 user 消息文本。
        """
        tools = tools or []
        sections: list[str] = []

        # ---- 1. 工具清单 ----
        # 把每个工具的 name/description/schema 拼成可读文本。模型据此选择 Action。
        # schema 用 JSON 序列化，模型能据此构造合法的 Action Input JSON。
        if tools:
            tool_lines = []
            for t in tools:
                tool_lines.append(f"- {t.name}: {t.description}")
                # schema 可能空（兜底工具），非空时附上 JSON 让模型知道入参结构。
                if t.schema:
                    tool_lines.append(f"  参数 schema: {json.dumps(t.schema, ensure_ascii=False)}")
            sections.append("【可用工具】\n" + "\n".join(tool_lines))
        else:
            # 无工具时，模型只能直接给 FinalAnswer，明确告知避免它瞎编 Action。
            sections.append("【可用工具】\n（无工具可用，请直接给出 FinalAnswer）")

        # ---- 2. 已知上下文 ----
        # 当前从 state 派生最简上下文。未来这里可注入 Memory.to_messages() 的压缩摘要，
        # 或接入 RAG 检索结果。保留这个 section 是为了"上下文管理"职责的可扩展性。
        sections.append("【已知上下文】\n（暂无额外上下文）")

        # ---- 3. 历史轨迹 ----
        # 关键：ReAct 的核心就是把"过去的 Thought/Action/Observation"喂回模型，
        # 让它基于完整轨迹而非仅最近一步做决策。这是 ReAct 区别于单轮 prompt 的本质。
        if state.steps:
            trace_lines = []
            for step in state.steps:
                if step.kind == "thought":
                    trace_lines.append(f"Thought: {step.content}")
                elif step.kind == "action":
                    trace_lines.append(f"Action: {step.content}")
                elif step.kind == "observation":
                    trace_lines.append(f"Observation: {step.content}")
            sections.append("【历史轨迹】\n" + "\n".join(trace_lines))

        # ---- 4. 当前任务 ----
        sections.append(f"【当前任务】\n{task}")

        # ---- 5. 输出格式约定 ----
        # 在 user 消息末尾再强调一次格式，配合 system 提示双保险，降低格式跑偏率。
        sections.append(
            "【输出要求】\n"
            "请严格按以下三行格式输出（不要加多余行）：\n"
            "Thought: <推理>\n"
            "Action: <工具名 或 FinalAnswer>\n"
            "Action Input: <JSON参数 或 最终答案文本>"
        )

        return "\n\n".join(sections)

    def _parse_action(self, llm_output: str) -> ToolCall:
        """用正则从 LLM 文本输出解析出 Thought/Action/Action Input。

        这是"手写 ReAct"最体现内部机制的一步：
        不依赖 function-calling 的结构化返回，而是从自然语言文本里抠出结构。

        解析策略（鲁棒性优先）：
            - 三个字段各用一条正则，DOTALL 模式让 . 匹配换行（Thought 可能多行）
            - 任一字段缺失都不抛异常，返回部分填充的 ToolCall，让主循环能继续走
            - Action Input 若是 JSON 则解析成 dict；若不是（如最终答案是纯文本）
              则放进 arguments["__answer__"]，保持返回结构统一

        Args:
            llm_output: 模型原始文本输出。

        Returns:
            ToolCall：含 thought / name / arguments 三字段。
            若 Action == FinalAnswer，arguments["__answer__"] 存最终答案文本。
        """
        # Thought：从 "Thought:" 到下一个 "Action:" 之间的内容。
        thought_match = _THOUGHT_RE.search(llm_output)
        thought = thought_match.group(1).strip() if thought_match else ""

        # Action：工具名或 FinalAnswer。
        action_match = _ACTION_RE.search(llm_output)
        action = action_match.group(1).strip() if action_match else ""

        # Action Input：行尾到结束，可能是 JSON 参数也可能是最终答案文本。
        input_match = _ACTION_INPUT_RE.search(llm_output)
        raw_input = input_match.group(1).strip() if input_match else ""

        # ---- 解析 Action Input ----
        arguments: dict = {}
        if action == FINAL_ANSWER:
            # 终局：Action Input 就是最终答案文本，统一塞进 __answer__ 字段。
            arguments["__answer__"] = raw_input
        elif raw_input:
            # 非终局：Action Input 应该是合法 JSON 参数。尝试解析；解析失败则
            # 把原始文本作为单个位置参数塞进 arguments，保证工具至少能被调用到，
            # 由 _execute_tool 再做参数适配（鲁棒性优先于严格性）。
            try:
                parsed = json.loads(raw_input)
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    # JSON 但不是对象（如纯字符串/数字），包成单参数。
                    arguments = {"input": parsed}
            except json.JSONDecodeError:
                # 非 JSON：可能是模型把自然语言当 Action Input。退化为单参数。
                arguments = {"input": raw_input}

        return ToolCall(name=action, arguments=arguments, thought=thought)

    def _execute_tool(self, tool_call: ToolCall, tools: list[Tool]) -> str:
        """执行一次工具调用，返回 Observation 文本。

        对应"工具调用"职责。设计要点：
            - 工具未找到：返回明确错误 Observation，让模型知道并改选其他工具
            - 参数不匹配：尽量适配（取 schema 默认 / 忽略多余字段），不崩
            - 工具抛异常：捕获并包成 Observation: <error>，喂回模型让它纠错
              这是 ReAct 的自我修复能力——模型看到错误会调整下一步

        Args:
            tool_call: 解析出的工具调用请求。
            tools: 可用工具列表。

        Returns:
            Observation 文本。成功则返回工具 func 的返回值；失败则返回错误描述。
        """
        # ---- 1. 找工具 ----
        tool = next((t for t in tools if t.name == tool_call.name), None)
        if tool is None:
            # 工具不存在：返回明确提示 + 可选工具列表，引导模型改选。
            available = ", ".join(t.name for t in tools) or "（无）"
            return f"[error] 未知工具 '{tool_call.name}'，可选工具：{available}"

        # ---- 2. 调用 ----
        try:
            # **arguments 展开：ToolCall.arguments 是 dict，直接作为关键字参数传入。
            # 这里不做过严的 schema 校验，让工具自身的函数签名做最后把关，
            # 原则：运行时不该因为参数校验把可用调用拦死。
            result = tool.func(**tool_call.arguments)
            # 统一成 str：Observation 要拼回 prompt，必须是文本。
            return str(result)
        except TypeError as e:
            # 参数签名不匹配（多传/少传/类型错）。返回错误 Observation，模型会纠错重试。
            return f"[error] 工具 '{tool_call.name}' 参数不匹配：{e!r}"
        except Exception as e:  # noqa: BLE001 - 工具异常必须吞掉包成 Observation，否则一个工具抛错整个 Agent 就崩了
            # 工具内部异常：捕获成 Observation 喂回模型，ReAct 的自我修复能力。
            return f"[error] 工具 '{tool_call.name}' 执行失败：{e!r}"
