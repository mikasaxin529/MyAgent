#!/usr/bin/env bash
# ============================================================================
# 智绘工坊 AIDraft 持续部署脚本（Docker，阿里云 ECS + Caddy HTTPS）
#
# 用法：
#   ./deploy.sh              # 构建并滚动重启（服务器上执行）
#   ./deploy.sh --no-pull    # 不 git pull，用当前目录代码构建
#   ./deploy.sh --local      # 用现成镜像启动（IMAGE=xxx/aidraft:tag 指定）
#   ./deploy.sh logs         # 跟踪应用日志
#   ./deploy.sh status       # 容器 + 域名健康总览
#
# 架构：
#   [用户] → https://duoduo-qi.cn (443, Caddy 自动证书)
#          → reverse_proxy → 127.0.0.1:8000 (docker, AIDraft)
#   8000 端口同时直接暴露（安全组已放行），可 IP 直连调试。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"
COMPOSE="docker compose"
command -v docker >/dev/null || { echo "✗ 未检测到 docker"; exit 1; }

ensure_dirs() { mkdir -p outputs logs; }

health_check() {
  echo "→ 本地健康检查…"
  for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      echo "✓ 容器就绪"
      echo "✓ 域名:   https://duoduo-qi.cn"
      echo "✓ 直连:   http://47.108.86.96:8000"
      return 0
    fi
    sleep 2
  done
  echo "✗ 健康检查未通过，最近日志："
  $COMPOSE logs --tail 40 || true
  return 1
}

case "${1:-deploy}" in
  logs)   exec $COMPOSE logs -f ;;
  status)
    $COMPOSE ps
    echo ---
    systemctl is-active caddy >/dev/null && echo "caddy: active" || echo "caddy: DOWN"
    curl -fsS -o /dev/null -w "https://duoduo-qi.cn → %{http_code}\n" https://duoduo-qi.cn/ || true
    ;;
  restart) ensure_dirs; $COMPOSE restart; health_check ;;
  --local)
    ensure_dirs
    IMAGE="${IMAGE:-aidraft:latest}" $COMPOSE up -d --no-build
    health_check
    ;;
  *)
    [[ "${1:-}" == "--no-pull" ]] || {
      echo "→ git pull"
      git pull --ff-only 2>/dev/null || echo "⚠ git pull 跳过（无远程/本地改动）"
    }
    ensure_dirs
    echo "→ 构建镜像（前端 stage + pip install，首次约 3-5 分钟）"
    $COMPOSE build
    echo "→ 滚动启动"
    $COMPOSE up -d
    health_check
    ;;
esac

# ============================================================================
# Caddy 反代（systemd 常驻，证书自动签发/续期，无需人工干预）
#   配置: /etc/caddy/Caddyfile   日志: journalctl -u caddy -f
#   改配置后: systemctl reload caddy
#
# 首次部署（已在本服务器执行过，新机器参考）：
#   1. 安全组放行 80/443（8000 可选）
#   2. 下载 Caddy（国内走 ghfast.top 镜像）：
#      curl -sL -o /tmp/caddy.gz https://ghfast.top/https://github.com/caddyserver/caddy/releases/download/v2.10.2/caddy_2.10.2_linux_amd64.tar.gz
#      tar xzf /tmp/caddy.gz -C /tmp caddy && install -m 755 /tmp/caddy /usr/local/bin/caddy
#   3. /etc/caddy/Caddyfile（域名列表改动后 systemctl reload caddy，
#      新增域名会自动签发证书）:
#      duoduo-qi.cn, www.duoduo-qi.cn, ai.duoduo-qi.cn {
#          encode gzip
#          reverse_proxy 127.0.0.1:8000 { flush_interval -1 }
#      }
#   4. systemd unit: /etc/systemd/system/caddy.service → caddy run --config /etc/caddy/Caddyfile
#      systemctl daemon-reload && systemctl enable --now caddy
#   5. DNS: A 记录 duoduo-qi.cn / www → 47.108.86.96（阿里云云解析）
# ============================================================================
