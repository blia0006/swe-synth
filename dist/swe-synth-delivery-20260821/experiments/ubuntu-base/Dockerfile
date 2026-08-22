# Ubuntu 版沙箱基础镜像（验证「基础层从 Debian 换成 Ubuntu」的可行性）
#
# 背景
# -----------------
# 官方 ags-image/sandbox-code 底层是 Debian 13 (trixie)，但客户环境几乎都是 Ubuntu。
# 课题原文也明确要求 ubuntu:22.04。
#
# 可行性依据（已实测）
# -------------------
# 让沙箱「能被 AGS 平台管理」的组件分两类：
#
#   A. envd + S6-Overlay  →  提供 commands.run / files.* 能力
#      · /usr/bin/envd 是**静态链接**（`not a dynamic executable`），可跨发行版
#      · S6-Overlay 3.2.1.0（/init /command /package）同为静态
#      → 可直接 COPY 到任意发行版
#
#   B. jupyter + uvicorn  →  提供 run_code（Python 代码解释器）能力
#      · 依赖官方镜像内 /usr/local/bin/python3 (3.12) 与 /root/.server 应用
#      · Debian 13 的 glibc 2.41 高于 Ubuntu 22.04 的 2.35，动态库无法向下搬运
#
# 本项目 Agent2 只使用 commands.run 与 files.read（见 agent2/sandbox_runner.py），
# **不使用 run_code**，因此只需搬运 A 类组件即可，B 类可整体省去。
#
# 收益：基础层从 6.86GB（官方镜像）降到 ubuntu:22.04 的约 78MB + 22MB 平台组件。

# ---- 阶段 1：从官方镜像取出平台组件（静态，可跨发行版）
FROM ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest AS platform

# ---- 阶段 2：Ubuntu 22.04 作为真正的基础层（满足课题原文要求）
# 走腾讯云内网 Docker Hub 镜像源：CVM 上 docker.io 直连不通（i/o timeout），
# 且内网拉取更快、不占公网带宽。等价于 docker.io/library/ubuntu:22.04。
FROM mirror.ccs.tencentyun.com/library/ubuntu:22.04

# 加速：清华 TUNA 源（只替换 host，不改路径与签名校验）
RUN sed -i 's|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g; s|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list

# 课题要求的运行时工具链：Python 3.11 + Git + Docker CLI
# Ubuntu 22.04 自带 Python 3.10，3.11 通过 deadsnakes 之外的方式获取：
# 用 uv 拉独立运行时（与主流水线保持一致，且不依赖第三方 PPA）
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git docker.io bash procps; \
    rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
RUN uv venv --python 3.11 --seed /opt/venv311
RUN /opt/venv311/bin/python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN /opt/venv311/bin/python -m pip install --no-cache-dir -q --upgrade pip setuptools wheel

# ---- 搬运 AGS 平台组件（S6-Overlay + envd，均为静态二进制）
COPY --from=platform /init /init
COPY --from=platform /command /command
COPY --from=platform /package /package
COPY --from=platform /usr/bin/envd /usr/bin/envd
COPY --from=platform /etc/s6-overlay /etc/s6-overlay

# 只保留 envd 服务：jupyter/uvicorn 依赖官方镜像内的 Python 3.12 环境
# （glibc 2.41 → 2.35 无法向下搬运），且本项目不使用 run_code 能力。
# 留着它们会导致 s6 反复拉起失败的服务，拖慢启动甚至判定不健康。
RUN rm -rf /etc/s6-overlay/s6-rc.d/user/contents.d/jupyter \
           /etc/s6-overlay/s6-rc.d/user/contents.d/uvicorn \
           /etc/s6-overlay/s6-rc.d/jupyter \
           /etc/s6-overlay/s6-rc.d/uvicorn

# ⚠️ 与官方镜像保持一致的平台约束（违反则沙箱起不来）：
#    不设 USER（保持 root）/ 不设 WORKDIR（保持 /）/ 不设 ENV / 不覆盖 ENTRYPOINT
#    端口 49983(envd) 保留
