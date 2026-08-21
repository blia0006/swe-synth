#!/usr/bin/env bash
# 探测 AGS 官方基础镜像的平台组件构成（用于评估「改用 Ubuntu 基础层」的可行性）
#
# 导师反馈 1：客户环境几乎都是 Ubuntu，需要评估能否把基础层从 Debian 换成 Ubuntu。
# 关键在于：官方镜像里让沙箱「能被平台管理」的组件（S6-Overlay + envd + run-code）
# 能否原样搬到 ubuntu:22.04 上。
set -uo pipefail

IMG=ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest

docker run --rm --entrypoint sh "$IMG" -s <<'PROBE'
echo "=== 1. s6 服务清单与类型 ==="
for s in $(ls /etc/s6-overlay/s6-rc.d/ 2>/dev/null); do
    t=$(cat "/etc/s6-overlay/s6-rc.d/$s/type" 2>/dev/null || echo "-")
    printf "  %-12s type=%s\n" "$s" "$t"
done

echo
echo "=== 2. 各服务 run 脚本 ==="
for s in envd jupyter uvicorn; do
    echo "  --- $s/run ---"
    sed -n '1,10p' "/etc/s6-overlay/s6-rc.d/$s/run" 2>/dev/null | sed 's/^/    /'
done

echo
echo "=== 3. 平台组件路径与体积 ==="
du -sh /command /package /etc/s6-overlay /usr/bin/envd 2>/dev/null

echo
echo "=== 4. run-code 服务依赖的解释器 ==="
command -v python3 jupyter uvicorn 2>/dev/null
python3 -V 2>/dev/null

echo
echo "=== 5. envd 监听端口相关配置 ==="
grep -rhoE "4998[0-9]|4999[0-9]" /etc/s6-overlay/ 2>/dev/null | sort -u | head
PROBE
