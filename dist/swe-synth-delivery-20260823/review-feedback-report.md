# 外部评审意见的验证与整改报告

> 日期：2026-08-21（8-21 三次更新：e2b 2.x 与「base 走挂载卷 + 题目走
> CustomConfiguration.Image」的双镜像方案均已**按用户明确要求的架构**
> 实测通过并接入生产代码，见 4.5 与文末更新记录）
> 说明：共 4 条评审意见，本文逐条给出**实测结论**与整改方案。
> 所有结论均来自真实环境实测（远端 amd64 构建机 + 腾讯云 AGS 沙箱），非推测。
>
> **⭐ 最新状态（2026-08-23）**：数据集最终核实为 **24 道题全部 ACCEPTED**（其中
> 12 道已用本报告 4.5 节的双镜像新架构），详见 `交付说明.md`。本文数字（19 道）
> 是撰写时的快照，整改结论与技术方案不受影响。

---

## 结论速览

| # | 评审意见 | 结论 | 状态 |
|---|---|---|---|
| 1 | Ubuntu base 变成了 Debian，客户几乎都是 Ubuntu | **可行，已实测通过**。镜像同时从 6.86GB 降到 872MB | ✅ 已解决 |
| 2 | e2b SDK 2.x 没试过 | **已实测协议兼容，现已是生产默认**（`E2B_VALIDATE_API_KEY=false` 跳过客户端一行格式校验，鉴权/协议不受影响），真实沙箱验证跑通 | ✅ 已落地 |
| 3 | build 题的环境没看到，在哪里？ | 在远端 amd64 构建机上，通过 `DOCKER_HOST=ssh://` 驱动。**此前文档未说明，是交付文档的缺陷** | ✅ 已补文档 |
| 4 | DinD 没验证；AGS 支持双镜像方式 | **DinD 已实测确认不可用**（无 docker.sock）；双镜像方案**按用户描述的确切架构**（base 走 `StorageMounts` 固定挂载卷 + 题目走 `CustomConfiguration.Image` 实例级覆盖）已重新实测通过（见 4.5）并**接入生产共享工具**，真实验证一道题 29.7 秒内 ACCEPTED | ✅ 已验证并落地 |

**综合收益**（意见 1 + 4 落地后）：

| 指标 | 原方案 | 新方案 | 改善 |
|---|---|---|---|
| 基础层发行版 | Debian 13 (trixie) | **Ubuntu 22.04.5 LTS** | 符合课题原文与客户环境 |
| 每题镜像体积 | 7.5 GB | **1.09 MB**（+ 共享 base 872MB，另有独立只读挂载卷一份） | ↓ 约 7000 倍 |
| 每题 build 耗时 | 6~22 分钟 | 秒级 | ↓ 两个数量级 |
| 每题 push 耗时 | 1000+ 秒 | **2.3 秒** | ↓ 约 450 倍 |
| 沙箱工具占用 | 每题 2 个（配额上限 10） | 1 个工具复用，换题靠实例级 `CustomConfiguration.Image` 覆盖；base 通过 `StorageMounts` 常驻挂载 | 不再受配额制约 |
| e2b SDK | 1.x（生产默认） | **2.x（生产默认）** | 已实测走通 |

---

## 意见 1：基础层改回 Ubuntu

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

## 意见 2：e2b SDK 2.x

### 实测结论：协议兼容，已作为生产默认落地

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

### 修正：客户端校验有官方开关，无需侵入式绕过

进一步排查 e2b 2.x 源码（`e2b/connection_config.py`）发现，该格式校验受
`E2B_VALIDATE_API_KEY` 环境变量控制（官方留的正规开关，不是私改内部实现），
设为 `false` 即可跳过纯格式校验，**鉴权本身（API Key 是否有效）、协议
内容都不受影响**：

```bash
export E2B_VALIDATE_API_KEY=false
```

`swe_synth/agent2/sandbox_runner.py` 已在导入 `e2b_code_interpreter` 之前
用 `os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")` 设置该开关，
`requirements.txt` 已把版本锁改为 `e2b-code-interpreter>=2.9.0,<3.0.0`。

### 2.x 相比 1.x 的实质收益

`Sandbox.connect()`（2.x 命名更明确的重连 API）+ `commands.run()` 已在真实
生产流水线（`run_pipeline.py agent2`）里验证跑通：对 `swe-synth-0036` 强制
重新验证，全程用 2.x 连接沙箱，29.7 秒内判定 `ACCEPTED`，结果与旧 1.x 时期
完全一致（FAIL_TO_PASS/PASS_TO_PASS 判据不受 SDK 版本影响）。

### 当前状态

**生产代码默认已是 2.x**，1.x 兼容分支保留在 `_connect_sandbox()` 里作为
兜底（`Sandbox` 类没有 `connect` 属性时自动退回 `Sandbox(sandbox_id=...)`
构造，覆盖环境意外装回 1.x 的情况）。

