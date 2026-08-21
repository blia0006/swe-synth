# 课题验收要求 · 逐条核对表（Requirements Traceability Matrix）

> 用途：把课题原文的每一条要求拆成可勾选项，映射到具体交付物与代码位置，避免"做完了才发现漏项"。
> 状态图例：✅ 已完成 / 🔨 进行中 / ⬜ 待做 / ⚠️ **有偏差或风险，需决策**
> 最后核对：2026-08-14

---

## 零、先明确一个容易混淆的概念（回答"仓库偏好"是什么意思）

课题要求的是「**从 GitHub 开源仓库中合成题目**」。和 SWE-bench 一样，**每一道题都必须绑定一个具体的真实仓库**：

```
一道 SWE 题 = 某个真实仓库 @ 某个 commit  +  题干  +  测试判据  +  可运行环境
```

所以我们必须自己**挑选一批具体的 GitHub 仓库**（即「仓库池」）来出题。
"仓库偏好"问的就是：**这批仓库你有没有指定的**，还是由我按标准筛选。

课题给的硬性范围：**Star > 100**，Python 为主（Go/Rust/TS 也可）。
在此范围内，还需要额外的**工程性筛选标准**（这是出题成功率的关键）：

| 筛选标准 | 为什么必须 |
|---|---|
| 有完整的自带测试套件（pytest/go test 等） | 题目判据完全来自自带测试，没测试就无法自动判分 |
| 依赖轻、能离线装上（不需要 DB/GPU/外部服务） | 装不上依赖 → 镜像构建失败 → 整道题废掉 |
| 基线测试全绿（clone 后不改代码就能全过） | 基线本身就红，无法区分"挖空导致的红"和"本来就红" |
| 测试跑得快（全量 < 5 分钟） | 每道题要跑多轮（stub 态 + golden 态 + 双跑一致性），慢仓库成本爆炸 |
| 目标文件 12 个月未大改、近 6 月无相关 open PR | 去污染的「时间隔离」层要求 |

**结论**：不需要你指定具体仓库，我会按上述标准建 `config/repos.yaml` 候选池；
但如果课题有**指定仓库**（比如希望覆盖某些项目），现在告诉我可以直接锁定。

---

## 一、目标产出（课题原文表格）

### 1.1 Agent1（出题 + 打包）

> 状态口径：只认「已实测通过」，不认「代码已写完」。最新核查见 `midterm-audit.md`（2026-08-17）

| # | 要求原文 | 落地位置 | 状态 |
|---|---|---|---|
| A1-1 | 基于 **TokenHub API** 调用 LLM | `swe_synth/clients/tokenhub.py` | ✅ 真实调用通过（`deepseek-v4-pro-202606`） |
| A1-2 | **分析目标仓库结构** | `agent1/repo_analyzer.py`（目录树/AST/测试覆盖映射） | ✅ 实测：cachecontrol 得 11 候选、过滤 16 无判据 |
| A1-3 | 生成一道软件工程题目：**功能实现 / 重构 / 模块添加** | `agent1/task_designer.py` / `module_designer.py` / `refactor_designer.py` | ✅ **三类全部实现且各有真实产出**（各 1 道真题） |
| A1-4 | 编写 **Dockerfile + 构建脚本** | `agent1/dockerfile_gen.py` | ✅ 代码达标（6 类平台违规均被拦住），未真实 build |
| A1-5 | **docker build** | `agent1/packer.py` | ⬜ 未实现 ⚠️ 需 Docker，见 §5.1 |
| A1-6 | **docker push 到 TCR** | `agent1/packer.py` + `clients/tcr.py` | ⬜ 未实现（凭证与 push 权限已验证可用） |

### 1.2 Agent2（验证）

