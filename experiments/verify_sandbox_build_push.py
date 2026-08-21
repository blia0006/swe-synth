#!/usr/bin/env python3
"""验证沙箱内「构建 + 推送」闭环（buildah 无守护进程方案）

前一轮已确认沙箱内 buildah 可安装且能构建镜像。本轮验证最后一环：
能否从沙箱内直接 push 到 CCR —— 这决定「出题+打包全在沙箱内」是否成立。

凭证处理
--------
凭证通过 AGS 的 Env 参数注入（不进镜像、不落盘），
对应 CustomConfiguration.Env，是官方支持的方式。
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
TOOL = "swe-synth-buildpush-probe"
TARGET = "ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-sandboxbuilt:v1"


def create_tool_with_env(ags: AGSClient) -> None:
    """创建带 Env 注入的沙箱工具（凭证走 Env，不进镜像）。"""
    m = ags._m  # noqa: SLF001
    req = m.CreateSandboxToolRequest()
    req.ToolName = TOOL
    req.ToolType = "custom"
    req.Description = "验证沙箱内 buildah 构建并推送到 CCR"
    req.DefaultTimeout = "30m"
    req.RoleArn = os.environ["AGS_ROLE_ARN"]

    net = m.NetworkConfiguration()
    net.NetworkMode = "PUBLIC"
    req.NetworkConfiguration = net

    custom = m.CustomConfiguration()
    custom.Image = IMAGE
    custom.ImageRegistryType = "personal"
    custom.Command = ["/init"]
    custom.Args = ["sleep", "infinity"]

    # ⭐ 凭证经 Env 注入：镜像内不含任何密钥
    #    S6_KEEP_ENV=1 是官方要求（否则 S6-Overlay 会清掉环境变量）
    envs = []
    for k in ("TCR_USERNAME", "TCR_PASSWORD"):
        e = m.EnvVar()
        e.Name, e.Value = k, os.environ[k]
        envs.append(e)
    keep = m.EnvVar()
    keep.Name, keep.Value = "S6_KEEP_ENV", "1"
    envs.append(keep)
    custom.Env = envs

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

    ags._cli.CreateSandboxTool(req)  # noqa: SLF001


PROBE = r"""
set -uo pipefail
echo "=== 0. 凭证是否成功经 Env 注入 ==="
[ -n "${TCR_USERNAME:-}" ] && echo "TCR_USERNAME: 已注入" || echo "TCR_USERNAME: 缺失"
[ -n "${TCR_PASSWORD:-}" ] && echo "TCR_PASSWORD: 已注入（值不打印）" || echo "TCR_PASSWORD: 缺失"

echo
echo "=== 1. 安装 buildah ==="
apt-get update -qq 2>&1 | tail -1
apt-get install -y -qq buildah 2>&1 | tail -1
buildah --version

echo
echo "=== 2. 模拟出题：clone 仓库 + 造内容镜像 ==="
cd /tmp
git clone -q --depth 1 https://github.com/psf/cachecontrol.git repo 2>&1
mkdir -p ctx/workspace ctx/task
cp -r repo ctx/workspace/repo
echo '{"task_id":"sandbox-built-demo","note":"built inside AGS sandbox"}' > ctx/task/metadata.json
printf 'FROM scratch\nCOPY workspace/ /workspace/\nCOPY task/ /task/\n' > ctx/Dockerfile
echo "构建上下文体积: $(du -sh ctx | cut -f1)"

echo
echo "=== 3. buildah 构建 ==="
cd /tmp/ctx
buildah bud --format docker -t TARGET_PLACEHOLDER . 2>&1 | tail -4

echo
echo "=== 4. 登录 CCR 并推送 ==="
echo "$TCR_PASSWORD" | buildah login --username "$TCR_USERNAME" --password-stdin ccr.ccs.tencentyun.com 2>&1 | tail -2
buildah push TARGET_PLACEHOLDER 2>&1 | tail -4
echo "push 退出码: $?"
"""


def main() -> int:
    ags = AGSClient()
    print("=" * 72)
    print("沙箱内「构建 + 推送」闭环验证（buildah，无需 DinD）")
    print("=" * 72)

    old = ags.find_tool(TOOL)
    if old:
        ags.delete_tool(old["tool_id"])
        time.sleep(3)
    create_tool_with_env(ags)
    ags.wait_tool_active(TOOL, timeout=300)
    print("  ✅ 工具就绪（凭证经 Env 注入，镜像内无密钥）")

    sbx = None
    try:
        from e2b_code_interpreter import Sandbox

        os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
        sbx = (Sandbox.create(template=TOOL, timeout=1800)
               if hasattr(Sandbox, "create") else
               Sandbox(template=TOOL, timeout=1800))
        print(f"  ✅ 实例已启动：{getattr(sbx, 'sandbox_id', '?')}\n")

        script = PROBE.replace("TARGET_PLACEHOLDER", TARGET)
        # 凭证走 commands.run 的 envs 参数（运行时注入，不进镜像、不落盘）。
        # 实测：AGS CreateSandboxTool 的 CustomConfiguration.Env 未能传到
        # commands.run 的执行环境（S6-Overlay 的 with-contenv 作用域问题），
        # 因此改用 e2b SDK 的 envs —— 这也是更合适的做法：
        # 凭证与「工具定义」解耦，同一个工具可服务不同凭证的调用方。
        r = sbx.commands.run(
            "bash -s <<'PEOF'\n" + script + "\nPEOF",
            timeout=1500, user="root",
            envs={
                "TCR_USERNAME": os.environ["TCR_USERNAME"],
                "TCR_PASSWORD": os.environ["TCR_PASSWORD"],
            },
        )
        print("-" * 72)
        print(r.stdout)
        if r.stderr:
            print("[stderr]", r.stderr[-600:])
        print("-" * 72)

        ok = "Writing manifest" in r.stdout or "Storing signatures" in r.stdout
        print(f"\n结论：沙箱内构建并推送镜像 → {'✅ 成立' if ok else '❌ 不成立'}")
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
