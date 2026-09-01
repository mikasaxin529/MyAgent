# DevPilot 一体化镜像：FastAPI 后端 + 托管前端 SPA（api.py 自动挂载 web/frontend/dist）
# 基础镜像用标准名 node:20-alpine / python:3.13-slim；本地 Docker Hub 不可达时
# 从 D3A_TOOLKIT 离线 tar load 后 tag 成同名即可，Dockerfile 不用改。

# ---------- Stage 1: 前端构建 ----------
FROM node:20-alpine AS webbuild
WORKDIR /web
# 先装依赖，源码改动不击穿这层缓存
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/frontend/ ./
RUN npm run build

# ---------- Stage 2: 运行时 ----------
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DEVPILOT_DIST_DIR=/app/dist \
    DEVPILOT_OUTPUTS_DIR=/app/outputs \
    DEVPILOT_DATA_DIR=/app/.devpilot

WORKDIR /app

# LibreOffice（soffice）：visual_review 节点把 PPTX 无头转 PDF 再逐页出图。
# 只装 impress 组件 + 中文字体（课件审查的渲染保真），装完清 apt 缓存。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-impress fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 包元数据 + 源码一起进第一层（setuptools 需要 src/ 才能 editable-less 安装）
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip \
    && pip install ".[web,graph]"

# 前端构建产物。api.py 优先读 DEVPILOT_DIST_DIR（非 editable 安装时
# __file__ 在 site-packages，仓库相对路径推断会失效）
COPY --from=webbuild /web/dist ./dist
# agent→model 绑定等资源配置
COPY config/ ./config/

# 非 root 运行；outputs 为课件交付物落盘目录，.devpilot 为会话/记忆 SQLite
# 落盘目录（两者均由 compose 挂载持久化）
RUN useradd -m devpilot \
    && mkdir -p /app/outputs /app/.devpilot \
    && chown -R devpilot:devpilot /app
USER devpilot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)"

CMD ["uvicorn", "devpilot.web.api:app", "--host", "0.0.0.0", "--port", "8000"]
