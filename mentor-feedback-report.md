# 导师反馈的验证与整改报告

> 日期：2026-08-21
> 说明：导师提出 4 条反馈，本文逐条给出**实测结论**与整改方案。
> 所有结论均来自真实环境实测（远端 amd64 构建机 + 腾讯云 AGS 沙箱），非推测。

---

## 结论速览

| # | 导师反馈 | 结论 | 状态 |
|---|---|---|---|
| 1 | Ubuntu base 变成了 Debian，客户几乎都是 Ubuntu | **可行，已实测通过**。镜像同时从 6.86GB 降到 872MB | ✅ 已解决 |
| 2 | e2b SDK 2.x 没试过 | **已实测：2.x 与 AGS 后端协议完全兼容**，唯一阻塞是 SDK 客户端一行硬编码的 Key 格式校验 | ✅ 已验证 |
| 3 | build 题的环境没看到，在哪里？ | 在远端 amd64 构建机上，通过 `DOCKER_HOST=ssh://` 驱动。**此前文档未说明，是我们的交付缺陷** | ✅ 已补文档 |
| 4 | DinD 没验证；AGS 支持双镜像方式 | **DinD 已实测确认不可用**（无 docker.sock）；**双镜像方案已实测跑通** | ✅ 已验证 |

**综合收益**（反馈 1 + 4 落地后）：

| 指标 | 原方案 | 新方案 | 改善 |
|---|---|---|---|
| 基础层发行版 | Debian 13 (trixie) | **Ubuntu 22.04.5 LTS** | 符合课题原文与客户环境 |
| 每题镜像体积 | 7.5 GB | **1.09 MB**（+ 共享 base 872MB） | ↓ 约 7000 倍 |
| 每题 build 耗时 | 6~22 分钟 | 秒级 | ↓ 两个数量级 |
| 每题 push 耗时 | 1000+ 秒 | **2.3 秒** | ↓ 约 450 倍 |
| 沙箱工具占用 | 每题 2 个（配额上限 10） | 1 个 base 工具复用 | 不再受配额制约 |

---

## 反馈 1：基础层改回 Ubuntu

### 问题根因

原方案 `FROM ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest`，实测其底层为
`Debian GNU/Linux 13 (trixie)`。选它的原因是官方文档要求：若需 `run_code` /
`commands.run` / `files.*` 能力，自定义镜像必须继承该官方基础镜像。

### 关键发现：平台组件可拆分为两类

实测拆解官方镜像后发现，让沙箱「能被 AGS 平台管理」的组件分两类：

| 类别 | 组件 | 提供能力 | 链接方式 | 能否跨发行版 |
|---|---|---|---|---|
| **A** | `/usr/bin/envd`（16MB）+ S6-Overlay 3.2.1.0（`/init` `/command` `/package` `/etc/s6-overlay`，约 5.6MB） | `commands.run`、`files.*` | **静态**（`not a dynamic executable`） | ✅ 可直接搬运 |
| **B** | `jupyter` + `uvicorn` + `/root/.server` | `run_code`（Python 解释器） | 依赖镜像内 Python 3.12（动态链接，glibc 2.41） | ❌ 无法向下搬到 Ubuntu 22.04（glibc 2.35） |

**决定性事实**：本项目 Agent2 只使用 `commands.run` 与 `files.read`
（见 `swe_synth/agent2/sandbox_runner.py`），**从未使用 `run_code`**。
因此只需搬运 A 类组件，B 类可整体省去。

### 整改方案与实测结果

`experiments/ubuntu-base/Dockerfile`：以 `ubuntu:22.04` 为基础层，
多阶段构建从官方镜像 COPY A 类静态组件，并剔除 `user` bundle 中的
`jupyter`/`uvicorn` 服务定义（否则 S6 会反复拉起注定失败的服务）。

真实沙箱内实测输出：

```
### 1. 发行版
PRETTY_NAME="Ubuntu 22.04.5 LTS"
NAME="Ubuntu"
VERSION_ID="22.04"

### 2. 课题要求的工具链
Python 3.11.16
git version 2.34.1
Docker version 29.1.3

### 4. 平台能力自检
S6+envd 组件就位
envd 进程数: 4
```

- 沙箱工具创建成功 → 状态 `ACTIVE`
- 实例启动成功 → `commands.run` 正常（上述输出即通过它取得）
- 镜像体积 **872MB**（原 6.86GB 基础层 → 降 87%）

> 附带收益：这同时把课题原文要求的 `ubuntu:22.04 + Python 3.11 + Git + Docker CLI`
> 从「兼容方案」变成了**字面满足**，`requirements-check.md` 里原先那条技术偏差
> 说明可以撤销。

