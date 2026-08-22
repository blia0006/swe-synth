"""镜像构建产物生成（Dockerfile + 镜像内 /task 契约）

镜像内契约（Agent2 完全通用、与具体题目解耦的关键）
--------------------------------------------------
    /task/problem_statement.md   题干（交付给被测 Coding Agent）
    /task/metadata.json          repo/base_commit/test_cmd/FAIL_TO_PASS/PASS_TO_PASS
    /task/run_tests.sh           只跑测试，输出机器可读结果
    /task/verify.sh              判分入口 → 退出码 + /task/result.json
    /workspace/repo/             已 stub 化的仓库（base_commit 固定）
    /opt/solution/golden.patch   **仅 :v1-sol 镜像存在**（防泄题）

平台硬约束（违反则沙箱起不来，详见 requirements-check.md §3.1）
-------------------------------------------------------------
    · 必须 FROM 一个带有 /init(S6) 与 envd 的镜像——可以是官方
      ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest，也可以是内部维护的
      共享 base 镜像（swe_synth/agent1/base_image/，已把 S6/envd 从官方镜像搬到
      Ubuntu 22.04 之上，双镜像方案的「环境层」，详见该目录下 Dockerfile 头部说明）。
      裸 ubuntu:22.04（未搬运这两个组件）没有这两样，run_code/commands/files 全失效。
    · USER 保持 root、WORKDIR 保持 /
    · 不依赖 Dockerfile 的 ENV（快照启动不生效，须走 API 的 Env 参数）
    · 不覆盖 ENTRYPOINT（若覆盖须回填 Command=["/init"]）
    · 必须 --platform=linux/amd64
    · 保留端口 49999(run_code) / 49983(envd)
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

__all__ = ["BuildContext", "render_dockerfile", "render_verify_sh",
           "render_run_tests_sh", "write_build_context", "audit_dockerfile",
           "docker_build_cmd", "docker_push_cmd",
           "podman_build_cmd", "podman_push_cmd", "podman_login_cmd",
           "buildah_build_cmd", "buildah_push_cmd", "buildah_login_cmd"]


@dataclass
class BuildContext:
    """构建一个题目镜像所需的全部信息。"""

    task_id: str
    repo: str                     # owner/name
    clone_url: str
    base_commit: str
    language: str
    test_cmd: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    problem_statement: str
    stub_patch: str               # 应用到原始仓库以得到 stub 态
    golden_patch: str             # stub → 原实现（标准答案）
    modified_files: list[str]
    do_not_modify: list[str]
    install_cmds: list[str]
    base_image: str = "ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest"
    # 注：Python 3.11 不再走 apt 装（基础镜像底层系统版本升级后，官方源可能已不
    # 提供 python3.11 系列包，比如 Debian 13/trixie 默认已是 3.13），改由 uv 拉取
    # 独立的 3.11 运行时（见 _DOCKERFILE_TMPL），不受系统 apt 源版本变化影响。
    extra_packages: tuple[str, ...] = ("git", "docker.io", "curl", "ca-certificates")
    task_venv: str = "/opt/venv311"
    symbol: str = ""              # 被挖空/重构的符号路径，用于续跑时去重
    task_type: str = "feature_implementation"   # 三种题型之一（Agent2 可据此调整策略）

    def metadata(self) -> dict:
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "language": self.language,
            "task_type": self.task_type,
            "symbol": self.symbol,
            "test_cmd": self.test_cmd,
            "FAIL_TO_PASS": self.fail_to_pass,
            "PASS_TO_PASS": self.pass_to_pass,
            "modified_files": self.modified_files,
            "do_not_modify": self.do_not_modify,
            "workspace": "/workspace/repo",
            "python": f"{self.task_venv}/bin/python",
        }


# ------------------------------------------------------------------ Dockerfile

_DOCKERFILE_TMPL = """\
# 题目镜像：{task_id}（{repo} @ {base_commit}）
#
# 基础镜像：官方 ags-image/sandbox-code，或内部维护的共享 base 镜像
# （swe_synth/agent1/base_image/，双镜像方案的「环境层」——已内置 S6/envd +
#  git/docker CLI + Python 3.11 venv + pip 镜像源）。两者都能保证
# S6-Overlay(/init)、envd(49983)、run-code(49999) 齐全；裸 ubuntu:22.04
# （未搬运这两个组件）会导致 run_code / commands.run / files.* 全部失效。
FROM {base_image}

