"""Agent2 核心：自定义镜像起沙箱，执行双向验证

对应课题要求（见 TASK-SPEC.md）
------------------------------
    Agent2（验证）：拉取 TCR 镜像 → 在 Agent Sandbox 启动容器 → 执行题目 →
                   验证解的正确性 → 无重叠校验

验证逻辑（双向 sanity 的「云端镜像版」）
--------------------------------------
    A2-1 拉取 TCR 镜像      （Agent1 已 push，见 agent1/packer.py）
    A2-2 自定义镜像起沙箱   （AGS CreateSandboxTool 注册工具 + E2B 启动）
    A2-3 空解必须失败       题目镜像 :v1 跑 /task/verify.sh → passed=False
    A2-4 参考解必须通过     答案镜像 :v1-sol 跑 /task/verify.sh --golden → passed=True
    A2-5 无重叠校验         （见 overlap_check.py，依赖 GITHUB_TOKEN）

两个镜像各起一个沙箱实例：
    · 题目镜像 :v1      → 空解验证（证明题目「有内容」，不是白给分）
    · 答案镜像 :v1-sol  → golden 验证（证明题目「可解」，答案真的能过判据）

沙箱按运行时长计费，**无论成败都必须 kill**（finally 保证）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from ..clients.ags import AGSClient, AGSError
from ..schemas.task import SweTask

__all__ = ["VerificationResult", "SandboxVerifyError", "verify_task"]


class SandboxVerifyError(RuntimeError):
    """沙箱验证失败（环境/流程错误，与题目对错无关）。"""


@dataclass
class VerificationResult:
    """Agent2 双向验证的完整结果（含沙箱证据）。"""

    task_id: str
    empty_run: dict[str, Any] | None = None       # 空解的 result.json
    golden_run: dict[str, Any] | None = None      # golden 解的 result.json
    empty_sandbox_id: str = ""
    golden_sandbox_id: str = ""
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


def _make_sandbox(template: str, timeout: int = 900):
    """用 E2B SDK 以工具名（= 沙箱工具 ToolName）启动实例。

    SDK 形态兼容（见 check_env.py）：e2b 1.x 用 `Sandbox(template=...)`，
    腾讯云 AGS 走这一支（Key 形如 ark_xxx）；e2b 2.x 用 `Sandbox.create(...)`。
    """
    try:
        from e2b_code_interpreter import Sandbox
    except ImportError as e:
        raise SandboxVerifyError("缺少 e2b-code-interpreter：pip install -r requirements.txt") from e

    if hasattr(Sandbox, "create"):
        return Sandbox.create(template=template, timeout=timeout)
    return Sandbox(template=template, timeout=timeout)


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
) -> VerificationResult:
    """对一道题执行 Agent2 的双向沙箱验证。

    步骤
    ----
    1. 把题目镜像与答案镜像各自注册为沙箱工具（已存在则复用）
    2. 题目镜像起沙箱 → 空解跑 verify.sh → 必须 passed=False
    3. 答案镜像起沙箱 → 跑 verify.sh --golden → 必须 passed=True

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
    short = task.task_id.replace("-", "")  # tool 名不支持太长的分隔？保留原名更直观
    tool_v1 = f"{task.task_id}"
    tool_sol = f"{task.task_id}-sol"

    # ---------- 注册工具（复用已有，避免重复创建报「名称已存在」）
    def ensure_tool(tool_name: str, image: str) -> None:
        existing = ags.find_tool(tool_name) if reuse_tool else None
        if existing:
            # CCR 的 :tag 可变、可被重新 push 覆盖，但已建好的沙箱工具不会
            # 自动感知镜像内容更新（实测确认：重推镜像后旧工具起的沙箱仍是旧内容）。
            # 镜像地址变了就必须先删后建；地址没变才能安全复用。
            if existing.get("image") == image:
                if str(existing.get("status", "")).upper() != "ACTIVE":
                    ags.wait_tool_active(tool_name, timeout=timeout)
                return
            ags.delete_tool(existing["tool_id"])
        ags.create_tool(tool_name, image, role_arn=role_arn,
                        image_registry_type=image_registry_type,
                        description=f"SWE-Synth 题目 {task.task_id}")
        # CreateSandboxTool 是异步的，必须等状态收敛到 ACTIVE 才能起沙箱
        ags.wait_tool_active(tool_name, timeout=timeout)

    ensure_tool(tool_v1, task.image)
    ensure_tool(tool_sol, task.solution_image)

    # ---------- 空解验证（题目镜像）
    sbx_empty = None
    sbx_golden = None
    try:
        sbx_empty = _make_sandbox(tool_v1, timeout=timeout)
        res.empty_sandbox_id = getattr(sbx_empty, "sandbox_id", "")
        res.empty_run = _run_verify(sbx_empty, "")
        empty_passed = bool(res.empty_run.get("passed"))

        # ---------- golden 解验证（答案镜像）
        sbx_golden = _make_sandbox(tool_sol, timeout=timeout)
        res.golden_sandbox_id = getattr(sbx_golden, "sandbox_id", "")
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
        # 沙箱按运行时长计费，务必销毁
        for sbx in (sbx_empty, sbx_golden):
            if sbx is not None:
                try:
                    sbx.kill()
                except Exception:  # noqa: BLE001
                    pass
