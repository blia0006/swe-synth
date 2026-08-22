# 开工前准备清单（面向腾讯云新手 / 企业内部账号）

> 你的情况：企业内部账号（自研上云管理平台，原云梯），子用户身份。
> 这意味着 **权限不是你自己给自己开的，要走提单审批** —— 这是唯一可能卡你好几天的事，所以**第 1 天就要把单提出去**，别等到写代码时才发现没权限。

---

## 一、先建立心智模型：名词对照表

不熟悉腾讯云时最大的障碍是名词。先把这张表看懂，后面文档就都能读了。

| 腾讯云名词 | 你可以理解成 | 在本课题里的角色 |
|---|---|---|
| **控制台** (console.cloud.tencent.com) | 云上的图形界面后台 | 所有资源先在这里点一遍，再用代码复现 |
| **UIN** | 主账号 ID（你截图里 `100051554448`） | 拼 CAM 角色 ARN 时要用 |
| **子用户** | 主账号下的员工账号（你就是 `azizliang`） | 你的所有操作都以子用户身份进行，权限受限 |
| **CAM** | 访问管理 = IAM，管"谁能干什么" | 建角色、授权、PassRole |
| **CAM 角色 (Role)** | 一个"身份"，可以授予给**云产品**而不是人 | Agent Sandbox 要"扮演"这个角色去拉 TCR 私有镜像 |
| **PassRole** | "允许你把某个角色交给云产品使用"的权限 | **最容易漏的一步**，没它创建沙箱工具会失败 |
| **API 密钥 (SecretId/SecretKey)** | 调用腾讯云 OpenAPI 的账号密码 | TCCLI / Python SDK 都靠它 |
| **地域 (Region)** | 机房位置，如 `ap-guangzhou`(广州) `ap-shanghai`(上海) | 沙箱和 TCR 尽量**放同一地域**，拉镜像快 |
| **TCR** | 容器镜像服务**企业版**（私有 Docker Hub） | 存题目镜像。域名形如 `xxx.tencentcloudcr.com` |
| **CCR** | 容器镜像服务**个人版**（免费、简单） | 域名形如 `ccr.ccs.tencentyun.com`，练手可用 |
| **AGS / Agent Runtime** | Agent 沙箱服务，产品页 `/product/agsx` | Agent2 在里面跑容器验证题目 |
| **沙箱工具 (Sandbox Tool)** | 一个"沙箱模板"，绑定一个镜像 | ≈ E2B 的 `template`；一道题一个工具 |
| **沙箱实例 (Sandbox Instance)** | 由工具启动的一个真实运行容器 | `sbi-xxxx`，按时长计费，**用完要 kill** |
| **TCCLI** | 腾讯云的命令行工具（≈ aws cli） | 快速试 API，不用写代码 |
| **TokenHub** | 内部 LLM 网关，OpenAI 兼容 | Agent1 调 `deepseek-v4-pro` 出题 |

**关键认知**：腾讯云 Agent Sandbox **不用腾讯云自己的 SDK**，而是**直接复用 E2B 的 Python SDK**（`e2b-code-interpreter`），只改两个环境变量指向腾讯云。所以你要学的"沙箱 SDK"其实就是 E2B 那一套，很简单。

---

## 二、权限申请（Day 1 就提单，别拖）

在你截图的**自研上云管理平台** → 「**子用户权限申请**」提单。建议一次性把下面全部申请齐，避免来回补单：

| 需要的权限 | 干什么用 | 建议策略 |
|---|---|---|
| **CAM 相关**：创建角色、创建自定义策略、`cam:PassRole` | 给 Agent Runtime 建载体角色，让它能拉 TCR 私有镜像 | `QcloudCamRoleFullAccess` 或最小集：`cam:CreateRole` `cam:AttachRolePolicy` `cam:CreatePolicy` `cam:PassRole` |
| **TCR 企业版**（或 CCR 个人版） | 存题目镜像，`docker push/pull` | `QcloudTCRFullAccess`（至少要有 push 权限 + 建命名空间） |
| **AGS / Agent Runtime** | 创建沙箱工具、创建沙箱实例、API Key | `QcloudAGSFullAccess`（名称以控制台实际为准） |
| **API 密钥（访问密钥）** | TCCLI / SDK 调用 | 内部账号有的会限制子用户自建密钥，需单独申请 |
| **CVM**（可选但建议） | 一台构建机跑 `docker build/push`，比本机快且网络通 | `QcloudCVMFullAccess`，2核4G + 100G 盘足够 |
| **CLS**（可选） | 沙箱日志投递，便于留证 | `QcloudCLSFullAccess` |

> ⚠️ 提单时"申请理由"写清楚：`实习课题三-SWE题目数据合成，需使用 Agent Sandbox + TCR + TokenHub 搭建双Agent流水线`。理由写清楚过单快很多。

