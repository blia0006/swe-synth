# 共享 base 镜像：Ubuntu 22.04 沙箱环境层（双镜像方案的固化产物）
#
# 用途
# ----
# 题目镜像统一 `FROM` 这个共享 base（见 ../dockerfile_gen.py 的
# `_DOCKERFILE_TMPL`），环境依赖（git / docker.io / uv / Python 3.11 venv
# / pip 镜像源）只在这里装一次，题目镜像只剩「COPY 仓库 + COPY /task 契约」
# 这一层内容差异，实测体积从每题 ~7.5GB 降到 MB 级内容层。
#
# 为什么基础层选 Ubuntu 22.04（而不是官方 sandbox-code 的 Debian 13）
# -----------------------------------------------------------------
# 课题原文明确要求 ubuntu:22.04；官方 ags-image/sandbox-code 底层是
# Debian 13 (trixie)。让沙箱「能被 AGS 平台管理」的组件分两类：
#
#   A. envd + S6-Overlay  →  提供 commands.run / files.* 能力
#      · /usr/bin/envd 是静态链接（not a dynamic executable），可跨发行版搬运
#      · S6-Overlay 3.2.1.0（/init /command /package）同为静态
#   B. jupyter + uvicorn  →  提供 run_code（Python 代码解释器）能力
#      · 依赖官方镜像内 /usr/local/bin/python3 (3.12) 与 /root/.server 应用，
#        Debian 13 的 glibc 2.41 高于 Ubuntu 22.04 的 2.35，动态库无法向下搬运
#
# 本项目 Agent2 只用 commands.run 与 files.read（见 agent2/sandbox_runner.py），
# 不用 run_code，因此只需搬运 A 类组件，B 类整体省去。
#
# 使用方式
# --------
#   1. 执行同目录 build_and_push.sh 构建并推送
#   2. 把推送出的地址填到 config/settings.yaml 的 `image.base`
#   3. 之后 Agent1 出的所有新题目镜像都会自动 FROM 这个共享 base
#      （已构建过的旧镜像不受影响，需要重新 pack 才会切过去）
#
# 若暂未执行以上步骤，`image.base` 保持指向官方镜像也能正常工作——
# dockerfile_gen.py 里的环境安装步骤是自愈式的（检测到 venv 不存在才安装），
# 不会因为没有共享 base 而构建失败，只是拿不到体积/速度收益。

ARG PLATFORM_BASE_IMAGE=ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest
ARG UBUNTU_BASE_IMAGE=mirror.ccs.tencentyun.com/library/ubuntu:22.04

# ---- 阶段 1：从官方镜像取出平台组件（静态二进制，可跨发行版搬运）
FROM ${PLATFORM_BASE_IMAGE} AS platform

# ---- 阶段 2：Ubuntu 22.04 作为真正的基础层（满足课题原文要求）
# 默认走腾讯云内网 Docker Hub 镜像源（等价于 docker.io/library/ubuntu:22.04）：
# 公网直连在部分构建机上会 i/o timeout，内网拉取也更快、不占公网带宽。
FROM ${UBUNTU_BASE_IMAGE}

# 加速：清华 TUNA 源（只替换 host，不改路径与签名校验，不影响完整性校验）
RUN sed -i 's|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g; s|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list

# 课题要求的运行时工具链：Python 3.11 + Git + Docker CLI
# Ubuntu 22.04 自带 Python 3.10，3.11 用 uv 拉独立运行时获取
# （与主流水线的题目镜像保持一致的做法，不依赖第三方 PPA）
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
