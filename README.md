# 交付说明 · 课题三：数据合成

> **给验收人**：本文件是交付包的入口。按第一节的三步走，5 分钟即可完成核心验收。

---

## 一、5 分钟快速验收（推荐路径）

> **前置说明**：第 1、2 步**不需要安装任何依赖、不需要任何凭证**，用系统自带的
> Python 3 即可（实测 Python 3.9 可运行）。第 3 步需要 Docker，镜像已设为**公开**，
> 无需登录凭证即可拉取。

### 第 1 步：确认数据集达标（30 秒，零依赖）

```bash
# 题目总数与状态
python3 -c "
import json
rows=[json.loads(l) for l in open('data/tasks.jsonl')]
print('题目总数:', len(rows))
print('全部 ACCEPTED:', all(r['state']=='ACCEPTED' for r in rows))
print('题型:', sorted({r['task_type'] for r in rows}))
"
```

预期输出：
```
题目总数: 19
全部 ACCEPTED: True
题型: ['feature_implementation', 'module_addition', 'refactoring']
```

### 第 2 步：抽查一道题的完整证据链（2 分钟，零依赖）

以 `swe-synth-0034` 为例，`data/proofs/swe-synth-0034/` 下的文件构成完整闭环：

| 文件 | 证明了什么 |
|---|---|
| `problem_statement.md` | 题干含 6 个必需小节（背景/功能/输入/输出/预期行为/约束） |
| `stub_run.log` | **题目态测试全红** —— 证明题目有实质内容，不是白送分 |
| `golden_run.log` | **golden 态测试全绿** —— 证明题目确实可解 |
| `solve_back.json` | LLM 只看题干能否独立解出 —— 证明题干准确、无歧义 |
| `verification.json` | **沙箱双向验证证据**，含真实沙箱实例 ID |
| `overlap_check.json` | 与仓库现有 issue/PR/commit 无重叠的检索结果 |
| `Dockerfile` | 镜像定义（可复现构建） |
| `stub.patch` / `golden.patch` | 题目态 / 标准答案的代码差异 |

### 第 3 步：验证镜像真实可用（可选，需 Docker，无需登录）

```bash
# 拉题目镜像，跑判分脚本 —— 不改代码应当判定失败
docker pull ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-0034:v1
docker run --rm ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-0034:v1 \
  bash /task/verify.sh; echo "退出码=$?（预期 1，即空解未通过）"

# 拉答案镜像，打入标准答案后应当判定通过
docker pull ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-0034:v1-sol
docker run --rm ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-0034:v1-sol \
  bash /task/verify.sh --golden; echo "退出码=$?（预期 0，即参考解通过）"
```

> - 镜像仓库已设为**公开**，`docker pull` 无需 `docker login`（已实测匿名可拉取 38/38）
> - 镜像基于 `linux/amd64`，Apple Silicon 需加 `--platform=linux/amd64`
> - 镜像较大（约 7.5GB，因平台强制继承腾讯云官方沙箱基础镜像 6.86GB），首次拉取需耐心等待

### 第 4 步（可选）：跑本项目自带的验收核对脚本