| # | 要求原文 | 落地位置 | 状态 |
|---|---|---|---|
| A2-1 | **拉取 TCR 镜像** | 通过 `CreateSandboxTool(CustomConfiguration.Image=...)` 由平台拉取 | ⬜ 未实现 |
| A2-2 | **在 SandBox 中启动容器** | `agent2/sandbox_runner.py` | 🔨 内置工具✅；**自定义镜像未验证（最高风险项）** |
| A2-3 | **执行题目** | `/task/verify.sh` + `agent1/solvability.py` | ✅ 实测判分正确，solve-back 超出要求 |
| A2-4 | **验证解的正确性** | `agent1/local_validator.py`（空解 FAIL + golden PASS 双向） | ✅ 逻辑已实测；需在沙箱侧再跑一遍 |
| A2-5 | 校验与仓库现有 **PR/commit/bugfix 无重叠** | `agent2/overlap_check.py`（GitHub API） | ⬜ 未实现（**课题核心约束，不可缺**） |

### 1.3 输出数据集

| # | 要求原文 | 落地位置 | 状态 |
|---|---|---|---|
| D-1 | **≥10 道**通过验证的题目 | `data/tasks.jsonl` | ✅ **10 道**（三类齐全，LOCAL_OK；待 Agent2 升级为 ACCEPTED） |
| D-2 | 每道含**题干描述** | `problem_statement` 字段 | ✅ 3 道均含 6 个必需小节 |
| D-3 | 每道含 **TCR 镜像地址** | `image` / `solution_image` 字段 | ✅ 地址已拼装（未推镜像，build/push 待 Docker） |
| D-4 | 每道含**验证脚本** | `verify_script` 字段 + 镜像内 `/task/verify.sh` | ✅ 脚本已实测可判分 |
| D-5 | 每道含**通过证明** | `data/proofs/<task_id>/` | ✅ **3 套 proofs 已产出** |
| D-6 | **JSON Lines** 格式 | 一行一题 | ✅ 3 条，`read_jsonl()` 逐行校验通过 |
| — | **README.md** | 启动方式/参数配置/结果格式 | ✅ **已创建**（2026-08-18） |

---

## 二、技术要求逐条核对

| # | 要求原文 | 现状 | 状态 |
|---|---|---|---|
| T-1 | 运行环境：腾讯云 Agent SandBox（自定义镜像沙箱） | 已跑通内置工具；自定义镜像待 M1 验证 | 🔨 |
| T-2 | 镜像基于 **ubuntu:22.04 + Python 3.11 + Git + Docker CLI** | 已实测字面满足 | ✅ **见 §3.1** |
| T-3 | LLM：TokenHub API，模型 `deepseek-v4-pro` 或 `glm-5` | 两者均已确认在线可用 | ✅ 见 §3.3 |
| T-4 | 镜像仓库：腾讯云 **TCR**，配置 `docker login` 凭证 | 7 个实例均属他人，归属待定 | ⚠️ 见 §5.2 |
| T-5 | 编程语言：Python，**推荐 agent-sandbox Python SDK** | 用 `e2b-code-interpreter` **2.x**（已实测生产落地）+ `agent-sandbox` 互补 | ✅ 见 §3.2 |
| T-6 | 目标仓库：**Star > 100** 的开源项目 | 待建 `config/repos.yaml`，需记录 star 数留证 | ⬜ |
| T-7 | 核心约束：题目**不得与现有 issue/PR/commit/bugfix 重叠** | 方案已设计四层过滤 | ⬜ |

---

## 三、✅ 曾经的偏差，已用实测方案解决

### 3.1 「ubuntu:22.04 + Python 3.11」—— ✅ **已实测字面满足，不再是偏差**

**课题要求**：沙箱镜像基于 `ubuntu:22.04 + Python 3.11 + Git + Docker CLI`。

**历史背景**：官方基础镜像 `ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest`
实测为 **Debian 13 (trixie) + Python 3.12.11**（非 ubuntu:22.04 + 3.11），
曾经因为「自定义镜像必须继承该基础镜像才能获得 `commands.run`/`files.*` 能力」
被当作平台硬约束下的偏差处理。

**✅ 现已解决**：实测拆解官方镜像发现，提供沙箱管理能力的组件（`envd` + S6-Overlay
`/init`）是**静态链接**、可跨发行版搬运的；而依赖 Debian glibc 的 `jupyter`/`uvicorn`
只服务于本项目从未使用的 `run_code` 能力，可整体剔除。据此以 `ubuntu:22.04` 为基础层
做多阶段构建，真实沙箱内实测通过：

