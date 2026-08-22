#!/usr/bin/env python3
"""把整条流水线（出题 → 打包 → 沙箱验证 → 校验）都收进同一个 AGS 沙箱实例里跑。

背景
----
之前的架构是「本地出题 + 远端构建机 build/push + 沙箱只跑判分」——这个
拆分本身没有违反平台能力（docker build 无论如何都不能在沙箱内跑，见下），
但导师要求的是「不要在本地做这件事」，所以本脚本把本地这一半也收进沙箱：

    本地（这个脚本）── 只做「创建沙箱实例、上传源码、发起远程命令、
                        回收实例」这类编排动作，不跑任何业务逻辑
    沙箱实例内     ── clone 仓库 / AST 分析 / 调 LLM 出题 / 本地 pytest 预检
                        (agent1) → buildah build+push 镜像 (pack) →
                        起另一批临时沙箱做双向判分 (agent2) → 校验 (validate)
                        全部在这一个沙箱实例的 commands.run 里执行

关键限制与应对
--------------
沙箱运行时不提供 docker.sock / 特权容器（DinD 不通，已实测确认），因此
`pack` 这一步不能用 docker，而是用 buildah（无需 daemon 的 OCI 构建工具，
已在真实沙箱里验证过安装、build、push 全部成功，
见 experiments/verify_sandbox_build_push.py）；`swe_synth/agent1/packer.py`
已经支持自动探测并退化到 buildah，这里只需在沙箱里 apt 装上它即可。

凭证经 `commands.run` 的 `envs=` 参数逐次注入（不进镜像、不落盘、不写入
沙箱工具定义），用完即弃。

运行模式：后台脱离（断网也不丢进度）
----------------------------------
本脚本只负责「建实例→上传源码→装依赖→把流水线以 nohup+setsid 方式
甩到沙箱后台跑起来」，起完就退出，**不会同步阻塞等待流水线跑完**。
流水线本身运行在沙箱侧的 envd 里，跟本机的网络连接状态完全无关——
本机断网、合盖、进程被杀，都不影响沙箱内任务继续跑。

用法
----
    python scripts/run_in_sandbox.py --n 10                      # 起环境 + 后台启动流水线，立即返回
    python scripts/run_in_sandbox.py --n 10 --stages agent1,pack,agent2,validate
    python scripts/run_in_sandbox.py --stages setup --keep       # 只搭环境，留着调试

启动后查看进度 / 下载产出 / 回收实例，用配套的：
    python scripts/sandbox_status.py --instance <instance_id>
"""

from __future__ import annotations

import argparse
import base64
import os
import shlex
import sys
import tarfile
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

# e2b 2.x 默认强制校验 API Key 前缀（"e2b_"），腾讯云 AGS 的 Key 是
# "ark_xxx" 格式会被拒绝；这个开关只跳过格式校验，不影响鉴权本身，
# 必须在 import e2b 系列包之前设置生效（与 agent2/sandbox_runner.py 一致）。
os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")
os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")

from swe_synth.clients.ags import AGSClient  # noqa: E402
from swe_synth.config.loader import load_settings  # noqa: E402

# 优先用控制台里手动创建、已验证可用的共享工具（SWE_SYNTH_SHARED_TOOL，
# 如 aziz-sandbox）；只有完全没配置时才退化到脚本自动建一个同名工具，
# 避免像之前那样因为区域不匹配而悄悄新建出一个「影子工具」。
BUILDER_TOOL_NAME = (
    os.environ.get("SWE_SYNTH_SHARED_TOOL")
    or os.environ.get("SWE_SYNTH_BUILDER_TOOL")
    or "swe-synth-builder"
)
REMOTE_ROOT = "/root/swe_synth"
PIPELINE_LOG = f"{REMOTE_ROOT}/pipeline.log"
PIPELINE_STATUS = f"{REMOTE_ROOT}/pipeline.status"
PIPELINE_SCRIPT = f"{REMOTE_ROOT}/_pipeline_wrapper.sh"

