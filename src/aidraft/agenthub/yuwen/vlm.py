"""阿里云百炼多模态视觉审查客户端（可选能力，无 key 即整节点跳过）。

环境变量（与 AI 生图共用 DASHSCOPE_API_KEY / DASHSCOPE_IMAGE_BASE，
视觉审查是增强不是主链路）：
- DASHSCOPE_API_KEY   必填开关：不配则 available=False
- DASHSCOPE_IMAGE_BASE 与生图共用同一 base env（账号专用 compatible-mode
                      端点），默认百炼公共端点
- DASHSCOPE_VL_MODEL  默认 "qwen3.8-flash"（逐页调用次数多，flash 便宜；
                      该账号可用且明确支持视觉理解的模型。账号清单里
                      没有 qwen-vl-plus，勿默认它）

一处配置 DASHSCOPE_IMAGE_BASE，生图与视觉审查同时指向专用 token-plan
端点，不必分头维护。

走百炼 OpenAI 兼容端点：AsyncOpenAI(base_url, api_key).chat.completions。
单页课件截图以 data:image/png;base64 data URI 内联进多模态消息——不落
临时文件，免清理与权限问题。
"""
from __future__ import annotations

import base64
import os

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class VLMReview:
    """百炼视觉模型客户端。available=False 时调用方整节点降级。"""

    def __init__(self) -> None:
        self._key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        self._model = (os.environ.get("DASHSCOPE_VL_MODEL", "").strip()
                       or "qwen3.8-flash")
        self._base = (os.environ.get("DASHSCOPE_IMAGE_BASE", "").strip()
                      or _DEFAULT_BASE_URL)

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def review_page(self, image_bytes: bytes, prompt: str) -> str:
        """一页渲染图 + 中文审查提示词 → 模型输出文本（JSON 解析归调用方）。

        失败 raise（调用方决定降级）。超时 120s——视觉模型对整页截图
        推理明显慢于纯文本。
        """
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self._base, api_key=self._key)
        data_uri = "data:image/png;base64," + base64.b64encode(
            image_bytes).decode("ascii")
        resp = await client.chat.completions.create(
            model=self._model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }],
            temperature=0.1,
            timeout=120,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("百炼 VLM 返回空内容")
        return content
