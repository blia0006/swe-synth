#!/usr/bin/env python3
"""构建耗时拆解实验：定位「7.5GB 镜像构建慢」的真正瓶颈

背景
----
同事质疑「docker 打包很快，2c4g 也不会慢」，需要用数据回答：
到底是 CPU 不够、磁盘不够、网络慢，还是别的原因。

方法
----
逐层构建同一个基础镜像之上的不同阶段，分别计时，找出耗时占比最大的层。
"""

import os
import subprocess
import tempfile
import time

BASE = "ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest"

STAGES = {
    "01_基础镜像(缓存命中)": f"""FROM {BASE}
RUN echo ok
""",
    "02_apt装git+dockerCLI": f"""FROM {BASE}
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \\
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \\
    fi
RUN chmod o-w /usr/bin
RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends git docker.io curl ca-certificates; \\
    rm -rf /var/lib/apt/lists/*
""",
    "03_uv拉Python3.11运行时": f"""FROM {BASE}
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
RUN uv venv --python 3.11 --seed /opt/venv311
RUN /opt/venv311/bin/python -m pip install --no-cache-dir -q --upgrade pip setuptools wheel
""",
}


def run(name: str, dockerfile: str, no_cache: bool) -> float:
    d = tempfile.mkdtemp(prefix="timing_")
    with open(os.path.join(d, "Dockerfile"), "w") as f:
        f.write(dockerfile)
    cmd = ["docker", "build", "-t", f"timing:{name.split('_')[0]}",
           "--platform=linux/amd64"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(d)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=5400)
    dt = time.time() - t0
    flag = "OK" if r.returncode == 0 else f"FAIL({r.returncode})"
    print(f"  {name:32s} {dt:7.1f}s  [{flag}]{' --no-cache' if no_cache else ''}",
          flush=True)
    if r.returncode != 0:
        print("    " + (r.stdout + r.stderr)[-300:].replace("\n", "\n    "))
    return dt


def main() -> None:
    print("=" * 72)
    print("构建耗时拆解（远端 CVM: 2核/3.5GB/50GB云盘）")
    print("=" * 72)

    print("\n【A】有缓存的情况（复用已有层）")
    for name, df in STAGES.items():
        run(name, df, no_cache=False)

    print("\n【B】--no-cache 的情况（答案镜像 :v1-sol 走的就是这条路）")
    for name, df in STAGES.items():
        run(name, df, no_cache=True)


if __name__ == "__main__":
    main()
