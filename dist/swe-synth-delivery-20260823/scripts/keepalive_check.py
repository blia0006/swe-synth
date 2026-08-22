#!/usr/bin/env python3
"""巡检 + 续期：给自动化任务用，单次调用做完「查进度 + 防超时被杀 + 完成后下载」。

跟 sandbox_status.py 的区别：这个脚本是非交互的、幂等的单次巡检，专门给
automation_update 建的定时任务调用——每次跑：
  1. 连接沙箱实例，读 pipeline.status / pipeline.log 尾部
  2. 只要流水线还没结束（不是 ALL_DONE/FAILED），就把实例超时续到 24h，
     防止腾讯云 AGS 因为实例本身到期把它强制回收（跟里面的后台进程是否
     存活无关，到期就杀，必须显式续期）
  3. 如果已经 ALL_DONE，下载 data/ 产出到本地并打印摘要（不自动回收实例，
     留给用户确认后手动用 sandbox_status.py --stop 回收）
  4. 如果 FAILED，打印日志尾部方便排查，同样不自动回收实例
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")
os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")

from swe_synth.clients.ags import AGSClient  # noqa: E402
from scripts.sandbox_status import (  # noqa: E402
    _connect, show_status, show_progress, show_log_tail, download_results,
)

INSTANCE_ID = os.environ.get("SWE_SYNTH_KEEPALIVE_INSTANCE",
                              "pfz4lxt46zp2fvtmicovwjaeppx2wsosxsq2w2lz")


def main() -> int:
    instance_id = INSTANCE_ID
    if len(sys.argv) > 1:
        instance_id = sys.argv[1]

    ags = AGSClient()
    print(f"巡检沙箱实例：{instance_id}")
    try:
        sbx = _connect(instance_id)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 连接失败（实例可能已被回收或未启动）：{e}")
        return 1

    status = show_status(sbx)
    show_progress(sbx)
    show_log_tail(sbx, 40)

    if status.startswith("ALL_DONE"):
        print("\n流水线已全部完成，下载产出…")
        download_results(sbx, ROOT)
        print("✅ 产出已下载到本地 data/ 目录，可用 --stop 手动回收实例。")
        return 0

    if status.startswith("FAILED"):
        print("\n⚠️ 流水线执行失败，请检查上面日志尾部定位问题。实例暂不自动回收，方便排查。")
        return 1

    # 仍在运行中：续期防止实例超时被杀
    try:
        ags.renew_instance(instance_id, timeout="24h")
        print("\n✅ 流水线仍在运行，已将实例超时续期至 24h。")
    except Exception as e:  # noqa: BLE001
        print(f"\n⚠️ 续期失败（请留意，如果连续失败可能导致实例过期被回收）：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
