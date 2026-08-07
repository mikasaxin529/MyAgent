# DevPilot Architecture

> 本文是 DevPilot 的技术架构文档：按「分层 → 模块 → 关键类/方法 → 设计要点 → 数据流」的顺序讲清每个组件的职责与设计取舍。配合源码逐行注释阅读。

## 一、目录结构与分层

```
src/devpilot/
├── __init__.py          # 包入口，只导出 __version__
├── config.py            # 配置层：从 .env 读模型/网关配置
├── app.py               # 入口层：CLI 命令分发（chat/run/eval/skills/gateway-test）
├── gateway/             # 模型网关层：统一封装多模型调用
├── runtime/             # Agent 运行时层：手写 ReAct + Planning + 记忆
├── skills/              # Skill 生态层：内部系统封装为标准化能力
├── rag/                 # 知识检索层：代码库混合检索
├── agents/              # 多 Agent 编排层：Orchestrator + Worker 角色
├── governance/          # 治理层：审批门 + 审计日志
└── eval/                # 评估层：LLM-judge + 多维度基准
```

**分层依赖方向（自底向上，不可逆）**：

```
config → gateway → runtime/skills/rag → agents → app
                 ↘ governance（被 agents 调用）
                 ↘ eval（调用 gateway，不依赖 agents）
```

- 上层只能调下层，下层不感知上层（`gateway` 不知道谁在用它）。
- `agents` 是唯一「组合者」，把 runtime + skills + governance 串成研发闭环。
- `eval` 与 `agents` 平级，独立调用 gateway 评测 Agent 能力。

---

## 二、各模块详解

### 1. `config.py` — 配置层

- `Settings` + `ProviderConfig` + `GatewayConfig`，用 `python-dotenv` 加载 `.env`。
- `settings.providers()` 返回 deepseek / qwen / openai 三家配置，供网关注册。
- **设计要点**：配置集中一处，避免散落；后续 RAG / MCP 配置也挂这里。

### 2. `gateway/` — 模型网关

Agent 与具体模型的唯一边界，对上只暴露 `Gateway.chat()`。

| 文件 | 作用 |
|---|---|
| `base.py` | 抽象类型：`ChatMessage`、`ChatResponse`、`LLMProvider` 协议 |
| `providers.py` | `OpenAICompatProvider`：DeepSeek/Qwen/OpenAI/vLLM 都走 OpenAI 兼容协议，一个实现覆盖多家 |
| `gateway.py` | `Gateway`：路由主模型 → 失败 fallback → RPM 限流 → 简易缓存；`build_default_gateway()` 工厂 |

**关键 API**：

```python
gw = build_default_gateway()
resp = gw.chat([ChatMessage("user", "...")], temperature=0.2, json_mode=True, tools=[...])
resp.content  # str
```

**设计要点**：网关让上层「模型可热切换」；fallback 保稳定性；限流防爆配额；缓存降本。私有化 vLLM 只需注册成 Provider 即可纳入。

### 3. `runtime/` — Agent 运行时（核心）

项目最核心的一层，承载 Agent 的思考循环。

| 文件 | 作用 |
|---|---|
| `types.py` | `Tool` / `ToolCall` / `AgentStep` / `AgentState` + `AgentRuntime` 协议 |
| `react.py` | 手写文本解析 ReAct 循环（不依赖 function-calling） |
| `planner.py` | `Planner`：用 json_mode 把需求拆成子任务列表 |
| `memory.py` | `Memory`：token 预算裁剪 + LLM 摘要压缩 |

**ReAct 循环算法**（`react.py`）：

1. `_build_prompt(task, state, tools)`：五段式组装 —— 工具清单 + schema / 已知上下文 / 历史轨迹（把 `state.steps` 串回 prompt）/ 当前任务 / 输出格式约定（`Thought:` / `Action:` / `Action Input:`）。
2. 调 `gateway.chat(...)`，`temperature=0.2` 保格式稳定。
3. `_parse_action(llm_output)`：正则解析出 thought / action / action_input；`Action=FinalAnswer` 则终止。
4. `_execute_tool`：在 tools 里匹配并调用，异常包成 `Observation: error` 喂回模型实现自我修复，不崩。
5. 主循环：`while not finished and step_count < max_steps`，超步数返回「未能在步数内完成」。