# 打包上传时排除：大/无用/含本机状态或密钥的目录，不需要也不应该带进沙箱
EXCLUDE_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".cache", "data", "dist",
    "node_modules", ".codebuddy", ".pytest_cache", "brain", ".DS_Store",
    ".env",  # 凭证只经 envs= 注入，绝不随源码打包上传
}

# 需要转发进沙箱执行环境的变量（经 commands.run 的 envs=，不进镜像、不落盘）
FORWARD_ENV_KEYS = [
    "TOKENHUB_API_KEY", "TOKENHUB_API_KEYS", "TOKENHUB_BASE_URL", "TOKENHUB_MODEL",
    "E2B_API_KEY", "E2B_DOMAIN", "E2B_VALIDATE_API_KEY",
    "TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_REGION",
    "AGS_ROLE_ARN",
    "TCR_REGISTRY", "TCR_NAMESPACE", "TCR_USERNAME", "TCR_PASSWORD", "TCR_REGISTRY_TYPE",
    "GITHUB_TOKEN",
    "SWE_SYNTH_SHARED_TOOL", "SWE_SYNTH_BASE_MOUNT_PATH", "SWE_SYNTH_BUILDER_TOOL",
]


def _pack_source_b64() -> str:
    """把项目源码打包成内存 tar.gz 并转 base64（文本形式上传最稳妥，
    避免依赖 SDK 对 files.write 二进制参数的支持情况）。"""
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(ROOT.iterdir()):
            if item.name in EXCLUDE_NAMES or (item.name.startswith(".") and item.name not in {".env.example"}):
                continue
            tar.add(item, arcname=item.name)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _ensure_builder_tool(ags: AGSClient, settings) -> str:
    """确保「流水线编排沙箱工具」存在。镜像用共享 base 镜像即可——它已经
    自带 git + python3.11 venv + uv（见 swe_synth/agent1/base_image/Dockerfile），
    足够跑整条流水线，buildah 现场 apt 装一次。"""
    existing = ags.find_tool(BUILDER_TOOL_NAME)
    if existing:
        if str(existing.get("status", "")).upper() != "ACTIVE":
            ags.wait_tool_active(BUILDER_TOOL_NAME, timeout=180)
        return existing["tool_id"]
    base_image = settings.get("image.base")
    tool_id = ags.create_tool(
        BUILDER_TOOL_NAME, base_image,
        description="SWE-Synth 流水线编排沙箱：出题+打包+验证全部在实例内执行，不在本地跑",
        default_timeout="60m",
        storage="20Gi",  # 默认 1Gi 装完 base 镜像就快满了，pip/git/buildah 需要落盘空间
    )
    ags.wait_tool_active(BUILDER_TOOL_NAME, timeout=180)
    return tool_id


def _forward_envs(extra: dict[str, str] | None = None) -> dict[str, str]:
    envs = {k: os.environ[k] for k in FORWARD_ENV_KEYS if os.environ.get(k)}
    if extra:
        envs.update(extra)
    return envs


def _run(sbx, cmd: str, *, timeout: int, cwd: str | None = None, label: str = "") -> int:
    """在沙箱里跑一条命令，实时打印结果；返回退出码（0=成功）。"""
    full_cmd = f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd
    print(f"\n{'=' * 72}\n▶ {label or cmd}\n{'=' * 72}")
    t0 = time.time()
    try:
        r = sbx.commands.run(
            "bash -lc " + shlex.quote(full_cmd),
            timeout=timeout, user="root", envs=_forward_envs(),
        )
        print(r.stdout[-8000:])
        if r.stderr:
            print("[stderr]", r.stderr[-2000:])
        print(f"✅ 完成（{time.time() - t0:.0f}s）")
        return 0
    except Exception as e:  # noqa: BLE001  CommandExitException 或其它 SDK 异常
        stdout = getattr(e, "stdout", "") or ""
        stderr = getattr(e, "stderr", "") or str(e)
        exit_code = getattr(e, "exit_code", 1)
        if stdout:
            print(stdout[-8000:])
        print("[stderr]", str(stderr)[-3000:])
        print(f"❌ 失败（exit={exit_code}，{time.time() - t0:.0f}s）")
        return exit_code if isinstance(exit_code, int) and exit_code else 1


