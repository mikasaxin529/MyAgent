# syntax=docker/dockerfile:1
# DevPilot 一体化镜像：FastAPI 后端 + 托管前端 SPA（api.py 自动挂载 web/frontend/dist）

# ---------- Stage 1: 前端构建 ----------
FROM node:24-alpine AS webbuild
WORKDIR /web
# 先装依赖，源码改动不击穿这层缓存
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/frontend/ ./
RUN npm run build

# ---------- Stage 2: 运行时 ----------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 包元数据 + 源码一起进第一层（setuptools 需要 src/ 才能 editable-less 安装）
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip \
    && pip install ".[web,graph]"

# 前端构建产物。api.py 按 parents[3]/web/frontend/dist 定位，路径必须一致
COPY --from=webbuild /web/dist ./web/frontend/dist
# agent→model 绑定等资源配置
COPY config/ ./config/

# 非 root 运行；outputs 为课件交付物落盘目录（compose 挂载持久化）
RUN useradd -m devpilot \
    && mkdir -p /app/outputs \
    && chown -R devpilot:devpilot /app
USER devpilot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)"

CMD ["uvicorn", "devpilot.web.api:app", "--host", "0.0.0.0", "--port", "8000"]
