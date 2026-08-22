# 题目镜像：swe-synth-0018（Delgan/loguru @ 2a17be730e111cf874d0bf967c4c5da15b7e4670）
#
# ⚠️ 基础镜像不可更换：官方要求自定义沙箱必须继承 ags-image/sandbox-code，
#    其中包含 S6-Overlay(/init)、envd(49983)、run-code(49999)。
#    使用裸 ubuntu:22.04 会导致 run_code / commands.run / files.* 全部失效。
FROM ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest

# ⚠️ 不设置 USER / WORKDIR / ENV —— 快照启动要求 root + WORKDIR=/，且镜像 ENV 不生效。
#    需要传环境变量时走 CreateSandboxTool 的 Env 参数（并设 S6_KEEP_ENV=1）。

# 加速：官方 deb.debian.org 在境内下载很慢，替换为清华 TUNA 镜像
# （只替换 host，路径/组件不变，签名校验 Signed-By 保持不变，不影响完整性校验）
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi

# 修复基础镜像遗留问题：/usr/bin 权限异常为全局可写(777)，会导致
# docker.io 等包的 postinst 脚本（Perl -T taint 模式）因 PATH 含"不安全目录"而报错退出。
# 仅去掉 world-write 位，不影响正常读取执行，属于安全加固。
RUN chmod o-w /usr/bin

# 课题要求的运行时工具链：Git + Docker CLI
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git docker.io curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Python 3.11 通过 uv 拉取独立运行时安装（不依赖系统 apt 源版本，
# 避免基础镜像底层系统升级后 apt 源不再提供 python3.11 系列包）
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# 题目代码运行在独立的 Python 3.11 虚拟环境中（满足课题「Python 3.11」要求）
# --seed：venv 内自带 pip/setuptools/wheel，兼容后续 `python -m pip install` 用法
RUN /opt/venv311/bin/python -V || uv venv --python 3.11 --seed /opt/venv311

# 加速：pip 默认走官方 PyPI 在境内很慢，配置清华 TUNA 镜像（写入 venv 内 pip.conf，
# 全局生效，后续 upgrade / install_block 都会自动走镜像，无需逐条加 -i 参数）
RUN /opt/venv311/bin/python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

RUN /opt/venv311/bin/python -m pip install --no-cache-dir -q --upgrade pip setuptools wheel

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
    /opt/venv311/bin/python -m pip install --no-cache-dir pytest freezegun colorama;

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
