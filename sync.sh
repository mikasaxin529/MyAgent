#!/usr/bin/env bash
# ============================================================================
# 把本地工作区（含未提交改动）打包传到 ECS，绕开 git commit/push。
# 用 tar over ssh，Git Bash / WSL / macOS / Linux 通用，无需 rsync。
#
# 用法：
#   ./sync.sh root@<ECS_IP> /opt/aidraft
#   ./sync.sh root@<ECS_IP> /opt/aidraft -i ~/.ssh/your_key.pem
#
# 传完后在服务器上：ssh root@<ECS_IP> 'cd /opt/aidraft && ./deploy.sh --no-pull'
# ============================================================================
set -euo pipefail

HOST="${1:?用法: ./sync.sh user@host /remote/dir [-i key.pem]}"
DEST="${2:?缺少远程目录}"
shift 2
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
while [[ $# -gt 0 ]]; do
  case "$1" in
    -i) SSH_OPTS+=(-i "$2"); shift 2 ;;
    -p) SSH_OPTS+=(-p "$2"); shift 2 ;;
    *) echo "未知参数 $1"; exit 1 ;;
  esac
done

cd "$(dirname "$0")"
SRC="$(pwd)"

# 打包要传的内容：部署相关文件 + 源码，排除环境/缓存/产物
INCLUDE=(Dockerfile docker-compose.yml deploy.sh sync.sh build-push-acr.sh
         pyproject.toml README.md .dockerignore .env.example
         src web config)

echo "→ 打包工作区：$SRC"
# --exclude 前端 node_modules/dist 与所有 __pycache__/.venv；.env 单独传（不进镜像）
tar czf /tmp/aidraft-sync.tgz \
  --exclude='node_modules' --exclude='dist' --exclude='__pycache__' \
  --exclude='.venv' --exclude='*.pyc' --exclude='.git' \
  --exclude='outputs' --exclude='logs' --exclude='*.tsbuildinfo' \
  "${INCLUDE[@]}"

echo "→ 准备远程目录 $DEST"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '$DEST'"

echo "→ 上传代码包"
scp "${SSH_OPTS[@]}" /tmp/aidraft-sync.tgz "$HOST:$DEST/aidraft-sync.tgz"

# .env（含 API Key）：存在才传，且传上去设为 600
if [[ -f .env ]]; then
  echo "→ 上传 .env（含密钥）"
  scp "${SSH_OPTS[@]}" .env "$HOST:$DEST/.env"
  ssh "${SSH_OPTS[@]}" "$HOST" "chmod 600 '$DEST/.env'"
fi

echo "→ 远程解包"
ssh "${SSH_OPTS[@]}" "$HOST" "cd '$DEST' && tar xzf aidraft-sync.tgz && rm aidraft-sync.tgz && chmod +x deploy.sh sync.sh build-push-acr.sh 2>/dev/null; true"

rm -f /tmp/aidraft-sync.tgz
echo "✓ 已同步到 $HOST:$DEST"
echo "下一步：ssh $HOST 'cd $DEST && ./deploy.sh --no-pull'"
