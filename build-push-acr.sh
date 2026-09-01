#!/usr/bin/env bash
# ============================================================================
# 本地构建 + 推送到阿里云容器镜像服务 ACR（个人版免费），服务器从 ACR 拉取。
# 适用：ECS 拉 Docker Hub 慢 / 想让部署走内网加速。
#
# 一次性准备：
#   1. 阿里云控制台开通「容器镜像服务 ACR 个人版」，创建命名空间（如 aidraft）
#   2. 设置镜像仓库访问密码
#   3. docker login registry.cn-hangzhou.aliyuncs.com -u <阿里云账号全名>
#
# 用法：
#   ./build-push-acr.sh registry.cn-hangzhou.aliyuncs.com/<ns>/aidraft
#   ./build-push-acr.sh registry.cn-hangzhou.aliyuncs.com/<ns>/aidraft v1.2
#
# 服务器上：
#   docker login registry.cn-hangzhou.aliyuncs.com
#   IMAGE=registry.cn-hangzhou.aliyuncs.com/<ns>/aidraft:<tag> ./deploy.sh --local
# ============================================================================
set -euo pipefail

IMAGE="${1:?用法: ./build-push-acr.sh <registry>/<ns>/aidraft [tag]}"
TAG="${2:-latest}"

cd "$(dirname "$0")"
echo "→ 构建 $IMAGE:$TAG"
docker build -t "$IMAGE:$TAG" -t "$IMAGE:$(date +%Y%m%d-%H%M)" .

echo "→ 推送"
docker push "$IMAGE:$TAG"
docker push "$IMAGE:$(date +%Y%m%d-%H%M)" 2>/dev/null || true

echo "✓ 完成。服务器上执行："
echo "  docker pull $IMAGE:$TAG"
echo "  IMAGE=$IMAGE:$TAG ./deploy.sh --local"
