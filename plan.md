# 课题三：数据合成 — 双 Agent 协作的 SWE 题目自动构建与验证（实施方案）

## 0. 一句话方案

用「**逆向消融 + 自带测试**」作为题目可验证性的地基：从 Star>100 的开源仓库里挑选**长期稳定**的模块，由 LLM 设计任务并把目标实现 **stub 化**（挖空），仓库自带测试即天然的 `FAIL_TO_PASS` 判据，原实现即 `golden patch`；Agent1 把「挖空后的仓库 + 题干 + 验证脚本」打包成镜像推到 TCR，Agent2 用该镜像**直接作为 Agent Sandbox 的沙箱工具镜像**启动，跑「参考解通过 / 空解失败」双向 sanity + GitHub API 交叉比对，产出 JSONL 数据集。

> 这样做的关键收益：题目**必然可解、必然可自动判分**，且**不来源于任何 bugfix commit/PR**，天然规避数据污染与「不可验证的 LLM 幻想题」两大坑。

---

## 1. 需求拆解与验收标准映射

| 验收标准 | 落地组件 | 产物 |
|---|---|---|
| Agent1 自动分析仓库并生成结构完整题目 | `agents/agent1/repo_analyzer.py` + `task_designer.py`(TokenHub) | `problem_statement.md`（含上下文/输入输出/预期行为/约束/不可改动文件） |
| Agent1 生成正确 Dockerfile 并 push 到 TCR | `dockerfile_gen.py` + `image_builder.py` | `ccr/tcr://swe-synth/<task_id>:v1` |
| Agent2 拉镜像并在 SandBox 启动执行验证 | `agents/agent2/sandbox_runner.py`(e2b-code-interpreter) | 沙箱执行日志 |
| 可解性验证 + PR/commit/bugfix 交叉比对 | `solvability.py` + `overlap_check.py`(GitHub API) | `verify_report.json` / `overlap_report.json` |
| ≥10 道题，JSON Lines 落盘 | `pipeline/orchestrator.py` | `data/tasks.jsonl` + `data/proofs/<task_id>/` |
| README.md | 文档 | 启动方式/参数/结果格式 |

---

## 2. 总体架构

```
                 ┌──────────────────────── Orchestrator (本地/CVM) ────────────────────────┐
                 │  repo_selector → Agent1 → (TCR) → Agent2 → dataset writer → 报告统计     │
                 └─────────────────────────────────────────────────────────────────────────┘
                                │                                    │
        ┌───────────────────────┴──────────┐          ┌──────────────┴─────────────────┐
        │ Agent1（出题 + 打包）             │          │ Agent2（验证）                  │
        │ 1 clone@base_commit / 结构分析     │          │ 1 以 TCR 镜像创建沙箱工具        │
        │ 2 TokenHub LLM 选靶点 + 出题       │          │ 2 CreateSandboxInstance         │
        │ 3 生成 stub patch / golden patch   │  TCR     │ 3 commands.run 跑 verify.sh     │
        │ 4 本地跑测试自校验(挖空后必须红)    │ ───────► │ 4 空解 FAIL + 参考解 PASS 双检   │
        │ 5 生成 Dockerfile + verify.sh      │          │ 5 GitHub API 无重叠交叉比对      │
        │ 6 docker build --platform amd64    │          │ 6 产出 verify_report + 通过证明  │
        │ 7 docker push                      │          │                                 │
        └────────────────────────────────────┘          └─────────────────────────────────┘
```

### 关键工程决策（含风险规避）

1. **`docker build` 不在 Agent Sandbox 内做**。官方沙箱不保证 DinD/特权。构建阶段放在本地或一台 CVM（有 Docker daemon）；沙箱侧只负责「运行」。若必须在沙箱内构建，备选：沙箱内 docker CLI 连接远端 `DOCKER_HOST`，或用 kaniko/buildkit rootless。**M0 阶段先实测确认，再定型**。
2. **Agent2 不在沙箱里 `docker run`，而是把题目镜像本身当作沙箱模板**。这既是官方支持路径（自定义沙箱工具 = 指定 TCR 镜像），也省掉 DinD。因此题目镜像必须：
   - `FROM ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest`
   - 不改 `USER`(root)、不改 `WORKDIR`(`/`)、不依赖 Dockerfile 的 `ENV`（快照启动不生效，需 API `Env` 传入）
   - 不覆盖 `ENTRYPOINT`；若覆盖必须回填 `Command=["/init"]`
   - `docker build --platform=linux/amd64`（仅支持 amd64）
   - 端口保留 49999(run_code) / 49983(envd)