**为什么手写而非用 LangChain**：

1. 掌握内部机制而非黑盒调用；
2. 模型无关（DeepSeek / Qwen 文本模式都稳）；
3. 可审计可评估 —— 每步落 `AgentState.steps`，可轨迹回放、错误归因。

### 4. `skills/` — Skill 生态

把内部系统（代码仓库 / CI-CD / 项目管理）封装为标准化能力，体现「低代码、可复用」。

| 文件 | 作用 |
|---|---|
| `registry.py` | `Skill` 协议 + `SkillSpec` + `SkillRegistry`（注册中心，聚合 `all_specs()`） |
| `repo_skill.py` | 代码仓库：走本地 git（subprocess），无需 Token 即可 demo；PR 部分惰性调 PyGithub |
| `cicd_skill.py` | CI/CD：`requests` 调 Jenkins REST API（含 crumb CSRF），无凭证降级 |
| `issue_skill.py` | 项目管理：`requests` 调 Jira REST API（GET issue / POST search+JQL / PUT 状态） |

**关键设计**：

- 每个 Skill 对应一个 MCP Server，暴露标准化 tools / resources。
- 新系统接入只需实现一个 Skill 类 → 低代码理念。
- 高危方法（`commit_and_pr` / `trigger_pipeline` / `update_issue`）以注释标注「对接 governance 审批门」。
- 第三方库一律惰性导入（方法内 import），保证 `pip install -e .` 无需额外包即可导入。

### 5. `rag/` — 知识检索

- `indexer.py` 的 `CodebaseRAG`：混合检索。
- `index()`：os.walk 扫源码，按函数 / 类粒度 + 行数兜底分块（50-120 行），embedding 入 ChromaDB。
- `search()`：向量召回 top_k×3 ∪ 手写 BM25 关键词召回，去重后分数加权（向量×0.6 + BM25×0.4），可选 LLM rerank。
- `ask()`：检索拼上下文交 LLM 回答，附引用。
- **定位**：「够用即可」，不深做；惰性导入 chromadb，零额外依赖即可导入。

### 6. `agents/` — 多 Agent 编排

唯一把各层串成研发闭环的「组合者」。

| 文件 | 作用 |
|---|---|
| `orchestrator.py` | `Orchestrator.run(task)` 七步流程 + `Blackboard` 共享黑板 |
| `agents.py` | `CoderAgent` / `ReviewerAgent` / `TesterAgent` 三个 Worker |

**拓扑：Orchestrator-Worker**：

- 研发流程天然流水线式（规划 → 改码 → 评审 → 测试），中央编排器比 P2P 更可控可追溯。
- 比单 Agent 全干：角色聚焦、prompt 短、质量高、可独立评估每个环节。

**通信：Blackboard 黑板模式**：

- 各 Worker 不直接调用彼此，读写同一 `Blackboard`（task / plan / code_diff / review / test_result / artifacts）。
- 新增角色（如 SecAgent）只需「读 X 写 Y」，零改动既有 Worker → 解耦。

**`Orchestrator.run(task)` 七步**：

1. 初始化 Blackboard + trace_id + 起始审计。
2. Planner 拆 3-5 步 → `bb.plan`。
3. `CoderAgent.act()` 产改动方案 → `bb.code_diff`。
4. `ReviewerAgent.act()` 评审 diff 标风险 → `bb.review`；高危触发 `approval.request()`。
5. `TesterAgent.act()` 调 cicd Skill 跑测试 → `bb.test_result`。
6. 全程 `audit.record`。
7. 返回 Blackboard。

**韧性**：每个 Worker try/except 包裹，单个失败不中断，失败写审计 → 「宁可降级不可崩溃」。

### 7. `governance/` — 治理层

Human-on-the-Loop：关键决策保留人工审核。

| 文件 | 作用 |
|---|---|
| `approval.py` | `ApprovalGate`：`requires_approval()` 判高危；`request()` 阻塞式 y/n/edit 人工裁决 |
| `audit.py` | `AuditLog`：`record()` 记事件；`export()` 写 JSONL；`to_summary()` 看板聚合 |

**审批门设计**：