这一步会逐项对照 `TASK-SPEC.md` 输出核对表，**需要先装依赖**（仍不需要凭证）：

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/run_pipeline.py validate
```

> ⚠️ 若跳过装依赖直接运行，会报 `ModuleNotFoundError: No module named 'pydantic'`。
> 该脚本只读本地文件，**不调用任何云服务、不需要 `.env`**。

---

## 二、课题验收标准逐条对照

| 课题原文验收标准 | 状态 | 证据位置 |
|---|---|---|
| Agent1 能自动分析仓库并生成结构完整的题目（含上下文、输入输出、预期行为） | ✅ | 19 份 `problem_statement.md`，6 小节由 `swe_synth/schemas/task.py` 强制校验 |
| Agent1 能生成正确 Dockerfile，预装依赖，且成功 push 到 TCR | ✅ | 19 份 `Dockerfile` + 第四节镜像清单（38 个 tag 已在 CCR 上） |
| Agent2 能拉取镜像并在 SandBox 中启动、执行验证流程 | ✅ | `verification.json` 中的 `empty_sandbox_id` / `golden_sandbox_id` |
| Agent2 验证含：可解性验证 + 与已有 PR/commit/bugfix 交叉比对（GitHub API） | ✅ | `solve_back.json`（可解性）+ `overlap_check.json`（交叉比对） |
| 最终产出 ≥10 道通过双 Agent 全流程验证的题目，JSON Lines 落盘 | ✅ **19 道** | `data/tasks.jsonl`，19 行全部 `state=ACCEPTED` |
| 提供 README.md，说明流水线启动方式、参数配置、结果文件格式 | ✅ | `PIPELINE.md` 第三/四/五节（本文件为验收入口） |

**结论：6/6 项全部达标，题目数超出要求近 2 倍。**

---

## 三、数据集总览（19 道题）

| task_id | 题型 | 难度 | 来源仓库 | Star | F2P | P2P |
|---|---|---|---|---|---|---|
| swe-synth-0001 | 功能实现 | easy | psf/cachecontrol | 500 | 19 | 9 |
| swe-synth-0003 | 模块添加 | medium | psf/cachecontrol | 500 | 10 | 35 |
| swe-synth-0004 | 模块添加 | medium | psf/cachecontrol | 500 | 9 | 35 |
| swe-synth-0005 | 模块添加 | easy | psf/cachecontrol | 500 | 11 | 35 |
| swe-synth-0006 | 功能实现 | easy | pallets/itsdangerous | 2900 | 38 | 121 |
| swe-synth-0007 | 重构 | medium | pallets/itsdangerous | 2900 | 2 | 19 |
| swe-synth-0008 | 模块添加 | medium | psf/cachecontrol | 500 | 11 | 35 |
| swe-synth-0010 | 模块添加 | medium | jd/tenacity | 6000 | 9 | 0 |
| swe-synth-0015 | 模块添加 | easy | jd/tenacity | 6000 | 11 | 0 |
| swe-synth-0016 | 模块添加 | easy | jd/tenacity | 6000 | 12 | 0 |
| swe-synth-0026 | 功能实现 | medium | pallets/itsdangerous | 2900 | 21 | 121 |
| swe-synth-0027 | 功能实现 | easy | pallets/itsdangerous | 2900 | 3 | 5 |
| swe-synth-0028 | 功能实现 | easy | pallets/itsdangerous | 2900 | 40 | 1 |
| swe-synth-0029 | 重构 | medium | pallets/itsdangerous | 2900 | 2 | 43 |
| swe-synth-0030 | 重构 | medium | psf/cachecontrol | 500 | 3 | 17 |
| swe-synth-0031 | 重构 | medium | psf/cachecontrol | 500 | 3 | 36 |
| swe-synth-0032 | 功能实现 | medium | psf/cachecontrol | 500 | 3 | 30 |
| swe-synth-0033 | 功能实现 | easy | psf/cachecontrol | 500 | 1 | 15 |
| swe-synth-0034 | 功能实现 | medium | psf/cachecontrol | 500 | 8 | 8 |

- **题型分布**：功能实现 8 / 模块添加 7 / 重构 4（课题要求的三类全覆盖）
- **难度分布**：easy 8 / medium 11
- **来源仓库**：均为 GitHub Star > 100 的 Python 项目（课题要求）
- **F2P** = FAIL_TO_PASS（题目态必红、解出后必绿的判据用例数）
- **P2P** = PASS_TO_PASS（全程必绿的回归用例数，防止改坏其他功能）

---

## 四、镜像清单（已推送至腾讯云 CCR，**公开可匿名拉取**）

镜像前缀统一为：`ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/`

每道题两个 tag，共 **38 个镜像**，全部已设为公开访问（实测匿名可拉取 38/38，
**无需 `docker login`**）：

| tag | 内容 | 用途 |
|---|---|---|
| `<task_id>:v1` | 题目镜像，**不含答案** | 交付给被测 Coding Agent 解题 |
| `<task_id>:v1-sol` | 答案镜像，含 `golden.patch` | 供验证「题目确实可解」 |

完整地址在 `data/tasks.jsonl` 每条记录的 `image` / `solution_image` 字段中。

### 镜像内统一契约（被测 Agent 的接口）

```
/task/problem_statement.md   题干
/task/metadata.json          repo/base_commit/test_cmd/FAIL_TO_PASS/PASS_TO_PASS
/task/run_tests.sh           只跑测试
/task/verify.sh              判分入口 → 退出码 + /task/result.json
/workspace/repo/             已 stub 化的仓库（base_commit 固定）
/opt/solution/golden.patch   仅 :v1-sol 镜像存在（题目镜像绝不含答案）
```

---

## 五、这套数据集怎么用（评测流程）

本交付物是**考场**，不是考生。评测某个 Coding Agent 的流程：

```
1. docker pull <task_id>:v1
2. 把 /task/problem_statement.md 交给被测 Agent
3. 被测 Agent 在 /workspace/repo/ 中自主修改代码
   （不得修改 metadata.json 里 do_not_modify 列出的判据文件）