3. **权限链路易漏**：CAM 角色（载体 = Agent Runtime）+ TCR 拉取权限 + 调用账号 `cam:PassRole`。M0 必须先打通。
4. **答案隔离**：`golden.patch` 不放进题目镜像的可见路径。方案：同一构建产出两个 tag —— `:v1`（题目镜像，无答案）与 `:v1-sol`（含 `golden.patch`，仅 Agent2 使用）。避免评测时泄题。
5. **镜像内统一契约**（让 Agent2 完全通用、与具体题目解耦）：

```
/task/problem_statement.md     # 题干（交付给被测 Coding Agent）
/task/metadata.json            # repo/base_commit/language/test_cmd/FAIL_TO_PASS/PASS_TO_PASS
/task/run_tests.sh             # 只跑测试，输出机器可读结果
/task/verify.sh                # 判分入口：退出码 + /task/result.json
/workspace/repo/               # 已 stub 化的仓库（base_commit 固定）
/opt/solution/golden.patch     # 仅 :v1-sol 镜像存在
```
   `verify.sh` 统一输出：`{"fail_to_pass":[...], "pass_to_pass":[...], "passed":bool, "raw_log_path":"..."}`。

---

## 3. 题目生成策略（三类题型 + 可验证性保证）

| 题型 | 构造方式 | 判据 | golden patch |
|---|---|---|---|
| A. 功能实现（主力，占比 ~60%） | 挑选被测试充分覆盖的函数/类，替换为 `raise NotImplementedError` stub，保留 docstring/签名；LLM 据源码与测试写题干（题干**不得泄露实现**） | 仓库自带测试：stub 态必红 → 补全后全绿 | 原始实现（100% 可靠） |
| B. 模块添加（~25%） | LLM 设计一个符合仓库风格的新模块（如新 CLI 子命令、新 backend adapter），同时产出**新测试**与**参考实现** | 新测试（`FAIL_TO_PASS`）+ 全量回归（`PASS_TO_PASS`） | LLM 参考实现，须先本地跑通 |
| C. 重构（~15%） | 指定一段坏味道代码，要求在保持行为等价前提下重构（拆函数/去重复/替换实现） | 行为等价测试全绿 + 静态门槛（接口签名不变、圈复杂度/重复率下降、`ruff/mypy` 无新增告警） | 参考重构实现 |

**难度分层**：easy（单函数）/ medium（跨 2-3 文件）/ hard（新模块 + 多文件协作），最终数据集尽量覆盖 3 档 + 至少 2 种语言（Python 主 + 1 个 Go/TS 题验证多语言通路）。

**必须过的自校验（Agent1 本地，节省沙箱成本）**：
- stub 态：`FAIL_TO_PASS` 全部失败，`PASS_TO_PASS` 全部通过（否则挖空过度/污染）
- golden 态：全部通过
- 测试确定性：同一状态连跑 2 次结果一致（剔除 flaky）
- 无网络依赖：测试在 `--network none` 下仍可通过（否则打标 `needs_network`）

---

## 4. 去污染 / 无重叠校验（Agent2 硬性环节）

多层过滤，任一层命中即打回：

1. **来源层面免疫**：题目不取自任何 bugfix commit/PR diff，而是对**已合入且长期稳定**的代码做挖空 → 不存在「答案就是某个 PR」的映射。
2. **时间隔离**：优先选目标文件 `last_modified` 距今 > 12 个月、且近 6 个月无相关 open PR 的模块。
3. **GitHub API 交叉比对**（`overlap_check.py`）：
   - `GET /search/issues?q=repo:<r>+<keywords>`（issue + PR，含 closed）
   - `GET /repos/{r}/commits?path=<target_file>`（该文件全部 commit 历史）
   - 对候选集做 BM25 粗筛 → embedding 相似度 → LLM 二次裁决（prompt：判断「题目要求的改动」与「该 PR/commit 的改动」是否实质重叠）
   - 阈值：BM25 top20 + 余弦 > 0.85 进入 LLM 裁决；LLM 判定 `overlap=true` 则丢弃并记录原因
4. **留痕**：`overlap_report.json` 落盘检索关键词、命中列表、相似度分数、LLM 裁决理由 —— 作为「通过证明」的一部分，可复核。
5. **API 限流处理**：GitHub token + ETag 缓存 + 指数退避（search API 30 req/min）。

---

## 5. 数据集格式（`data/tasks.jsonl`，一行一题）

