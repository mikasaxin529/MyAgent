# 智绘工坊 AIDraft

> 多智能体内容创作平台：与用户对话式协作，生成可交付的创作产物（课件 / 剧本 / 分镜…）。

AIDraft 把"内容生产管线"抽象为一条条对话式智能体流水线。每个智能体是一个 langgraph 状态图（AgentHub 目录扫描自动发现），在关键关口停下来等用户确认，跨轮状态落在磁盘（`outputs/<agent>/<会话>/state.json`，磁盘即真相），AI 生图 / 视觉审查等增值能力按 key 有无优雅接入。

## 功能特性

- **AgentHub 即插即用** — `agenthub/` 下每个子目录一个智能体（`manifest.py` + `graph.py`），新增智能体零注册代码，import 失败自动跳过不拖垮服务。
- **语文课件智能体（yuwen）** — 课文名 + 年级 → 大纲确认 → 逐页生成 → AI 审查评分 → AI 配图（百炼生图）→ 三件套渲染（pptx / HTML / docx）→ 视觉审查与自动修复 → 交付报告，全程跨轮状态机驱动。
- **跨轮人机协同** — 大纲 / 角色 / 分镜等关键产物生成后停下等确认，用户可以确认、改稿、切主题、换配图风格，下一轮对话自动接上。
- **AI 生图与视觉审查** — 百炼 wanx 生图（封面 / 四格连环画 / 插图三种角色 prompt），PPTX 无头转图后 qwen-vl 逐页视觉审查，发现版面问题自动重生成、降分回滚。
- **模型网关** — 统一封装 DeepSeek / Qwen / OpenAI / vLLM，路由 + fallback + 限流，按 `config/agents.yaml` 给各节点绑定不同模型。
- **Skill 生态** — 联网搜索（Tavily）/ 天气 / 仓库 / CI / Issue 封装为标准化工具，general 智能体按意图路由调度。
- **Web 工作台** — 千问风格对话界面：智能体选择、会话持久化（SQLite）、实时步骤时间线、大纲 / 审查 / 视觉审查卡片、产物下载与预览。

## 架构

```
                     浏览器（千问风格 SPA）
                            │  SSE 流式（token / step / outline / review / visual / done 帧）
                    ┌───────▼────────┐
                    │  FastAPI (web) │   会话存储 SQLite · 产物静态文件服务
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │  AgentHub      │   目录扫描注册中心：general / yuwen / …
                    └───────┬────────┘
                            │ build_graph()
              ┌─────────────┴─────────────┐
              ▼                           ▼
      general（通用对话图）          yuwen（多阶段管线图）
      路由→规划→推理⇄工具→          大纲→确认→逐页→审查⇄修订→
      反思→记忆提取/压缩            生图→渲染→视觉审查→修复→报告
              │                           │
              └─────────────┬─────────────┘
                            │
                    ┌───────▼────────┐
                    │  模型网关       │   DeepSeek / Qwen / OpenAI · fallback · 限流
                    └───────┬────────┘
                            │
        ┌───────────┬───────┴──────┬─────────────┐
        ▼           ▼              ▼             ▼
   Skill 生态    百炼生图      百炼视觉审查    LibreOffice
   (Tavily 等)   (wanx)        (qwen-vl)      (pptx→pdf→png)
```

跨轮状态机（yuwen 的核心机制，story 等新智能体同构复用）：langgraph 每轮请求都是新图实例，图内不存检查点；**磁盘 state.json 是唯一真相**——每轮入口的条件路由查盘决定走"生成大纲 / 等确认 / 续跑生成"哪个分支，实现多轮人机协同。

## 快速开始

环境要求：Python ≥ 3.10，Node ≥ 18（前端构建）。

```bash
# 1. 安装
pip install -e ".[web,graph,dev]"

# 2. 配置环境变量
cp .env.example .env
# 至少填一个 LLM API Key（DEEPSEEK_API_KEY / QWEN_API_KEY / OPENAI_API_KEY）
# 可选：DASHSCOPE_API_KEY 启用 AI 生图与视觉审查

# 3. 网关自测
python -m aidraft.app gateway-test

# 4. 启动 Web 服务
uvicorn aidraft.web.api:app --reload     # API 在 http://localhost:8000

# 5. 前端开发模式（可选，生产由后端托管 dist）
cd web/frontend && npm install && npm run dev   # 5173，proxy 到 8000
```

打开 `http://localhost:8000`，在智能体下拉选「语文课件生成」，输入如 `《静夜思》 二年级 古诗词`，确认大纲后自动走完生成管线。

### Docker 一键部署

```bash
docker compose up -d --build
```

镜像内置 LibreOffice + 中文字体（视觉审查链路可用）、前端构建产物（API 同源托管）、持久化卷（`outputs/` 交付物 + `.aidraft/` 会话库）。

