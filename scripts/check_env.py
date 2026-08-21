#!/usr/bin/env python3
"""
开工前环境自检（M0 门禁）
=========================

一条命令检查全部 6 条链路，哪条不通、为什么不通、去哪里解决，一目了然。

用法：
    python scripts/check_env.py                 # 跑全部检查
    python scripts/check_env.py --only sandbox  # 只跑某一项
    python scripts/check_env.py --tcr-push      # 额外实测 build+push 到 TCR（会产生镜像）

检查项（按依赖顺序）：
    local     本地工具链：Python 3.11 / Docker / buildx / tccli
    tokenhub  TokenHub LLM 网关是否可调用
    github    GitHub API 是否可用（含限流额度）
    tcr       TCR 是否能 docker login（--tcr-push 时额外实测推送）
    sandbox   Agent Sandbox 是否能创建实例并执行命令
    dind      【关键探测】沙箱内有没有 Docker daemon —— 决定整体架构

退出码：全部通过 0，有失败 1。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 结果模型

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"


@dataclass
class Result:
    name: str
    status: str = SKIP
    detail: str = ""
    hint: str = ""
    notes: list[str] = field(default_factory=list)


def _ok(name, detail="", notes=None) -> Result:
    return Result(name, PASS, detail, notes=notes or [])


def _fail(name, detail, hint="") -> Result:
    return Result(name, FAIL, detail, hint)


def _skip(name, detail, hint="") -> Result:
    return Result(name, SKIP, detail, hint)


def _warn(name, detail, hint="") -> Result:
    return Result(name, WARN, detail, hint)


# ---------------------------------------------------------------- 工具函数

def load_env() -> None:
    """加载 .env；没装 python-dotenv 也能退化为手工解析。"""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        print("提示：未找到 .env，将只使用当前 shell 的环境变量。")
        print("      执行 `cp .env.example .env` 后填入凭证。\n")
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
    except ImportError:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """执行本地命令，返回 (退出码, 合并输出)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时({timeout}s): {' '.join(cmd)}"


# ---------------------------------------------------------------- 1. 本地工具链

def check_local() -> Result:
    notes: list[str] = []
    problems: list[str] = []

    major, minor = sys.version_info[:2]
    py = f"{major}.{minor}"
    if (major, minor) == (3, 11):
        notes.append(f"Python {py}（符合课题要求）")
    else:
        notes.append(f"Python {py} —— 课题指定 3.11，建议用 3.11 建虚拟环境")

    if shutil.which("docker"):
        code, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
        if code == 0 and out:
            notes.append(f"Docker daemon 正常（server {out}）")
        else:
            problems.append("docker 已安装但 daemon 未运行（启动 Docker Desktop）")
        code, _ = run(["docker", "buildx", "version"])
        notes.append("buildx 可用" if code == 0 else "buildx 不可用（跨架构构建会受影响）")
    else:
        problems.append("未安装 docker")

    notes.append("tccli 已安装" if shutil.which("tccli") else "未安装 tccli（pip install tccli）")

    # Mac Apple Silicon 提醒：沙箱只支持 linux/amd64
    code, arch = run(["uname", "-m"])
    if code == 0 and arch in ("arm64", "aarch64"):
        notes.append(
            f"当前架构 {arch}：沙箱镜像只支持 linux/amd64，"
            "构建须加 --platform=linux/amd64（慢），建议用 amd64 构建机"
        )

    if problems:
        return Result("local 本地工具链", FAIL, "；".join(problems),
                      hint="装好 Docker 并启动，再重跑", notes=notes)
    return _ok("local 本地工具链", "本地工具链就绪", notes)


# ---------------------------------------------------------------- 2. TokenHub