```json
{
  "task_id": "swe-synth-0001",
  "task_type": "feature_implementation",
  "difficulty": "medium",
  "repo": "psf/requests",
  "repo_stars": 52000,
  "base_commit": "a1b2c3d",
  "language": "python",
  "problem_statement": "……含上下文/输入输出/预期行为/约束……",
  "hints": null,
  "modified_files": ["src/requests/utils.py"],
  "test_cmd": "pytest -q tests/test_utils.py",
  "FAIL_TO_PASS": ["tests/test_utils.py::test_super_len_io_no_len"],
  "PASS_TO_PASS": ["tests/test_utils.py::test_super_len_correctly_calculates_len_of_partially_read_file"],
  "image": "xxx.tencentcloudcr.com/swe-synth/swe-synth-0001:v1",
  "solution_image": "xxx.tencentcloudcr.com/swe-synth/swe-synth-0001:v1-sol",
  "verify_script": "/task/verify.sh",
  "generated_by": {"model": "deepseek-v4-pro", "agent": "agent1", "ts": "2026-08-13T10:00:00Z"},
  "validation": {
    "sandbox_tool": "swe-synth-0001",
    "sandbox_instance_id": "sbi-xxxx",
    "empty_solution_result": "fail",
    "golden_solution_result": "pass",
    "deterministic": true,
    "duration_sec": 182,
    "proof_dir": "data/proofs/swe-synth-0001/"
  },
  "overlap_check": {
    "passed": true, "method": "github_search+bm25+embedding+llm_judge",
    "queried": ["super_len", "utils.py length"],
    "top_candidates": [{"type": "pr", "number": 1234, "score": 0.61, "verdict": "no_overlap"}]
  }
}
```

`data/proofs/<task_id>/` 内含：`stub_run.log`、`golden_run.log`、`result.json`、`overlap_report.json`、`dockerfile`、`golden.patch`、`sandbox_meta.json` —— 即「通过证明」。

---

## 6. 代码结构

```
swe-synth/
├── README.md                  # 启动方式/参数/结果格式（验收要求）
├── requirements.txt
├── .env.example               # TOKENHUB_API_KEY / E2B_API_KEY / TCR / GITHUB_TOKEN
├── config/
│   ├── settings.yaml          # 模型、并发、超时、阈值、镜像前缀
│   └── repos.yaml             # 候选仓库池（Star>100，含语言/构建命令）
├── swe_synth/
│   ├── clients/
│   │   ├── tokenhub.py        # OpenAI 兼容，重试/超时/JSON schema 输出/成本统计
│   │   ├── sandbox.py         # e2b-code-interpreter 封装 + 沙箱工具 CRUD(API) + 兜底 kill
│   │   ├── tcr.py             # docker login/build/push
│   │   └── github.py          # search/commits/PR + 缓存 + 退避
│   ├── agent1/
│   │   ├── repo_selector.py   # Star>100 筛选、可测性探测（有测试/能装依赖）
│   │   ├── repo_analyzer.py   # 目录树、模块热度、测试覆盖映射、AST 提取候选靶点
│   │   ├── task_designer.py   # LLM 出题（structured output）
│   │   ├── patch_builder.py   # stub patch / golden patch 生成（AST 精准挖空）
│   │   ├── local_validator.py # 本地双向 sanity（挖空必红 / golden 必绿）
│   │   ├── dockerfile_gen.py  # 模板 + LLM 补依赖，产出 :v1 / :v1-sol
│   │   └── packer.py          # build & push
│   ├── agent2/
│   │   ├── sandbox_runner.py  # 建工具 → 建实例 → commands.run verify.sh → 收日志 → kill
│   │   ├── solvability.py     # 空解 fail + golden pass + 重复稳定性
│   │   ├── overlap_check.py   # GitHub 交叉比对
│   │   └── reporter.py        # verify_report / proof 落盘
│   ├── schemas/task.py        # pydantic 模型 + JSON Schema 校验
│   └── pipeline/orchestrator.py # 并发调度、状态机、断点续跑、失败重试
├── templates/
│   ├── Dockerfile.python.j2 / Dockerfile.node.j2 / Dockerfile.go.j2
│   ├── verify.sh.j2 / run_tests.sh.j2
│   └── prompts/*.md           # 出题/裁决 prompt（版本化）
├── scripts/run_pipeline.py    # CLI: --repos --n 10 --resume --only agent1|agent2
└── data/tasks.jsonl, data/proofs/, data/state.db(sqlite 状态)
```

