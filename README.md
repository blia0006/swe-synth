# SWE-Synth — 双 Agent 协作的 SWE 题目自动构建与验证

> 课题三「数据合成」：从 GitHub 开源仓库**自动合成全新的 SWE 题目**，规避 SWE-bench
> 的 Issue-PR 数据污染，产出 ≥10 道通过全流程验证的题目。
>
> - Agent1（出题 + 打包）：分析仓库 → LLM 出题 → 写 Dockerfile → build → push 到 CCR
> - Agent2（验证）：拉取镜像 → 在腾讯云 Agent Sandbox 启动 → 执行验证 → 无重叠校验
> - **生产默认：Agent1 + Agent2 全流程运行在同一个腾讯云 AGS 沙箱实例内部**，
>   本机只做编排、不参与任何出题/打包/验证计算，**本机零依赖**（不需要 Docker、
>   不需要 Python 环境、不需要任何凭证落盘）
> - LLM：TokenHub（`deepseek-v4-pro-202606`）；沙箱：腾讯云 AGS；镜像仓库：腾讯云 CCR

---

## 一、架构总览（双 Agent 协作，全程跑在云端沙箱内）

> 这是当前**生产默认架构**（`scripts/run_in_sandbox.py`）。本机不跑任何业务
> 逻辑，只负责“喊沙箱去干活”；出题、打包镜像、双向验证、交付前校验这四个阶段
> **全部在同一个 AGS 沙箱实例内部完成**。

```
┌── 本机（仅编排，零业务依赖）────────────────────────────────────────────────┐
│  1. 创建 AGS 沙箱实例                                                       │
│  2. 打包源码（tar.gz，凭证不打包）→ 上传进沙箱                              │
│  3. 通过 commands.run 发起远程命令（nohup 后台跑，凭证经 envs= 逐次注入，    │
│     不落盘、不写入沙箱磁盘）                                                │
│  4. 巡检状态 / 到点续期 → 完成后下载产出 → 回收实例                          │
└──────────────────────────────┬───────────────────────────────────────────────┘
                                │ 远程命令 + 源码上传
                                ▼
┌── AGS 沙箱实例内部（Agent1 + Agent2 全部计算在此完成）────────────────────────┐
│                                                                              │
│  ┌── Agent1：出题 + 打包 ───────────────────────────────────────────────┐   │
│  │  git clone 仓库 ──► 分析结构 ──► LLM 生成题目                       │   │
│  │      (repos.yaml)         (功能实现 / 模块添加 / 重构)               │   │
│  │              │                                                       │   │
│  │              ▼                                                       │   │
│  │       双向 sanity（stub 态必红 / golden 态必绿）                     │   │
│  │              │                                                       │   │
│  │              ▼                                                       │   │
│  │       solve-back（LLM 只看题干能否独立解出）← 终极判据               │   │
│  │              │                                                       │   │
│  │              ▼                                                       │   │
│  │       写 Dockerfile ──► buildah build ──► push 到 CCR                │   │
│  │       （沙箱内无 docker.sock/DinD，用免 daemon 的 buildah 代替）      │   │
│  │                                                                       │   │
│  │  产物：任务镜像 :v1（无答案） + 答案镜像 :v1-sol（含 golden.patch）   │   │
│  └───────────────────────────┬───────────────────────────────────────────┘   │
│                              │ 镜像内统一契约 /task/metadata.json            │
│  ┌───────────────────────────▼───────────────────────────────────────────┐   │
│  │  Agent2：验证（在沙箱内再拉起一个临时子沙箱实例执行判分）              │   │
│  │  拉取 :v1 镜像 ──► 临时 Sandbox 启动 ──► 执行 /task/verify.sh         │   │
│  │      · 空解（不改代码）必须失败 → 证明题目"有内容"                    │   │
│  │      · golden 解（打 golden.patch）必须通过 → 证明题目"可解"          │   │
│  │  校验与仓库现有 PR/commit/bugfix 无重叠（GitHub API 检索）            │   │
│  └───────────────────────────┬───────────────────────────────────────────┘   │
│                              ▼                                              │
│         validate：产出 data/tasks.jsonl（JSON Lines，state=ACCEPTED）       │
└──────────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼ 巡检脚本下载回本机
        本机 data/tasks.jsonl + data/proofs/<task_id>/（最终交付物）
```

**要点：**

1. **本机零依赖**：本机不需要装 Docker、不需要跑 Python 依赖、不需要挂载任何
   凭证文件——所有凭证以环境变量形式随远程命令逐次注入沙箱进程，用完即随进程
   销毁，本机磁盘上不落一份密钥。本机唯一动作是发指令、传文件、收结果。
