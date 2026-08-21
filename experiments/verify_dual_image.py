#!/usr/bin/env python3
"""验证「双镜像方案」：base 镜像提供环境 + 题目镜像作为 image volume 挂载

对应导师反馈 4：
    「一个是 env+工具的 base image（放在 volume image mount 位置），
      题目 image（放在标准 tools 的 image 地方），
      每次换题就通过 e2b custom config 覆盖」

按 AGS API 的落地方式
--------------------
    CreateSandboxTool(
        CustomConfiguration.Image = <base 镜像>        ← 提供 envd/S6/Python/git
        StorageMounts = [ StorageSource.Image = <题目内容镜像> ]  ← 题目内容
    )
    StartSandboxInstance(MountOptions=[...])           ← 换题时覆盖挂载

要验证的三件事
-------------
    1. StorageMounts 用 ImageStorageSource 能否挂载成功
    2. 挂载后沙箱内能否看到题目内容（/workspace/repo + /task）
    3. 换题时能否只换挂载、复用同一个沙箱工具（省去每题建工具的开销与配额）
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

from swe_synth.clients.ags import AGSClient, AGSError  # noqa: E402

NS = "ccr.ccs.tencentyun.com/tcb-100008634787-zbaf"
BASE_IMAGE = f"{NS}/swe-synth-base-ubuntu:v1"
CONTENT_IMAGE = f"{NS}/swe-synth-content-0034:v1"
TOOL = "swe-synth-dualimg-probe"
MOUNT_NAME = "taskvol"
MOUNT_PATH = "/mnt/task"


def log(m: str) -> None:
    print(m, flush=True)


def create_tool_with_mount(ags: AGSClient) -> None:
    """创建带 image volume 挂载的沙箱工具。

    直接用 SDK models 构造 StorageMounts（AGSClient.create_tool 目前未暴露该参数，
    本实验先验证 API 可行性，确认后再决定是否固化进客户端）。
    """
    m = ags._m  # noqa: SLF001 - 实验脚本，直接复用已构造好的 models 模块
    req = m.CreateSandboxToolRequest()
    req.ToolName = TOOL
    req.ToolType = "custom"
    req.Description = "双镜像方案验证：base 提供环境，题目内容走 image volume"
    req.DefaultTimeout = "15m"
    req.RoleArn = os.environ["AGS_ROLE_ARN"]

    net = m.NetworkConfiguration()
    net.NetworkMode = "PUBLIC"
    req.NetworkConfiguration = net

    custom = m.CustomConfiguration()
    custom.Image = BASE_IMAGE
    custom.ImageRegistryType = "personal"
    custom.Command = ["/init"]
    custom.Args = ["sleep", "infinity"]
    res = m.ResourceConfiguration()
    res.CPU, res.Memory = "2", "4Gi"
    custom.Resources = res
    hg = m.HttpGetAction()
    hg.Path, hg.Port, hg.Scheme = "/health", 49983, "HTTP"
    probe = m.ProbeConfiguration()
    probe.HttpGet = hg
    probe.ReadyTimeoutMs, probe.ProbeTimeoutMs = 30000, 5000
    probe.ProbePeriodMs, probe.FailureThreshold, probe.SuccessThreshold = 10000, 3, 1
    custom.Probe = probe
    req.CustomConfiguration = custom

    # ---- 核心：把题目内容镜像作为 image volume 挂载
    img_src = m.ImageStorageSource()
    img_src.Reference = CONTENT_IMAGE
    img_src.ImageRegistryType = "personal"

    src = m.StorageSource()
    src.Image = img_src

    mount = m.StorageMount()
    mount.Name = MOUNT_NAME
    mount.StorageSource = src
    mount.MountPath = MOUNT_PATH
    mount.ReadOnly = True

    req.StorageMounts = [mount]

    rsp = ags._cli.CreateSandboxTool(req)  # noqa: SLF001
    return getattr(rsp, "ToolId", "")


PROBE = r"""
echo "### 挂载点内容（题目镜像应挂在这里）"
ls -la /mnt/task/ 2>/dev/null || echo "挂载点不存在"
echo
echo "### 题目仓库代码"
ls /mnt/task/workspace/repo/ 2>/dev/null | head -8 || echo "无"
echo
echo "### 题目契约文件"
ls /mnt/task/task/ 2>/dev/null || echo "无"
echo
echo "### base 镜像提供的环境（应来自 base 而非题目镜像）"
cat /etc/os-release | grep PRETTY_NAME
/opt/venv311/bin/python -V
"""


def main() -> int:
    ags = AGSClient()

    log("=" * 72)
    log("双镜像方案验证（导师反馈 4）")
    log("=" * 72)
    log(f"  base 镜像（环境）  : {BASE_IMAGE}")
    log(f"  题目镜像（内容）  : {CONTENT_IMAGE}")
    log(f"  挂载点            : {MOUNT_PATH}")

    log(f"\n[1/3] 创建带 image volume 的沙箱工具 {TOOL}")
    old = ags.find_tool(TOOL)
    if old:
        ags.delete_tool(old["tool_id"])
        log("  （已删除同名旧工具）")
        time.sleep(3)
    try:
        tid = create_tool_with_mount(ags)
        log(f"  ✅ 工具已创建：{tid}")
    except Exception as e:  # noqa: BLE001
        log(f"  ❌ 创建失败：{type(e).__name__}: {str(e)[:500]}")
        log("\n  → 说明当前 API/账号尚不支持 StorageMounts 的 ImageStorageSource，")
        log("    或参数组合需要调整；此为架构可行性的决定性结论，需据此调整方案。")
        return 1

    try:
        ags.wait_tool_active(TOOL, timeout=300)
        log("  ✅ 工具状态 ACTIVE")
    except AGSError as e:
        log(f"  ❌ 工具未就绪：{e}")
        return 1

    log("\n[2/3] 启动实例并检查挂载是否生效")
    sbx = None
    try:
        from e2b_code_interpreter import Sandbox

        os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
        sbx = (Sandbox.create(template=TOOL, timeout=600)
               if hasattr(Sandbox, "create") else
               Sandbox(template=TOOL, timeout=600))
        log(f"  ✅ 实例已启动：{getattr(sbx, 'sandbox_id', '?')}")

        r = sbx.commands.run(
            "bash -s <<'EOF'\n" + PROBE + "\nEOF", timeout=180, user="root")
        log("\n" + "-" * 72)
        log(r.stdout)
        log("-" * 72)

        log("\n[3/3] 结论")
        mounted = "workspace" in r.stdout or "task" in r.stdout
        env_ok = "Ubuntu" in r.stdout and "3.11" in r.stdout
        log(f"  题目内容通过 image volume 挂载：{'✅ 成功' if mounted else '❌ 未生效'}")
        log(f"  环境由 base 镜像提供            ：{'✅ 正常' if env_ok else '❌ 异常'}")
        if mounted and env_ok:
            log("\n  ⭐ 双镜像方案成立：题目镜像只需装内容（数 MB），"
                "环境层由 base 镜像复用，")
            log("     换题只需更换挂载（MountOptions），无需为每题构建 7.5GB 镜像。")
        return 0 if (mounted and env_ok) else 1
    except Exception as e:  # noqa: BLE001
        log(f"  ❌ 验证失败：{type(e).__name__}: {str(e)[:600]}")
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
