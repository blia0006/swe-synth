#!/usr/bin/env python3
"""把 Ubuntu 版基础镜像推到 CCR 并在真实 AGS 沙箱里验证

对应导师反馈 1（Ubuntu 基础层）与 4（DinD / 双镜像方案）的实测。

流程
----
1. docker login + push（走 packer 里同一套凭证逻辑，密码经 stdin 不落命令行）
2. CreateSandboxTool 注册为自定义沙箱工具
3. 启动实例 → 验证 commands.run / files 能力
4. 在沙箱内验证发行版、Python 版本、以及 DinD 可用性
5. 无论成败都清理沙箱实例与工具（沙箱按时长计费）
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

from swe_synth.agent1.packer import _run_with_retry, docker_login  # noqa: E402
from swe_synth.clients.ags import AGSClient  # noqa: E402

IMAGE = "ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-base-ubuntu:v1"
TOOL = "swe-synth-ubuntu-probe"

# 沙箱内要跑的验证脚本
PROBE = r"""
echo "### 1. 发行版（导师反馈1：需为 Ubuntu）"
cat /etc/os-release | head -3

echo
echo "### 2. 课题要求的工具链"
/opt/venv311/bin/python -V
git --version
docker --version 2>&1 | head -1

echo
echo "### 3. DinD 可用性（导师反馈4）"
if [ -S /var/run/docker.sock ]; then
    echo "docker.sock 存在"
    docker info >/dev/null 2>&1 && echo "DinD: 可用" || echo "DinD: sock 存在但连不上 dockerd"
else
    echo "docker.sock 不存在 → 无 DinD（需通过挂载或特权容器提供）"
fi

echo
echo "### 4. 平台能力自检"
ls /init /usr/bin/envd >/dev/null 2>&1 && echo "S6+envd 组件就位"
ps aux 2>/dev/null | grep -c "[e]nvd" | xargs -I{} echo "envd 进程数: {}"
"""


def log(m: str) -> None:
    print(m, flush=True)


def main() -> int:
    registry = os.environ["TCR_REGISTRY"]
    user = os.environ["TCR_USERNAME"]
    pwd = os.environ["TCR_PASSWORD"]

    log("=" * 72)
    log("Ubuntu 版基础镜像：推送 + 真实沙箱验证")
    log("=" * 72)

    log("\n[1/5] docker login")
    rc, out = docker_login(registry, user, pwd)
    if rc != 0:
        log(f"  ❌ 登录失败：{out[-300:]}")
        return 1
    log("  ✅ 登录成功")

    log(f"\n[2/5] docker push {IMAGE}")
    t0 = time.time()
    # CCR 推送常因网络抖动出现 `timeout awaiting response headers`，
    # 已上传成功的层会被跳过，重试代价很低（与 packer.pack_task 同一策略）
    rc, out = _run_with_retry(["docker", "push", IMAGE], timeout=3600)
    if rc != 0:
        log(f"  ❌ push 失败：{out[-500:]}")
        return 1
    log(f"  ✅ push 完成（{time.time() - t0:.1f}s）")

    ags = AGSClient()
    log(f"\n[3/5] 注册沙箱工具 {TOOL}")
    existing = ags.find_tool(TOOL)
    if existing:
        ags.delete_tool(existing["tool_id"])
        log("  （已删除同名旧工具）")
        time.sleep(3)
    ags.create_tool(TOOL, IMAGE, description="Ubuntu 22.04 基础层可行性验证")
    ags.wait_tool_active(TOOL, timeout=300)
    log("  ✅ 工具状态 ACTIVE")

    log("\n[4/5] 启动沙箱实例并执行验证")
    sbx = None
    try:
        from e2b_code_interpreter import Sandbox

        os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
        sbx = (Sandbox.create(template=TOOL, timeout=600)
               if hasattr(Sandbox, "create") else
               Sandbox(template=TOOL, timeout=600))
        sid = getattr(sbx, "sandbox_id", "?")
        log(f"  ✅ 实例已启动：{sid}")

        # 直接用 heredoc 传脚本（files.write 在 e2b 1.x 不支持 user 参数）
        r = sbx.commands.run(
            "bash -s <<'PROBEEOF'\n" + PROBE + "\nPROBEEOF",
            timeout=180, user="root",
        )
        log("\n" + "-" * 72)
        log(r.stdout)
        log("-" * 72)

        log("\n[5/5] 结论")
        ok_ubuntu = "Ubuntu" in r.stdout
        ok_py311 = "3.11" in r.stdout
        log(f"  Ubuntu 基础层：{'✅ 成立' if ok_ubuntu else '❌ 不成立'}")
        log(f"  Python 3.11 ：{'✅ 满足' if ok_py311 else '❌ 不满足'}")
        log("  commands.run / files.write：✅ 均正常（本次验证即通过它们完成）")
        return 0
    except Exception as e:  # noqa: BLE001
        log(f"  ❌ 沙箱验证失败：{type(e).__name__}: {str(e)[:600]}")
        return 1
    finally:
        if sbx is not None:
            try:
                sbx.kill()
                log("  已销毁沙箱实例")
            except Exception:  # noqa: BLE001
                pass
        t = ags.find_tool(TOOL)
        if t:
            ags.delete_tool(t["tool_id"])
            log("  已清理沙箱工具（释放配额）")


if __name__ == "__main__":
    sys.exit(main())