- `HIGH_RISK_ACTIONS = {commit_and_pr, trigger_pipeline, update_issue, deploy}` —— 判定标准：不可逆 / 外部可见 / 影响他人。
- 非交互环境（无 tty，如 CI / 后台 / 容器）默认拒绝 → 安全合规底线。
- `edit` 分支：人可改写参数后放行 → 「纠正而非否决」。

**审计**：

- 全程记录 llm_call / tool_call / approval / agent_step，支撑可解释性与事后追溯。
- 导出 JSONL（utf-8 防中文损坏）→ 为 eval 与数据飞轮提供原始数据。

### 8. `eval/` — 评估体系

| 文件 | 作用 |
|---|---|
| `dataset.py` | `GoldenSet`：加载 `eval_data/golden.jsonl`；`add()` 数据飞轮追加新 case |
| `judge.py` | `LLMJudge.judge()`：严格评审人设 + rubric 逐维度打分，json_mode 输出 |
| `metrics.py` | `run_evaluation()` + `Metrics`：跑全量评测集，汇总多维度指标 |

**LLM-judge 设计要点**：

- judge 模型应与被测 Agent 不同厂商 → 避免 self-preference 自评偏袒。
- rubric（评分维度清单）把「主观判断」约束成「逐条核对」 → 降漂移。
- `temperature=0` 求可复现；json_mode 求可解析；失败降级返回 0 分不中断流水线。

**多维度指标**（`run_evaluation` 计算）：

- `accuracy`：各 case overall 均值。
- `task_completion_rate`：passed 比例。
- `robustness`：edge / adversarial tag 子集 accuracy。
- `avg_latency_ms` / `avg_token_cost`。
- `per_tag`：按 tag 分维度统计。

**数据飞轮闭环**：失败 case 经人工标注 → `GoldenSet.add()` → 驱动 Prompt / 工具迭代 → 指标提升 → 回归基准追踪。

### 9. `app.py` — CLI 入口

命令分发：

| 命令 | 作用 |
|---|---|
| `chat <prompt>` | 单轮对话，验证网关。 |
| `gateway-test` | 列 provider + 测试调用。 |
| `run <task>` | 跑完整 Multi-Agent 流程。 |
| `eval` | 跑 Evaluation Harness。 |
| `skills` | 列已注册 Skill 及 specs。 |

---

## 三、一次完整调用的数据流

以 `devpilot run "给 parse_date 加时区参数"` 为例：

```
app.py(cmd_run)
  └─ build_default_gateway()           # 网关：注册 deepseek/qwen
  └─ SkillRegistry.register(Repo/CICD/Issue Skill)  # Skill 生态
  └─ AuditLog() / ApprovalGate()
  └─ Orchestrator(gateway, registry, audit, approval)
       └─ .run(task)
            ├─ audit.record("start")
            ├─ gateway.chat_text → Planner 拆步 → bb.plan      # 运行时+网关
            ├─ CoderAgent.act(): registry repo Skill 查码 + gateway 产 diff → bb.code_diff
            ├─ ReviewerAgent.act(): gateway 评审 → bb.review
            │     └─ (高危) approval.request("commit_and_pr",...) → 人审批 → audit
            ├─ TesterAgent.act(): registry cicd Skill trigger/fetch → bb.test_result
            ├─ audit.record(each step)
            └─ return Blackboard
  └─ 打印 Blackboard + 审计条目数
```

---

## 四、阅读建议顺序

按「从外到内、从主干到支线」读，最易建立全局：

1. `app.py` —— 先看入口有哪些命令，建立功能地图。
2. `gateway/base.py` + `gateway/gateway.py` —— 看模型调用抽象。
3. `runtime/types.py` + `runtime/react.py` —— 核心，逐行读 ReAct 循环。
4. `skills/registry.py` + 任一 Skill —— 看 Skill 如何标准化。
5. `agents/orchestrator.py` —— 看编排如何串起各层。
6. `governance/approval.py` —— 看人机协同关卡。
7. `eval/judge.py` + `eval/metrics.py` —— 看评估体系。
8. `rag/indexer.py` —— 最后看，定位「够用即可」。

每读一个文件，先看顶部 docstring（讲了「做什么 + 为什么」），再看方法注释，逻辑自顶向下。