**同时确认 3 件事（这些通常问人比查文档快）**：
1. **TokenHub** 的 API Key 怎么申请 / 有没有团队共用的 Key？调用配额和计费归谁？
2. TCR 用**团队已有的企业版实例**（借用现成的命名空间），还是要我**新建实例**？（企业版实例是收费资源，内部通常复用现成的）
3. 有没有**内部现成的示例代码/wiki**（Agent Sandbox 内部用法可能与公网文档略有差异，比如内部域名、内部 TokenHub 模型列表）

---

## 三、要拿到手的「凭证五件套」

准备一个 `.env`（**绝不提交 Git**），把这五组东西凑齐，凑齐了就等于开工条件具备：

```bash
# 1. 腾讯云 OpenAPI 密钥（控制台 → 访问管理 → 访问密钥 → API 密钥管理）
TENCENTCLOUD_SECRET_ID=AKIDxxxxxxxx
TENCENTCLOUD_SECRET_KEY=xxxxxxxx
TENCENTCLOUD_REGION=ap-guangzhou

# 2. Agent Sandbox（AGS 控制台 → API Keys → 新建）
E2B_API_KEY=e2b_xxxxxxxx
E2B_DOMAIN=ap-guangzhou.tencentags.com     # 固定值，指向腾讯云而非 E2B 官方

# 3. TCR（镜像仓库）
TCR_REGISTRY=xxx.tencentcloudcr.com        # 企业版；个人版是 ccr.ccs.tencentyun.com
TCR_NAMESPACE=swe-synth
TCR_USERNAME=xxx                           # 企业版用「长期访问凭证」的用户名
TCR_PASSWORD=xxx                           # 企业版用「长期访问凭证」；个人版用「访问凭证」

# 4. TokenHub（LLM 网关）
TOKENHUB_BASE_URL=https://tokenhub.tencentmaas.com/v1
TOKENHUB_API_KEY=xxxxxxxx
TOKENHUB_MODEL=deepseek-v4-pro

# 5. GitHub（去重检索 + clone）
GITHUB_TOKEN=ghp_xxxxxxxx                  # 只需 public_repo 读权限即可
```

**各自去哪里拿**：

| 凭证 | 获取路径 |
|---|---|
| SecretId/Key | 控制台 → 右上角头像 → 访问管理 → 访问密钥 → API 密钥管理 → 新建密钥（`console.cloud.tencent.com/cam/capi`） |
| `E2B_API_KEY` | Agent 沙箱服务控制台 → API Keys → 新建（`console.cloud.tencent.com/ags/sandbox`） |
| TCR 凭证 | 容器镜像服务 → 选实例 → 访问凭证 → 新建**长期访问凭证**（会给你 `docker login` 完整命令，直接复制） |
| TokenHub Key | 内部平台申请 |
| GitHub PAT | github.com → Settings → Developer settings → Personal access tokens |

---

## 四、本地环境（半小时能装完）

```bash
# Python 3.11（课题指定）
python3.11 --version

# 虚拟环境
python3.11 -m venv .venv && source .venv/bin/activate

# 核心依赖
pip install e2b-code-interpreter      # Agent Sandbox（E2B 兼容）
pip install openai                    # 调 TokenHub（OpenAI 兼容协议）
pip install tccli                     # 腾讯云命令行
pip install tencentcloud-sdk-python   # 腾讯云 Python SDK（建沙箱工具等 API）
pip install PyGithub requests pydantic jinja2 python-dotenv gitpython

# TCCLI 初始化
tccli configure    # 依次填 SecretId / SecretKey / region(ap-guangzhou) / output(json)
                   # 配置落在 ~/.tccli/default.credential
```

**Docker**（Mac 上装 Docker Desktop 即可）：
```bash
docker version
docker buildx version    # 需要 buildx，因为 Mac 是 arm64，而沙箱只支持 amd64
```
> ⚠️ **Mac 用户必须注意**：你的 Mac 大概率是 Apple Silicon（arm64），但腾讯云沙箱**只支持 linux/amd64**。所以 `docker build` 必须加 `--platform=linux/amd64`，而且跨架构构建**很慢**（QEMU 模拟）。
> **强烈建议申请一台 CVM（amd64）当构建机**，把 `docker build/push` 放上面跑，能省掉大量等待时间和玄学报错。这也是我在方案里把"构建"和"运行"分离的原因之一。

---

## 五、控制台要逛的 6 个页面（建议多逛逛控制台）

按顺序点一遍，每个页面搞清楚"它管什么"，你对整条链路的理解就通了：

| # | 页面 | 你要在这里确认/做什么 |
|---|---|---|
| 1 | **访问管理 CAM → 用户 → 用户列表** | 找到自己 `azizliang`，看**已有哪些策略**（这决定你现在能干什么） |
| 2 | **CAM → 访问密钥** | 创建 API 密钥，抄下 SecretId/SecretKey |
| 3 | **CAM → 角色** | 新建角色 → 载体选「云产品服务」→ 选 **Agent Runtime** → 授予 TCR 拉取权限 → **记下 RoleArn** |
| 4 | **CAM → 策略** | 新建自定义策略（空白模板），内容为 `cam:PassRole` 指向上面的 RoleArn → 关联给你自己 |
| 5 | **容器镜像服务 TCR** | 确认实例 → 建命名空间 `swe-synth` → 访问凭证 → 拿 `docker login` 命令 |
| 6 | **Agent 沙箱服务 AGS** | API Keys 建 Key；沙箱工具页先建一个**内置** `code-interpreter` 工具练手，再建**自定义镜像**工具 |

