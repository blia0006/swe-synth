"""镜像打包与推送（Agent1 的最后一步：docker build → docker push）

对应课题要求（见 TASK-SPEC.md）
------------------------------
    Agent1（出题+打包）：… 编写 Dockerfile + 构建脚本 → **docker build → docker push 到 TCR**

本模块把 `pipeline` 产出的构建上下文（`.build/<task_id>/{task,sol}/`）打包成镜像并推送。

为什么与出题分离
----------------
· 出题不需要 Docker，可在无 Docker 环境下开发、验证（本项目全程如此）
· build/push 需要 Docker daemon，且 arm64 本机跨架构构建极慢，通常放 amd64 构建机
  → 通过 `DOCKER_HOST` 环境变量指向远端构建机即可，代码无需改动

安全
----
· 所有 docker 命令用**参数列表**形式（不经 shell），避免命令注入
· `docker login` 的密码经 stdin 传入（`--password-stdin`），**绝不出现在命令行**，
  避免被 `ps` 泄露
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..config.loader import Settings
from .dockerfile_gen import docker_build_cmd, docker_push_cmd

__all__ = ["PackResult", "docker_login", "pack_task", "pack_all"]


@dataclass
class PackResult:
    """一次打包/推送的结果。"""

    task_id: str
    image: str
    pushed: bool
    duration_sec: float
    log_tail: str = ""


class DockerError(RuntimeError):
    """docker build / push 失败。"""


def _run(cmd: list[str], *, input_text: str | None = None, timeout: int = 1800) -> tuple[int, str]:
    """执行命令（参数列表形式，不经 shell；支持 stdin 传密）。"""
    try:
        p = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True,
            errors="replace", timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）：{' '.join(cmd[:3])}"
    except FileNotFoundError:
        return 127, f"找不到命令：{cmd[0]}（是否安装了 Docker 或未配置 DOCKER_HOST？）"


def docker_login(registry: str, username: str, password: str) -> tuple[int, str]:
    """登录镜像仓库。密码经 stdin 传入，绝不出现在命令行。"""
    return _run(
        ["docker", "login", "--username", username, "--password-stdin", registry],
        input_text=password + "\n",
        timeout=120,
    )


def _run_with_retry(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 1800,
    retries: int = 15,
    backoff_sec: float = 10.0,
    max_backoff_sec: float = 60.0,
) -> tuple[int, str]:
    """执行命令，失败时重试（用于 `docker push` 因网络抖动超时等瞬时错误）。

    `docker push` 对已上传成功的层会直接跳过（"Layer already exists"），
    所以重试代价很低，只会重传上次失败的那一层。实测部分层会连续多次
    卡在同一 blob 的 commit 请求上（`net/http: timeout awaiting response headers`），
    因此默认重试次数、等待时间都放宽（线性递增退避，上限 60s）。
    """
    rc, out = _run(cmd, input_text=input_text, timeout=timeout)
    attempt = 1
    while rc != 0 and attempt < retries:
        time.sleep(min(backoff_sec * attempt, max_backoff_sec))
        rc, out = _run(cmd, input_text=input_text, timeout=timeout)
        attempt += 1
    return rc, out


def _docker_available() -> str | None:
    """检查 Docker 是否可用，返回错误说明（None = 可用）。"""
    rc, out = _run(["docker", "info"], timeout=60)
    if rc != 0:
        return f"docker 不可用：{out.strip()[-300:]}"
    return None


def pack_task(
    task_id: str,
    build_root: str | Path,
    settings: Settings,
    *,
    registry: str | None = None,
    username: str | None = None,
    password: str | None = None,
    platform: str | None = None,
    skip_login: bool = False,
) -> list[PackResult]:
    """把一个题目的两份镜像（`:v1` 题目镜像 + `:v1-sol` 答案镜像）build + push。

    返回两个 PackResult（题目镜像、答案镜像）。

    `skip_login=True`：供 `pack_all` 并发打包多个题目时使用 —— 登录只需做一次，
    并发场景下让每个 worker 各自重复 `docker login` 会并发写同一份
    `~/.docker/config.json`，存在把凭据文件写坏的风险，因此改为调用方
    统一在派发并发任务前登录一次。
    """
    import os

    build_root = Path(build_root)
    registry = (registry or os.environ.get("TCR_REGISTRY", "")).rstrip("/")
    username = username or os.environ.get("TCR_USERNAME", "")
    password = password or os.environ.get("TCR_PASSWORD", "")
    platform = platform or settings.get("image.platform", "linux/amd64")

    if not registry or not username or not password:
        raise DockerError("未配置 TCR_REGISTRY / TCR_USERNAME / TCR_PASSWORD（见 .env）")

    base = build_root / task_id
    if not base.is_dir():
        raise DockerError(f"构建上下文不存在：{base}（先跑 agent1 产出）")

    if not skip_login:
        rc, out = docker_login(registry, username, password)
        if rc != 0:
            raise DockerError(f"docker login {registry} 失败：{out[-500:]}")

    results: list[PackResult] = []
    for sub, is_sol in (("task", False), ("sol", True)):
        ctx_dir = base / sub
        if not (ctx_dir / "Dockerfile").is_file():
            raise DockerError(f"{ctx_dir} 缺少 Dockerfile")
        image = settings.image_ref(task_id, solution=is_sol)
        t0 = time.time()

        # sol 镜像强制 --no-cache：规避 AGS 沙箱对共享缓存层的合并 bug（见 dockerfile_gen.docker_build_cmd）
        rc, out = _run(
            docker_build_cmd(ctx_dir, image, platform=platform, no_cache=is_sol),
            timeout=3600,
        )
        if rc != 0:
            raise DockerError(f"docker build {image} 失败：\n{out[-800:]}")
        # push 对网络抖动（超时等瞬时错误）做重试；已上传成功的层会被跳过，重试代价很低
        rc, out = _run_with_retry(docker_push_cmd(image))
        if rc != 0:
            raise DockerError(f"docker push {image} 失败（已重试仍失败）：\n{out[-800:]}")

        results.append(PackResult(
            task_id=task_id, image=image, pushed=True,
            duration_sec=round(time.time() - t0, 1), log_tail=out[-300:],
        ))
    return results


def pack_all(
    build_root: str | Path,
    settings: Settings,
    *,
    task_ids: list[str] | None = None,
    platform: str | None = None,
    max_workers: int = 1,
    on_progress: "callable" = print,
) -> list[PackResult]:
    """打包 `.build/` 下所有（或指定）题目的镜像。

    `max_workers > 1` 时并发 build+push 多个题目 —— 这是「1 万道题」规模下
    打包环节的水平扩展点：docker build 本身 CPU/IO 密集但彼此独立
    （不同 task_id 的构建上下文互不相干），并发数建议按构建机的
    CPU 核数/磁盘 IO 能力设置（见 `config/settings.yaml` 的 `scale.pack_workers`）。
    """
    import os

    build_root = Path(build_root)
    ids = task_ids or sorted(d.name for d in build_root.iterdir()
                             if d.is_dir() and d.name.startswith("swe-synth-"))
    if not ids:
        return []

    # 登录只做一次（见 pack_task 的 skip_login 说明），避免并发写坏 docker 凭据文件
    registry = os.environ.get("TCR_REGISTRY", "").rstrip("/")
    username = os.environ.get("TCR_USERNAME", "")
    password = os.environ.get("TCR_PASSWORD", "")
    if not registry or not username or not password:
        raise DockerError("未配置 TCR_REGISTRY / TCR_USERNAME / TCR_PASSWORD（见 .env）")
    rc, out = docker_login(registry, username, password)
    if rc != 0:
        raise DockerError(f"docker login {registry} 失败：{out[-500:]}")

    out_results: list[PackResult] = []
    if max_workers <= 1:
        for tid in ids:
            out_results += pack_task(tid, build_root, settings, platform=platform, skip_login=True)
            on_progress(f"  ✅ 打包完成：{tid}")
        return out_results

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(pack_task, tid, build_root, settings, platform=platform, skip_login=True): tid
            for tid in ids
        }
        for fut in as_completed(futs):
            tid = futs[fut]
            try:
                out_results += fut.result()
                on_progress(f"  ✅ 打包完成：{tid}")
            except DockerError as e:
                on_progress(f"  ❌ 打包失败：{tid}：{e}")
    return out_results