> 长期看，更干净的方案仍是反馈给 AGS 侧，请其在服务端签发 `e2b_` 兼容格式
> 的 Key，这样连官方开关都不用设置；但眼下用官方提供的环境变量开关已经是
> 非侵入式的正规做法，不依赖私改第三方库内部实现，可以放心作为生产默认。

---

## 意见 3：build 题的环境在哪里

### 这是此前交付文档的缺陷，已修正

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
| 平台限制 | AGS 沙箱内**没有 docker CLI、也没有 DinD**（见意见 4），因此 build 无法放在沙箱内 |
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
- 交付说明：明确 build 发生在何处，以及自行复现需要准备什么

---

## 意见 4：DinD 与双镜像方案

### 4.1 DinD：已实测，确认不可用

在 Ubuntu 版自定义镜像的真实沙箱实例内实测：

```
### 3. DinD 可用性
docker.sock 不存在 → 无 DinD（需通过挂载或特权容器提供）
```

镜像内**已安装 docker CLI**（`Docker version 29.1.3`），但沙箱运行时未注入
`/var/run/docker.sock`，也无特权模式，因此**沙箱内无法执行 docker build**。

→ 这印证了「build 必须放在外部构建机」的架构决策是正确的（对应意见 3）。
这是一个明确的**架构结论**：不是尚待解决的缺口，而是"沙箱不做 build，build
放外部构建机"这个既有设计的实测证据。

### 4.2 双镜像方案：已实测跑通 ⭐

最初设想的架构在 AGS API 中确有对应能力，已完整验证：

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

### 4.3 重要纠正：上面这个方向搞反了，正确方案更简单

复盘最初的架构设想——「env+工具的 base image **放在 volume image mount
位置**，题目 image **放在标准 tools 的 image 地方**，每次换题就通过 e2b
custom config 覆盖」——对照 AGS SDK 的真实字段定义
（`tencentcloud.ags.v20250920.models`）逐一核对后发现，**4.2 的实现方向反了**：

```
MountOption(Name, MountPath, SubPath, ReadOnly)   ← 没有 Reference 字段！
CustomConfiguration(Image, ...)                    ← 有 Image 字段，可整体覆盖
```

即：`StorageMounts` 挂载卷背后指向的镜像地址在 `CreateSandboxTool` 时就
**写死**了，`StartSandboxInstance` 的 `MountOptions` 只能微调路径/只读属性，
**换不了挂载卷指向的镜像**；只有 `CustomConfiguration.Image` 才支持
**按实例整体覆盖**（无需重建工具）。

4.2 的验证把 base（不变）放进了 `CustomConfiguration.Image`，把题目内容（要变）
放进了 `StorageMounts`——如果不改方向，"换题只改挂载、复用同一工具"这个目标
其实**做不到**：每换一道新题的挂载镜像，仍然要重新 `CreateSandboxTool`，
配额问题并未真正解决。

**更进一步**：既然沙箱主镜像必须自带 envd/S6 才能被平台接管（否则起不来），
"题目镜像"本身也得 `FROM base + COPY 题目内容`——这正是意见 1 已经做到的
"每题 1.09MB 内容层 + 872MB 共享 base 层"（Docker 分层天然去重）。也就是说
**根本不需要 `StorageMounts`/`ImageStorageSource` 这个额外机制**，用
"共享 base 层的普通题目镜像 + 实例级 `CustomConfiguration.Image` 覆盖"
就能同时满足：环境复用、镜像体积小、复用同一沙箱工具换题。

**修正后的实测**（`experiments/verify_customconfig_switch.py`，同一个工具、
中途不调用 `CreateSandboxTool`）：

```
[1] 创建工具 swe-synth-cc-switch-probe（默认镜像 = 题目 0034）
[2] 启动实例 #1（不覆盖）→ 内容 = swe-synth-0034（psf/cachecontrol）
[3] 启动实例 #2（同一工具 ID，仅覆盖 CustomConfiguration.Image = 题目 0007）
    → 内容 = swe-synth-0007（pallets/itsdangerous）
[4] 全程同名工具数：1 ✅   两次内容确实不同 ✅   两次环境层输出一致 ✅
```

→ **结论成立且已用真实沙箱验证**：只需在 `StartSandboxInstance` 时覆盖
`CustomConfiguration.Image`，即可复用同一个沙箱工具切换任意题目，不占用
额外配额，不需要 `StorageMounts`。4.2 的挂载卷验证证明了 API 参数本身可用，
但作为"换题机制"应采用本节方案，而不是 4.2 的方向。