**状态机**（支持断点续跑）：`SELECTED → DESIGNED → PATCHED → LOCAL_OK → IMAGE_PUSHED → SANDBOX_OK → OVERLAP_OK → ACCEPTED` / 任一步 `REJECTED(reason)`。

---

## 7. 里程碑与排期（建议 8~10 个工作日）

| 里程碑 | 内容 | 交付 | 预估 |
|---|---|---|---|
| **M0 环境打通** | TokenHub 连通 hello world；e2b SDK 起内置沙箱；`docker login` TCR push 通；CAM 角色 + PassRole；GitHub token；确认沙箱内能否 build | `docs/env_checklist.md` + 4 个连通性脚本 | 1 d |
| **M1 单题打样（最关键）** | 手工选 1 个仓库，手写完整链路跑通一题（含镜像作为沙箱工具启动 + verify.sh 判分） | 1 条合法 jsonl + proof | 2 d |
| **M2 Agent1 自动化** | 仓库分析、LLM 出题、AST 挖空、Dockerfile 生成、build/push、本地双向 sanity | Agent1 可批量出题 | 2 d |
| **M3 Agent2 自动化** | 沙箱工具复用/清理、验证、双向 sanity、GitHub 交叉比对、证明落盘 | Agent2 可批量验证 | 2 d |
| **M4 批量产出** | 跑 25~30 候选，筛出 ≥10 通过题（覆盖 3 难度 + ≥2 语言） | `tasks.jsonl` + proofs | 1.5 d |
| **M5 收尾** | README、参数文档、成本/耗时统计、人工抽检 3 题、失败案例分析 | README + 结题报告 | 1 d |

**并行建议**：M2/M3 可并行开发（靠第 2 节的镜像内契约解耦，Agent2 先用 M1 的样例镜像联调）。

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 沙箱不支持 DinD，无法 build | 阻塞 | 构建移到本地/CVM；沙箱只运行；备选远端 DOCKER_HOST / kaniko |
| TCR 私有镜像拉取失败（权限） | 阻塞 Agent2 | M0 先跑通 CAM 角色 + PassRole + 最小权限策略 |
| 快照启动约束（ENV 不生效、USER/WORKDIR 固定） | 沙箱起不来 | Dockerfile 模板固化约束；环境变量走 API `Env` + `S6_KEEP_ENV=1` |
| 仓库依赖装不上 / 测试本身红 | 出题失败率高 | 仓库池预筛：先构建基线镜像跑一次全量测试，基线不绿直接淘汰 |
| LLM 出题「不可验证」或题干泄题 | 数据质量差 | 题型 A 为主（判据来自现成测试）；题干过 LLM+规则审查（禁含实现代码/函数体） |
| flaky 测试导致误判 | 假阳/假阴 | 双跑一致性 + 剔除时间/网络/随机依赖用例；`--network none` 试跑 |
| 挖空过度导致 `PASS_TO_PASS` 也红 | 题目不合法 | AST 精准挖空（仅函数体），并强制 PASS_TO_PASS 基线校验 |
| GitHub / TokenHub 限流 | 吞吐低 | 缓存 + 退避 + 并发上限；沙箱实例用完即 `kill()` 控成本 |
| 镜像体积大、推送慢 | 耗时 | 多阶段 + 依赖层缓存 + 只拷 `base_commit` 单次 shallow clone |
| 成本（沙箱按时长计费） | 超预算 | 本地先过 sanity，只把候选题送沙箱；`Timeout` 收紧；失败快速退出 |

---

## 9. 需要你确认/提供的信息（不阻塞开工，先按默认值推进）

1. 凭证：TokenHub API Key、Agent Sandbox `E2B_API_KEY`、TCR 实例域名/命名空间/账密、GitHub PAT（默认先写 `.env.example`，用占位符跑 dry-run）
2. TCR 用**企业版 TCR** 还是**个人版 CCR**？（影响 CAM 权限与 `ImageRegistryType`）
3. 是否有可用的构建机（本地 Docker 还是 CVM）？
4. 模型选 `deepseek-v4-pro`（默认）还是 `glm-5`？
5. 仓库池是否指定？（默认从 Python Star>100 中选 6~8 个易构建仓库 + 1 个 Go/TS）

## 10. 下一步动作（M0）

1. 建工程骨架 + `.env.example` + `requirements.txt`
2. 写 4 个连通性自检脚本：`check_tokenhub.py` / `check_sandbox.py` / `check_tcr.py` / `check_github.py`
3. 实测「沙箱内能否 docker build」，据结果定型构建位置
4. 选定 1 个样例仓库，手工完成 M1 单题打样
