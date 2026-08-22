#!/usr/bin/env bash
# 从已有的题目镜像中提取「纯内容」，构建轻量的题目内容镜像
#
# 用途：验证双镜像方案时需要一个只含题目内容、不含环境的镜像。
# 做法：起一个临时容器，把 /workspace/repo 与 /task 导出，再用 scratch 打包。
set -euo pipefail

NS=ccr.ccs.tencentyun.com/tcb-100008634787-zbaf
SRC=$NS/swe-synth-0034:v1
DST=$NS/swe-synth-content-0034:v1
WORK=/tmp/content_build

rm -rf "$WORK"
mkdir -p "$WORK/repo" "$WORK/task"

echo "=== 从 $SRC 导出题目内容 ==="
CID=$(docker create --entrypoint sh "$SRC")
docker cp "$CID:/workspace/repo/." "$WORK/repo/" 2>/dev/null
docker cp "$CID:/task/." "$WORK/task/" 2>/dev/null
docker rm -f "$CID" >/dev/null

echo "内容体积："
du -sh "$WORK/repo" "$WORK/task"

cat > "$WORK/Dockerfile" <<'DF'
FROM scratch
COPY repo/ /workspace/repo/
COPY task/ /task/
DF

echo
echo "=== 构建内容镜像 $DST ==="
docker build -t "$DST" --platform=linux/amd64 "$WORK" 2>&1 | tail -3
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep content-0034