> `verify_dual_image.py` 与 `verify_customconfig_switch.py` 都保留在
> `experiments/`：前者证明"挂载卷机制可用"，后者证明"正确方向可用"，
> 两者结论不矛盾，只是后者才是最初设想、也是唯一能落地的方向。

### 4.4 这个方案为什么重要

它一次性解决了原方案的三个结构性问题：

| 原方案的问题 | 新方案（共享 base 层镜像 + 实例级 Image 覆盖） |
|---|---|
| 每题镜像 7.5GB（因为每题都完整继承 6.86GB 基础镜像 + 重装一遍环境） | 每题只有 1.09MB 内容层，环境层全局复用（Docker 分层天然去重） |
| `:v1-sol` 被迫 `--no-cache`（规避 AGS 层合并 bug），每题白跑 18~22 分钟 | 内容镜像秒级构建，`--no-cache` 代价可忽略 |
| 每题占 2 个沙箱工具，配额上限 10 → 必须串行并反复清理 | 1 个工具复用，换题只改 `StartSandboxInstance` 的 `CustomConfiguration.Image` |

**这也是此前「传输慢」的真正原因**：不是网络或服务器问题（实测公网
11.5MB/s、到 CCR 内网 5ms 延迟、构建机 load 0.00），而是**架构上每道题都在
搬运一座 7.5GB 的山**。

### 4.5 按用户明确要求，重测「base 走挂载卷 + 题目走标准镜像位」的确切架构 ⭐⭐

4.3 的结论是"换题机制本身不需要 `StorageMounts`"，但这只回答了"能不能换题"，
没有回答"要不要额外用挂载卷承载 base"——这是两件不同的事。用户明确要求
按最初设想的确切架构验证：

```
base image（env+工具，不随题目变化）→ 放在 volume image mount 位置（StorageMounts）
题目 image（每次换题都变）          → 放在标准 tools 的 image 地方（CustomConfiguration.Image）
换题 = 通过 e2b custom config 覆盖 CustomConfiguration.Image
```

区别于 4.2（方向搞反：base 在 `CustomConfiguration.Image`，题目内容在
`StorageMounts`）——这次是**用户描述的正确方向**：base 固定不变，天然适合
放进「创建时写死、不能按实例切换」的挂载卷；题目内容天然适合放进「支持按
实例整体覆盖」的 `CustomConfiguration.Image`。两者刚好各自匹配 API 的能力
边界，而不是像 4.2 那样反过来用。

**真实实测**（`experiments/verify_dual_image_v2.py`，全程用 e2b 2.x 连接）：

```
[1] 创建工具 swe-synth-dualimage-v2-test
    StorageMounts = [base-env: swe-synth-base:ubuntu22.04-v1 → /mnt/base-env (只读)]
    CustomConfiguration.Image（占位） = swe-synth-0034:v1

[2] 实例 #1：CustomConfiguration.Image 覆盖 = swe-synth-0034:v1
    /mnt/base-env/etc/os-release → PRETTY_NAME="Ubuntu 22.04.5 LTS"（挂载卷生效）
    /task/metadata.json → task_id 对应 0034（主镜像内容生效）

[3] 实例 #2（同一个工具，未重建）：CustomConfiguration.Image 覆盖 = swe-synth-0007:v1
    /task/metadata.json → task_id 变为 0007（题目内容随实例正确切换）
    /mnt/base-env/etc/os-release → 仍是同样的 Ubuntu 22.04.5 LTS（挂载卷内容保持不变）

判定：
  挂载卷两次都能访问          : PASS
  题目内容随实例正确切换      : PASS
  挂载卷内容在换题后保持不变  : PASS
```

**结论：用户描述的确切架构完全成立，已用真实沙箱验证**——base 作为独立、
只读、创建时固定一次的挂载卷存在，题目内容通过实例级 `CustomConfiguration.Image`
覆盖正确切换，两条机制互不干扰。

**已接入生产代码**（不是留在 `experiments/` 里的孤立验证）：

- `swe_synth/clients/ags.py`：`create_tool()` 新增 `storage_mounts` 参数
  （构造 `StorageMount`/`StorageSource`/`ImageStorageSource`），`start_instance()`
  新增 `mount_options` 参数（构造 `MountOption`，可选的实例级路径覆盖）
- `swe_synth/agent2/sandbox_runner.py`：`_ensure_shared_tool()` 首次创建
  共享工具时，若配置了 `base_image` 就自动挂载到 `/mnt/base-env`（只读）
- `scripts/run_pipeline.py`：`agent2` 命令读取 `settings.get("image.base")`
  作为 `base_image` 传入，无需额外配置项

