#!/usr/bin/env python3
"""验证「复用同一个沙箱工具、只在启动实例时切换镜像」是否真的可行。

背景 / 为什么要补这个实验
--------------------------
`verify_dual_image.py`（反馈 4 的第一版验证）把方向搞反了：

    CreateSandboxTool(
        CustomConfiguration.Image = <base 镜像>          ← 环境层，固定
        StorageMounts = [ ImageStorageSource(<题目内容镜像>) ]  ← 题目内容
    )

但直接查 SDK 模型字段（`tencentcloud.ags.v20250920.models`）发现：

    MountOption(Name, MountPath, SubPath, ReadOnly)      ← 没有 Reference 字段！
    CustomConfiguration(Image, ...)                       ← 有 Image 字段

也就是说：
  - StorageMounts 挂载的镜像地址在 `CreateSandboxTool` 时就写死了，
    `StartSandboxInstance` 的 `MountOptions` 只能微调路径/只读属性，
    **换不了挂载卷背后指向的镜像**。
  - 只有 `CustomConfiguration.Image` 能在 `StartSandboxInstance` 时
    按实例整体覆盖。

这与最初的架构设想完全吻合：「题目 image 放在标准 tools 的 image 地方，
每次换题就通过 custom config 覆盖」——换的应该是 `CustomConfiguration.Image`，
而不是 `StorageMounts`。

更进一步的推论：既然 AGS 沙箱要求主镜像里必须有 envd/S6（否则起不来），
「题目镜像」本身也得继承 base（`FROM base + COPY 题目内容`），
这恰好是反馈 1 已经做到的「每题 1.09MB 内容层 + 872MB 共享 base 层」——
根本不需要 StorageMounts/ImageStorageSource 这个额外机制，
用普通 Docker 分层 + 实例级 CustomConfiguration 覆盖即可。

本实验直接验证这个更简单的方案：
  1. 只创建 **一个** 沙箱工具（工具级 Image 随便填一个默认值）
  2. 第一次 StartSandboxInstance：不覆盖 → 应该拿到工具默认镜像的内容
  3. 第二次 StartSandboxInstance（**同一个** 工具，中间不调用
     CreateSandboxTool）：覆盖 CustomConfiguration.Image = 另一道题的镜像
     → 应该拿到不同的题目内容
  4. 全程只有 1 个工具存在（不占用额外配额）
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
IMAGE_A = f"{NS}/swe-synth-0034:v1"   # 工具创建时的默认镜像（题目 A）
IMAGE_B = f"{NS}/swe-synth-0007:v1"   # 实例级覆盖用的镜像（题目 B）
TOOL = "swe-synth-cc-switch-probe"
DOMAIN = "ap-guangzhou.tencentags.com"

PROBE = r"""
echo "### 环境（应来自共享 base 层，两次应完全一致）"
cat /etc/os-release | grep PRETTY_NAME
echo
echo "### 题目仓库内容（应随实例切换而不同）"
ls /workspace/repo/ 2>/dev/null | head -6 || echo "无 /workspace/repo"
echo
echo "### 题目元数据"
cat /task/metadata.json 2>/dev/null | head -c 300 || echo "无 /task/metadata.json"
"""


def log(m: str) -> None:
    print(m, flush=True)


def build_custom_configuration(ags: AGSClient, image: str):
    m = ags._m  # noqa: SLF001
    custom = m.CustomConfiguration()
    custom.Image = image
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
    return custom


def start_instance(ags: AGSClient, tool_id: str, override_image: str | None):
    m = ags._m  # noqa: SLF001
    req = m.StartSandboxInstanceRequest()
    req.ToolId = tool_id
    req.Timeout = "10m"
    if override_image:
        req.CustomConfiguration = build_custom_configuration(ags, override_image)
    rsp = ags._cli.StartSandboxInstance(req)  # noqa: SLF001
    inst = rsp.Instance
    return inst.InstanceId, getattr(inst.CustomConfiguration, "Image", None)


def stop_instance(ags: AGSClient, instance_id: str) -> None:
    m = ags._m  # noqa: SLF001
    req = m.StopSandboxInstanceRequest()
    req.InstanceId = instance_id
    ags._cli.StopSandboxInstance(req)  # noqa: SLF001


def run_probe(instance_id: str) -> str:
    from e2b_code_interpreter import Sandbox

    os.environ.setdefault("E2B_DOMAIN", DOMAIN)
    sbx = Sandbox(sandbox_id=instance_id)
    try:
        r = sbx.commands.run(
            "bash -s <<'EOF'\n" + PROBE + "\nEOF", timeout=120, user="root")
        return r.stdout
    finally:
        pass  # 不 kill：由 StopSandboxInstance 统一回收


def main() -> int:
    ags = AGSClient()

    log("=" * 72)
    log("验证：同一沙箱工具，仅靠实例级 CustomConfiguration.Image 覆盖换题")
    log("=" * 72)
    log(f"  工具默认镜像（题目 A）: {IMAGE_A}")
    log(f"  实例覆盖镜像（题目 B）: {IMAGE_B}")

    old = ags.find_tool(TOOL)
    if old:
        ags.delete_tool(old["tool_id"])
        log("（已删除同名旧工具）")
        time.sleep(3)

    log(f"\n[1/4] 创建工具 {TOOL}（默认镜像 = 题目 A）")
    try:
        tool_id = ags.create_tool(
            TOOL, IMAGE_A,
            role_arn=os.environ["AGS_ROLE_ARN"],
            description="验证实例级镜像覆盖是否可行（复用同一工具切题）",
        )
        log(f"  ✅ 工具已创建：{tool_id}")
    except Exception as e:  # noqa: BLE001
        log(f"  ❌ 创建失败：{type(e).__name__}: {str(e)[:400]}")
        return 1

    try:
        ags.wait_tool_active(TOOL, timeout=300)
        log("  ✅ 工具状态 ACTIVE")
    except AGSError as e:
        log(f"  ❌ 工具未就绪：{e}")
        return 1

    inst_a = inst_b = None
    try:
        log("\n[2/4] 启动实例 #1（不覆盖，应得到题目 A 的内容）")
        inst_a, img_a = start_instance(ags, tool_id, override_image=None)
        log(f"  实例 ID: {inst_a}  |  生效镜像: {img_a}")
        time.sleep(5)
        out_a = run_probe(inst_a)
        log("-" * 60)
        log(out_a)
        log("-" * 60)

        log("\n[3/4] 启动实例 #2（同一工具 ID，覆盖为题目 B，不重建工具）")
        inst_b, img_b = start_instance(ags, tool_id, override_image=IMAGE_B)
        log(f"  实例 ID: {inst_b}  |  生效镜像: {img_b}")
        time.sleep(5)
        out_b = run_probe(inst_b)
        log("-" * 60)
        log(out_b)
        log("-" * 60)

        tools_now = [t for t in ags.list_tools() if t["name"] == TOOL]
        log(f"\n[4/4] 结论核对（当前同名工具数：{len(tools_now)}，应为 1）")

        same_tool = len(tools_now) == 1
        different_content = (out_a.strip() != out_b.strip()) and out_a and out_b
        same_env = ("PRETTY_NAME" in out_a and "PRETTY_NAME" in out_b)

        log(f"  全程只用了 1 个工具（未因换题新建工具）：{'✅' if same_tool else '❌'}")
        log(f"  两次实例内容确实不同（真正换题）      ：{'✅' if different_content else '❌'}")
        log(f"  两次环境层输出均正常（base 层复用）    ：{'✅' if same_env else '❌'}")

        ok = same_tool and different_content and same_env
        if ok:
            log("\n  ⭐ 结论成立：只需 CustomConfiguration.Image 的实例级覆盖，")
            log("     即可在复用同一沙箱工具的前提下切换题目内容，")
            log("     不需要 StorageMounts/ImageStorageSource 这个额外机制。")
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        log(f"  ❌ 验证失败：{type(e).__name__}: {str(e)[:600]}")
        return 1
    finally:
        for iid in (inst_a, inst_b):
            if iid:
                try:
                    stop_instance(ags, iid)
                    log(f"  已回收实例 {iid}")
                except Exception:  # noqa: BLE001
                    pass
        t = ags.find_tool(TOOL)
        if t:
            ags.delete_tool(t["tool_id"])
            log("  已清理沙箱工具（释放配额）")


if __name__ == "__main__":
    sys.exit(main())