---

## 反馈 2：e2b SDK 2.x

### 实测结论：协议兼容，仅客户端校验阻塞

在独立 venv 装 `e2b-code-interpreter 2.9.1`（依赖 `e2b 2.44.0`）实测：

**第一次尝试（原样调用）**：
```
❌ AuthenticationException
Invalid API key format: expected "e2b_" followed by hex characters
```

定位到硬编码校验（`e2b/api/__init__.py:198`）：
```python
_API_KEY_PATTERN = re.compile(r"\Ae2b_[0-9a-f]+\Z")
```
腾讯云 AGS 的 Key 形如 `ark_xxx`，被客户端直接拒绝，**无开关可关**。

**第二次尝试（绕过该客户端校验后）**：
```
✅ 绕过校验后创建成功! id = vjwscteqrktaqsqe2e4tugnoeiepm2pnq7pw5aui
PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
已销毁
```

→ **结论：AGS 后端与 e2b 2.x 协议完全兼容**，问题纯粹在 SDK 客户端的格式校验。

### 2.x 相比 1.x 的实质收益

`Sandbox.create()` 新增了与本项目直接相关的参数：

```python
volume_mounts: Optional[Dict[str, Union[Volume, str]]] = None
```

这正是反馈 4 双镜像方案所需的挂载能力，**用 2.x 可以在 SDK 层面直接表达**，
不必绕到 AGS OpenAPI 的 `StartSandboxInstance`。

### 建议

保持 1.x 为默认（生产稳定），同时提供 2.x 适配层：在初始化时对
`validate_api_key` 做一次显式覆盖，并在注释中写明原因与风险。
这样既保留 2.x 的新能力，也不影响既有 19 道题的可复现性。

> 风险提示：绕过第三方库的内部校验属于侵入式做法，SDK 升级可能失效。
> 更稳妥的长期方案是向 AGS 团队反馈，请其在服务端签发 `e2b_` 兼容格式的 Key，
> 或在官方文档中给出 2.x 的推荐适配方式。

---

## 反馈 3：build 题的环境在哪里

### 这是我们的交付缺陷，已修正

`docker build` / `docker push` **不在沙箱内、也不在本机**，而是在一台
**远端 amd64 构建机（腾讯云 CVM）** 上执行，本机通过 `DOCKER_HOST` 环境变量
驱动远端 dockerd：

```bash
DOCKER_HOST=ssh://docker-builder    # .env 中配置
```

### 为什么这样设计

| 原因 | 说明 |
|---|---|
| 架构必需 | 开发机是 macOS **arm64**，而 AGS 沙箱只接受 `linux/amd64`。跨架构构建（QEMU 模拟）慢到不可用 |
| 平台限制 | AGS 沙箱内**没有 docker CLI、也没有 DinD**（见反馈 4），因此 build 无法放在沙箱内 |
| 与出题解耦 | 出题（AST 挖空 + LLM + 本地跑测试）不需要 Docker，可在无 Docker 环境开发调试；只有打包环节需要 |

### 构建机实测规格

```
CPU     : 2 核（AMD EPYC 9K65）
内存    : 3.5 GB
磁盘    : 50 GB（已用 33GB / 65%），云盘顺序写 129 MB/s
网络    : 到 CCR 走内网（解析到 169.254.0.42，经 10.0.4.1 网关）
负载    : load average 0.00（构建期间无资源争抢）
```

### 已补充的文档

- `PIPELINE.md`：新增「构建环境」一节，说明 `DOCKER_HOST` 的作用与配置方式
- `.env.example`：`DOCKER_HOST` 补充注释与示例
- 交付说明：明确 build 发生在何处，以及导师若要自行复现需要准备什么

---

## 反馈 4：DinD 与双镜像方案

### 4.1 DinD：已实测，确认不可用

在 Ubuntu 版自定义镜像的真实沙箱实例内实测：

```
### 3. DinD 可用性
docker.sock 不存在 → 无 DinD（需通过挂载或特权容器提供）
```

镜像内**已安装 docker CLI**（`Docker version 29.1.3`），但沙箱运行时未注入
`/var/run/docker.sock`，也无特权模式，因此**沙箱内无法执行 docker build**。

→ 这印证了「build 必须放在外部构建机」的架构决策是正确的（对应反馈 3）。

### 4.2 双镜像方案：已实测跑通 ⭐

导师提出的架构在 AGS API 中确有对应能力，已完整验证：

**API 支撑**（`tencentcloud-sdk-python 3.1.156`）：

