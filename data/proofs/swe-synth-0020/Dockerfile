# 题目镜像：swe-synth-0020（pallets/itsdangerous @ 672971d66a2ef9f85151e53283113f33d642dabd）
#
# 基础镜像：官方 ags-image/sandbox-code，或内部维护的共享 base 镜像
# （swe_synth/agent1/base_image/，双镜像方案的「环境层」——已内置 S6/envd +
#  git/docker CLI + Python 3.11 venv + pip 镜像源）。两者都能保证
# S6-Overlay(/init)、envd(49983)、run-code(49999) 齐全；裸 ubuntu:22.04
# （未搬运这两个组件）会导致 run_code / commands.run / files.* 全部失效。
FROM ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-base:ubuntu22.04-v3

# ⚠️ 不设置 USER / WORKDIR / ENV —— 快照启动要求 root + WORKDIR=/，且镜像 ENV 不生效。
#    需要传环境变量时走 CreateSandboxTool 的 Env 参数（并设 S6_KEEP_ENV=1）。

# 环境层自愈安装：如果 FROM 的是共享 base 镜像，下面这些依赖（apt 包 / uv /
# Python 3.11 venv / pip 镜像源）已经装好，这一层几乎是空操作，仅体现为几 MB
# 的内容层；如果 FROM 的是裸官方镜像（尚未构建/切换共享 base），则照常从零
# 安装一遍，行为与切换前完全一致——因此换不换共享 base 都不会导致构建失败，
# 只是有没有拿到体积/速度收益的区别。
RUN if [ ! -x /opt/venv311/bin/python ]; then \
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
        fi; \
        if [ -f /etc/apt/sources.list ]; then \
            sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g; s|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
        fi; \
        chmod o-w /usr/bin; \
        apt-get update; \
        apt-get install -y --no-install-recommends git docker.io curl ca-certificates; \
        rm -rf /var/lib/apt/lists/*; \
        command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh; \
        uv venv --python 3.11 --seed /opt/venv311; \
        /opt/venv311/bin/python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple; \
        /opt/venv311/bin/python -m pip install --no-cache-dir -q --upgrade pip setuptools wheel; \
    fi

# 仓库代码（构建期已固定到 base_commit 并应用 stub patch，保证可复现）
COPY repo/ /workspace/repo/

# 安装题目依赖（含测试依赖 —— 实测测试依赖常藏在 conftest.py 中）
# 注：构建上下文不含 .git（write_build_context 主动排除），若仓库用
# setuptools_scm/hatch-vcs 从 git tag 推导版本号，会因取不到 git 历史报
# LookupError 导致 build 失败。这里固定伪造一个版本号绕过，不影响功能
# （只影响 __version__ 字符串，不影响被测代码逻辑与判据）。
RUN set -eux; cd /workspace/repo; \
    export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+synth; \
    /opt/venv311/bin/python -m pip install --no-cache-dir -e .; \
    /opt/venv311/bin/python -m pip install --no-cache-dir pytest freezegun;

# 判据执行器：不管仓库自己的 extras/dependency-groups 是否声明了 pytest
# （常见做法是放进 [dependency-groups].dev，而 `pip install -e .` 装不到这些），
# 都强制装上，否则 verify.sh 里的 `python -m pytest` 会因 ModuleNotFoundError
# 而 n_collected=0，产生"空解、golden 解都判定失败"的假阴性（实测踩过）。
RUN /opt/venv311/bin/python -m pip install --no-cache-dir pytest pytest-cov

# 任务契约文件
COPY task/ /task/
RUN chmod +x /task/run_tests.sh /task/verify.sh

# 预热：把 pytest 收集缓存跑一遍，缩短沙箱内首次执行时间（失败不影响构建）
RUN cd /workspace/repo && /opt/venv311/bin/python -m pytest --collect-only -q >/dev/null 2>&1 || true

# ⚠️ 不覆盖 ENTRYPOINT/CMD，保持基础镜像的 /init + sleep infinity
