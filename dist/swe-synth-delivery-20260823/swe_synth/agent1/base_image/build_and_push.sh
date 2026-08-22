#!/usr/bin/env bash
# 构建并推送双镜像方案的共享 base 镜像（环境层）。
#
# 用法：
#   ./build_and_push.sh <registry>/<namespace>/swe-synth-base:<version>
# 或先 export SWE_SYNTH_BASE_IMAGE=<地址> 再不带参数执行。
#
# 推送成功后，把打印出的地址填到 config/settings.yaml 的 `image.base`，
# 后续 Agent1 出的题目镜像会自动 FROM 这个共享 base（见 ../dockerfile_gen.py）。
set -euo pipefail
cd "$(dirname "$0")"

TAG="${1:-${SWE_SYNTH_BASE_IMAGE:-}}"
if [ -z "$TAG" ]; then
  echo "用法: $0 <registry>/<namespace>/swe-synth-base:<version>" >&2
  echo "或设置环境变量 SWE_SYNTH_BASE_IMAGE 后不带参数执行" >&2
  exit 1
fi

echo "[1/2] docker build --platform=linux/amd64 -t ${TAG} ."
docker build --platform=linux/amd64 -t "$TAG" .

echo "[2/2] docker push ${TAG}"
docker push "$TAG"

echo
echo "完成。请把 config/settings.yaml 的 image.base 改为：${TAG}"
