# 题目镜像：swe-synth-0010（jd/tenacity @ 26f719dc73d3c5612b9c1b8d18a7883837790ad8）
#
# ⚠️ 基础镜像不可更换：官方要求自定义沙箱必须继承 ags-image/sandbox-code，
#    其中包含 S6-Overlay(/init)、envd(49983)、run-code(49999)。
#    使用裸 ubuntu:22.04 会导致 run_code / commands.run / files.* 全部失效。
FROM ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest

# ⚠️ 不设置 USER / WORKDIR / ENV —— 快照启动要求 root + WORKDIR=/，且镜像 ENV 不生效。
#    需要传环境变量时走 CreateSandboxTool 的 Env 参数（并设 S6_KEEP_ENV=1）。

# 课题要求的运行时工具链：Git + Docker CLI + Python 3.11
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git docker.io python3.11 python3.11-venv python3.11-dev; \
    rm -rf /var/lib/apt/lists/*

# 题目代码运行在独立的 Python 3.11 虚拟环境中（满足课题「Python 3.11」要求）
RUN /opt/venv311/bin/python -V || python3.11 -m venv /opt/venv311
RUN /opt/venv311/bin/python -m pip install --no-cache-dir -q --upgrade pip setuptools wheel

# 仓库代码（构建期已固定到 base_commit 并应用 stub patch，保证可复现）
COPY repo/ /workspace/repo/

# 安装题目依赖（含测试依赖 —— 实测测试依赖常藏在 conftest.py 中）
RUN set -eux; cd /workspace/repo; \
    /opt/venv311/bin/python -m pip install --no-cache-dir -e .; \
    /opt/venv311/bin/python -m pip install --no-cache-dir pytest tornado;

# 任务契约文件
COPY task/ /task/
RUN chmod +x /task/run_tests.sh /task/verify.sh

# 预热：把 pytest 收集缓存跑一遍，缩短沙箱内首次执行时间（失败不影响构建）
RUN cd /workspace/repo && /opt/venv311/bin/python -m pytest --collect-only -q >/dev/null 2>&1 || true

# ⚠️ 不覆盖 ENTRYPOINT/CMD，保持基础镜像的 /init + sleep infinity