2. **两个 Agent 解耦**：Agent1 和 Agent2 通过**镜像内的 `/task/metadata.json`**
   解耦——Agent1 只负责生产镜像，Agent2 只依赖镜像内的统一契约（题干 / 判据 /
   验证脚本），互不知道对方的实现细节。
3. **打包环节用 `buildah` 代替 `docker`**：沙箱内没有 `docker.sock`、不支持
   DinD（详见 `review-feedback-report.md` 意见4），因此打包镜像用无需 daemon
   的 `buildah` 完成，效果与 `docker build/push` 等价。
4. **双镜像复用机制**：env+工具的 base 镜像通过 AGS `StorageMounts` 固定挂载
   到沙箱内，创建一次全程复用；每道题的内容镜像通过 `CustomConfiguration.Image`
   按实例整体覆盖切换，换题不用重建沙箱工具、不占用额外配额（实测细节见
   `review-feedback-report.md` §4.5）。

```bash
# 一条命令：起沙箱环境 + 后台启动全流程，立即返回（不阻塞本机）
python scripts/run_in_sandbox.py --n 10
python scripts/run_in_sandbox.py --n 10 --stages agent1,pack,agent2,validate

# 巡检进度 / 完成后自动下载产出 / 回收实例
python scripts/sandbox_status.py --instance <instance_id>
```

> 仅用于**开发调试单步验证**时，也可以在本机直接跑 `scripts/run_pipeline.py`
> （见下方第二、三节）；但这需要本机装 Python/Docker 环境，属于**非生产路径**，
> 交付的 29 道题目绝大多数产出于本节描述的全沙箱内生产路径。

---

## 二、快速开始

### 2.1 环境准备

```bash
# 1) 依赖（Python 3.11 + venv）
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 凭证配置（每个变量去哪拿见 .env.example 注释）
cp .env.example .env
# 填入：TOKENHUB_API_KEY / E2B_API_KEY / TENCENTCLOUD_SECRET_ID/KEY /
#       AGS_ROLE_ARN / TCR_USERNAME / TCR_PASSWORD / GITHUB_TOKEN
```

> 运行一律用 `.venv/bin/python`（本机默认 `python3` 可能是 3.9，不能用）。

### 2.2 环境自检（M0 门禁）

```bash
.venv/bin/python scripts/check_env.py     # 6 项：local / tokenhub / github / tcr / sandbox / dind
.venv/bin/python scripts/probe_cloud.py   # 只读探测 CAM/TCR/AGS/TokenHub 家底
```

---

## 三、流水线启动方式

统一入口 `scripts/run_pipeline.py`，三个子命令：

```bash
# 1) 查看候选仓库池（课题要求 Star>100）
.venv/bin/python scripts/run_pipeline.py list-repos

# 2) 跑 Agent1：出题 + 生成构建上下文（不含 build/push）
.venv/bin/python scripts/run_pipeline.py agent1 --repo psf/cachecontrol --n 1
.venv/bin/python scripts/run_pipeline.py agent1 --type ABC --n 12 --per-repo 3

# 3) 校验数据集 + 逐项对标课题交付标准
.venv/bin/python scripts/run_pipeline.py validate
```

### 3.1 `agent1` 子命令参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` | 全部 | 只处理指定仓库（如 `psf/cachecontrol`） |
| `--n` | 1 | 本次目标产出题数 |
| `--type` | `ABC` | 题型组合：`A`=功能实现，`B`=模块添加，`C`=重构 |
| `--per-repo` | 3 | 每个仓库最多产出几道 |
| `--max-candidates` | 8 | 每仓库最多尝试的候选靶点数 |
| `--python` | 当前解释器 | 用于创建仓库专用 venv 的基础解释器 |
| `--no-solve-back` | 关 | 跳过 solve-back 可解性验证（仅调试用，题干准确性无保障） |
| `--verified-only` | 关 | 只用已验证基线的仓库 |

### 3.2 题型说明

| 简写 | 题型 | 题目态 | 判据来源 | 答案来源 |
|---|---|---|---|---|
| `A` | 功能实现 | 函数体 AST 挖空 | 仓库自带测试 | 原始实现（100% 可靠） |
| `B` | 模块添加 | 新模块骨架 + LLM 新测试 | LLM 新写的测试 | LLM 参考实现 |
| `C` | 重构 | 原代码 + 重构守卫测试 | 自动生成的静态指标守卫测试 | LLM 参考重构 |

三道质量关卡（三类题型共用）：**双向 sanity**（题目态必红 / golden 态必绿）→
**solve-back**（只看题干能否解出）→ **schema 强制校验**（含防作弊）。