def check_tokenhub() -> Result:
    key, base = env("TOKENHUB_API_KEY"), env("TOKENHUB_BASE_URL")
    model = env("TOKENHUB_MODEL", "deepseek-v4-pro")
    if not key:
        return _skip("tokenhub LLM 网关", "TOKENHUB_API_KEY 未配置",
                     "内部平台申请 Key / 确认是否有团队共用 Key")
    try:
        from openai import OpenAI
    except ImportError:
        return _fail("tokenhub LLM 网关", "缺少 openai 包", "pip install -r requirements.txt")

    try:
        t0 = time.time()
        client = OpenAI(base_url=base, api_key=key, timeout=60)
        # ⚠️ 不要设小的 max_tokens：deepseek-v4-pro / glm-5 等是推理模型，
        #    会先生成 reasoning_content（思维链）再生成 content。
        #    max_tokens 太小会被思维链吃光 → finish_reason=length 且 content 为空。
        rsp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复两个字：在线"}],
            max_tokens=512,
        )
        choice = rsp.choices[0]
        text = (choice.message.content or "").strip()
        reasoning = getattr(choice.message, "reasoning_content", None)
        usage = getattr(rsp, "usage", None)

        notes = [f"模型 {model} 响应：{text[:40]!r}", f"耗时 {time.time() - t0:.1f}s"]
        if usage:
            notes.append(
                f"token 用量 prompt={usage.prompt_tokens} completion={usage.completion_tokens}"
            )
        if reasoning:
            notes.append(
                f"⚠️ 该模型为推理模型（返回 reasoning_content，本次 {len(reasoning)} 字符）："
                "调用时 max_tokens 要留足，否则思维链占满会导致 content 为空"
            )

        # 空内容必须报失败：链路虽通但结果不可用，放过去会在出题阶段大面积踩坑
        if not text:
            return _fail(
                "tokenhub LLM 网关",
                f"调用成功但 content 为空（finish_reason={choice.finish_reason}）",
                "推理模型的思维链占满了 max_tokens。把 max_tokens 调大（≥512），"
                "或改用非推理模型",
            )
        return _ok("tokenhub LLM 网关", "调用成功", notes)
    except Exception as e:  # noqa: BLE001
        return _fail("tokenhub LLM 网关", f"{type(e).__name__}: {e}",
                     f"确认 base_url({base})、Key 有效性、模型名 {model} 是否在可用列表内")


# ---------------------------------------------------------------- 3. GitHub

def check_github() -> Result:
    token = env("GITHUB_TOKEN")
    if not token:
        return _skip("github API", "GITHUB_TOKEN 未配置",
                     "github.com → Settings → Developer settings → PAT（public_repo 即可）")
    try:
        import requests
    except ImportError:
        return _fail("github API", "缺少 requests 包", "pip install -r requirements.txt")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get("https://api.github.com/rate_limit", headers=headers, timeout=20)
        if r.status_code != 200:
            return _fail("github API", f"HTTP {r.status_code}: {r.text[:120]}", "检查 token 是否过期")
        res = r.json()["resources"]
        notes = [
            f"core 剩余 {res['core']['remaining']}/{res['core']['limit']}",
            f"search 剩余 {res['search']['remaining']}/{res['search']['limit']}（去重检索靠它，注意限流）",
        ]
        # 顺带验证搜索能力（去重比对的核心接口）
        q = "repo:psf/requests+super_len"
        rs = requests.get(f"https://api.github.com/search/issues?q={q}&per_page=1",
                          headers=headers, timeout=20)
        notes.append(
            f"search/issues 可用，命中 {rs.json().get('total_count', '?')} 条"
            if rs.status_code == 200 else f"search/issues 异常 HTTP {rs.status_code}"
        )
        return _ok("github API", "token 有效", notes)
    except Exception as e:  # noqa: BLE001
        return _fail("github API", f"{type(e).__name__}: {e}", "检查网络 / 代理")


# ---------------------------------------------------------------- 4. TCR

