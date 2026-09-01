"""阿里云百炼（DashScope）AI 生图客户端（可选能力，无 key 即整链路跳过）。

环境变量（与 LLM 的 *_API_KEY 分离，生图是增强不是主链路）：
- DASHSCOPE_API_KEY     必填开关：不配则 available=False
- DASHSCOPE_IMAGE_MODEL 默认 qwen-image-3.0-pro（百炼文生图模型）
- DASHSCOPE_IMAGE_BASE  与 VLM（vlm.py）共用同一 base env。注意取的是
                        域名根（如 https://xxx.maas.aliyuncs.com），
                        本模块自动拼 /api/v1 原生协议前缀

协议选型（2026-09 真 key 实测）：
- OpenAI 兼容 /compatible-mode/v1/images/generations：官方公共端点支持，
  但用户账号的 token-plan 专用端点一律 400 InvalidParameter "url error"，
  实测不可用；
- DashScope 原生 /api/v1/services/aigc/multimodal-generation/generation：
  两个端点都通，qwen-image-3.0-pro 同步返回图片 URL。
故走原生协议——base 从 compatible-mode 路径回退到域名根再拼原生路径，
公共端点与专用端点都能用。
"""
from __future__ import annotations

import os

_DEFAULT_BASE = "https://dashscope.aliyuncs.com"
_NATIVE_PATH = "/api/v1/services/aigc/multimodal-generation/generation"


def _native_url(base: str) -> str:
    """把任意形态的 base（域名根 / compatible-mode 路径）归一成原生协议 URL。

    用户 .env 里 DASHSCOPE_IMAGE_BASE 可能写 ".../compatible-mode/v1"（VLM
    用的形态），生图需要域名根——剥掉兼容层路径再拼原生路径。
    """
    root = base.split("/compatible-mode")[0].rstrip("/")
    return root + _NATIVE_PATH


class ImageGen:
    """百炼（DashScope 原生协议）生图客户端。

    available=False 时调用方跳过整个 gen_images。
    """

    def __init__(self) -> None:
        self._key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        self._base = (os.environ.get("DASHSCOPE_IMAGE_BASE", "").strip()
                      or _DEFAULT_BASE)
        self._model = (os.environ.get("DASHSCOPE_IMAGE_MODEL", "").strip()
                       or "qwen-image-3.0-pro")

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def generate(self, prompt: str, size: str = "1024*1024") -> bytes:
        """文生图一次，返回图片 bytes。失败 raise（调用方决定降级）。

        原生协议 body：{"model", "input": {"messages": [{"content":
        [{"text": prompt}]}]}}——同步返回 output.choices[0].message
        .content[0].image 的 OSS URL（短期有效），当场下载转 bytes。
        size 参数原生协议在此模型上无对应入参（固定输出分辨率），签名
        保留是为了兼容调用方，实际忽略。
        """
        import httpx

        body = {
            "model": self._model,
            "input": {"messages": [{"role": "user",
                                    "content": [{"text": prompt}]}]},
        }
        async with httpx.AsyncClient(timeout=240) as http:
            r = await http.post(
                _native_url(self._base),
                headers={"Authorization": f"Bearer {self._key}",
                         "Content-Type": "application/json"},
                json=body)
            if r.status_code != 200:
                raise RuntimeError(
                    f"百炼生图失败 [{r.status_code}]：{r.text[:200]}")
            data = r.json()
            # 输出结构：output.choices[].message.content[].image（URL）
            # 或同位置 "video"（视频模型）——生图只取 image
            choices = (data.get("output") or {}).get("choices") or []
            contents = ((choices[0].get("message") or {}).get("content")
                        if choices else None) or []
            url = next((c.get("image") for c in contents if c.get("image")),
                       None)
            if not url:
                raise RuntimeError(f"百炼生图响应无图片：{str(data)[:200]}")
            img = await http.get(url)
            img.raise_for_status()
            return img.content
