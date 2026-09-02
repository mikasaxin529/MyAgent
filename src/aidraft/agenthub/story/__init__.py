"""剧本分镜创作智能体（三确认点状态机）。

创意 → 梗概【确认1】→ 角色卡+立绘【确认2】→ 分镜【确认3】→ 导出
（docx 剧本 / xlsx 分镜表 / HTML 预览 / 立绘图片包）。

与 yuwen 同构的跨轮状态机：无状态 langgraph 图 + 磁盘 state.json。
角色一致性双层锚点：description 文字锚（逐镜拼进 image_prompt）+
标准立绘（百炼生图）。
"""
from __future__ import annotations