**建议的学习方法（很有效）**：每一步都「**控制台点一遍 → 再用 TCCLI/SDK 复现一遍**」。
比如沙箱工具，先在控制台建一个，然后：
```bash
tccli ags DescribeSandboxTools --cli-unfold-argument
```
看返回的 JSON 长什么样 —— 这就是你后面写 `CreateSandboxTool` 请求体的最好参考。**控制台是你的 API 说明书。**

---

## 六、Day 1~2 的 5 个 Hello World（按顺序做，每个都要跑通）

这 5 个跑通了，M0 就算完成，后面全是业务逻辑，不会再被平台卡住。

**① TokenHub 通不通**（最简单，先做这个建立信心）
```python
from openai import OpenAI
c = OpenAI(base_url="https://tokenhub.tencentmaas.com/v1", api_key="<KEY>")
print(c.chat.completions.create(model="deepseek-v4-pro",
      messages=[{"role":"user","content":"说一句话证明你在线"}]).choices[0].message.content)
```

**② 沙箱能不能起来**（用内置模板，先不碰自定义镜像）
```python
import os
os.environ["E2B_DOMAIN"]  = "ap-guangzhou.tencentags.com"
os.environ["E2B_API_KEY"] = "e2b_xxxx"
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create(template="<控制台里的沙箱工具名称>", timeout=600)
print(sbx.run_code('print("hello sandbox")').logs)
print(sbx.commands.run("cat /etc/os-release; python3 -V").stdout)
sbx.kill()          # 一定要 kill，按时长计费
```

**③ TCR 能不能推镜像**
```bash
docker login <registry> -u <user> -p <pass>
printf 'FROM ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest\nRUN pip install requests\n' > Dockerfile
docker build -t <registry>/swe-synth/hello:v1 . --platform=linux/amd64
docker push <registry>/swe-synth/hello:v1
```

**④ 自定义镜像能不能变成沙箱**（**整条链路最关键的一步，风险最高**）
用 ③ 推的镜像，在 AGS 控制台建自定义沙箱工具 → 用 ② 的代码换成新工具名启动 → 能 `run_code` 就说明 CAM 角色 + PassRole + TCR 拉取权限全打通了。
> 这一步如果失败，99% 是权限问题（角色没授 TCR 权限 / 缺 PassRole），别怀疑代码。

**⑤ 探一个关键未知：沙箱里能不能 `docker build`**
```python
print(sbx.commands.run("which docker; docker info || echo 'NO DOCKER DAEMON'").stdout)
```
> 结论决定架构：如果没有 docker daemon（大概率），就按方案里那样，**构建放构建机、沙箱只负责运行**。这个结论要早点拿到。

---

## 七、内部账号特有的坑（提前知道能省很多时间）

1. **审批周期**：权限单可能 1~3 天，所以 Day 1 提单，等待期间做本地能做的（仓库池筛选、AST 挖空逻辑、GitHub 检索模块 —— 这些都不依赖腾讯云）。
2. **资源标签**：内部平台要求资源打标签（你截图里有「资源标签管理」）。建资源时统一打 `project=swe-synth`，方便费用归属和清理。
3. **费用意识**：沙箱**按运行时长计费**。写代码时 `try/finally` 里必须 `sbx.kill()`，并给整个流水线设兜底清理，否则忘关的实例会一直烧钱。
4. **公网访问**：沙箱要 `git clone` GitHub、`pip install`。创建沙箱工具时 `NetworkMode` 要设 `PUBLIC`；如果内部网络出不去，就改成**镜像里预置好依赖和仓库代码**（这本来就是我方案里的做法，所以影响不大）。
5. **地域一致**：TCR 实例、沙箱、CVM 尽量都放同一地域（如 `ap-guangzhou`），跨地域拉镜像慢且可能不通。
6. **公网文档 vs 内部实际**：内部环境可能有专用域名/网关。文档看不通的地方，优先找内部 wiki 或实测确认，别自己硬猜。

---

## 八、一句话行动清单

- [ ] **今天**：自研上云平台提权限单（CAM + TCR + AGS + 密钥 +（可选）CVM）
- [ ] **今天**：确认三件事（TokenHub Key、TCR 实例是否复用、有无内部示例）
- [ ] **今天**：本地装 Python 3.11 + Docker + 依赖 + `tccli configure`
- [ ] **权限下来后**：控制台把第五节 6 个页面点一遍，凑齐凭证五件套写进 `.env`
- [ ] **然后**：按第六节顺序跑通 5 个 Hello World（尤其 ④ 和 ⑤）
- [ ] **等待期不浪费**：先写不依赖云的模块（仓库筛选 / AST 挖空 / GitHub 去重检索）

✅ 5 个 Hello World 全绿 = M0 完成 = 可以进 M1 单题打样。
