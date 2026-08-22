# 课题原文（唯一基准 · 不可修改）

> ⚠️ 本文件是**用户最初给出的课题要求原文**，逐字保存，作为所有开发与验收的唯一基准。
> 任何时候对"该不该做某件事"有疑问，回到本文件对照。**禁止修改本文件内容。**

---

## 题目三：数据合成 — 基于双Agent协作的SWE题目自动构建与验证

### 背景

SWE-bench 是当前评估 Coding Agent 能力的权威基准，但其题目来源于 GitHub 历史 Issue-PR 对，存在数据污染风险（模型训练数据中可能已见过答案）。为保证评测公平性，需要从 GitHub 开源仓库中合成全新的软件工程题目——不依赖已有 PR/commit/bugfix，而是由 Agent 自主设计任务并构建可验证的执行环境。

本题目要求实习生利用腾讯云 Agent SandBox + TokenHub API，搭建一套「双 Agent 协作」的题目合成流水线。

### 目标产出

| 组件 | 说明 |
|---|---|
| Agent1（出题+打包） | 基于 TokenHub API 调用 LLM，分析目标仓库结构 → 生成一道软件工程题目（功能实现/重构/模块添加） → 编写 Dockerfile + 构建脚本 → docker build → docker push 到 TCR |
| Agent2（验证） | 拉取 TCR 镜像 → 在 SandBox 中启动容器 → 执行题目 → 验证解的正确性 → 校验题目与仓库现有 PR/commit/bugfix 无重叠 |
| 输出数据集 | 至少 10 道通过验证的 SWE 题目，每道题目包含：题干描述、TCR 镜像地址、验证脚本、通过证明 |

### 技术要求

- 运行环境：腾讯云 Agent SandBox（自定义镜像沙箱，基于 ubuntu:22.04 + Python 3.11 + Git + Docker CLI）
- LLM 调用：TokenHub API（https://tokenhub.tencentmaas.com/v1），推荐模型 deepseek-v4-pro 或 glm-5
- 镜像仓库：腾讯云 TCR（容器镜像服务），需配置 docker login 凭证
- 编程语言：Python（推荐使用 agent-sandbox Python SDK）
- 目标仓库范围：GitHub 上 Star > 100 的 Python 开源项目，语言不限于 Python（Go/Rust/TypeScript 也可）
- 核心约束：题目不得与仓库现有 issue/PR/commit/bugfix 内容重叠

### 验收标准

- Agent1 能自动分析仓库并生成结构完整的软件工程题目（含上下文、输入输出、预期行为）
- Agent1 能生成正确的 Dockerfile，镜像中预装题目所需依赖，且镜像成功 push 到 TCR
- Agent2 能拉取镜像并在 SandBox 中启动、执行验证流程
- Agent2 的验证逻辑包含：题目可解性验证 + 与仓库已有 PR/commit/bugfix 的交叉比对（通过 GitHub API 检索）
- 最终产出 ≥10 道通过双Agent全流程验证的题目，以 JSON Lines 格式落盘
- 提供一份 README.md，说明流水线启动方式、参数配置、结果文件格式

---

## 验收标准 → 交付物 映射（便于自查，不改变上方原文）

| 验收标准原文 | 必须存在的产物 | 自检方式 |
|---|---|---|
| Agent1 自动分析仓库并生成结构完整题目（含上下文、输入输出、预期行为） | `problem_statement` 含 6 个必需小节 | `schemas.task.SweTask` 强制校验 |
| Agent1 生成正确 Dockerfile，预装依赖，成功 push 到 TCR | `Dockerfile` + `image` 字段可 pull | `audit_dockerfile()` + 真实 push |
| Agent2 拉取镜像并在 SandBox 启动、执行验证 | `validation.sandbox_instance_id` | 沙箱实例日志 |
| Agent2 验证含：可解性验证 + PR/commit/bugfix 交叉比对（GitHub API） | `validation` + `overlap_check` 两段 | 双向 sanity + `overlap_report.json` |
| ≥10 道通过双Agent全流程验证，JSON Lines 落盘 | `data/tasks.jsonl` ≥10 行且全部 `state=ACCEPTED` | `read_jsonl()` 逐行校验 |
| README.md 说明启动方式、参数配置、结果文件格式 | `README.md` 含这三节 | 人工核对 |