**真实生产验证**：删除旧的（无挂载卷版本的）共享工具后，用
`python3 scripts/run_pipeline.py agent2 --task-id swe-synth-0036 --force`
重新走一遍生产判分链路——新共享工具自动带上挂载卷（已用
`DescribeSandboxToolList` 查询确认 `StorageMounts[0].StorageSource.Image.Reference`
指向共享 base 镜像、`MountPath=/mnt/base-env`），全程用 e2b 2.x 连接，
**29.7 秒内判定 ACCEPTED**，与改造前的判分结果一致。

> `verify_dual_image.py`（4.2，方向反了的最初尝试）、
> `verify_customconfig_switch.py`（4.3，证明换题机制本身不依赖挂载卷）、
> `verify_dual_image_v2.py`（4.5，用户要求的确切架构，现已生产落地）
> 三份脚本都保留在 `experiments/`，完整记录了从最初设想到最终落地的
> 排查路径，互不矛盾。

---

## 后续整改计划（8-21 三次更新：P0/P1/P2 均已落地）

意见 1、2、4 的可行性均已用实测证据确认，并按用户要求的确切架构接入生产：

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 固化 Ubuntu base 镜像构建脚本，纳入版本管理 | ✅ 已完成：`swe_synth/agent1/base_image/Dockerfile` + `build_and_push.sh` |
| P0 | `AGSClient` 增加 `start_instance(tool_id, image_override=...)` / `stop_instance(instance_id)` 封装 | ✅ 已完成：`swe_synth/clients/ags.py` |
| P1 | `dockerfile_gen` 确保每题镜像统一 `FROM` 同一个 base，产出的内容层保持 KB~MB 级；未构建共享 base 时自动退回从零安装，不影响现有单镜像模式 | ✅ 已完成：`swe_synth/agent1/dockerfile_gen.py`（自愈式环境安装 + 泛化后的 `audit_dockerfile` 基础镜像校验） |
| P1 | `sandbox_runner` 改为复用同一共享工具 + 每次 `StartSandboxInstance` 时覆盖 `CustomConfiguration.Image` | ✅ 已完成：`swe_synth/agent2/sandbox_runner.py`（全流水线 1 个共享工具，`config/settings.yaml` 的 `sandbox.shared_tool_name`） |
| P2 | 执行 `swe_synth/agent1/base_image/build_and_push.sh` 构建并推送共享 base，并用新方案重跑题目验证判分结果与旧方案一致 | ✅ 已完成：base 已推送（`swe-synth-base:ubuntu22.04-v1`），新出题 `swe-synth-0036` 端到端 ACCEPTED |
| P2 | e2b 2.x 适配层（生产默认） | ✅ 已完成：`E2B_VALIDATE_API_KEY=false` 官方开关 + `requirements.txt` 锁 2.x |
| P2 | base 走 `StorageMounts` 挂载卷（用户明确要求的确切双镜像架构） | ✅ 已完成：`AGSClient.create_tool(storage_mounts=...)` + `_ensure_shared_tool(base_image=...)`，见 4.5 |

> ⚠️ 重要：现有 19 道 ACCEPTED 题目及其 38 个镜像**仍然有效可用**，
> 改造属于工程优化，不影响已交付数据集的正确性。`config/settings.yaml` 的
> `image.base` **现已指向共享 base 镜像**（`swe-synth-base:ubuntu22.04-v1`），
> 新出题/新验证都会自动走共享 base 快速路径 + 挂载卷；`dockerfile_gen` 保留
> 自愈式回退（找不到共享 base 时自动退回从零安装），属于**可回退**的渐进式
> 切换，不是一次性的破坏性变更。

---

## 附：本次验证使用的脚本

| 文件 | 作用 |
|---|---|
| `experiments/ubuntu-base/Dockerfile` | Ubuntu 22.04 版基础镜像定义（验证阶段产物，正式版见 `swe_synth/agent1/base_image/Dockerfile`） |
| `experiments/verify_ubuntu_base.py` | 推送 + 真实沙箱验证 Ubuntu 基础层 |
| `experiments/build_content_image.sh` | 从题目镜像提取纯内容，构建轻量内容镜像 |
| `experiments/verify_dual_image.py` | 挂载卷（`StorageMounts`）机制可行性验证——方向不是最终方案，见 4.3 |
| `experiments/verify_customconfig_switch.py` | 修正后的换题机制验证：同一工具 + 实例级 `CustomConfiguration.Image` 覆盖 |
| `experiments/verify_dual_image_v2.py` | ⭐ 用户明确要求的确切架构验证：base 走 `StorageMounts` 固定挂载 + 题目走 `CustomConfiguration.Image` 实例级覆盖，全程用 e2b 2.x，已接入生产（见 4.5） |
| `scripts/_probe_base_image.sh` | 探测官方基础镜像的平台组件构成 |

所有脚本在运行结束时都会清理沙箱实例与工具（沙箱按时长计费）。
本次验证完成后已确认：本项目在 AGS 上零残留工具占用。
