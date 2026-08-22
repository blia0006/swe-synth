#!/usr/bin/env python3
"""交付包脱敏（public 仓库发布前必跑）

背景
----
交付文档里记录了大量真实的决策依据，其中含三类**不宜公开**的内部信息：

    1. 同事/他人的云资源名（企业版实例名、命名空间、沙箱工具名）
    2. 团队共享账号的资源规模（CVM/VPC/集群数量、账号内 Key 数量）
    3. 子用户 UIN（个人版 docker login 用户名）

处理原则
--------
· **只泛化标识符，不删技术论证** —— 决策依据的完整性是交付质量的一部分，
  「为什么不用企业版」这类论证必须保留，只把「zone-cbr 属于 openclaw 项目」
  这类他人资源标识替换成「某项目」
· **镜像地址中的主账号 UIN 保留** —— `ccr.ccs.tencentyun.com/tcb-<UIN>-zbaf/`
  是真实可拉取地址，改了就用不了。UIN 仅为账号标识符，
  单独泄露无法用于认证（真正敏感的密钥已确认零泄露）
· **幂等** —— 可反复执行，已脱敏的内容不会被二次替换

用法
----
    .venv/bin/python scripts/sanitize_delivery.py <交付目录>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ------------------------------------------------------------------ 替换规则
#
# 每条为 (正则, 替换文本, 说明)。顺序敏感：先处理长模式，避免被短模式截断。

RULES: list[tuple[str, str, str]] = [
    # ---- 1. 他人的企业版 TCR 实例名（连同其归属项目）
    (r"`zone-cbr`\s*的?\s*`?DeletionProtection=False`?",
     "该实例的 `DeletionProtection=False`", "企业版实例名"),
    (r"他人\(openclaw\)资源", "他人项目的资源", "实例归属"),
    (r"（他人\(openclaw\)）", "（属他人项目）", "实例归属"),
    (r"属他人 openclaw 项目", "属他人项目", "实例归属"),
    (r"`zone-cbr`", "`<广州企业版实例>`", "企业版实例名"),
    (r"zone-cbr", "<广州企业版实例>", "企业版实例名"),
    (r"`euson-tcr`", "`<北京企业版实例>`", "企业版实例名"),
    (r"euson-tcr", "<北京企业版实例>", "企业版实例名"),
    (r"`dcc-test`", "`<上海企业版实例A>`", "企业版实例名"),
    (r"dcc-test", "<上海企业版实例A>", "企业版实例名"),
    (r"`isaac-test`\s*/\s*`alan-registry`\s*/\s*`carltest`",
     "`<上海企业版实例B/C/D>`", "企业版实例名"),
    (r"`cedricbwang-sg-test`", "`<新加坡企业版实例>`", "企业版实例名"),
    (r"cedricbwang-sg-test", "<新加坡企业版实例>", "企业版实例名"),
    (r"`isaac-test`", "`<上海企业版实例B>`", "企业版实例名"),
    (r"`alan-registry`", "`<上海企业版实例C>`", "企业版实例名"),
    (r"`carltest`", "`<上海企业版实例D>`", "企业版实例名"),

    # ---- 2. 他人的 CCR 命名空间与沙箱工具
    (r"同事的沙箱工具 `custom-2qkimrymvt4` 正在用 "
     r"`ccr\.ccs\.tencentyun\.com/workpod/jenkins` \+ `personal`",
     "账号内已有沙箱工具在用 `ImageRegistryType=personal` 的 CCR 个人版镜像",
     "他人沙箱工具+命名空间"),
    (r"`custom-2qkimrymvt4`", "`<他人的沙箱工具>`", "他人沙箱工具"),
    (r"ccr\.ccs\.tencentyun\.com/workpod/jenkins",
     "ccr.ccs.tencentyun.com/<他人命名空间>/<镜像>", "他人命名空间"),
    (r"备选空命名空间：`lilyns`（仓库数=0）。",
     "（账号内另有其它空命名空间可作备选）", "他人命名空间"),
    (r"`lilyns`", "`<备选命名空间>`", "他人命名空间"),
    (r"备选 `<备选命名空间>`", "备选另一个空命名空间", "他人命名空间"),

    # ---- 3. 他人的 SWE-bench 沙箱工具（作为「内部先例」被引用）
    (r"`swe_marshmallow_1343`", "`<他人的 SWE 沙箱工具1>`", "他人沙箱工具"),
    (r"`swe_sandbox_test`", "`<他人的 SWE 沙箱工具2>`", "他人沙箱工具"),

    # ---- 4. 团队共享账号的资源规模
    (r"（109 台 CVM / 324 VPC / 17 个容器集群）",
     "（内有大量在用资源）", "账号规模"),
    (r"账号内\*\*已有 42 个 Key\*\*（都是同事的，`Platform=maas`）",
     "账号内**已有其他成员创建的 Key**（`Platform=maas`）", "账号规模"),
    (r"7 个实例全属他人", "现有实例均属他人", "账号规模"),
    (r"现存 1 个沙箱实例为他人的 `code-interpreter-v1`",
     "现存沙箱实例属他人", "他人资源"),

    # ---- 5. 子用户 UIN（个人版 docker login 用户名）
    #      注意：只替换「作为用户名出现」的场景，镜像地址里的主账号 UIN 保留
    #      UIN 用正则动态匹配（不写明文，避免本脚本自身成为泄露源）
    (r"TCR_USERNAME=1000\d{8}", "TCR_USERNAME=<子用户 Uin>", "子用户 UIN"),
    (r"子用户 `Uin=1000\d{8}`", "子用户（Uin 见控制台）", "子用户 UIN"),
    (r"Uin=1000\d{8}", "Uin=<子用户 Uin>", "子用户 UIN"),
    (r"(?<!tcb-)\b10005\d{7}\b", "<子用户 Uin>", "子用户 UIN"),

    # ---- 6. CAM 角色名（含他人前缀的候选角色）
    (r"其他候选：`[a-z]+-tcr-ags` / `zone-sandbox-ccr`。",
     "（账号内另有其它可用角色）", "他人 CAM 角色"),
    (r"`[a-z]+-tcr-ags`", "`<他人的CAM角色1>`", "他人 CAM 角色"),
    (r"`zone-sandbox-ccr`", "`<他人的CAM角色2>`", "他人 CAM 角色"),
]

# 只处理文本类文件；证据文件（task.json 等）里的镜像地址必须原样保留
TARGET_SUFFIXES = {".md"}


def sanitize_text(text: str) -> tuple[str, dict[str, int]]:
    """按规则脱敏，返回 (新文本, {说明: 命中次数})。"""
    hits: dict[str, int] = {}
    for pattern, repl, desc in RULES:
        text, n = re.subn(pattern, repl, text)
        if n:
            hits[desc] = hits.get(desc, 0) + n
    return text, hits


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    target = Path(sys.argv[1]).resolve()
    if not target.is_dir():
        print(f"❌ 目录不存在：{target}")
        return 1

    print("=" * 72)
    print(f"脱敏处理：{target}")
    print("=" * 72)

    total: dict[str, int] = {}
    changed_files = 0

    for p in sorted(target.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TARGET_SUFFIXES:
            continue
        original = p.read_text(encoding="utf-8")
        new, hits = sanitize_text(original)
        if new != original:
            p.write_text(new, encoding="utf-8")
            changed_files += 1
            rel = p.relative_to(target)
            print(f"\n  ✅ {rel}")
            for desc, n in sorted(hits.items()):
                print(f"       · {desc} × {n}")
            for desc, n in hits.items():
                total[desc] = total.get(desc, 0) + n

    print("\n" + "=" * 72)
    if changed_files:
        print(f"处理了 {changed_files} 个文件，共 {sum(total.values())} 处替换：")
        for desc, n in sorted(total.items(), key=lambda x: -x[1]):
            print(f"  · {desc}: {n} 处")
    else:
        print("无需脱敏（已是干净状态）")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