# ⚠️ 不设置 USER / WORKDIR / ENV —— 快照启动要求 root + WORKDIR=/，且镜像 ENV 不生效。
#    需要传环境变量时走 CreateSandboxTool 的 Env 参数（并设 S6_KEEP_ENV=1）。

# 环境层自愈安装：如果 FROM 的是共享 base 镜像，下面这些依赖（apt 包 / uv /
# Python 3.11 venv / pip 镜像源）已经装好，这一层几乎是空操作，仅体现为几 MB
# 的内容层；如果 FROM 的是裸官方镜像（尚未构建/切换共享 base），则照常从零
# 安装一遍，行为与切换前完全一致——因此换不换共享 base 都不会导致构建失败，
# 只是有没有拿到体积/速度收益的区别。
RUN if [ ! -x {venv}/bin/python ]; then \\
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \\
            sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \\
        fi; \\
        if [ -f /etc/apt/sources.list ]; then \\
            sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g; s|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \\
        fi; \\
        chmod o-w /usr/bin; \\
        apt-get update; \\
        apt-get install -y --no-install-recommends {packages}; \\
        rm -rf /var/lib/apt/lists/*; \\
        command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh; \\
        uv venv --python 3.11 --seed {venv}; \\
        {venv}/bin/python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple; \\
        {venv}/bin/python -m pip install --no-cache-dir -q --upgrade pip setuptools wheel; \\
    fi

# 仓库代码（构建期已固定到 base_commit 并应用 stub patch，保证可复现）
COPY repo/ /workspace/repo/

# 安装题目依赖（含测试依赖 —— 实测测试依赖常藏在 conftest.py 中）
# 注：构建上下文不含 .git（write_build_context 主动排除），若仓库用
# setuptools_scm/hatch-vcs 从 git tag 推导版本号，会因取不到 git 历史报
# LookupError 导致 build 失败。这里固定伪造一个版本号绕过，不影响功能
# （只影响 __version__ 字符串，不影响被测代码逻辑与判据）。
WORKDIR_PLACEHOLDER
RUN set -eux; cd /workspace/repo; \\
    export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+synth; \\
{install_block}

# 判据执行器：不管仓库自己的 extras/dependency-groups 是否声明了 pytest
# （常见做法是放进 [dependency-groups].dev，而 `pip install -e .` 装不到这些），
# 都强制装上，否则 verify.sh 里的 `python -m pytest` 会因 ModuleNotFoundError
# 而 n_collected=0，产生"空解、golden 解都判定失败"的假阴性（实测踩过）。
RUN {venv}/bin/python -m pip install --no-cache-dir pytest pytest-cov

# 任务契约文件
COPY task/ /task/
RUN chmod +x /task/run_tests.sh /task/verify.sh

# 预热：把 pytest 收集缓存跑一遍，缩短沙箱内首次执行时间（失败不影响构建）
RUN cd /workspace/repo && {venv}/bin/python -m pytest --collect-only -q >/dev/null 2>&1 || true

# ⚠️ 不覆盖 ENTRYPOINT/CMD，保持基础镜像的 /init + sleep infinity
"""


def render_dockerfile(ctx: BuildContext, *, with_solution: bool = False) -> str:
    """生成 Dockerfile。

    `with_solution=True` 时额外拷入 `golden.patch`（仅用于 `:v1-sol` 镜像，
    Agent2 用它验证「题目可解」；题目镜像绝不含答案，防泄题）。
    """
    install_lines = []
    for cmd in (ctx.install_cmds or ["pip install -e ."]):
        # 把裸 pip 替换为 venv 内的 pip，确保装进 3.11 环境
        c = cmd.replace("pip install", f"{ctx.task_venv}/bin/python -m pip install --no-cache-dir")
        install_lines.append(f"    {c}; \\")
    install_block = "\n".join(install_lines).rstrip(" \\")

    body = _DOCKERFILE_TMPL.format(
        task_id=ctx.task_id,
        repo=ctx.repo,
        base_commit=ctx.base_commit,
        base_image=ctx.base_image,
        packages=" ".join(ctx.extra_packages),
        venv=ctx.task_venv,
        install_block=install_block,
    )
    # WORKDIR 不能改（快照约束），用 cd 代替
    body = body.replace("WORKDIR_PLACEHOLDER\n", "")

    if with_solution:
        body += """
# ---- 仅 :v1-sol 镜像：标准答案（供 Agent2 验证可解性，不进题目镜像）
COPY solution/golden.patch /opt/solution/golden.patch
"""
    return body


# ------------------------------------------------------------------ 脚本

_RUN_TESTS_TMPL = """\
#!/usr/bin/env bash
# 只负责跑测试并输出结果，不做判分。
# 用法：/task/run_tests.sh [额外的 pytest 参数]
set -uo pipefail

REPO_DIR=/workspace/repo
PY={venv}/bin/python
OUT=${{RESULT_DIR:-/task}}/pytest_raw.log

cd "$REPO_DIR" || exit 90

# -p no:cacheprovider：避免写缓存污染工作区
# --report-log 在 pytest>=9 已移除，故统一用 -v 文本输出，由 verify.sh 解析
"$PY" -m pytest -v -p no:cacheprovider --no-header "$@" 2>&1 | tee "$OUT"
exit "${{PIPESTATUS[0]}}"
"""

_VERIFY_TMPL = """\
#!/usr/bin/env bash
# 判分入口（Agent2 唯一需要调用的脚本）
#
# 用法：
#   /task/verify.sh              # 验证当前工作区状态（用于「空解必须失败」）
#   /task/verify.sh --golden     # 先打入 golden.patch 再验证（用于「参考解必须通过」）
#
# 输出：
#   /task/result.json  机器可读结果
#   退出码 0 = 判定通过（FAIL_TO_PASS 全绿且 PASS_TO_PASS 全绿）
#            1 = 未通过
#           90+ = 环境/流程错误（与题目对错无关，便于区分）
set -uo pipefail

TASK_DIR=/task
REPO_DIR=/workspace/repo
PY={venv}/bin/python
META="$TASK_DIR/metadata.json"
RESULT="$TASK_DIR/result.json"
LOG="$TASK_DIR/pytest_raw.log"
MODE="${{1:-}}"

[ -f "$META" ] || {{ echo "metadata.json 缺失"; exit 90; }}

# --golden：应用标准答案（仅 :v1-sol 镜像存在该文件）
if [ "$MODE" = "--golden" ]; then
  PATCH=/opt/solution/golden.patch
  if [ ! -f "$PATCH" ]; then
    echo "golden.patch 不存在（当前为题目镜像，请用 :v1-sol 镜像）" >&2
    exit 91
  fi
  cd "$REPO_DIR" || exit 90
  git apply --verbose "$PATCH" || patch -p1 < "$PATCH" || {{
    echo "golden.patch 应用失败" >&2; exit 92;
  }}
fi

# 只跑本题判据涉及的测试文件，**不跑全量**。
# 原因（实测踩过）：仓库里可能存在与本题无关、但依赖缺失的测试文件，
# pytest 收集阶段一旦报错会 Interrupted，导致所有用例都不执行（n_collected=0），
# 判分结果变成假阴性。限定范围既准确又快。
cd "$REPO_DIR" || exit 90
TEST_FILES=$("$PY" -c '
import json, sys
meta = json.load(open(sys.argv[1], encoding="utf-8"))
ids = (meta.get("FAIL_TO_PASS") or []) + (meta.get("PASS_TO_PASS") or [])
files = []
for nid in ids:
    f = nid.split("::", 1)[0]
    if f not in files:
        files.append(f)
print(" ".join(files))
' "$META")

if [ -z "$TEST_FILES" ]; then
  echo "metadata.json 中没有任何测试用例，无法判分" >&2
  exit 93
fi

# shellcheck disable=SC2086
"$PY" -m pytest -v -p no:cacheprovider --no-header $TEST_FILES > "$LOG" 2>&1
PYTEST_RC=$?

# 用 Python 解析结果并生成 result.json（避免 shell 处理 JSON 出错）
"$PY" - "$META" "$LOG" "$RESULT" "$PYTEST_RC" <<'PYEOF'
import json, re, sys

meta_path, log_path, out_path, rc = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
meta = json.load(open(meta_path, encoding="utf-8"))
log = open(log_path, encoding="utf-8", errors="replace").read()
# 有些仓库自身 pytest 配置写死 addopts=--color=yes（如 humanize），即使输出
# 重定向到文件仍带 ANSI 颜色码，导致下面的结果解析正则匹配不到，需先剥离。
log = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log)

MAP = {{"PASSED": "passed", "FAILED": "failed", "ERROR": "error",
       "SKIPPED": "skipped", "XFAIL": "xfailed", "XPASS": "xpassed"}}
outcomes = {{}}
for m in re.finditer(r"^(\\S+::\\S+?)\\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\\b", log, re.M):
    outcomes[m.group(1)] = MAP[m.group(2)]
for m in re.finditer(r"^(?:FAILED|ERROR)\\s+(\\S+::\\S+)", log, re.M):
    outcomes.setdefault(m.group(1), "failed")

def ok(nid):
    return outcomes.get(nid) in ("passed", "xfailed")

f2p = meta.get("FAIL_TO_PASS") or []
p2p = meta.get("PASS_TO_PASS") or []
f2p_ok = [n for n in f2p if ok(n)]
p2p_ok = [n for n in p2p if ok(n)]
collect_error = bool(re.search(r"ERROR collecting|ImportError while loading conftest|INTERNALERROR", log))

result = {{
    "task_id": meta.get("task_id"),
    "passed": len(f2p_ok) == len(f2p) and len(p2p_ok) == len(p2p) and not collect_error,
    "fail_to_pass": {{"total": len(f2p), "passed": len(f2p_ok),
                    "failing": [n for n in f2p if not ok(n)]}},
    "pass_to_pass": {{"total": len(p2p), "passed": len(p2p_ok),
                    "failing": [n for n in p2p if not ok(n)]}},
    "collect_error": collect_error,
    "pytest_returncode": rc,
    "n_collected": len(outcomes),
    "raw_log_path": log_path,
}}
json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps({{k: result[k] for k in ("passed", "fail_to_pass", "pass_to_pass",
                                        "collect_error", "n_collected")}}, ensure_ascii=False))
sys.exit(0 if result["passed"] else 1)
PYEOF
exit $?
"""


def render_run_tests_sh(ctx: BuildContext) -> str:
    return _RUN_TESTS_TMPL.format(venv=ctx.task_venv)


def render_verify_sh(ctx: BuildContext) -> str:
    return _VERIFY_TMPL.format(venv=ctx.task_venv)


# ------------------------------------------------------------------ 落盘

def write_build_context(
    ctx: BuildContext,
    out_dir: str | Path,
    repo_src: str | Path,
    *,
    with_solution: bool = False,
) -> Path:
    """把构建上下文写到目录，供 `docker build` 使用。

    目录结构：
        <out>/Dockerfile
        <out>/repo/           ← 已 stub 化的仓库副本
        <out>/task/{problem_statement.md, metadata.json, run_tests.sh, verify.sh}
        <out>/solution/golden.patch   （仅 with_solution=True）
    """
    import shutil

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1) Dockerfile
    (out / "Dockerfile").write_text(render_dockerfile(ctx, with_solution=with_solution),
                                    encoding="utf-8")

    # 2) 仓库副本（排除 .git 与虚拟环境，减小镜像体积）
    repo_dst = out / "repo"
    if repo_dst.exists():
        shutil.rmtree(repo_dst)
    shutil.copytree(
        repo_src, repo_dst,
        ignore=shutil.ignore_patterns(".git", ".venv*", "venv", "__pycache__",
                                      "*.pyc", ".pytest_cache", ".tox", "node_modules"),
    )

    # 3) /task 契约
    task_dir = out / "task"
    task_dir.mkdir(exist_ok=True)
    (task_dir / "problem_statement.md").write_text(ctx.problem_statement, encoding="utf-8")
    (task_dir / "metadata.json").write_text(
        json.dumps(ctx.metadata(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (task_dir / "run_tests.sh").write_text(render_run_tests_sh(ctx), encoding="utf-8")
    (task_dir / "verify.sh").write_text(render_verify_sh(ctx), encoding="utf-8")

    # 4) 答案（仅 sol 镜像）
    if with_solution:
        sol = out / "solution"
        sol.mkdir(exist_ok=True)
        (sol / "golden.patch").write_text(ctx.golden_patch, encoding="utf-8")
    else:
        # 双重保险：确保题目镜像的构建上下文里没有答案
        sol = out / "solution"
        if sol.exists():
            shutil.rmtree(sol)

    return out


def docker_build_cmd(
    ctx_dir: str | Path, image: str, *, platform: str = "linux/amd64", no_cache: bool = False,
) -> list[str]:
    """返回 docker build 命令（参数列表形式，不经 shell，避免注入）。

    `no_cache`：AGS 沙箱工具对镜像做 erofs 快照转换时，若 sol 镜像与 task 镜像
    共享缓存层（仅末层 `COPY golden.patch` 不同），实测会出现层合并 bug——
    沙箱里看到的仍是旧内容（如缺 pytest）。用 `--no-cache` 让 sol 镜像所有层
    digest 与 task 镜像完全独立，规避该 bug（`pack_task` 对 sol 镜像默认开启）。
    """
    cmd = ["docker", "build", "-t", image, f"--platform={platform}"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(ctx_dir))
    return cmd


def docker_push_cmd(image: str) -> list[str]:
    return ["docker", "push", image]


# ------------------------------------------------------------ 无 daemon 构建（沙箱内场景）
#
# 沙箱运行时不提供 docker.sock / 特权容器（DinD 不通，已实测确认），因此
# 「整个流水线跑进沙箱」时，build/push 这一步不能用 docker daemon，只能用
# 无需 daemon 的工具。实测过程：
#   · buildah（Ubuntu 22.04 apt 仓库的 1.23.1 版本）——命中一个已知的
#     containers/image 老版本 bug：base 镜像的 layer manifest 一旦经由
#     Docker Desktop containerd 存储链路推送过，media type 会带有 OCI
#     manifest 包裹 docker 层类型的情况，1.23 无法转换，build 直接报错。
#   · podman（从 GitHub 拉取的 5.8.4 静态二进制，见
#     experiments/verify_sandbox_build_push.py）—— 同一套 base 镜像用它
#     build+push 全部成功，已验证是真正可用的方案，因此列为首选。
#   两者命令行语法与 docker 几乎一致（podman 本身就是 docker 兼容 CLI；
#   buildah 的 build 子命令叫 "bud"），保留 buildah 函数作为兼容/备选。

def podman_build_cmd(
    ctx_dir: str | Path, image: str, *, platform: str = "linux/amd64", no_cache: bool = False,
) -> list[str]:
    cmd = ["podman", "build", "--format", "docker", "-t", image, f"--platform={platform}"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(ctx_dir))
    return cmd


def podman_push_cmd(image: str) -> list[str]:
    return ["podman", "push", image]


def podman_login_cmd(registry: str, username: str) -> list[str]:
    return ["podman", "login", "--username", username, "--password-stdin", registry]


def buildah_build_cmd(
    ctx_dir: str | Path, image: str, *, platform: str = "linux/amd64", no_cache: bool = False,
) -> list[str]:
    """`buildah bud` 等价于 `docker build`；`--format docker` 保证产物是标准
    Docker Image Manifest（而非 buildah 默认的 OCI 格式），确保 AGS 沙箱工具
    拉取时行为与 docker build 出的镜像完全一致。"""
    cmd = ["buildah", "bud", "--format", "docker", "-t", image, f"--platform={platform}"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(ctx_dir))
    return cmd


def buildah_push_cmd(image: str) -> list[str]:
    return ["buildah", "push", image]


def buildah_login_cmd(registry: str, username: str) -> list[str]:
    return ["buildah", "login", "--username", username, "--password-stdin", registry]


# ------------------------------------------------------------------ 约束自检

_APPROVED_BASE_MARKERS = ("ags-image/sandbox-code", "swe-synth-base")


def audit_dockerfile(
    dockerfile: str,
    *,
    expect_solution: bool = False,
    approved_base_markers: tuple[str, ...] = _APPROVED_BASE_MARKERS,
) -> list[str]:
    """检查 Dockerfile 是否违反平台硬约束，返回问题列表（空 = 合规）。

    这些约束一旦违反，**沙箱会起不来或内置能力失效**，而且报错通常很隐晦
    （表现为创建实例超时或 run_code 无响应），排查成本极高。
    因此在构建前就静态拦住 —— 比事后到云上 debug 便宜得多。

    `approved_base_markers`：FROM 行需要命中其中任意一个子串才算合规。
    默认同时接受官方镜像（ags-image/sandbox-code）与内部共享 base 镜像
    （命名约定含 swe-synth-base，见 swe_synth/agent1/base_image/）——
    两者都已验证内置 S6/envd，因此都能让沙箱正常起来。这里只能做字符串级
    静态检查，共享 base 是否真的内置了这两个组件，由该镜像自身的构建/发布
    流程保证，不在每道题构建时重新校验。
    """
    problems: list[str] = []
    # 只看有效指令行，忽略注释与空行（注释里出现 USER 等字样不算违规）
    lines = [
        ln.strip() for ln in dockerfile.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

    def has_instr(name: str) -> bool:
        return any(re.match(rf"^{name}\b", ln, re.I) for ln in lines)

    from_lines = [ln for ln in lines if re.match(r"^FROM\s+", ln, re.I)]
    if not any(
        any(marker in ln for marker in approved_base_markers) for ln in from_lines
    ):
        problems.append(
            "未继承受信任的基础镜像（官方 ags-image/sandbox-code 或内部共享 base "
            "swe-synth-base）—— 沙箱可能缺少 /init(S6)、envd(49983)、run-code(49999)，"
            "run_code/commands/files 存在失效风险"
        )
    if has_instr("USER"):
        problems.append("设置了 USER —— 快照启动要求保持 root，会导致启动失败")
    if has_instr("WORKDIR"):
        problems.append("设置了 WORKDIR —— 快照启动要求保持 /，会导致启动失败")
    if has_instr("ENV"):
        problems.append(
            "设置了 ENV —— 快照启动不读取镜像 ENV，环境变量须走 API 的 Env 参数"
            "（并设 S6_KEEP_ENV=1）"
        )
    if has_instr("ENTRYPOINT"):
        problems.append("覆盖了 ENTRYPOINT —— 若确实需要，必须在创建沙箱工具时回填 Command=[\"/init\"]")

    has_golden = "golden.patch" in dockerfile
    if expect_solution and not has_golden:
        problems.append(":v1-sol 镜像应包含 golden.patch，但未找到")
    if not expect_solution and has_golden:
        problems.append("题目镜像(:v1)包含 golden.patch —— 会泄露答案，必须移除")

    return problems