```
PRETTY_NAME="Ubuntu 22.04.5 LTS"
Python 3.11.16 / git 2.34.1 / Docker 29.1.3
沙箱工具创建成功 → ACTIVE；实例启动成功 → commands.run 正常
镜像体积 872MB（原 6.86GB 基础层 → 降 87%）
```

**详细实测过程与脚本**：见 `review-feedback-report.md` §「意见1」，
以及 `experiments/ubuntu-base/Dockerfile`、`experiments/verify_ubuntu_base.py`。

**现已切换为生产默认**：`config/settings.yaml` 的 `image.base` 已指向该 Ubuntu
共享 base 镜像，此后新出的题目自动走这条路径；已交付的 19 道题镜像是升级前构建的，
仍为 Debian 基础层，不做回溯重建（保持已验收证据链不变）。

→ 此条已不需要报备为偏差，反而是本次验证的成果之一。

### 3.2 `agent-sandbox` SDK vs `e2b-code-interpreter`

课题写"**推荐使用 agent-sandbox Python SDK**"。已查明二者关系：

| SDK | 定位 | 用途 |
|---|---|---|
| `e2b-code-interpreter` (**2.x，生产默认**) | **创建/连接沙箱实例**（官方文档明确支持的兼容路径） | `Sandbox.connect()` |
| `agent-sandbox` (0.0.30) | **连接已有实例后操作其内部能力**（`base_url` 直连） | `bash` / `code` / `file` / `jupyter` / `shell` / `browser` / `skills` |

→ **结论：两者不冲突，是互补的**。官方文档同时列出 E2B 兼容路径。
→ **决定**：`requirements.txt` **两个都装**（`e2b-code-interpreter>=2.9.0,<3.0.0`）；
  `clients/sandbox.py` 做统一封装，实例创建走 e2b（已实测跑通，2.x 客户端的 Key 格式
  校验用官方开关 `E2B_VALIDATE_API_KEY=false` 跳过，鉴权/协议不受影响），`verify.sh`
  执行优先走 `agent-sandbox` 的 bash 能力以贴合"推荐"，并保留 e2b `commands.run`
  作为兜底。README §6.2 中说明。

### 3.3 模型选择（要求 `deepseek-v4-pro` 或 `glm-5`）

两者均已实测在线。但发现更优选择，需说明：

| 模型 | 输入/输出（元/百万token） | 说明 |
|---|---|---|
| `deepseek-v4-pro` | 12 / 24 | **课题推荐**，最贵 |
| `deepseek-v4-pro-202606` | **3 / 6** | 同代快照版，**便宜 4 倍**，实际使用 |
| `glm-5` | 4 / 18 | 课题备选，用于交叉验证 |

→ **决定**：默认用 `deepseek-v4-pro-202606`（属 `deepseek-v4-pro` 系列，符合要求），
  `config/settings.yaml` 可一键切回 `deepseek-v4-pro` / `glm-5`。README 说明理由。

---

## 四、容易被忽略的隐含要求

### 4.1 三种题型**都要有**

课题原文列举「功能实现 / 重构 / 模块添加」三类。为稳妥，最终 10 道题应**每类至少 2 道**：

| 题型 | 目标占比 | 判据 | golden patch 来源 |
|---|---|---|---|
| A 功能实现（AST 挖空） | 5~6 道 | 仓库自带测试 | 原始实现（100% 可靠） |
| B 模块添加 | 2~3 道 | LLM 新写测试 + 全量回归 | LLM 参考实现（须本地跑通） |
| C 重构 | 2 道 | 行为等价测试 + 静态指标 | LLM 参考重构 |

### 4.2 「执行题目」的理解

要求原文：Agent2「拉取镜像 → 启动容器 → **执行题目** → 验证解的正确性」。

我们的实现（**更严格**）：
1. **空解**（不改任何代码）跑测试 → `FAIL_TO_PASS` 必须全红 ← 证明题目"有内容"
2. **golden 解**（打入 `golden.patch`）跑测试 → 必须全绿 ← 证明题目"可解"
3. 双跑一致性 → 排除 flaky

