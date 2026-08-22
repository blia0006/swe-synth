"""腾讯云 AGS（Agent Sandbox）客户端：沙箱工具的创建、查询与实例级换题。

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

StartSandboxInstance 同样接受可选的 `CustomConfiguration`，且其中的 `Image`
可以覆盖工具创建时的默认镜像——也就是说切换题目不需要重新创建/删除工具，
只需复用同一个 ToolId、在每次启动实例时传不同的 `Image`（已实测验证，见
`experiments/verify_customconfig_switch.py`：全程 1 个工具、2 次实例分别
拿到不同题目内容、环境层输出一致）。这是双镜像方案在生产流水线里的落地
方式：`start_instance()` 封装了这个实例级覆盖。

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
        storage: str | None = None,
        probe_path: str = "/health",
        probe_port: int = 49983,
        probe_ready_timeout_ms: int = 30000,
        probe_timeout_ms: int = 5000,
        probe_period_ms: int = 10000,
        probe_failure_threshold: int = 3,
        probe_success_threshold: int = 1,
        storage_mounts: list[dict[str, Any]] | None = None,
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

        `storage_mounts`：双镜像方案的「挂载卷」半边——每项
        `{"name", "image", "mount_path", "read_only"?, "image_registry_type"?, "sub_path"?}`，
        构造 `StorageMount(StorageSource=StorageSource(Image=ImageStorageSource(...)))`。
        **镜像引用在这里（工具创建时）就固定死了**：`StartSandboxInstance` 的
        `MountOptions`（见 `start_instance`）只能覆盖 `MountPath`/`SubPath`/`ReadOnly`，
        没有 `Reference` 字段——即挂载卷指向的镜像内容无法按实例切换（已用 SDK
        model 定义确认，`MountOption` 没有任何镜像/引用类字段）。因此挂载卷天生
        只适合放「不随题目变化」的内容（比如共享 base 镜像），题目内容仍必须走
        `CustomConfiguration.Image` 的实例级覆盖（见 `start_instance`）。
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

        if storage_mounts:
            req.StorageMounts = self._build_storage_mounts(storage_mounts)

        req.CustomConfiguration = self._build_custom_configuration(
            image,
            image_registry_type=image_registry_type,
            command=command,
            args=args,
            cpu=cpu,
            memory=memory,
            storage=storage,
            probe_path=probe_path,
            probe_port=probe_port,
            probe_ready_timeout_ms=probe_ready_timeout_ms,
            probe_timeout_ms=probe_timeout_ms,
            probe_period_ms=probe_period_ms,
            probe_failure_threshold=probe_failure_threshold,
            probe_success_threshold=probe_success_threshold,
        )

        rsp = self._cli.CreateSandboxTool(req)
        return getattr(rsp, "ToolId", "")

    # ------------------------------------------------------------ 挂载卷（双镜像方案的固定半边）
    def _build_storage_mounts(self, mounts: list[dict[str, Any]]) -> list[Any]:
        """构造 `StorageMounts`（工具级，镜像引用创建后不可变，见 `create_tool` 说明）。

        每项 dict 支持的 key：`name`（必填）、`image`（必填，镜像地址）、
        `mount_path`（必填）、`read_only`（默认 True）、
        `image_registry_type`（默认 "personal"）、`sub_path`（可选）。
        """
        m = self._m
        out = []
        for spec in mounts:
            img_src = m.ImageStorageSource()
            img_src.Reference = spec["image"]
            img_src.ImageRegistryType = spec.get("image_registry_type", "personal")
            if spec.get("sub_path"):
                img_src.SubPath = spec["sub_path"]

            src = m.StorageSource()
            src.Image = img_src

            mount = m.StorageMount()
            mount.Name = spec["name"]
            mount.StorageSource = src
            mount.MountPath = spec["mount_path"]
            mount.ReadOnly = spec.get("read_only", True)
            out.append(mount)
        return out

    def _build_mount_options(self, options: list[dict[str, Any]]) -> list[Any]:
        """构造 `MountOptions`（实例级，只能改 `MountPath`/`SubPath`/`ReadOnly`，
        不能改镜像引用——`MountOption` model 没有 `Reference` 字段）。
        """
        m = self._m
        out = []
        for spec in options:
            opt = m.MountOption()
            opt.Name = spec["name"]
            if spec.get("mount_path"):
                opt.MountPath = spec["mount_path"]
            if spec.get("sub_path"):
                opt.SubPath = spec["sub_path"]
            if "read_only" in spec:
                opt.ReadOnly = spec["read_only"]
            out.append(opt)
        return out

    # ------------------------------------------------------------ 实例级换题（双镜像方案）
    def _build_custom_configuration(
        self,
        image: str,
        *,
        image_registry_type: str = "personal",
        command: list[str] | None = None,
        args: list[str] | None = None,
        cpu: str = "2",
        memory: str = "4Gi",
        storage: str | None = None,
        probe_path: str = "/health",
        probe_port: int = 49983,
        probe_ready_timeout_ms: int = 30000,
        probe_timeout_ms: int = 5000,
        probe_period_ms: int = 10000,
        probe_failure_threshold: int = 3,
        probe_success_threshold: int = 1,
    ):
        """构造 `CustomConfiguration`，`create_tool` 与 `start_instance` 共用。

        `storage`：容器 rootfs 大小，枚举 "1Gi"/"5Gi"/"10Gi"/"20Gi"，不传则
        用平台默认（实测默认仅 1Gi，装完 base 镜像本身就快占满，跑
        pip install / git clone / buildah build 这类需要落盘空间的场景必须
        显式调大，否则会报 "No space left on device"）。
        """
        m = self._m
        custom = m.CustomConfiguration()
        custom.Image = image
        custom.ImageRegistryType = image_registry_type
        custom.Command = command if command is not None else ["/init"]
        custom.Args = args if args is not None else ["sleep", "infinity"]

        res = m.ResourceConfiguration()
        res.CPU = cpu
        res.Memory = memory
        if storage:
            res.Storage = storage
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
        return custom

    def start_instance(
        self,
        tool_id: str,
        *,
        image_override: str | None = None,
        timeout: str = "15m",
        image_registry_type: str = "personal",
        cpu: str = "2",
        memory: str = "4Gi",
        storage: str | None = None,
        mount_options: list[dict[str, Any]] | None = None,
        max_wait: float = 600,
        poll_interval: float = 20,
    ) -> tuple[str, str | None]:
        """启动一个沙箱实例，可选按实例覆盖题目镜像 / 挂载路径。

        双镜像方案的核心 API：沙箱工具只创建一次（默认镜像随便填，通常是
        共享 base 镜像本身），之后每道题验证时都调用这个方法、传入
        `image_override=task.image` / `task.solution_image`，不再为每道题
        创建/删除沙箱工具（已实测验证，见
        `experiments/verify_customconfig_switch.py`）。

        `mount_options`：实例级覆盖工具上已声明的 `StorageMounts`（按 `name`
        匹配），只能改 `mount_path`/`sub_path`/`read_only`，**不能**换挂载卷
        指向的镜像内容（`MountOption` model 无 `Reference` 字段）。不传则沿用
        工具创建时的挂载配置（见 `create_tool` 的 `storage_mounts`）。

        返回 `(instance_id, effective_image)`；`effective_image` 为空表示
        未覆盖，实际生效的是工具创建时的默认镜像。

        AGS 平台侧存在若干已知的瞬时性错误，官方错误信息里明确写了
        "retry later"，包括：
        - `ResourceUnavailable`：镜像刚 push 到仓库后平台需要异步
          「预处理/预热」窗口，期间报 message 含 "still preparing"。
        - `FailedOperation.Timeout`：高并发起沙箱实例时，平台内部
          provider 短暂响应超时，message 含
          "Sandbox creation timed out"。
        对这类已识别的瞬时性错误，这里做限时轮询重试（默认最多
        `max_wait` 秒、每 `poll_interval` 秒重试一次）；其余错误（如参数
        错误、权限不足等）直接抛出，不做无意义重试。
        """
        import time

        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )

        m = self._m
        req = m.StartSandboxInstanceRequest()
        req.ToolId = tool_id
        req.Timeout = timeout
        if image_override:
            req.CustomConfiguration = self._build_custom_configuration(
                image_override,
                image_registry_type=image_registry_type,
                cpu=cpu,
                memory=memory,
                storage=storage,
            )
        if mount_options:
            req.MountOptions = self._build_mount_options(mount_options)

        deadline = time.time() + max_wait
        attempt = 0
        while True:
            attempt += 1
            try:
                rsp = self._cli.StartSandboxInstance(req)
                break
            except TencentCloudSDKException as e:
                code = getattr(e, "code", "") or ""
                message = str(e)
                is_transient = (
                    (code == "ResourceUnavailable" and "still preparing" in message)
                    or (
                        code == "FailedOperation.Timeout"
                        and "Sandbox creation timed out" in message
                    )
                    or "retry later" in message.lower()
                )
                if not is_transient or time.time() >= deadline:
                    raise AGSError(
                        f"StartSandboxInstance 失败（tool_id={tool_id}, "
                        f"image={image_override or '<默认>'}，第 {attempt} 次尝试，"
                        f"code={code}）：{e}"
                    ) from e
                time.sleep(poll_interval)
        inst = rsp.Instance
        effective_image = getattr(
            getattr(inst, "CustomConfiguration", None), "Image", None
        )
        return getattr(inst, "InstanceId", ""), effective_image

    def stop_instance(self, instance_id: str) -> None:
        """停止/回收一个沙箱实例（按实例计费的资源需要显式回收）。"""
        m = self._m
        req = m.StopSandboxInstanceRequest()
        req.InstanceId = instance_id
        self._cli.StopSandboxInstance(req)

    def renew_instance(self, instance_id: str, timeout: str = "24h") -> None:
        """给一个正在运行的实例续期（`UpdateSandboxInstance`）。

        `Timeout` 是「从这次设置时刻重新计算」的新超时时长，不是叠加；
        支持 `5m`/`300s`/`1h` 等格式，最小 30s、最大 24h（单次调用上限）。
        用于长跑流水线（可能持续数小时～近一天）防止实例因超时被系统
        强制回收——跟沙箱内 envd 托管的后台进程是否存活无关，实例本身
        到期就会被杀，必须显式续期。
        """
        m = self._m
        req = m.UpdateSandboxInstanceRequest()
        req.InstanceId = instance_id
        req.Timeout = timeout
        self._cli.UpdateSandboxInstance(req)

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