---

## 四、参数配置

### 4.1 `.env`（凭证，绝不入库）

| 变量 | 用途 |
|---|---|
| `TOKENHUB_BASE_URL` / `TOKENHUB_API_KEY` | LLM 网关（Agent1 出题） |
| `TOKENHUB_MODEL` | 出题模型，默认 `deepseek-v4-pro-202606` |
| `E2B_API_KEY` / `E2B_DOMAIN` | 腾讯云 AGS（Agent2 沙箱），`E2B_DOMAIN=ap-guangzhou.tencentags.com` |
| `AGS_SANDBOX_TEMPLATE` | 沙箱工具名（= E2B 的 template） |
| `TENCENTCLOUD_SECRET_ID` / `SECRET_KEY` | 腾讯云 OpenAPI（创建沙箱工具） |
| `AGS_ROLE_ARN` | 创建沙箱工具用的 CAM 角色（载体=Agent Runtime） |
| `TCR_REGISTRY` / `TCR_NAMESPACE` / `TCR_USERNAME` / `TCR_PASSWORD` | 镜像仓库凭证 |
| `TCR_REGISTRY_TYPE` | `personal`（CCR 个人版）/ `enterprise`（企业版） |
| `GITHUB_TOKEN` | 仓库 clone + 无重叠交叉比对检索（`public_repo` 读权限即可） |
| `DOCKER_HOST` | **构建机地址**（见下方 §4.3），如 `ssh://root@<IP>`。留空则用本机 Docker |

### 4.3 构建环境：`docker build` 到底在哪里执行

> 这一节回答「题目镜像是在哪里 build 的、整条流水线在哪里跑」。

**生产默认方案（当前，`scripts/run_in_sandbox.py`）：整条流水线收进同一个云端沙箱实例**

出题（Agent1）→ 打包（buildah build+push）→ 验证（Agent2）→ 校验（validate）
**全部在一个 AGS 沙箱实例内部执行**，本机只做编排（建实例、传源码、发远程命令、
下载产出、回收实例），不参与任何实际计算：

```
┌── 本机（仅编排，不跑业务逻辑）──────────────────────────────┐
│  创建沙箱实例 → 上传源码（tar.gz，凭证不打包）              │
│  → 发起远程命令（nohup 后台跑，断网/合盖不影响）             │
│  → 巡检状态 / 下载产出 / 回收实例                            │
└────────────────┬────────────────────────────────────────────┘
                 │ commands.run（凭证经 envs= 逐次注入，不落盘）
                 ▼
┌── AGS 沙箱实例（全部计算在此完成）───────────────────────────┐
│  Agent1 出题：git clone → AST 挖空/LLM 出题 → 本地 pytest 预检│
│  pack：buildah build + push 到 CCR（沙箱无 docker.sock/DinD，│
│         用免 daemon 的 buildah 代替 docker）                 │
│  Agent2 验证：起临时沙箱做双向判分（空解必红/golden 必绿）    │
│  validate：产出 data/tasks.jsonl 等结果文件                  │
└───────────────────────────────────────────────────────────────┘
```

```bash
python scripts/run_in_sandbox.py --n 10                      # 起环境 + 后台启动流水线，立即返回
python scripts/run_in_sandbox.py --n 10 --stages agent1,pack,agent2,validate
python scripts/sandbox_status.py --instance <instance_id>    # 查看进度 / 下载产出 / 回收实例
```

**本地调试方案（可选，仅用于开发阶段单步调试）**：也支持在本机直接跑
`scripts/run_pipeline.py agent1/validate`，此时 `docker build` 可通过 `DOCKER_HOST`
指向一台远端 amd64 构建机（因为开发机常是 Apple Silicon arm64，而镜像需要
`linux/amd64`；AGS 沙箱内也没有 docker CLI，无法在沙箱内直接 `docker build`）：

```bash
# 留空则用本机 Docker；开发机是 arm64 时需指向远端 amd64 构建机
DOCKER_HOST=ssh://root@<构建机IP>
```