def _backup_local_data(local_root: Path) -> None:
    """下载覆盖前先备份本地已有 data/ 目录（若存在），防止误覆盖历史交付数据。"""
    data_dir = local_root / "data"
    if not data_dir.exists():
        return
    import shutil
    backup = local_root / f"data.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(data_dir, backup)
    print(f"\n（下载前已备份本地现有 data/ 到 {backup.name}/，避免误覆盖历史数据）")


def _build_pipeline_script(stages: list[str], n: int) -> str:
    """生成一份合并了 agent1/pack/agent2/validate 的 bash 脚本，供后台
    (setsid nohup) 方式在沙箱内独立跑完，不依赖本机连接存活。每个阶段
    开始/失败都写入 PIPELINE_STATUS，方便随时用 sandbox_status.py 查看。"""
    py = "/opt/venv311/bin/python"
    stage_cmds = {
        "agent1": f"{py} scripts/run_pipeline.py agent1 --n {n}",
        "pack": f"{py} scripts/run_pipeline.py pack",
        "agent2": f"{py} scripts/run_pipeline.py agent2",
        "validate": f"{py} scripts/run_pipeline.py validate",
    }
    lines = [
        "#!/bin/bash",
        "set -uo pipefail",
        f"cd {shlex.quote(REMOTE_ROOT)}",
        f"STATUS_FILE={shlex.quote(PIPELINE_STATUS)}",
        'echo "RUNNING stage=start" > "$STATUS_FILE"',
        "",
    ]
    for st in stages:
        if st == "setup" or st not in stage_cmds:
            continue
        lines += [
            f'echo "" ; echo "===== STAGE: {st} ($(date -Is)) ====="',
            f'echo "RUNNING stage={st}" > "$STATUS_FILE"',
            stage_cmds[st],
            "rc=$?",
            "if [ $rc -ne 0 ]; then",
            f'  echo "FAILED stage={st} rc=$rc" > "$STATUS_FILE"',
            "  exit $rc",
            "fi",
            "",
        ]
    lines.append('echo "ALL_DONE ($(date -Is))" > "$STATUS_FILE"')
    return "\n".join(lines) + "\n"


def _launch_pipeline_background(sbx, stages: list[str], n: int) -> None:
    """把流水线脚本交给沙箱侧 envd 以 background 模式托管执行，本次调用
    立即返回（不等流水线跑完）。envd 托管的后台进程与「发起这次调用的
    RPC 连接」生命周期无关——本机断网、关终端都不影响它继续跑；进度写入
    PIPELINE_LOG/STATUS，用 scripts/sandbox_status.py 随时查看（哪怕换一
    台机器重新连接同一个 instance_id）。

    注意：这里不能用 shell 层 `cmd & disown` 的土办法让 envd 的 `run()`
    「以为」命令已经退出——SDK 的 background=False 默认行为是同步等待
    「命令真正退出」这个事件流，我们让 bash 立即退出会导致 RPC 流提前
    结束，命中 30s/60s 超时报错（已实测复现）。正确做法是显式传
    `background=True`：SDK 直接返回一个 CommandHandle，不等待、不阻塞，
    命令本身继续由 envd 在沙箱内独立运行。
    """
    script = _build_pipeline_script(stages, n)
    sbx.files.write(PIPELINE_SCRIPT, script, user="root")
    launch_cmd = (
        f"chmod +x {shlex.quote(PIPELINE_SCRIPT)} && "
        f"bash {shlex.quote(PIPELINE_SCRIPT)} > {shlex.quote(PIPELINE_LOG)} 2>&1"
    )
    handle = sbx.commands.run(
        "bash -lc " + shlex.quote(launch_cmd),
        background=True, timeout=0, user="root", envs=_forward_envs(),
    )
    print(f"LAUNCHED（envd pid={getattr(handle, 'pid', '?')}）")


