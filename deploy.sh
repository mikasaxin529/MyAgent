#!/usr/bin/env bash
# ============================================================================
# DevPilot 持续部署脚本（Docker）
#
# 用法：
#   ./deploy.sh              # 拉最新代码 + 重建 + 滚动重启（默认）
#   ./deploy.sh --no-pull    # 不 git pull（用当前工作区代码构建）
#   ./deploy.sh --local      # 从本机镜像标签启动（跳过 build，配 sync.sh 推镜像用）
#   ./deploy.sh logs         # 跟踪日志
#   ./deploy.sh status       # 查看状态与健康检查
#
# 首次在新服务器部署：见文件末尾注释，或用 sync.sh 把代码/镜像传上来。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"
COMPOSE="docker compose"
command -v docker >/dev/null || { echo "✗ 未检测到 docker，先装：curl -fsSL https://get.docker.com | sh"; exit 1; }

# 首次运行自动生成 .env（模型 Key 稍后手动填）
if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  echo "⚠ 已从 .env.example 生成 .env —— 部署前请编辑填入 DEEPSEEK_API_KEY 等"
fi

ensure_dirs() { mkdir -p outputs logs; }

health_check() {
  echo "→ 等待健康检查…"
  for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      echo "✓ 服务已就绪：http://$(curl -fsS ifconfig.me 2>/dev/null || echo localhost):8000"
      return 0
    fi
    sleep 2
  done
  echo "✗ 30 次探测未通过，最近日志："
  $COMPOSE logs --tail 40 || true
  return 1
}

case "${1:-deploy}" in
  logs)   exec $COMPOSE logs -f ;;
  status) exec $COMPOSE ps ;;
  restart) ensure_dirs; $COMPOSE restart; health_check ;;
  --local)
    # 用已拉取的镜像启动（IMAGE=registry.../devpilot:tag 指定；不 build）
    ensure_dirs
    IMAGE="${IMAGE:-devpilot:latest}" $COMPOSE up -d --no-build
    health_check
    ;;
  *)
    [[ "${1:-}" == "--no-pull" ]] || {
      echo "→ git pull"
      git pull --ff-only || echo "⚠ git pull 跳过（无远程/有本地改动）"
    }
    ensure_dirs
    echo "→ 构建镜像（前端 stage + 后端 pip install，首次约 3-5 分钟）"
    $COMPOSE build
    echo "→ 滚动启动"
    $COMPOSE up -d
    health_check
    ;;
esac

# ============================================================================
# 首次在新 ECS 部署（三选一，把代码弄到服务器）：
#
#  A. 有 git 远程仓库（推荐）：
#     git clone <你的仓库> /opt/devpilot && cd /opt/devpilot
#     cp .env.example .env && vi .env   # 填 API Key
#     chmod +x deploy.sh && ./deploy.sh
#
#  B. 从本地直接同步代码（无需 git）：运行本仓库的 sync.sh
#     ./sync.sh root@<ECS_IP> /opt/devpilot
#     ssh root@<ECS_IP> 'cd /opt/devpilot && ./deploy.sh'
#
#  C. 镜像走阿里云 ACR（服务器无法访问 Docker Hub / 源码走内网时）：
#     本地 ./build-push-acr.sh  → 服务器上
#     docker pull registry.cn-hangzhou.aliyuncs.com/<ns>/devpilot:latest
#     IMAGE=registry.cn-hangzhou.aliyuncs.com/<ns>/devpilot:latest ./deploy.sh --local
#
# 云防火墙：ECS 安全组需放行入方向 TCP 8000（或改成 80/443 反代）。
# ============================================================================
