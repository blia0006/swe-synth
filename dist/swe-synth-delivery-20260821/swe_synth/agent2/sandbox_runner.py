"""Agent2 核心：自定义镜像起沙箱，执行双向验证

对应课题要求（见 TASK-SPEC.md）
------------------------------
    Agent2（验证）：拉取 TCR 镜像 → 在 Agent Sandbox 启动容器 → 执行题目 →
                   验证解的正确性 → 无重叠校验

验证逻辑（双向 sanity 的「云端镜像版」）
--------------------------------------
    A2-1 拉取 TCR 镜像      （Agent1 已 push，见 agent1/packer.py）
    A2-2 自定义镜像起沙箱   （AGS 共享工具 + 实例级镜像覆盖，见下）
    A2-3 空解必须失败       题目镜像 :v1 跑 /task/verify.sh → passed=False
    A2-4 参考解必须通过     答案镜像 :v1-sol 跑 /task/verify.sh --golden → passed=True
    A2-5 无重叠校验         （见 overlap_check.py，依赖 GITHUB_TOKEN）

单工具复用 + 实例级镜像覆盖（双镜像方案的生产接入）
--------------------------------------------------
    旧版做法：每道题创建 2 个沙箱工具（`{task_id}` / `{task_id}-sol`），
    工具数随题目数线性增长，容易顶到账号的沙箱工具配额上限。

    现在：全流水线只维护 **1 个共享工具**（`shared_tool_name`），题目/答案
    的切换通过 `StartSandboxInstance` 的 `CustomConfiguration.Image` 做
    **实例级**覆盖完成，工具本身只在第一次调用时创建一次（已实测验证，
    见 `experiments/verify_customconfig_switch.py`：全程 1 个工具、
    2 次实例分别拿到不同题目内容、环境层输出一致）：
        · 空解验证   → `start_instance(image_override=task.image)`
        · golden 验证 → `start_instance(image_override=task.solution_image)`

    两个镜像各起一个沙箱实例：
    · 题目镜像 :v1      → 空解验证（证明题目「有内容」，不是白给分）
    · 答案镜像 :v1-sol  → golden 验证（证明题目「可解」，答案真的能过判据）

    沙箱按运行时长计费，**无论成败都必须回收**（finally 里 `stop_instance`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from ..clients.ags import AGSClient
from ..schemas.task import SweTask

__all__ = ["VerificationResult", "SandboxVerifyError", "verify_task"]

# 全流水线共享的沙箱工具名：所有题目复用同一个 ToolId，切题靠实例级镜像覆盖，
# 不再随题目数新增工具。可用环境变量覆盖（比如多进程并发跑时按 worker 分开）。
DEFAULT_SHARED_TOOL_NAME = os.environ.get("SWE_SYNTH_SHARED_TOOL", "swe-synth-shared-runner")

# 双镜像方案：base（env+工具，不随题目变化）挂载到这个路径，工具创建时通过
# StorageMounts 固定一次（已实测验证，见 experiments/verify_dual_image_v2.py：
# 挂载卷两次实例都能访问、内容随换题保持不变；题目内容通过下面
# CustomConfiguration.Image 的实例级覆盖正确切换）。题目镜像本身仍 `FROM`
# 共享 base（见 dockerfile_gen.py），这里额外挂载只是让 base 环境在沙箱内
# 也能以「独立、只读、不随题目变化」的路径存在，两种机制并存不冲突。
BASE_ENV_MOUNT_PATH = os.environ.get("SWE_SYNTH_BASE_MOUNT_PATH", "/mnt/base-env")


class SandboxVerifyError(RuntimeError):
    """沙箱验证失败（环境/流程错误，与题目对错无关）。"""


@dataclass
class VerificationResult:
    """Agent2 双向验证的完整结果（含沙箱证据）。"""

    task_id: str
    empty_run: dict[str, Any] | None = None       # 空解的 result.json
    golden_run: dict[str, Any] | None = None      # golden 解的 result.json
    empty_sandbox_id: str = ""                    # 实为 AGS InstanceId
    golden_sandbox_id: str = ""                   # 实为 AGS InstanceId
    passed: bool = False
    reason: str = ""
    duration_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "reason": self.reason,
            "empty_sandbox_id": self.empty_sandbox_id,
            "golden_sandbox_id": self.golden_sandbox_id,
            "empty_run": self.empty_run,
            "golden_run": self.golden_run,
            "duration_sec": self.duration_sec,
        }


def _ags_timeout_str(seconds: int) -> str:
    """把秒数转换成 `StartSandboxInstance` 要求的 `<N>m` 格式（分钟，至少 1）。"""
    minutes = max(1, (int(seconds) + 59) // 60)
    return f"{minutes}m"


# e2b 2.x 默认强制校验 API Key 必须是 "e2b_" + hex 前缀，腾讯云 AGS 的 Key
# 是 "ark_xxx" 格式会被拒绝；2.x 官方留了这个开关跳过纯格式校验（不影响鉴权本身，
# 见 e2b/connection_config.py），必须在 import e2b 系列包之前设置生效。
os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")


def _connect_sandbox(instance_id: str):
    """连接到一个已经由 `AGSClient.start_instance()` 创建好的沙箱实例。

    生产环境走 e2b 2.x：`Sandbox.connect(sandbox_id=...)`（已实测走通，见
    `experiments/verify_dual_image_v2.py`）；`Sandbox(sandbox_id=...)` 的
    1.x 构造分支保留作为兜底（例如环境里意外装回了 1.x）。
    """
    try:
        from e2b_code_interpreter import Sandbox
    except ImportError as e:
        raise SandboxVerifyError("缺少 e2b-code-interpreter：pip install -r requirements.txt") from e

    if hasattr(Sandbox, "connect"):
        return Sandbox.connect(instance_id)
    return Sandbox(sandbox_id=instance_id)


def _ensure_shared_tool(
    ags: AGSClient,
    tool_name: str,
    bootstrap_image: str,
    *,
    role_arn: str | None,
    image_registry_type: str,
    timeout: float,
    force_recreate: bool = False,
    base_image: str | None = None,
) -> str:
    """确保共享沙箱工具存在，返回 ToolId。

    工具自身的默认镜像只起「占位」作用（`bootstrap_image`，通常是第一次
    调用时某道题的题目镜像即可）——每次验证都会用
    `AGSClient.start_instance(image_override=...)` 按实例覆盖成实际的
    题目/答案镜像，因此镜像内容变化不会导致「要删工具重建」，切换发生在
    实例级而不是工具级。`force_recreate=True` 仅用于调试（例如改了
    role_arn/probe/base_image 配置需要让工具级设置生效）。

    `base_image`：双镜像方案里「固定不变」的那一半，工具创建时通过
    `StorageMounts` 挂到 `BASE_ENV_MOUNT_PATH`（已实测验证，见
    `experiments/verify_dual_image_v2.py`）。不传则不挂载（工具仍能正常
    工作，只是少了这层独立、只读的 base 环境挂载卷）。
    """
    existing = ags.find_tool(tool_name)
    if existing and force_recreate:
        ags.delete_tool(existing["tool_id"])
        existing = None
    if existing:
        if str(existing.get("status", "")).upper() != "ACTIVE":
            ags.wait_tool_active(tool_name, timeout=timeout)
        return existing["tool_id"]

    storage_mounts = None
    if base_image:
        storage_mounts = [{
            "name": "base-env",
            "image": base_image,
            "mount_path": BASE_ENV_MOUNT_PATH,
            "read_only": True,
        }]

    try:
        tool_id = ags.create_tool(
            tool_name, bootstrap_image, role_arn=role_arn,
            image_registry_type=image_registry_type,
            description="SWE-Synth 共享沙箱工具（题目切换走实例级 CustomConfiguration.Image 覆盖）",
            storage_mounts=storage_mounts,
        )
    except Exception:  # noqa: BLE001
        # 并发跑多个 worker 时，可能有另一个 worker 抢先创建了同名工具
        # （首次调用时的竞态；后续调用都会走上面 existing 分支直接复用）。
        # 这里不判断具体错误类型/文案，统一回退到再查一次：查到就直接复用，
        # 查不到说明确实是别的原因失败，原样抛出更利于定位问题。
        existing = ags.find_tool(tool_name)
        if not existing:
            raise
        ags.wait_tool_active(tool_name, timeout=timeout)
        return existing["tool_id"]

    # CreateSandboxTool 是异步的，必须等状态收敛到 ACTIVE 才能起沙箱
    ags.wait_tool_active(tool_name, timeout=timeout)
    return tool_id


def _run_verify(sbx, args: str) -> dict[str, Any]:
    """在沙箱内跑 verify.sh，读取并返回 /task/result.json。

    必须显式 `user="root"`：e2b `commands.run` 默认以非 root 的 `user`(uid 1000)
    执行；而镜像内 `/opt/venv311/bin/python` 是指向 `/root/.local/share/uv/...`
    的符号链接，`/root` 目录权限 700，非 root 用户连遍历都进不去，
    表现为 `Permission denied`（已实测确认）。镜像内容本就是给 root 用的，
    题目契约（/task、/workspace/repo）也全属主 root，用 root 执行是正确身份。
    """
    try:
        from e2b.sandbox.commands.command_handle import CommandExitException
    except ImportError:  # pragma: no cover - SDK 结构变动兜底
        CommandExitException = ()  # type: ignore[assignment]

    try:
        # PYTEST_ADDOPTS=--color=no：有些仓库自身 pytest 配置里写了
        # `addopts = --color=yes`（如 humanize），即使输出被重定向到文件
        # 仍会带 ANSI 颜色码，导致 verify.sh 内的结果解析正则匹配不到
        # PASSED/FAILED，被误判为「全部失败」（假阴性）。强制关闭颜色规避。
        sbx.commands.run(
            f"bash /task/verify.sh {args}".strip(),
            timeout=600,
            user="root",
            envs={"PYTEST_ADDOPTS": "--color=no"},
        )
        exit_code, stderr = 0, ""
    except CommandExitException as e:
        # verify.sh 的约定：0=通过，1=不通过（正常结果），>=90=环境/脚本错误
        exit_code, stderr = e.exit_code, (e.stderr or "")
    if exit_code >= 90:
        raise SandboxVerifyError(f"verify.sh 环境错误（exit={exit_code}）：{stderr[-500:]}")
    try:
        raw = sbx.files.read("/task/result.json", user="root")
    except Exception as e:  # noqa: BLE001
        raise SandboxVerifyError(f"读取 /task/result.json 失败：{e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SandboxVerifyError(f"result.json 不是合法 JSON：{e}") from e


def verify_task(
    task: SweTask,
    *,
    region: str | None = None,
    role_arn: str | None = None,
    image_registry_type: str = "personal",
    timeout: int = 900,
    reuse_tool: bool = True,
    shared_tool_name: str = DEFAULT_SHARED_TOOL_NAME,
    base_image: str | None = None,
) -> VerificationResult:
    """对一道题执行 Agent2 的双向沙箱验证。

    步骤
    ----
    1. 确保共享沙箱工具存在（已存在则直接复用，不因题目不同新建；
       首次创建时若传了 `base_image`，会额外挂一个只读的 base 环境挂载卷，
       双镜像方案，见 `_ensure_shared_tool`）
    2. 题目镜像覆盖启动实例 → 空解跑 verify.sh → 必须 passed=False
    3. 答案镜像覆盖启动实例 → 跑 verify.sh --golden → 必须 passed=True

    判定通过条件：空解失败 且 golden 解通过（两者缺一不可）。
    """
    import time

    t0 = time.time()
    res = VerificationResult(task_id=task.task_id)

    e2b_key = os.environ.get("E2B_API_KEY", "")
    e2b_domain = os.environ.get("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
    if not e2b_key:
        raise SandboxVerifyError("未配置 E2B_API_KEY（见 .env）")
    os.environ.setdefault("E2B_API_KEY", e2b_key)
    os.environ.setdefault("E2B_DOMAIN", e2b_domain)

    ags = AGSClient(region=region)

    tool_id = _ensure_shared_tool(
        ags, shared_tool_name, task.image,
        role_arn=role_arn, image_registry_type=image_registry_type,
        timeout=timeout, force_recreate=not reuse_tool,
        base_image=base_image,
    )
    ags_timeout = _ags_timeout_str(timeout)

    # ---------- 空解验证（题目镜像）与 golden 解验证（答案镜像）
    instance_empty = instance_golden = None
    try:
        instance_empty, _ = ags.start_instance(
            tool_id, image_override=task.image,
            timeout=ags_timeout, image_registry_type=image_registry_type,
        )
        res.empty_sandbox_id = instance_empty
        sbx_empty = _connect_sandbox(instance_empty)
        res.empty_run = _run_verify(sbx_empty, "")
        empty_passed = bool(res.empty_run.get("passed"))

        instance_golden, _ = ags.start_instance(
            tool_id, image_override=task.solution_image,
            timeout=ags_timeout, image_registry_type=image_registry_type,
        )
        res.golden_sandbox_id = instance_golden
        sbx_golden = _connect_sandbox(instance_golden)
        res.golden_run = _run_verify(sbx_golden, "--golden")
        golden_passed = bool(res.golden_run.get("passed"))

        # ---------- 判定
        if empty_passed:
            res.passed = False
            res.reason = ("空解居然通过了 —— 题目没有有效判据（被测 Agent 不改代码也能拿分），"
                          "判为废题")
        elif not golden_passed:
            res.passed = False
            res.reason = ("参考解未通过判据 —— 镜像内的 golden.patch 与判据不一致，"
                          f"FAIL_TO_PASS 未全绿：{res.golden_run.get('fail_to_pass')}")
        else:
            res.passed = True
            res.reason = ("双向验证通过：空解失败（题目有内容）、参考解通过（题目可解）")

        res.duration_sec = round(time.time() - t0, 1)
        return res
    finally:
        # 沙箱实例按运行时长计费，务必回收；共享工具本身不删（下一道题还要用）
        for instance_id in (instance_empty, instance_golden):
            if instance_id:
                try:
                    ags.stop_instance(instance_id)
                except Exception:  # noqa: BLE001
                    pass