def _check_tcr_via_api(reg: str, user: str, pwd: str, ns: str) -> Result:
    """不依赖本机 Docker，用 Docker Registry v2 API 验证凭证与 push 权限。

    等价于 `docker login` + 推送权限预检：
      1) GET /v2/ 拿 WWW-Authenticate，得到 token realm
      2) 用 Basic Auth 换 token（200 即用户名/密码正确）
      3) 解析 token 里的 access.actions，确认含 push
    """
    try:
        import requests
    except ImportError:
        return _fail("tcr 镜像仓库", "缺少 requests 包", "pip install -r requirements.txt")

    notes = ["本机未装 Docker → 改用 Registry v2 API 验证（等价于 docker login）"]
    repo = f"{ns}/swe-synth-envcheck"
    try:
        r = requests.get(f"https://{reg}/v2/", timeout=20)
        wa = r.headers.get("WWW-Authenticate", "")
        realm = re.search(r'realm="([^"]+)"', wa)
        service = re.search(r'service="([^"]+)"', wa)
        if not realm:
            return _fail("tcr 镜像仓库", f"未取到 token realm（HTTP {r.status_code}）",
                         f"确认 {reg} 是正确的镜像仓库域名")

        params = {"scope": f"repository:{repo}:pull,push"}
        if service:
            params["service"] = service.group(1)
        t = requests.get(realm.group(1), params=params, auth=(user, pwd), timeout=20)
        if t.status_code != 200:
            return _fail("tcr 镜像仓库", f"凭证校验失败 HTTP {t.status_code}: {t.text[:120]}",
                         "个人版：控制台→容器镜像服务→实例管理→个人版实例→初始化/重置密码；"
                         "用户名为账号 Uin")
        notes.append("凭证有效（token 换取成功）")

        tok = t.json().get("token") or t.json().get("access_token") or ""
        # 解析 JWT payload，确认授予了 push
        try:
            seg = tok.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode())
            actions: list[str] = []
            for a in payload.get("access") or []:
                actions += a.get("actions") or []
            if "push" in actions:
                notes.append(f"push 权限已授予（actions={sorted(set(actions))}）")
            else:
                return _fail("tcr 镜像仓库", f"凭证有效但无 push 权限（actions={actions}）",
                             "确认账号对该命名空间有写权限")
        except Exception:  # noqa: BLE001
            notes.append("token 解析跳过（不影响 docker push）")

        notes.append(f"目标镜像前缀：{reg}/{ns}/<task_id>:v1")
        notes.append("⚠️ 仍需装 Docker 或用 amd64 构建机才能真正 build/push")
        return _ok("tcr 镜像仓库", "凭证与 push 权限已验证（API 方式）", notes)
    except Exception as e:  # noqa: BLE001
        return _fail("tcr 镜像仓库", f"{type(e).__name__}: {e}", "检查网络 / 域名")


