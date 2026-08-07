# DevPilot

> An AI Agent platform for the full software development lifecycle: requirement → planning → coding → testing → human review → pull request.

DevPilot 把软件研发流程抽象为可编排的 Agent 工作流。手写的 ReAct 运行时驱动 Planner / Coder / Reviewer / Tester 多角色协作，内部系统（代码仓库、CI/CD、项目管理）通过 MCP 标准化为 Skill 能力，全程由审批门与审计日志构成的治理层把关，并配套面向研发场景的多维度评估体系。

## Features

- **手写 ReAct 运行时** — Thought / Action / Observation 循环自实现，不依赖重型 Agent 框架，模型无关、可审计、可回放。
- **MCP Skill 生态** — 代码仓库 / CI-CD / 项目管理封装为标准化能力，新系统接入只需实现一个 Skill 类。
- **多 Agent 编排** — Orchestrator-Worker 拓扑 + Blackboard 黑板模式，角色解耦、流水线式协作。
- **评估体系** — LLM-as-judge + rubric 逐维度打分，多维度指标与回归拦截，支撑数据飞轮。
- **人在回路治理** — 审批门集中定义高危动作，非交互环境默认拒绝；审计日志全程留痕、可追溯。
- **模型网关** — 统一封装 DeepSeek / Qwen / OpenAI / vLLM，支持路由、fallback、限流与缓存。

## Architecture

```
            User / IDE
                │
        ┌───────▼────────┐
        │  Model Gateway │   路由 / fallback / 限流 / 缓存
        └───────┬────────┘
                │
        ┌───────▼────────┐
        │  Agent Runtime │   手写 ReAct + Planner + Memory
        └───────┬────────┘
                │
   ┌────────────┼────────────┐
   │            │            │
   ▼            ▼            ▼
 Planner     Coder      Reviewer / Tester   Multi-Agent 编排（Blackboard）
   │            │            │
   └────────────┼────────────┘
                │
        ┌───────▼────────┐
        │   MCP Skills   │   Repo / CI-CD / Issue / Codebase RAG
        └───────┬────────┘
                │
        ┌───────▼────────┐
        │  Governance    │   ApprovalGate / AuditLog / 反馈回流
        └───────┬────────┘
                │
        ┌───────▼────────┐
        │  Evaluation    │   Golden Set / LLM-judge / Metrics
        └────────────────┘
```

分层依赖方向（自底向上，不可逆）：

```
config → gateway → runtime / skills / rag → agents → app
                  ↘ governance（被 agents 调用）
                  ↘ eval（调用 gateway，不依赖 agents）
```

## Quick Start

环境要求：Python ≥ 3.10。

```bash
# 1. 安装（基础依赖即可 import 全部模块，第三方库惰性导入）
pip install -e .

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填入一个 LLM API Key
# 可选：GITHUB_TOKEN / JENKINS_* / JIRA_* 启用对应 Skill

# 3. 网关自测
python -m devpilot.app gateway-test

# 4. 单轮对话
python -m devpilot.app chat "你好，介绍一下你自己"

# 5. 跑完整 Multi-Agent 流程
python -m devpilot.app run "给 FastAPI 加一个 /health 健康检查接口"

# 6. 跑评估套件
python -m devpilot.app eval

# 7. 列出已注册 Skill
python -m devpilot.app skills
```

可选 extras 按需启用能力：

```bash
pip install -e ".[skills]"   # PyGithub + requests：真实发 PR / 调 Jenkins / Jira
pip install -e ".[rag]"      # chromadb：Codebase RAG 向量检索
pip install -e ".[dev]"      # pytest + ruff：测试与代码风格
```

缺失凭证或可选依赖时，对应能力返回明确提示而非崩溃（优雅降级）。

## CLI Commands

| 命令 | 说明 |
|---|---|
| `chat <prompt>` | 单轮对话，验证模型网关连通性。 |
| `gateway-test` | 列出已注册 provider 并测试调用，验证路由 / fallback / 限流。 |
| `run <task>` | 跑完整 Multi-Agent 流程：Planner → Coder → Reviewer → Tester，含审批与审计。 |
| `eval` | 跑 Evaluation Harness，对 Golden 集逐 case 评测并汇总多维度指标。 |
| `skills` | 列出已注册 Skill 及其能力清单（tools / resources）。 |

## Project Structure

```
DevPilot/
├── src/devpilot/
│   ├── config.py          # 配置层：从 .env 加载模型与网关配置
│   ├── app.py             # 入口层：CLI 命令分发
│   ├── gateway/           # 模型网关：统一封装多模型调用
│   ├── runtime/           # Agent 运行时：手写 ReAct + Planner + Memory
│   ├── skills/            # Skill 生态：内部系统封装为标准化能力
│   ├── rag/               # 知识检索：代码库混合检索
│   ├── agents/            # 多 Agent 编排：Orchestrator + Worker 角色
│   ├── governance/        # 治理层：审批门 + 审计日志
│   └── eval/              # 评估层：LLM-judge + 多维度基准
├── eval_data/             # Golden 评测集与基准数据
├── docs/                  # 文档（架构文档等）
├── .env.example           # 环境变量模板
├── pyproject.toml         # 构建与依赖配置
├── LICENSE                # MIT
└── README.md
```

## Configuration

通过 `.env` 配置，`config.py` 用 `python-dotenv` 加载。

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | — |
| `DEEPSEEK_BASE_URL` | DeepSeek 接口地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | DeepSeek 模型名 | `deepseek-chat` |
| `QWEN_API_KEY` | 通义千问 API Key | — |
| `QWEN_BASE_URL` | 通义千问接口地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `QWEN_MODEL` | 通义千问模型名 | `qwen-plus` |
| `OPENAI_API_KEY` | OpenAI API Key | — |
| `OPENAI_BASE_URL` | OpenAI 接口地址 | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | OpenAI 模型名 | `gpt-4o-mini` |
| `DEVILOT_PRIMARY_MODEL` | 默认主模型（gateway 注册的 provider 名） | `deepseek` |
| `DEVILOT_FALLBACK_MODEL` | fallback 模型 | `qwen` |
| `DEVILOT_RPM_LIMIT` | 网关每分钟最大请求数 | `60` |
| `GITHUB_TOKEN` / `GITHUB_REPO` | RepoSkill 远端写操作凭证 | — |
| `JENKINS_URL` / `JENKINS_USER` / `JENKINS_TOKEN` | CICDSkill 凭证 | — |
| `JIRA_URL` / `JIRA_USER` / `JIRA_TOKEN` | IssueSkill 凭证 | — |

## Roadmap

- [ ] 接入 MCP Server 标准协议，Skill 一键暴露为 MCP tools / resources。
- [ ] Skill 生态扩展：GitLab RepoSkill、Argo CD 部署 Skill、飞书 / Jira 同步 Skill。
- [ ] RAG 升级：换用更强 embedding 与 reranker，支持跨仓库语义检索。
- [ ] 评估体系：增加在线 A/B 评测、按 tag 自动回归拦截、历史基准趋势看板。
- [ ] 治理层：审批接入 IM（飞书 / Slack），审计导出 OpenTelemetry。
- [ ] 运行时：支持 function-calling 模式与文本 ReAct 模式双轨切换。
- [ ] 多 Agent：新增 SecAgent（安全审计）、DocAgent（文档同步）角色。
- [ ] 可观测性：集成 Langfuse / OpenTelemetry 全链路 trace。

## License

[MIT](./LICENSE) © 2026 DevPilot Contributors
