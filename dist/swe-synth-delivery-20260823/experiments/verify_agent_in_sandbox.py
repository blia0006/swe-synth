#!/usr/bin/env python3
"""验证「Agent 进沙箱」的三个技术前提

问题背景
--------
Scale Out 的一个自然想法是：把 Agent 打包进镜像、让出题过程在沙箱里跑，
沙箱内再自带 build 环境。但这需要先验证三件事：

    前提 1：沙箱内能否执行 git clone（出题要拉目标仓库）
    前提 2：沙箱内能否装依赖并跑 pytest（出题要做双向 sanity 验证）
    前提 3：沙箱内能否产出镜像（已知无 DinD，测试无守护进程的构建工具
            如 buildah / kaniko / img 是否可用）

前提 3 是关键：若沙箱内无法产出镜像，则「出题 + 打包」全放沙箱不成立，
必须保留外部构建层。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from swe_synth.clients.ags import AGSClient  # noqa: E402

IMAGE = "ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-base-ubuntu:v1"
TOOL = "swe-synth-agentinbox-probe"

PROBE = r"""
echo "=== 前提1：git clone 能力 ==="
cd /tmp
timeout 120 git clone -q --depth 1 https://github.com/psf/cachecontrol.git c1 2>&1 \
    && echo "git clone: 成功（$(ls c1 | wc -l) 个顶层条目）" \
    || echo "git clone: 失败（网络受限？）"

echo
echo "=== 前提2：装依赖 + 跑 pytest ==="
if [ -d /tmp/c1 ]; then
    cd /tmp/c1
    timeout 300 /opt/venv311/bin/python -m pip install -q -e . 2>&1 | tail -2
    timeout 120 /opt/venv311/bin/python -m pytest --collect-only -q 2>&1 | tail -3
fi

echo
echo "=== 前提3：无守护进程的镜像构建工具 ==="
for t in buildah kaniko img podman skopeo nerdctl crane; do
    if command -v $t >/dev/null 2>&1; then
        echo "  $t: 已安装"
    else
        echo "  $t: 未安装"
    fi
done

echo
echo "--- 尝试 apt 安装 buildah（沙箱内是否有安装权限与网络）---"
timeout 240 apt-get update -qq 2>&1 | tail -2
timeout 300 apt-get install -y -qq buildah 2>&1 | tail -3
command -v buildah >/dev/null 2>&1 && echo "buildah 安装成功" || echo "buildah 安装失败"

echo
echo "--- buildah 是否能真正构建（需要 user namespace 权限）---"
if command -v buildah >/dev/null 2>&1; then
    cd /tmp && mkdir -p bt && printf 'FROM scratch\nCOPY f.txt /f.txt\n' > bt/Dockerfile
    echo hello > bt/f.txt
    timeout 180 buildah bud -t probe:v1 bt 2>&1 | tail -5
fi

echo
echo "=== 附：容器权限与能力 ==="
id
echo "unprivileged_userns_clone: $(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo 'N/A')"
grep -c . /proc/self/uid_map 2>/dev/null | xargs -I{} echo "uid_map 行数: {}"
capsh --print 2>/dev/null | grep -i "current" | head -2 || echo "capsh 不可用"
"""


def main() -> int:
    ags = AGSClient()
    print("=" * 72)
    print("验证「Agent 进沙箱」的三个技术前提")
    print("=" * 72)

    old = ags.find_tool(TOOL)
    if old:
        ags.delete_tool(old["tool_id"])
        time.sleep(3)
    ags.create_tool(TOOL, IMAGE, description="Agent 进沙箱可行性验证")
    ags.wait_tool_active(TOOL, timeout=300)
    print("  ✅ 沙箱工具就绪")

    sbx = None
    try:
        from e2b_code_interpreter import Sandbox

        os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
        sbx = (Sandbox.create(template=TOOL, timeout=1800)
               if hasattr(Sandbox, "create") else
               Sandbox(template=TOOL, timeout=1800))
        print(f"  ✅ 实例已启动：{getattr(sbx, 'sandbox_id', '?')}\n")

        r = sbx.commands.run(
            "bash -s <<'PEOF'\n" + PROBE + "\nPEOF",
            timeout=1500, user="root")
        print("-" * 72)
        print(r.stdout)
        if r.stderr:
            print("[stderr]", r.stderr[-800:])
        print("-" * 72)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {type(e).__name__}: {str(e)[:700]}")
        return 1
    finally:
        if sbx is not None:
            try:
                sbx.kill()
                print("  已销毁实例")
            except Exception:  # noqa: BLE001
                pass
        t = ags.find_tool(TOOL)
        if t:
            ags.delete_tool(t["tool_id"])
            print("  已清理工具")


if __name__ == "__main__":
    sys.exit(main())