def check_tcr(do_push: bool = False) -> Result:
    reg, user, pwd = env("TCR_REGISTRY"), env("TCR_USERNAME"), env("TCR_PASSWORD")
    ns = env("TCR_NAMESPACE", "swe-synth")
    if not all([reg, user, pwd]):
        return _skip("tcr 镜像仓库", "TCR_REGISTRY/USERNAME/PASSWORD 未配齐",
                     "个人版 CCR：控制台→容器镜像服务→实例管理→个人版实例→初始化密码"
                     "（用户名=账号 Uin）；企业版 TCR：实例→访问凭证→长期访问凭证")
    if not shutil.which("docker"):
        # 没有 Docker 也能验证凭证，不必整项跳过
        return _check_tcr_via_api(reg, user, pwd, ns)

    # 用 stdin 传密码，避免密码出现在进程列表里
    p = subprocess.run(["docker", "login", reg, "-u", user, "--password-stdin"],
                       input=pwd, capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        return _fail("tcr 镜像仓库", (p.stdout + p.stderr).strip()[:200],
                     "确认凭证有效、域名正确、子用户有 TCR 权限")

    notes = [f"docker login {reg} 成功"]
    if not do_push:
        notes.append("未实测推送（加 --tcr-push 可实测 build+push）")
        return _ok("tcr 镜像仓库", "登录成功", notes)

    # 实测：构建一个最小镜像并推送，验证命名空间权限与 amd64 构建链路
    base = env("SANDBOX_BASE_IMAGE", "ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest")
    tag = f"{reg}/{ns}/env-check:v1"
    ctx = os.path.join(ROOT, ".tmp_envcheck")
    os.makedirs(ctx, exist_ok=True)
    try:
        with open(os.path.join(ctx, "Dockerfile"), "w", encoding="utf-8") as f:
            # 只加依赖、不动 USER/WORKDIR/ENTRYPOINT —— 保证能作为沙箱工具镜像启动
            f.write(f"FROM {base}\nRUN echo swe-synth-env-check > /opt/env_check.txt\n")
        code, out = run(["docker", "build", "-t", tag, "--platform=linux/amd64", ctx], timeout=1800)
        if code != 0:
            return _fail("tcr 镜像仓库", f"build 失败: {out[-300:]}",
                         "arm64 机器跨架构构建易失败/极慢，建议用 amd64 构建机")
        notes.append("amd64 构建成功")
        code, out = run(["docker", "push", tag], timeout=1800)
        if code != 0:
            return _fail("tcr 镜像仓库", f"push 失败: {out[-300:]}",
                         f"确认命名空间 {ns} 已创建且凭证有写权限")
        notes.append(f"推送成功：{tag}")
        notes.append("下一步可用该镜像在 AGS 控制台建自定义沙箱工具，验证 CAM 角色 + PassRole")
        return _ok("tcr 镜像仓库", "登录 + 构建 + 推送全通", notes)
    finally:
        shutil.rmtree(ctx, ignore_errors=True)


# ---------------------------------------------------------------- 5. Agent Sandbox

_sandbox_holder: dict = {}


def check_sandbox() -> Result:
    key, domain = env("E2B_API_KEY"), env("E2B_DOMAIN")
    template = env("AGS_SANDBOX_TEMPLATE")
    if not key:
        return _skip("sandbox Agent 沙箱", "E2B_API_KEY 未配置",
                     "AGS 控制台 → API Keys → 新建：https://console.cloud.tencent.com/ags/sandbox")
    if not template:
        return _skip("sandbox Agent 沙箱", "AGS_SANDBOX_TEMPLATE 未配置",
                     "AGS 控制台 → 沙箱工具 → 先建一个内置 code-interpreter 工具，把名称填进来")
    if not domain:
        return _fail("sandbox Agent 沙箱", "E2B_DOMAIN 未配置",
                     "必须设为 ap-guangzhou.tencentags.com，否则会打到 E2B 官方")

    # e2b 2.x 默认强制校验 API Key 必须是 "e2b_" 前缀，AGS 的 Key 是 "ark_xxx" 格式
    # 会被拦在客户端；官方留的开关 E2B_VALIDATE_API_KEY=false 可跳过纯格式校验
    # （不影响鉴权/协议本身），必须在 import e2b 系列包之前设置生效。
    os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")
    try:
        from e2b_code_interpreter import Sandbox
    except ImportError:
        return _fail("sandbox Agent 沙箱", "缺少 e2b-code-interpreter",
                     "pip install -r requirements.txt")

    os.environ["E2B_API_KEY"] = key
    os.environ["E2B_DOMAIN"] = domain

    sbx = None
    try:
        t0 = time.time()
        # SDK 形态兼容：
        #   e2b 2.x（生产默认）→ Sandbox.create(...)，配合 E2B_VALIDATE_API_KEY=false 已实测走通
        #   e2b 1.x（兜底）    → Sandbox(template=...)
        if hasattr(Sandbox, "create"):
            sbx = Sandbox.create(template=template, timeout=600)
        else:
            sbx = Sandbox(template=template, timeout=600)
        boot = time.time() - t0
        notes = [f"实例创建成功，冷启动 {boot:.1f}s", f"sandbox_id={getattr(sbx, 'sandbox_id', '?')}"]

        logs = sbx.run_code('print("hello sandbox")')
        notes.append(f"run_code 正常：{str(logs.logs)[:60]}")

        info = sbx.commands.run("python3 -V; git --version; uname -m", timeout=60)
        notes.append("commands.run 正常：" + " | ".join(info.stdout.split("\n")[:3]))

        sbx.files.write("/tmp/probe.txt", "ok")
        notes.append("files 读写正常" if sbx.files.read("/tmp/probe.txt").strip() == "ok"
                     else "files 读写异常")

        _sandbox_holder["sbx"] = sbx  # 交给 dind 探测复用，避免重复计费
        return _ok("sandbox Agent 沙箱", "沙箱链路全通", notes)
    except Exception as e:  # noqa: BLE001
        if sbx is not None:
            try:
                sbx.kill()
            except Exception:  # noqa: BLE001
                pass
        return _fail("sandbox Agent 沙箱", f"{type(e).__name__}: {e}",
                     f"确认工具名 {template!r} 与控制台一致、Key 有效、子用户有 AGS 权限")


# ---------------------------------------------------------------- 6. 沙箱内 Docker 探测（架构决策点）

def check_dind() -> Result:
    sbx = _sandbox_holder.get("sbx")
    if sbx is None:
        return _skip("dind 沙箱内 Docker", "沙箱未就绪，跳过", "先让 sandbox 检查通过")
    try:
        has_cli = sbx.commands.run("command -v docker || echo NONE", timeout=60).stdout.strip()
        if "NONE" in has_cli or not has_cli:
            return _warn("dind 沙箱内 Docker", "沙箱内无 docker CLI",
                         "架构定型：docker build/push 放构建机，沙箱只负责运行题目镜像（推荐方案）")
        daemon = sbx.commands.run("docker info >/dev/null 2>&1 && echo OK || echo NO_DAEMON",
                                  timeout=90).stdout.strip()
        if "OK" in daemon:
            return _ok("dind 沙箱内 Docker", "沙箱内 Docker daemon 可用（可在沙箱内构建）",
                       [f"docker CLI: {has_cli}"])
        return _warn("dind 沙箱内 Docker", "有 docker CLI 但无 daemon（非特权容器，预期结果）",
                     "架构定型：构建放构建机；沙箱侧把题目镜像作为沙箱工具镜像直接启动")
    except Exception as e:  # noqa: BLE001
        return _warn("dind 沙箱内 Docker", f"探测异常 {type(e).__name__}: {e}",
                     "按无 DinD 处理，构建放构建机")


def cleanup_sandbox() -> None:
    """沙箱按运行时长计费，务必销毁。"""
    sbx = _sandbox_holder.pop("sbx", None)
    if sbx is None:
        return
    try:
        sbx.kill()
        print("已销毁沙箱实例（按时长计费，切记 kill）")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  沙箱销毁失败，请去控制台手动确认：{e}")


# ---------------------------------------------------------------- 输出

ICON = {PASS: "✅", FAIL: "❌", SKIP: "⏭️ ", WARN: "⚠️ "}


def report(results: list[Result]) -> int:
    print("\n" + "=" * 72)
    print("环境自检结果")
    print("=" * 72)
    for r in results:
        print(f"\n{ICON[r.status]} [{r.status}] {r.name}")
        if r.detail:
            print(f"     {r.detail}")
        for n in r.notes:
            print(f"     · {n}")
        if r.hint and r.status in (FAIL, SKIP):
            print(f"     → 怎么办：{r.hint}")

    n_pass = sum(r.status == PASS for r in results)
    n_fail = sum(r.status == FAIL for r in results)
    n_skip = sum(r.status == SKIP for r in results)
    n_warn = sum(r.status == WARN for r in results)
    print("\n" + "-" * 72)
    print(f"通过 {n_pass} | 失败 {n_fail} | 跳过 {n_skip} | 警告 {n_warn}")
    if n_fail == 0 and n_skip == 0:
        print("🎉 M0 门禁通过，可以进入 M1 单题打样")
    elif n_skip:
        print("凭证未配齐属正常（权限审批中）。已通过的项说明该链路没问题。")
    print("-" * 72)
    return 1 if n_fail else 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="开工前环境自检（M0 门禁）")
    ap.add_argument("--only", choices=["local", "tokenhub", "github", "tcr", "sandbox", "dind"],
                    help="只跑指定检查项")
    ap.add_argument("--tcr-push", action="store_true",
                    help="实测 build+push 到 TCR（会在仓库产生 env-check:v1 镜像）")
    args = ap.parse_args()

    load_env()

    checks: dict[str, Callable[[], Result]] = {
        "local": check_local,
        "tokenhub": check_tokenhub,
        "github": check_github,
        "tcr": lambda: check_tcr(args.tcr_push),
        "sandbox": check_sandbox,
        "dind": check_dind,
    }
    names = [args.only] if args.only else list(checks)

    results: list[Result] = []
    try:
        for name in names:
            print(f"→ 检查 {name} ...")
            results.append(checks[name]())
    finally:
        cleanup_sandbox()

    return report(results)


if __name__ == "__main__":
    sys.exit(main())