> 构建机参考规格：2 核 / 3.5GB 内存 / 50GB 磁盘即可跑通。
>
> **交付时已在生产流水线里落地的双镜像方案**（`swe_synth/agent1/base_image/` +
> `swe_synth/agent2/sandbox_runner.py`，实测证据见 `review-feedback-report.md` §4.5）：
> base（env+工具）作为共享层只 build/push 一次，通过 AGS `StorageMounts` 挂载到沙箱的
> `/mnt/base-env`（只读、创建时固定）；每道题的镜像只剩内容层（**1MB 级**），换题时通过
> `StartSandboxInstance` 的实例级 `CustomConfiguration.Image` 覆盖，不再逐题重复搬运
> base。`config/settings.yaml` 的 `image.base` 已指向该共享 base 镜像，新出的题目会
> 自动走这条路径。
>
> 29 道题中有 7 道（`swe-synth-0010/0023~0028`）的镜像是升级前构建的，仍是单镜像
> 形态（每个 ~7.5GB，因原基础层直接继承官方 Debian 沙箱镜像 6.86GB）——这些镜像
> 不会被回溯重建，以保持已验收证据链不变；另外 22 道已是升级后在生产流水线里
> 批量跑出的产物（`Dockerfile FROM` 共享 Ubuntu base，内容层仅几十 MB），验证该
> 架构在生产路径真实可用、可规模化复用。

### 4.2 `config/settings.yaml`（可调参数）

| 区块 | 关键项 | 说明 |
|---|---|---|
| `models` | `task_design` / `repo_analyze` / `overlap_judge` | 模型分工 |
| `llm` | `max_tokens`（8192）/ `temperature` / `max_concurrency`（4） | 推理模型 `max_tokens` 须给足（思维链占额度） |
| `stubbing` | `min_body_lines`（4）/ `max_body_lines`（60）/ `require_test_coverage` | A 类挖空靶点筛选 |
| `refactoring` | `min_body_lines`（12）/ `min_cyclomatic`（6）/ `slack_ratio`（0.25） | C 类守卫阈值推导 |
| `task_mix.quota` | `feature_implementation:6` / `module_addition:3` / `refactoring:3` | 三类题型配比 |
| `image` | `base` / `tag_task`（v1）/ `tag_solution`（v1-sol） | 镜像基础层与 tag 约定 |
| `sandbox` | `timeout_sec` / `cpu` / `memory` / `probe` | Agent2 沙箱实例参数 |
| `overlap_check` | `bm25_top_k` / `embedding_threshold`（0.85）/ `llm_judge` | 无重叠三级过滤 |
| `output` | `tasks_jsonl` / `proofs_dir` / `state_db` / `report` | 产物路径 |

---

## 五、结果文件格式

### 5.1 `data/tasks.jsonl`（JSON Lines，一行一题）

每条记录是一个 `SweTask` 对象，关键字段：

| 字段 | 说明 |
|---|---|
| `task_id` | 唯一编号，形如 `swe-synth-0001` |
| `task_type` | `feature_implementation` / `module_addition` / `refactoring` |
| `difficulty` | `easy` / `medium` / `hard` |
| `state` | 状态机：`LOCAL_OK` → `IMAGE_PUSHED` → `SANDBOX_OK` → `OVERLAP_OK` → `ACCEPTED` |
| `repo` / `repo_stars` / `base_commit` | 来源仓库（Star>100 留证） |
| `problem_statement` | 题干（含 6 个必需小节，schema 强制校验） |
| `modified_files` / `do_not_modify` | 可改文件 / 判据文件（防作弊） |
| `FAIL_TO_PASS` / `PASS_TO_PASS` | 判据测试用例列表 |
| `image` / `solution_image` | 任务镜像 / 答案镜像地址 |
| `verify_script` | 判分入口（镜像内 `/task/verify.sh`） |
| `validation` | 验证证据（双向 sanity + solve-back + 沙箱实例 ID） |
| `overlap_check` | 无重叠校验结果 |

### 5.2 `data/proofs/<task_id>/`（通过证明）

```
problem_statement.md   题干
task.json              完整题目记录（= tasks.jsonl 的一行）
metadata.json          含 symbol / modified_files / 判据（供续跑去重）
local_sanity.json      双向 sanity 结论（题目态必红 / golden 态必绿）
task_run.log           题目态测试日志（FAIL_TO_PASS 全红的证据）
golden_run.log         golden 态测试日志（全绿的证据）
solve_back.json        可解性验证证据（LLM 只看题干能否解出）
stub.patch / golden.patch   题目态 / 答案 patch
Dockerfile             镜像定义
```

### 5.3 镜像内 `/task/` 契约（Agent2 的统一接口）

```
/task/problem_statement.md   题干（交付被测 Coding Agent）
/task/metadata.json          repo/base_commit/test_cmd/FAIL_TO_PASS/PASS_TO_PASS
/task/run_tests.sh           只跑测试
/task/verify.sh              判分入口 → 退出码 + /task/result.json
/workspace/repo/             已 stub 化的仓库（base_commit 固定）
/opt/solution/golden.patch   仅 :v1-sol 镜像存在（防泄题）
```

---

## 六、技术权衡说明