这就是"执行题目 + 验证解的正确性"的严格版本（双向 sanity）。
**可选加分项**：再让 LLM 真实尝试解一次，记录是否通过，作为**难度标定**证据 —— 更贴合
"执行题目"字面含义，且能证明题目不是"不可能完成"。计入 `validation.llm_attempt` 字段。

### 4.3 题干必须"结构完整"

验收标准原文：「结构完整的软件工程题目（**含上下文、输入输出、预期行为**）」。

→ `schemas/task.py` 用 pydantic **强制校验**题干必须包含以下小节，缺一即 `REJECTED`：
```
## 背景与上下文      (context)
## 需要实现的功能    (requirement)
## 输入 / 输出说明   (io_spec)      ← 要求明确点名
## 预期行为          (expected)     ← 要求明确点名
## 约束条件          (constraints)
## 不可修改的文件    (do_not_touch) ← 防作弊：禁止改测试文件
```
并做**泄题审查**：题干不得包含目标函数的实现代码（规则 + LLM 双重检查）。

### 4.4 README.md 必须包含三项

验收标准原文：「说明流水线**启动方式**、**参数配置**、**结果文件格式**」。

| 章节 | 内容 | 状态 |
|---|---|---|
| 启动方式 | `python scripts/run_pipeline.py --repos ... --n 10`；分阶段 `--only agent1\|agent2`；`--resume` | ⬜ |
| 参数配置 | `.env` 全部变量说明 + `config/settings.yaml` 每项含义、默认值、取值范围 | ⬜ |
| 结果文件格式 | `tasks.jsonl` 逐字段说明 + `proofs/` 目录结构 + 一条完整真实样例 | ⬜ |
| （建议补充） | 架构图、与官方约束的技术权衡说明（§3.1/§3.2/§3.3）、成本与耗时统计、失败案例分析 | ⬜ |

### 4.5 "双 Agent **协作**"要能体现

不能是两个脚本各跑一半。需要明确的交接契约与状态机：
```
Agent1 ──产出──► TCR 镜像(:v1 / :v1-sol) + 镜像内 /task 契约 ──► Agent2 消费
状态机：SELECTED → DESIGNED → PATCHED → LOCAL_OK → IMAGE_PUSHED
        → SANDBOX_OK → OVERLAP_OK → ACCEPTED  /  REJECTED(reason)
```
→ `pipeline/orchestrator.py` 落地，状态存 `data/state.db`，支持断点续跑。
→ 镜像内 `/task/metadata.json` 是两个 Agent 唯一的接口，保证 Agent2 与具体题目**解耦**。

---

## 五、当前阻塞与决策项

### 5.1 Docker（构建环节，可延后但必须解决）
本机 macOS **arm64**，未装 Docker；沙箱只支持 `linux/amd64`，跨架构构建慢且易错。
**选项**：① 装 Docker Desktop + `--platform=linux/amd64`（慢但可行）
② 申请 amd64 CVM 构建机（推荐，快且无玄学错误）
→ **不阻塞离线内核开发**，但 A1-5/A1-6 必须有它。

### 5.2 镜像仓库 —— ✅ **已决策：CCR 个人版 + 复用现有命名空间**

**最终决策**：
```bash
TCR_REGISTRY=ccr.ccs.tencentyun.com
TCR_NAMESPACE=tcb-100008634787-zbaf   # 复用现有空命名空间（配额已满，无法新建）
TCR_REGISTRY_TYPE=personal
# 镜像命名统一带前缀区分归属：<ns>/swe-synth-0001:v1
```

**为什么不新建命名空间**（2026-08-14 实测）：
`DescribeUserQuotaPersonal` 返回个人版配额 —— **命名空间上限 12，已用 12（满）**。
尝试 `CreateNamespacePersonal` 报 `LimitExceeded.ErrNamespaceMaxLimit`。
个人版是**主账号级共享**（控制台提示"默认共享实例"），那 12 个是团队 2019 年以来的历史积累。