def _download_results(sbx, local_root: Path) -> None:
    """把沙箱内 data/ 目录逐个文本文件读回本地（tasks.jsonl / report.json /
    proofs/*.json 全是文本，不涉及二进制，直接 files.read 即可，不需要
    额外打包/解 base64 这一步）。

    `local_root` 是本地项目根（不是 data/ 本身）——远程文件相对路径本身
    就带 "data/…" 前缀，拼接时不能再多套一层 data/，否则会写进
    `data/data/…`。
    """
    local_out = local_root
    local_out.mkdir(parents=True, exist_ok=True)
    r = sbx.commands.run(
        f"find {REMOTE_ROOT}/data -type f 2>/dev/null || true",
        timeout=30, user="root",
    )
    remote_files = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not remote_files:
        print("（沙箱内 data/ 目录为空，没有可下载的产出）")
        return
    print(f"\n下载产出（{len(remote_files)} 个文件）…")
    for rf in remote_files:
        rel = rf[len(REMOTE_ROOT) + 1:]
        local_path = local_out / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = sbx.files.read(rf, user="root")
            mode = "w" if isinstance(content, str) else "wb"
            with open(local_path, mode) as f:
                f.write(content)
            print(f"  ✅ {rel}")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {rel} 下载失败：{e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=10, help="agent1 目标出题数")
    ap.add_argument("--stages", default="setup,agent1,pack,agent2,validate",
                     help="逗号分隔：setup,agent1,pack,agent2,validate")
    ap.add_argument("--keep", action="store_true", help="setup 跑完不回收沙箱实例（调试用；有后台阶段时天然不回收，此项无意义）")
    ap.add_argument("--instance-timeout", default="360m",
                     help="StartSandboxInstance 的 Timeout，格式 <N>m（分钟）。"
                          "沙箱实例本身的存活上限，后台流水线可能跑数小时，默认给 360m(6h)，"
                          "不够可传 --instance-timeout 720m")
    ap.add_argument("--foreground", action="store_true",
                     help="不使用后台模式，同步阻塞等待每个阶段跑完（旧行为，调试用）")
    args = ap.parse_args()
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    background_stages = [s for s in stages if s in ("agent1", "pack", "agent2", "validate")]

    from e2b_code_interpreter import Sandbox

    settings = load_settings()
    ags = AGSClient()

    print("=" * 72)
    print("整条流水线在沙箱实例内执行（本地只做编排，不跑任何业务逻辑）")
    print("=" * 72)

    tool_id = _ensure_builder_tool(ags, settings)
    print(f"✅ 沙箱工具就绪：{BUILDER_TOOL_NAME} ({tool_id})")

    instance_id, _ = ags.start_instance(tool_id, timeout=args.instance_timeout)
    print(f"✅ 沙箱实例已启动：{instance_id}")
    sbx = Sandbox.connect(instance_id)

    rc = 0
    try:
        if "setup" in stages:
            print("\n打包项目源码（排除 .git/.venv/data 等）…")
            b64 = _pack_source_b64()
            print(f"  体积（base64 后）：{len(b64) / 1024 / 1024:.1f}MB")
            sbx.files.write(f"{REMOTE_ROOT}.tar.gz.b64", b64, user="root")
            rc = _run(
                sbx,
                f"mkdir -p {REMOTE_ROOT} && base64 -d {REMOTE_ROOT}.tar.gz.b64 "
                f"> {REMOTE_ROOT}.tar.gz && tar xzf {REMOTE_ROOT}.tar.gz -C {REMOTE_ROOT}",
                timeout=120, label="上传并解包源码",
            )
            if rc:
                return rc
            rc = _run(
                sbx, "/opt/venv311/bin/python -m pip install -q -r requirements.txt",
                timeout=900, cwd=REMOTE_ROOT, label="安装 Python 依赖",
            )
            if rc:
                return rc
            rc = _run(
                sbx,
                # 静态 podman（5.8.4，GitHub release，走加速镜像更稳）：
                # apt 仓库自带的 buildah 1.23.1 命中过一个 containers/image
                # 老版本 bug（某些 base 镜像 layer manifest 转换失败），
                # 实测这个静态 podman 才是真正 build+push 通的方案
                # （见 experiments/verify_sandbox_build_push.py）。
                "cd /tmp && "
                "( curl --connect-timeout 8 -m 60 -fsSL -o podman-static.tar.gz "
                "  https://ghfast.top/https://github.com/mgoltzsche/podman-static/releases/latest/download/podman-linux-amd64.tar.gz "
                "  || curl --connect-timeout 8 -m 60 -fsSL -o podman-static.tar.gz "
                "  https://ghproxy.net/https://github.com/mgoltzsche/podman-static/releases/latest/download/podman-linux-amd64.tar.gz ) && "
                "tar xzf podman-static.tar.gz && "
                "cp -rn podman-linux-amd64/usr/local/* /usr/local/ && "
                "cp -rn podman-linux-amd64/etc/containers/* /etc/containers/ 2>/dev/null || true; "
                "apt-get update -qq && apt-get install -y -qq conmon && "
                "podman --version",
                timeout=300,
                label="安装 podman（沙箱内无 docker daemon，pack 步骤自动改用它 build+push）",
            )
            if rc:
                return rc

        py = "/opt/venv311/bin/python"

        if not background_stages:
            return 0  # 只跑了 setup，没有业务阶段要执行

        if args.foreground:
            # 旧行为：同步阻塞逐阶段跑完（调试用，本机断线会中断）
            stage_cmds = {
                "agent1": (f"{py} scripts/run_pipeline.py agent1 --n {args.n}", 7200, f"Agent1 出题（目标 {args.n} 道）"),
                "pack": (f"{py} scripts/run_pipeline.py pack", 3600, "打包镜像（buildah/podman build + push 到 TCR）"),
                "agent2": (f"{py} scripts/run_pipeline.py agent2", 7200, "Agent2 沙箱双向验证（另起临时沙箱判分）"),
                "validate": (f"{py} scripts/run_pipeline.py validate", 600, "交付前校验"),
            }
            for st in background_stages:
                cmd, timeout, label = stage_cmds[st]
                rc = _run(sbx, cmd, timeout=timeout, cwd=REMOTE_ROOT, label=label)
                if rc:
                    return rc
            _backup_local_data(ROOT)
            _download_results(sbx, ROOT)
            return rc

        # 后台模式（默认）：甩到沙箱后台跑，本机立即返回，不阻塞等待
        print(f"\n以后台方式在沙箱内启动流水线（阶段：{','.join(background_stages)}）…")
        _launch_pipeline_background(sbx, background_stages, args.n)
        print(
            "\n" + "=" * 72 +
            "\n✅ 流水线已在沙箱后台启动，本机可以断网/关闭，不影响它继续运行。\n"
            f"   沙箱实例 ID：{instance_id}\n"
            "   查看进度 / 下载产出 / 完成后回收实例，用：\n"
            f"     python scripts/sandbox_status.py --instance {instance_id}\n"
            f"     python scripts/sandbox_status.py --instance {instance_id} --follow\n"
            "=" * 72
        )
        return 0
    finally:
        # 后台模式下任务仍在运行，绝不能在这里回收实例；只有 setup-only /
        # foreground 模式跑完才按 --keep 决定是否回收。
        if background_stages and not args.foreground:
            pass
        elif args.keep:
            print(f"\n⚠️ --keep 已指定，沙箱实例未回收：{instance_id}")
            print(f"   记得手动清理：python scripts/sandbox_status.py --instance {instance_id} --stop")
        else:
            try:
                ags.stop_instance(instance_id)
                print(f"\n已回收沙箱实例：{instance_id}")
            except Exception as e:  # noqa: BLE001
                print(f"\n⚠️ 回收实例失败，请手动检查/清理：{e}")


if __name__ == "__main__":
    sys.exit(main())