### 6.1 基础镜像：`ubuntu:22.04` —— 已实测通过并切换为生产默认

课题要求镜像基于 `ubuntu:22.04`。腾讯云 Agent Sandbox 官方文档规定：若需使用
`run_code` / `commands.run` / `files.*` 等代码解释器能力，自定义镜像**必须**继承官方
`ags-image/sandbox-code` 基础镜像（内含 S6-Overlay `/init`、envd 与 run-code 服务，
监听 49983/49999）。使用裸 `ubuntu:22.04` 会导致沙箱无法启动、Agent2 无法执行验证。

本项目只用到 `commands.run`/`files.*`，不用 `run_code`；实测把官方镜像里这两项依赖的
静态组件（S6-Overlay + envd）搬到 `ubuntu:22.04` 之上完全可行（详见
`review-feedback-report.md` §「意见1」），已固化为 `swe_synth/agent1/base_image/`
下的 Dockerfile + `build_and_push.sh`，构建产物验证结果：

```
PRETTY_NAME="Ubuntu 22.04.5 LTS"
镜像体积 872MB（原 Debian 基础层 6.86GB → 降 87%）
```

**该 base 已构建推送，并回填到 `config/settings.yaml` 的 `image.base`，是当前生产
默认**——此后新出的题目镜像字面满足课题的 `ubuntu:22.04` 要求，且配合双镜像方案（见
§4.3），每题只需 build/push 内容层（MB 级）。当前 29 道题中 7 道镜像是升级前的产物，
仍是 Debian 基础层，不受影响；另外 22 道已是该架构在生产环境下的批量实测产出（详见
§4.3 说明）。

### 6.2 SDK：`e2b-code-interpreter` 2.x（生产默认）+ `agent-sandbox` 互补

- `e2b-code-interpreter`（`>=2.9.0,<3.0.0`）：创建/连接沙箱实例。2.x 客户端多了一层
  对 Key 格式（`e2b_` 前缀）的本地校验，AGS 签发的 Key 格式不同会被拦在客户端；用官方
  提供的环境变量开关 `E2B_VALIDATE_API_KEY=false` 跳过纯格式校验即可（`sandbox_runner.py`
  已在导入前设置），**鉴权与协议本身不受影响**，已实测在真实沙箱中创建/连接/执行命令
  全部走通。
- `agent-sandbox`：连接已有实例后操作内部能力（bash/code/file 等）。

二者互补：实例创建走 e2b，`verify.sh` 执行优先走 `agent-sandbox` 的 bash 能力。

### 6.3 镜像仓库：CCR 个人版（同产品个人版）

课题要求「腾讯云 TCR（容器镜像服务）」。CCR 是**同一产品的个人版**，官方文档
`/1814/129691` 明确支持 `ImageRegistryType=personal`，且账号级服务无实例到期风险
（团队企业版实例均属他人且临近到期）。功能与验收要求等价。

### 6.4 模型：`deepseek-v4-pro-202606`

属课题推荐的 `deepseek-v4-pro` 系列快照版，价格便宜 4 倍（3/6 vs 12/24 元/百万 tokens）。
`config/settings.yaml` 可一键切回 `deepseek-v4-pro` 或 `glm-5`。

---

## 七、成本与耗时（实测）

| 题型 | 单题耗时 | 单题成本（LLM） |
|---|---|---|
| A 功能实现 | 56~122s | 0.023~0.044 元 |
| B 模块添加 | ~300s | ~0.17 元 |
| C 重构 | ~390s | ~0.23 元 |

> 每个模型各有 100 万 token 免费额度；按上述单题成本估算，产出 29 道题的
> LLM 总成本约数元至十余元量级（含候选题被淘汰的探索成本）。

---

## 八、目录结构

```
config/            settings.yaml（参数）+ repos.yaml（仓库池）
scripts/           run_pipeline.py（入口）/ check_env.py / probe_cloud.py
swe_synth/
  agent1/          出题 + 打包：stubber / repo_analyzer / task_designer /
                   module_designer / refactor_designer / refactor_metrics /
                   local_validator / solvability / dockerfile_gen / pipeline
  agent2/          验证：sandbox_runner / overlap_check（GitHub 去重）
  clients/         tokenhub / sandbox / tcr
  schemas/         task.py（SweTask 强制校验）
data/              tasks.jsonl（交付）/ proofs/（通过证明）/ report.json（统计）
```

---

## 九、当前进度与已知偏差

详见 `PROGRESS.md`（进度）、`midterm-audit.md`（中期对标）、`requirements-check.md`
（验收逐条核对）、`TASK-SPEC.md`（课题原文，唯一基准）。
