"""双镜像方案 v2 实测：base 走挂载卷（StorageMounts，工具级固定）+
题目走标准镜像位（CustomConfiguration.Image，实例级覆盖），完全按用户描述的
架构验证，同时全程使用 e2b SDK 2.x 连接沙箱。

背景
----
第一版验证（verify_dual_image.py）把方向搞反了：base 放进了
CustomConfiguration.Image（工具主镜像位），题目内容放进了 StorageMounts
（挂载卷位）——测下来发现挂载卷指向的镜像在 StartSandboxInstance 时无法切换
（MountOption model 没有 Reference 字段），只能覆盖 MountPath/SubPath/ReadOnly，
这条路走不通（题目每次都要变，放进不可切换的挂载卷里换不了题）。

第二版（verify_customconfig_switch.py）绕开了挂载卷机制，直接让题目镜像
`FROM` 共享 base（Docker 分层去重），整个组合镜像整体塞进
CustomConfiguration.Image 切换——这是当前生产代码在用的方案。

本脚本测的是用户明确要求的第三种、真正的「双镜像」组合：
    · base（env+工具，不随题目变化）  → StorageMounts，工具创建时固定一次
    · 题目内容（每次换题都变）        → CustomConfiguration.Image，实例级覆盖

验证点
------
1. 工具创建时挂载 base 镜像成功（AGS 接受 StorageMounts 参数，不报错）
2. 实例 #1：CustomConfiguration.Image = 题目 A 镜像
   - /mnt/base-env 下能看到 base 镜像内容（证明挂载卷生效）
   - /task 下能看到题目 A 的内容（证明主镜像覆盖生效）
3. 实例 #2（同一个工具，不重建）：CustomConfiguration.Image = 题目 B 镜像
   - /task 下内容变成题目 B（证明换题只需实例级覆盖，工具不用重建）
   - /mnt/base-env 内容不变（证明挂载卷是「固定不变」的那一半，天然适合放 base）
4. 全程用 e2b SDK 2.x（`Sandbox.connect`）连接，验证 2.x 在 AGS 后端可用
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# ---- 强制走 e2b 2.x：跳过 e2b_ 前缀格式校验（AGS 的 Key 是 ark_ 前缀） ----
os.environ["E2B_VALIDATE_API_KEY"] = "false"

from swe_synth.clients.ags import AGSClient  # noqa: E402

BASE_IMAGE = "ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-base:ubuntu22.04-v1"
TASK_A_IMAGE = "ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-0034:v1"
TASK_B_IMAGE = "ccr.ccs.tencentyun.com/tcb-100008634787-zbaf/swe-synth-0007:v1"
TOOL_NAME = "swe-synth-dualimage-v2-test"
MOUNT_PATH = "/mnt/base-env"


def connect_e2b_v2(instance_id: str):
    """强制用 e2b 2.x 的 `Sandbox.connect`（不走 1.x 兼容分支）。"""
    import e2b
    from e2b_code_interpreter import Sandbox

    print(f"    [e2b] package version = {getattr(e2b, '__version__', '?')}")
    assert hasattr(Sandbox, "connect"), "当前装的不是 e2b 2.x（缺 Sandbox.connect）"
    return Sandbox.connect(instance_id)


def check_path(sbx, path: str, label: str) -> str:
    try:
        out = sbx.commands.run(f"ls -la {path} 2>&1 | head -20", user="root", timeout=30)
        text = out.stdout if hasattr(out, "stdout") else str(out)
        print(f"    [{label}] {path} ->\n{text}")
        return text
    except Exception as e:  # noqa: BLE001
        print(f"    [{label}] {path} 访问失败: {e}")
        return ""


def read_task_id(sbx) -> str:
    try:
        out = sbx.commands.run(
            "python3 -c \"import json;d=json.load(open('/task/metadata.json'));"
            "print(d.get('task_id') or d.get('repo') or d.get('base_commit','?'))\" 2>&1",
            user="root", timeout=30,
        )
        text = (out.stdout if hasattr(out, "stdout") else str(out)).strip()
        return text
    except Exception as e:  # noqa: BLE001
        return f"<读取失败: {e}>"


def main() -> None:
    role_arn = os.environ.get("AGS_ROLE_ARN")
    ags = AGSClient()

    print("=" * 70)
    print("步骤 1/5：创建测试工具（StorageMounts 挂载 base，主镜像先给 A）")
    print("=" * 70)
    existing = ags.find_tool(TOOL_NAME)
    if existing:
        print(f"  发现同名旧工具 {existing['tool_id']}，先删除以保证是干净测试")
        ags.delete_tool(existing["tool_id"])
        time.sleep(5)

    tool_id = ags.create_tool(
        TOOL_NAME,
        TASK_A_IMAGE,
        role_arn=role_arn,
        description="双镜像 v2 实测：base 走 StorageMounts 挂载卷，题目走 CustomConfiguration.Image",
        storage_mounts=[{
            "name": "base-env",
            "image": BASE_IMAGE,
            "mount_path": MOUNT_PATH,
            "read_only": True,
        }],
    )
    print(f"  工具已创建：ToolId={tool_id}")

    print("\n" + "=" * 70)
    print("步骤 2/5：等待工具 ACTIVE")
    print("=" * 70)
    ags.wait_tool_active(TOOL_NAME, timeout=180)
    print("  工具已 ACTIVE")

    instance_a = instance_b = None
    try:
        print("\n" + "=" * 70)
        print("步骤 3/5：实例 #1 —— CustomConfiguration.Image 覆盖为题目 A")
        print("=" * 70)
        instance_a, effective_a = ags.start_instance(
            tool_id, image_override=TASK_A_IMAGE, timeout="10m",
        )
        print(f"  InstanceId={instance_a}, effective_image={effective_a}")
        sbx_a = connect_e2b_v2(instance_a)

        base_mount_out_a = check_path(sbx_a, MOUNT_PATH, "实例A/挂载卷")
        check_path(sbx_a, f"{MOUNT_PATH}/etc/os-release", "实例A/挂载卷发行版")
        os_release_a = sbx_a.commands.run(f"cat {MOUNT_PATH}/etc/os-release 2>&1", user="root", timeout=30)
        print(f"    [实例A/挂载卷 os-release] {os_release_a.stdout if hasattr(os_release_a,'stdout') else os_release_a}")
        task_id_a = read_task_id(sbx_a)
        print(f"  实例A /task/task.json 里的 task_id = {task_id_a}")

        print("\n" + "=" * 70)
        print("步骤 4/5：实例 #2（同一个工具，不重建）—— CustomConfiguration.Image 覆盖为题目 B")
        print("=" * 70)
        instance_b, effective_b = ags.start_instance(
            tool_id, image_override=TASK_B_IMAGE, timeout="10m",
        )
        print(f"  InstanceId={instance_b}, effective_image={effective_b}")
        sbx_b = connect_e2b_v2(instance_b)

        base_mount_out_b = check_path(sbx_b, MOUNT_PATH, "实例B/挂载卷")
        os_release_b = sbx_b.commands.run(f"cat {MOUNT_PATH}/etc/os-release 2>&1", user="root", timeout=30)
        print(f"    [实例B/挂载卷 os-release] {os_release_b.stdout if hasattr(os_release_b,'stdout') else os_release_b}")
        task_id_b = read_task_id(sbx_b)
        print(f"  实例B /task/task.json 里的 task_id = {task_id_b}")

        print("\n" + "=" * 70)
        print("步骤 5/5：结论判定")
        print("=" * 70)
        mount_ok = bool(base_mount_out_a.strip()) and bool(base_mount_out_b.strip())
        task_switched = bool(task_id_a) and bool(task_id_b) and task_id_a != task_id_b and "失败" not in task_id_a and "失败" not in task_id_b
        mount_stable = base_mount_out_a.strip() != "" and base_mount_out_a == base_mount_out_b

        print(f"  挂载卷两次都能访问          : {'PASS' if mount_ok else 'FAIL'}")
        print(f"  题目内容随实例正确切换      : {'PASS' if task_switched else 'FAIL'} (A={task_id_a}, B={task_id_b})")
        print(f"  挂载卷内容在换题后保持不变  : {'PASS' if mount_stable else 'FAIL'}")

        if mount_ok and task_switched and mount_stable:
            print("\n  >>> 双镜像 v2（base=StorageMounts固定挂载 + 题目=CustomConfiguration.Image实例覆盖）验证通过 <<<")
        else:
            print("\n  >>> 未完全通过，见上面各项 PASS/FAIL <<<")

    finally:
        print("\n清理：回收实例 + 删除测试工具")
        for inst in (instance_a, instance_b):
            if inst:
                try:
                    ags.stop_instance(inst)
                    print(f"  已回收实例 {inst}")
                except Exception as e:  # noqa: BLE001
                    print(f"  回收实例 {inst} 失败（可能已自动过期）：{e}")
        try:
            ags.delete_tool(tool_id)
            print(f"  已删除测试工具 {tool_id}")
        except Exception as e:  # noqa: BLE001
            print(f"  删除测试工具失败：{e}")


if __name__ == "__main__":
    main()