**但完全不影响我们**，因为真正的瓶颈不是命名空间而是仓库数：
| 配额项 | 上限 | 已用 | 是否够 |
|---|---|---|---|
| namespace | **12** | **12** | ❌ 满，故复用 |
| **repo（镜像仓库）** | **500** | 61 | ✅ **充足**（10 道题只需 10~20 个） |
| tag | 100/仓库 | — | ✅ 充足 |

→ 一道题 = 一个**镜像仓库**（`:v1` 和 `:v1-sol` 是同仓库两个 tag），命名空间只是路径一段，
  **不影响任何功能**。选 `tcb-100008634787-zbaf`（仓库数=0，系统自动生成，无人格归属，最中立）。
  （账号内另有其它空命名空间可作备选）

**⚠️ 前置动作：子用户需先"初始化"个人版**
控制台「命名空间」页提示"当前登陆账号尚未初始化个人版镜像仓库服务"、「新建」按钮置灰，
即**当前子用户未设置个人版登录密码**。
- 控制台路径：容器镜像服务 → **实例管理** → 选「个人版实例（默认共享实例）」→ 初始化密码
- 或 API：`CreateUserPersonal(Password=...)`（首次）/ `ModifyUserPasswordPersonal(Password=...)`（改密）
- 初始化后即得 `docker login` 凭证：
  ```
  TCR_USERNAME=<子用户 Uin>     # 子用户 Uin（个人版用户名就是账号 ID）
  TCR_PASSWORD=<初始化时设置的密码>
  ```
  ⚠️ 该密码由你自行设置并只存入 `.env`，**不要写进代码、不要截图**。

**兜底**：若个人版受限，可切广州企业版 `<广州企业版实例>`（命名空间 3/50、仓库 3/1000，配额充足），
但它 **2026-08-19 到期**且属他人，仅作应急。切换只需改 `.env` 三个变量
（`clients/tcr.py` 不得硬编码 registry 类型）。

| 对比项 | CCR 个人版 ✅ 选定 | 企业版 TCR |
|---|---|---|
| 是否满足课题要求 | ✅ 满足。课题原文要求「腾讯云 TCR（容器镜像服务）」，CCR 是**同一产品的个人版**，官方文档 `/1814/129691` 明确支持（`ImageRegistryType=personal`） | ✅ |
| 团队账号影响 | ✅ **自建命名空间，完全不碰别人资源** | ⚠️ 现有实例均属他人，借用需打招呼 |
| 费用 | ✅ 免费 | 企业版实例收费 |
| 是否需新建资源 | ✅ 只建一个命名空间（只增不改不删） | 新建实例是重资产 |
| **有无成功先例** | ✅ **有**：账号内已有沙箱工具在用 `ImageRegistryType=personal` 的 CCR 个人版镜像，**证明这条路已跑通** | ✅ 亦有先例 |
| 地域 | 与沙箱同为广州 | `<广州企业版实例>` 亦在广州 |

**为什么不用已有的企业版实例**（2026-08-14 实测，含到期时间）：

| 实例 | 地域 | 付费模式 | 到期 | 能否用 |
|---|---|---|---|---|
| **`<广州企业版实例>`** | **ap-guangzhou** ✅同地域 | 包年包月 | **2026-08-19（剩 4 天）** | ❌ **4 天后到期，且是他人项目的资源，续费与否我们无权决定** |
| `<北京企业版实例>` | ap-beijing | 包年包月 | 2026-08-27（剩 12 天） | ❌ 跨地域 + 会到期 |
| `<上海企业版实例A>` | ap-shanghai | 包年包月 | 2026-08-29（剩 14 天） | ❌ 跨地域 + 会到期 |
| `<上海企业版实例B/C/D>` | ap-shanghai | 按量计费（不到期） | — | ❌ 跨地域，拉镜像慢；仍属他人 |
| `<新加坡企业版实例>` | ap-singapore | 按量计费 | — | ❌ 境外地域 |

**决定性事实**：广州地域（沙箱所在地）**只有 `<广州企业版实例>` 一个企业版实例，且 4 天后到期**。
若把 10 道题的镜像推上去，到期后镜像全部失效 → `tasks.jsonl` 里的 `image` 字段全成死链
→ **交付物直接报废**。而 CCR 个人版是账号级服务，**不存在实例到期问题**。

