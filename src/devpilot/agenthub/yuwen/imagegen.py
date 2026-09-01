"""OpenAI 兼容 AI 生图客户端（可选能力，无 key 即整链路跳过）。

环境变量（与 LLM 的 *_API_KEY 分离，生图是增强不是主链路）：
- IMAGE_API_KEY   必填开关：不配则 available=False
- IMAGE_API_BASE  默认 https://api.openai.com/v1（火山 doubao-seedream 等
                  兼容网关填各自 base）
- IMAGE_API_MODEL 默认 doubao-seedream

用 openai SDK（依赖已有）：AsyncOpenAI(base_url, api_key).images.generate。
"""
from __future__ import annotations

import base64
import os


class ImageGen:
    """OpenAI 兼容生图客户端。available=False 时调用方跳过整个 gen_images。"""

    def __init__(self) -> None:
        self._key = os.environ.get("IMAGE_API_KEY", "").strip()
        self._base = (os.environ.get("IMAGE_API_BASE", "").strip()
                      or "https://api.openai.com/v1")
        self._model = (os.environ.get("IMAGE_API_MODEL", "").strip()
                       or "doubao-seedream")

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def generate(self, prompt: str, size: str = "1024x1024") -> bytes:
        """文生图一次，返回图片 bytes。失败 raise（调用方决定降级）。

        走 OpenAI images 协议的 b64_json 响应格式；部分兼容网关（如
        doubao）返回 url 而非 b64——遇到 url 时 raise 明确提示网关差异。
        """
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self._base, api_key=self._key)
        resp = await client.images.generate(
            model=self._model, prompt=prompt, size=size,
            response_format="b64_json")
        item = resp.data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(item, "url", None)
        if url:
            # 部分网关忽略 response_format 直接给 URL——下载它
            import httpx
            async with httpx.AsyncClient(timeout=60) as http:
                r = await http.get(url)
                r.raise_for_status()
                return r.content
        raise RuntimeError("生图响应既无 b64_json 也无 url")