4. 运行 /task/verify.sh
   退出码 0 = 解题成功；1 = 未通过；≥90 = 环境错误
```

判分标准：`FAIL_TO_PASS` 全部转绿 **且** `PASS_TO_PASS` 保持全绿。

---

## 六、如何复现出题流水线（可选）

```bash
# 1) 环境
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 凭证（每个变量去哪拿，见 .env.example 内的注释）
cp .env.example .env   # 然后填入自己的凭证

# 3) 出一道新题
.venv/bin/python scripts/run_pipeline.py agent1 --repo psf/cachecontrol --type A --n 1

# 4) 校验数据集并逐项对标课题标准
.venv/bin/python scripts/run_pipeline.py validate
```

详细参数说明见 `PIPELINE.md` 第三、四节。

> ⚠️ 交付包中**不含任何凭证**（`.env` 已排除）。`.env.example` 只有键名与获取途径说明。

---

## 七、交付包内容说明

```
├── README.md                ← 本文件，验收入口（GitHub 首页）
├── PIPELINE.md              流水线完整文档（启动方式/参数配置/结果格式）
├── mentor-feedback-report.md  导师反馈的逐条实测验证与整改方案
├── TASK-SPEC.md             课题原文逐字备份（对照基准）
├── requirements-check.md    验收标准逐条核对 + 技术偏差说明
├── PROGRESS.md              研发全过程与 26 条踩坑记录
├── requirements.txt         依赖清单
├── .env.example             凭证模板（无真实值）
├── data/
│   ├── tasks.jsonl          ⭐ 核心交付：19 道题
│   ├── report.json          通过率 / 失败分布 / LLM 成本统计
│   └── proofs/<task_id>/    ⭐ 19 份通过证明（每份 9~13 个证据文件）
├── swe_synth/               双 Agent 源码
│   ├── agent1/              出题 + 打包（挖空/出题/验证/Dockerfile 生成）
│   ├── agent2/              验证（沙箱运行 + 无重叠校验）
│   ├── clients/             TokenHub / AGS / GitHub 客户端
│   └── schemas/             题目数据模型与强制校验
├── scripts/                 流水线入口与环境自检
└── config/                  settings.yaml（参数）+ repos.yaml（仓库池）
```

---

## 八、需要向验收人说明的两处技术权衡

以下两点与课题原文字面表述有差异，均为**平台硬约束下的等价方案**，详细论证见
`requirements-check.md`：

### 1. 基础镜像：`ubuntu:22.04` → 腾讯云官方沙箱镜像

课题要求镜像基于 `ubuntu:22.04`。但腾讯云 Agent Sandbox 官方文档
（`/document/product/1814/129691`）明确规定：若需使用 `run_code` / `commands.run` /
`files.*` 等能力，自定义镜像**必须继承** `ags-image/sandbox-code`（内含 S6-Overlay
`/init`、envd 与 run-code 服务）。使用裸 `ubuntu:22.04` 会导致沙箱无法启动、
Agent2 无法执行验证。

**采用方案**：以官方基础镜像为基础层保证平台能力，镜像内额外安装
**Python 3.11 + Git + Docker CLI**，题目代码与测试全部运行在 Python 3.11
虚拟环境（`/opt/venv311`）中，**实质满足课题的运行时要求**。

> 副作用：官方基础镜像本身 6.86GB，导致成品镜像约 7.5GB，拉取较慢。

### 2. 镜像仓库：TCR 企业版 → CCR 个人版

课题要求「腾讯云 TCR」。CCR 是**同一产品的个人版**，官方文档明确支持
`ImageRegistryType=personal`，功能与验收要求等价。

**选择原因**：团队账号下的企业版实例均属他人且临近到期
（`DeletionProtection=False`），若镜像推上去，实例到期后 `tasks.jsonl` 里的
`image` 字段会全部变成死链，交付物报废。CCR 个人版是账号级服务，无此风险。