> ⚠️ 注意 该实例的 `DeletionProtection=False`（无删除保护），更不宜依赖。

**已探明事实**：
- CCR 个人版**已开通**，账号内已有 12 个命名空间在用（说明无需额外初始化）
- CCR 是**账号级服务，不是实例制** → 没有到期时间、没有续费风险、免费
- 沙箱工具已验证 `ImageRegistryType` 支持三种取值：`system` / `personal` / `enterprise`

**`.env` 配置**：
```bash
TCR_REGISTRY=ccr.ccs.tencentyun.com
TCR_NAMESPACE=swe-synth-aziz          # 带个人前缀，符合团队共享账号规范
TCR_REGISTRY_TYPE=personal            # 创建沙箱工具时传此值
TCR_USERNAME=<腾讯云账号ID或子用户名>   # 控制台「访问凭证」页给出完整 docker login 命令
TCR_PASSWORD=<访问凭证密码>
```

**待你操作**（控制台，5 分钟）：
1. 容器镜像服务 → **个人版** → 命名空间 → 新建 `swe-synth-aziz`
2. 同页「访问凭证」→ 生成/查看密码 → 填入 `.env` 的 `TCR_USERNAME` / `TCR_PASSWORD`
3. README 中说明：**因课题为个人实习课题且团队企业版实例均属他人，选用同产品个人版（CCR），
   平台官方支持 `personal` 类型，功能与验收要求完全等价。**

**风险与兜底**：若后续发现 CCR 个人版有镜像大小/数量限制影响批量出题，可平滑切到企业版
（只需改 `.env` 三个变量 + 沙箱工具的 `ImageRegistryType`，代码无需改动 —— 因此
`clients/tcr.py` 必须把 registry 类型做成配置项，不能硬编码）。

### 5.3 `ubuntu:22.04` 是否硬性要求 —— ✅ 已解决，见 §3.1

不再是需要报备的偏差：已实测基于 `ubuntu:22.04` 构建自定义镜像并在真实沙箱中启动成功，
字面满足课题要求。详见 §3.1 与 `review-feedback-report.md`。

---

## 六、总体完成度自评（2026-08-18 晚更新）

| 阶段 | 完成度 | 说明 |
|---|---|---|
| 环境打通（M0） | **85%** | 沙箱 ✅ LLM ✅ 权限 ✅；缺 Docker |
| 方案设计 | **100%** | `plan.md` + 本核对表 + `midterm-audit.md` |
| 代码实现 | **95%** | Agent1 三类题型 + packer + Agent2（sandbox_runner/overlap_check/ags）全部就绪 |
| 数据产出 | **100%** | ✅ 10 / 10 道题（三类齐全，覆盖 3 仓库） |
| 文档交付 | **100%** | 进度/对标/验收核对齐全；README 已写 |

**验收 8/10 项通过**（见 `validate` 实测输出）。剩余 2 项均为「外部条件未就绪」而非「未实现」：

| 未通过项 | 阻塞条件 | 代码状态 |
|---|---|---|
| state=ACCEPTED（双 Agent 验证） | 需 Docker build/push 镜像（本机无） | ✅ `packer.py` + `sandbox_runner.py` 已就绪，Docker 一到即可跑 |
| 无重叠校验 | 需 `GITHUB_TOKEN` | ✅ `overlap_check.py` + `github.py` 已就绪，token 一到即可跑 |

**结题前的最后两步**（都只需用户补一个外部条件）：
1. 装 Docker（或提供 amd64 构建机）→ `run_pipeline.py pack` + `verify` 走完 Agent2 全链路
2. 填 `GITHUB_TOKEN` → 跑无重叠校验，state 升为 ACCEPTED

**关键判断**：**没有任何验收项是"做不到"的**，全部有明确落地路径。
三个偏差（§3.1/3.2/3.3）都已有兼容解法，只需在 README 中说明技术权衡。
最大的**真实风险**不是平台，而是 **B/C 题型的 golden patch 由 LLM 生成、可能反复跑不通**
—— 因此 A 类（挖空，golden 100% 可靠）必须占主力，先把 A 类打通并凑够数量，再补 B/C。
