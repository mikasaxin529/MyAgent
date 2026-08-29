AGENT_ID = "general"
DISPLAY_NAME = "通用对话"
DESCRIPTION = "默认助手：搜索、写代码、规划多步任务"
IDENTITY_COLOR = "#3D6CC4"
PLACEHOLDER = "输入需求，如「帮我总结7月最新AI资讯」"
# 端点层替本智能体注入 SYSTEM_CHAT（true=由端点管理 system 消息）。
# 未设 managed_system 的智能体（如 yuwen_skill）由各自图自管 system。
MANAGED_SYSTEM = True