# 题目内容镜像（Content-only Image）—— 双镜像方案的「题目」那一半
#
# 导师反馈 4 提出的架构：
#     base image（env + 工具链）→ 放 volume image mount 位置
#     题目 image                → 放标准 tools 的 image 位置
#     每次换题 → 通过 e2b custom config / MountOptions 覆盖
#
# 本镜像只装「题目内容」，不含任何运行时环境：
#     /workspace/repo/   已 stub 化的仓库代码
#     /task/             题干 + metadata + verify.sh
#
# 与旧方案对比
# -----------
#   旧：每道题一个 7.5GB 镜像（继承 6.86GB 官方基础镜像 + 装一遍环境）
#   新：环境层复用（1 个 base 镜像），每道题只有几 MB 的内容层
#
# 用 scratch 是因为本镜像**不需要被启动**，只作为 image volume 被挂载，
# 平台只读取其文件系统内容，不执行其中任何程序。
FROM scratch

COPY repo/ /workspace/repo/
COPY task/ /task/