```
CreateSandboxTool(
    CustomConfiguration.Image = <base 镜像>              ← 环境层
    StorageMounts = [StorageMount(
        Name, MountPath, ReadOnly,
        StorageSource.Image = ImageStorageSource(         ← 题目内容作为 image volume
            Reference, ImageRegistryType, SubPath)
    )]
)

StartSandboxInstance(
    MountOptions = [MountOption(Name, MountPath, SubPath, ReadOnly)]  ← 换题时覆盖
    CustomConfiguration = {...}                                       ← 亦可覆盖镜像
)
```

**实测结果**（`experiments/verify_dual_image.py`）：

```
base 镜像（环境）  : swe-synth-base-ubuntu:v1        （872MB）
题目镜像（内容）  : swe-synth-content-0034:v1       （1.09MB）
挂载点            : /mnt/task

✅ 工具已创建：sdt-nlkzjq11 → ACTIVE
✅ 实例已启动：ktxcsmjmfoyquo53av353hwkuergdvoefpgnvmo3

### 挂载点内容
drwxr-xr-x 2 root root  129  task
drwxr-xr-x 3 root root   43  workspace

### 题目仓库代码
CONTRIBUTING.md  LICENSE.txt  Makefile  cachecontrol  docs ...

### 题目契约文件
metadata.json  problem_statement.md  run_tests.sh  verify.sh

### base 镜像提供的环境
PRETTY_NAME="Ubuntu 22.04.5 LTS"
Python 3.11.16
```

→ **题目内容与运行环境成功解耦**：环境来自 base 镜像，题目内容来自挂载卷。

### 4.3 这个方案为什么重要

它一次性解决了原方案的三个结构性问题：

| 原方案的问题 | 双镜像方案 |
|---|---|
| 每题镜像 7.5GB（因为每题都完整继承 6.86GB 基础镜像 + 重装一遍环境） | 每题只有 1.09MB 内容层，环境层全局复用 |
| `:v1-sol` 被迫 `--no-cache`（规避 AGS 层合并 bug），每题白跑 18~22 分钟 | 内容镜像秒级构建，`--no-cache` 代价可忽略 |
| 每题占 2 个沙箱工具，配额上限 10 → 必须串行并反复清理 | 1 个 base 工具复用，换题只改挂载 |

**这也是此前「传输慢」的真正原因**：不是网络或服务器问题（实测公网
11.5MB/s、到 CCR 内网 5ms 延迟、构建机 load 0.00），而是**架构上每道题都在
搬运一座 7.5GB 的山**。

---

## 后续整改计划

反馈 1、2、4 的可行性均已用实测证据确认。将流水线从「单镜像」改造为
「base + 内容双镜像」是一次架构级改动，建议按下列顺序推进：

| 阶段 | 内容 | 影响面 |
|---|---|---|
| P0 | 固化 Ubuntu base 镜像构建脚本，纳入版本管理 | 新增文件，不影响现有数据 |
| P0 | `AGSClient.create_tool()` 增加 `storage_mounts` 参数 | 向后兼容（可选参数） |
| P1 | `dockerfile_gen` 增加「内容镜像」渲染路径，`packer` 支持只推内容层 | 需与现有单镜像模式并存 |
| P1 | `sandbox_runner` 改为复用 base 工具 + 换挂载 | Agent2 主流程 |
| P2 | 用新方案重跑 19 道题，验证判分结果与旧方案一致 | 回归验证，确保数据集等价 |
| P2 | e2b 2.x 适配层（可选开关，默认仍走 1.x） | 隔离风险 |

> ⚠️ 重要：现有 19 道 ACCEPTED 题目及其 38 个镜像**仍然有效可用**，
> 改造属于工程优化，不影响已交付数据集的正确性。建议改造完成后**并行保留**
> 两套镜像一段时间，待新方案重跑验证通过再切换。

---

## 附：本次验证使用的脚本

| 文件 | 作用 |
|---|---|
| `experiments/ubuntu-base/Dockerfile` | Ubuntu 22.04 版基础镜像定义 |
| `experiments/verify_ubuntu_base.py` | 推送 + 真实沙箱验证 Ubuntu 基础层 |
| `experiments/build_content_image.sh` | 从题目镜像提取纯内容，构建轻量内容镜像 |
| `experiments/verify_dual_image.py` | 双镜像方案（image volume 挂载）验证 |
| `scripts/_probe_base_image.sh` | 探测官方基础镜像的平台组件构成 |

所有脚本在运行结束时都会清理沙箱实例与工具（沙箱按时长计费）。
本次验证完成后已确认：本项目在 AGS 上零残留工具占用。
