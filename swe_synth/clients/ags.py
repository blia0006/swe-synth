"""腾讯云 AGS（Agent Sandbox）客户端：沙箱工具的创建与查询。

Agent2 的「自定义镜像起沙箱」依赖本模块：把 Agent1 推送到 CCR 的题目镜像
注册成一个「沙箱工具」，再用 E2B SDK 以该工具名启动沙箱实例。

API 依据（官方文档，版本 2025-09-20）
------------------------------------
CreateSandboxTool（/document/product/1814/124812）关键参数：
    ToolName             必选  工具名（同一 AppId 下唯一）
    ToolType             必选  枚举含 `custom`（自定义镜像）/ `swebench`
    NetworkConfiguration 必选  { NetworkMode: "PUBLIC" }
    RoleArn              可选  自定义镜像拉取所需角色（缺它会报 MissingParameter.RoleArn）
    CustomConfiguration  可选  { Image, ImageRegistryType, Ports, Resources, Probe, ... }
                              · Image 是镜像地址（不是 ImageUri）
                              · ImageRegistryType 枚举：enterprise / personal / custom
                              · Ports 是数组，元素含 Port(Integer) / Protocol

安全
----
· 凭证（SecretId/Key）只从环境读取，不落盘、不出现在日志
· 所有字段值经 SDK 的 models 对象传递（参数化，无拼接注入面）
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["AGSClient", "AGSError", "client_of", "models_of"]


class AGSError(RuntimeError):
    """AGS API 调用失败。"""


def client_of(module_name: str, client_cls: str, version: str, region: str):
    """构造某个腾讯云产品的 SDK client（参数列表，无 shell 拼接）。"""
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile

    sid = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    skey = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    if not sid or not skey:
        raise AGSError("未配置 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY（见 .env）")
    cred = credential.Credential(sid, skey)
    hp = HttpProfile(reqTimeout=60)
    cp = ClientProfile(httpProfile=hp)

    # client 类位于 tencentcloud.<产品>.<版本>.<产品>_client 子模块，包 __init__ 未导出
    mod = __import__(f"tencentcloud.{module_name}.{version}.{module_name}_client",
                     fromlist=[client_cls])
    cls = getattr(mod, client_cls)
    return cls(cred, region, cp)


def models_of(module_name: str, version: str):
    return __import__(f"tencentcloud.{module_name}.{version}.models", fromlist=["models"])


class AGSClient:
    """AGS 沙箱工具管理（创建 / 列表查询）。"""

    VERSION = "v20250920"
    PRODUCT = "ags"

    def __init__(self, region: str | None = None) -> None:
        self.region = region or os.environ.get("TENCENTCLOUD_REGION", "ap-guangzhou")
        self._cli = client_of(self.PRODUCT, "AgsClient", self.VERSION, self.region)
        self._m = models_of(self.PRODUCT, self.VERSION)

    # ------------------------------------------------------------ 创建
    def create_tool(
        self,
        tool_name: str,
        image: str,
        *,
        role_arn: str | None = None,
        image_registry_type: str = "personal",   # CCR 个人版
        tool_type: str = "custom",
        description: str = "",
        network_mode: str = "PUBLIC",
        default_timeout: str = "15m",
        command: list[str] | None = None,
        args: list[str] | None = None,
        cpu: str = "2",
        memory: str = "4Gi",
        probe_path: str = "/health",
        probe_port: int = 49983,
        probe_ready_timeout_ms: int = 30000,
        probe_timeout_ms: int = 5000,
        probe_period_ms: int = 10000,
        probe_failure_threshold: int = 3,
        probe_success_threshold: int = 1,
    ) -> str:
        """把镜像注册为沙箱工具，返回 ToolId。

        自定义镜像必须传 `role_arn`（AGS 用它去 CCR 拉取镜像），
        否则会报 `MissingParameter.RoleArn`。

        `command`/`args`/`cpu`/`memory`/`probe_*` 对 `custom` 类型是接口文档标注
        「非必选」，但实测自定义镜像若不传会报 `MissingParameter`（文档与实际行为
        不一致，已通过实测账号内另一个可用的自定义沙箱工具反推出正确取值）：
        · 我们的题目镜像继承官方 `ags-image/sandbox-code` 且未覆盖 ENTRYPOINT/CMD
          （见 Dockerfile 注释），保持基础镜像自带的 `/init` + `sleep infinity`，
          因此 Command/Args 默认沿用同款取值，不会打断 S6-Overlay 对 envd 的托管。
        · Probe 默认值取自 `config/settings.yaml` 的 `sandbox.probe`（探测
          `/health:49983`，即基础镜像内置 envd 的健康检查端口）。
        """
        role_arn = role_arn or os.environ.get("AGS_ROLE_ARN", "")
        if not role_arn:
            raise AGSError("未配置 AGS_ROLE_ARN（自定义镜像拉取必需，见 .env）")

        m = self._m
        req = m.CreateSandboxToolRequest()
        req.ToolName = tool_name
        req.ToolType = tool_type
        req.Description = description
        req.DefaultTimeout = default_timeout
        req.RoleArn = role_arn

        net = m.NetworkConfiguration()
        net.NetworkMode = network_mode
        req.NetworkConfiguration = net

        custom = m.CustomConfiguration()
        custom.Image = image
        custom.ImageRegistryType = image_registry_type
        custom.Command = command if command is not None else ["/init"]
        custom.Args = args if args is not None else ["sleep", "infinity"]

        res = m.ResourceConfiguration()
        res.CPU = cpu
        res.Memory = memory
        custom.Resources = res

        http_get = m.HttpGetAction()
        http_get.Path = probe_path
        http_get.Port = probe_port
        http_get.Scheme = "HTTP"
        probe = m.ProbeConfiguration()
        probe.HttpGet = http_get
        probe.ReadyTimeoutMs = probe_ready_timeout_ms
        probe.ProbeTimeoutMs = probe_timeout_ms
        probe.ProbePeriodMs = probe_period_ms
        probe.FailureThreshold = probe_failure_threshold
        probe.SuccessThreshold = probe_success_threshold
        custom.Probe = probe

        req.CustomConfiguration = custom

        rsp = self._cli.CreateSandboxTool(req)
        return getattr(rsp, "ToolId", "")

    # ------------------------------------------------------------ 查询
    def list_tools(self) -> list[dict[str, Any]]:
        """列出已有沙箱工具（名称 / ID / 类型 / 状态 / 镜像地址）。"""
        m = self._m
        req = m.DescribeSandboxToolListRequest()
        req.Offset, req.Limit = 0, 100
        rsp = self._cli.DescribeSandboxToolList(req)
        out = []
        for t in (rsp.SandboxToolSet or []):
            cc = getattr(t, "CustomConfiguration", None)
            out.append({
                "name": getattr(t, "ToolName", "?"),
                "tool_id": getattr(t, "ToolId", "?"),
                "type": getattr(t, "ToolType", "?"),
                "status": getattr(t, "Status", "?"),
                "image": getattr(cc, "Image", None) if cc else None,
            })
        return out

    def find_tool(self, tool_name: str) -> dict[str, Any] | None:
        for t in self.list_tools():
            if t["name"] == tool_name:
                return t
        return None

    # ------------------------------------------------------------ 删除
    def delete_tool(self, tool_id: str) -> None:
        """删除沙箱工具。

        注意：CCR 的 `:tag` 是可变的，重新 `docker push` 同一 tag 会覆盖内容，
        但已创建的沙箱工具在启动实例时用的镜像内容是「创建那一刻」拉取/固定的，
        不会因为仓库里 tag 指向了新内容而自动刷新。镜像有更新后必须删除重建
        （已实测确认：重推镜像后旧工具起的沙箱里看到的仍是旧内容）。
        """
        m = self._m
        req = m.DeleteSandboxToolRequest()
        req.ToolId = tool_id
        self._cli.DeleteSandboxTool(req)

    # ------------------------------------------------------------ 等待就绪
    def wait_tool_active(
        self, tool_name: str, *, timeout: float = 180, interval: float = 3,
    ) -> dict[str, Any]:
        """轮询直到工具状态变为 ``ACTIVE``。

        `CreateSandboxTool` 是异步的：刚创建完立即用 E2B SDK 起沙箱，
        工具很可能还处于 ``CREATING``，会报 `ResourceUnavailable.SandboxTool
        ... is not active, current status: CREATING`。这里显式等待收敛。
        """
        import time

        deadline = time.time() + timeout
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            last = self.find_tool(tool_name)
            if last is None:
                raise AGSError(f"工具 {tool_name} 未找到（可能创建失败或已被删除）")
            status = str(last.get("status", "")).upper()
            if status == "ACTIVE":
                return last
            if status in ("FAILED", "ERROR", "DELETED", "DELETING"):
                raise AGSError(f"工具 {tool_name} 未能就绪，状态：{status}")
            time.sleep(interval)
        raise AGSError(
            f"等待工具 {tool_name} 变为 ACTIVE 超时（{timeout}s），"
            f"当前状态：{last.get('status') if last else '未知'}"
        )