## CLI

| 命令 | 说明 |
|---|---|
| `aidraft chat <prompt>` | 单轮对话，验证模型网关连通性。 |
| `aidraft gateway-test` | 列出已注册 provider 并测试调用，验证路由 / fallback / 限流。 |

## 项目结构

```
AIDraft/
├── src/aidraft/
│   ├── config.py           # 配置层：.env 加载 + agents.yaml 模型绑定
│   ├── app.py              # CLI 入口：chat / gateway-test
│   ├── gateway/            # 模型网关：多 provider 统一封装
│   ├── agenthub/           # ★ 智能体注册中心（目录扫描自动发现）
│   │   ├── general/        #   通用对话：路由/工具调用/记忆
│   │   └── yuwen/          #   语文课件：14 节点多阶段管线
│   │       ├── nodes/      #     extract_params→…→visual_fix→report
│   │       ├── scripts/    #     渲染三件套 + common（schema/themes）
│   │       └── references/ #     版式 schema 与课型知识（运行时读）
│   ├── graph/              # general 的 langgraph 编排图（cf/ 节点族）
│   ├── skills/             # Skill 生态：websearch / weather / repo / cicd / issue
│   ├── runtime/            # Memory：对话历史三段式压缩
│   ├── governance/         # 审计日志
│   └── web/                # FastAPI：SSE 聊天 / 会话存储 / 产物文件服务
├── web/frontend/           # React SPA（Vite + TS，千问风格）
├── tests/                  # pytest 测试套件
├── .env.example            # 环境变量模板
└── Dockerfile              # 一体化镜像（前端构建 + LibreOffice）
```

## 配置

通过 `.env` 配置，`config.py` 用 python-dotenv 加载。缺失凭证时对应能力**优雅降级**而非崩溃（如无 `DASHSCOPE_API_KEY` 则跳过 AI 配图，管线照常走完）。

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（推荐，成本低） | — |
| `QWEN_API_KEY` | 通义千问 API Key | — |
| `OPENAI_API_KEY` | OpenAI API Key | — |
| `DASHSCOPE_API_KEY` | 百炼 key：AI 生图 + 视觉审查 | — |
| `DASHSCOPE_IMAGE_MODEL` | 生图模型 | `qwen-image-3.0-pro` |
| `DASHSCOPE_VL_MODEL` | 视觉审查模型 | `qwen3.8-flash` |
| `TAVILY_API_KEY` | 联网搜索 Skill | — |
| `AIDRAFT_PRIMARY_MODEL` | 默认主模型 | `deepseek` |
| `AIDRAFT_FALLBACK_MODEL` | fallback 模型 | `qwen` |
| `AIDRAFT_RPM_LIMIT` | 网关每分钟最大请求数 | `60` |
| `AIDRAFT_OUTPUTS_DIR` | 交付物落盘根目录 | `./outputs` |
| `AIDRAFT_DIST_DIR` | 前端构建产物目录（Docker 用） | `web/frontend/dist` |

`config/agents.yaml` 可按节点绑定模型（`yuwen_outline` / `yuwen_slide` / `yuwen_review` 等键，格式 `provider:model`，缺省走默认链）。

## 语文课件智能体管线

```
extract_params（对话收参数）
→ gen_outline（大纲 → END 等确认）
→ confirm（确认 / 改纲 / 切主题 / 换配图，查盘恢复）
→ gen_slides（逐页生成，页级反思重试）
→ gen_plan（教案 + 学习单）
→ review（AI 审查评分）⇄ revise（按问题清单修订，≤2 轮）
→ gen_images（百炼生图：封面 / 四格 / 插图三角色 prompt）
→ render（pptx / HTML / docx 三件套）
→ visual_review（PPTX→图 → qwen-vl 逐页审查）
→ visual_fix（版面问题重生成 → 复查对比，降分回滚）
→ report（交付汇总）
```

四个内置主题（暖橙 / 青蓝 / 墨绿 / 青绿）由 `scripts/common/themes/*.json` 定义，含色板、字号梯度、版式参数；版式元素含目录页、闯关练习卡、四格图解、全出血封面背景等。

## Roadmap

- [ ] 主题即插即用：注册表目录扫描替代硬编码枚举，新主题 JSON 即放即用。
- [ ] 大纲前联网搜索：research 节点搜教学参考资料注入大纲 prompt。
- [ ] 剧本分镜智能体（story）：梗概 → 角色立绘 → 剧本 → 分镜 → 逐镜画面。
- [ ] 插图风格自由输入：预置档位 + 用户自定义描述词透传。

## License

[MIT](./LICENSE) © 2026 AIDraft Contributors
