#!/usr/bin/env python3
"""查看 / 跟踪 / 收尾一个由 run_in_sandbox.py 启动的后台流水线。

因为 run_in_sandbox.py 是把流水线用 setsid+nohup 甩到沙箱后台跑完就返回，
本机随时可能断网、关终端、换机器——这个脚本只是重新 `Sandbox.connect()`
到同一个 instance_id 上，跟本机之前是否是同一个进程/同一次网络连接完全
无关，可以在任意时刻、任意机器上重新连接查看。

用法
----
    python scripts/sandbox_status.py --instance <id>              # 看一次状态
    python scripts/sandbox_status.py --instance <id> --follow      # 持续跟踪日志（Ctrl-C 退出，不影响沙箱内任务）
    python scripts/sandbox_status.py --instance <id> --download    # 完成后下载 data/ 产出到本地
    python scripts/sandbox_status.py --instance <id> --stop        # 回收沙箱实例（确认任务已完成/需要中止再用）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")
os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")

from swe_synth.clients.ags import AGSClient  # noqa: E402

REMOTE_ROOT = "/root/swe_synth"
PIPELINE_LOG = f"{REMOTE_ROOT}/pipeline.log"
PIPELINE_STATUS = f"{REMOTE_ROOT}/pipeline.status"


def _connect(instance_id: str):
    from e2b_code_interpreter import Sandbox
    return Sandbox.connect(instance_id)


def show_status(sbx) -> str:
    """返回 pipeline.status 的内容（RUNNING stage=xxx / FAILED.../ ALL_DONE...）。"""
    try:
        r = sbx.commands.run(f"cat {PIPELINE_STATUS} 2>/dev/null || echo 'NO_STATUS_FILE'",
                              timeout=15, user="root")
        status = r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"UNREACHABLE ({e})"
    print(f"[状态] {status}")
    return status


def show_log_tail(sbx, lines: int = 60) -> None:
    try:
        r = sbx.commands.run(f"tail -n {lines} {PIPELINE_LOG} 2>/dev/null || echo '(暂无日志)'",
                              timeout=15, user="root")
        print(r.stdout)
    except Exception as e:  # noqa: BLE001
        print(f"[读取日志失败] {e}")


def show_progress(sbx) -> None:
    """出题/打包/验证各阶段的产出数量，粗粒度进度感。"""
    try:
        r = sbx.commands.run(
            "echo '--- proofs ---'; ls /root/swe_synth/data/proofs/ 2>/dev/null | wc -l; "
            "echo '--- images(pack) ---'; ls /root/swe_synth/data/images/ 2>/dev/null | wc -l; "
            "echo '--- reports ---'; ls /root/swe_synth/data/reports/ 2>/dev/null || true",
            timeout=15, user="root",
        )
        print(r.stdout)
    except Exception as e:  # noqa: BLE001
        print(f"[读取进度失败] {e}")


def download_results(sbx, local_root: Path) -> None:
    r = sbx.commands.run(f"find {REMOTE_ROOT}/data -type f 2>/dev/null || true",
                          timeout=30, user="root")
    remote_files = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not remote_files:
        print("（沙箱内 data/ 目录为空，没有可下载的产出）")
        return
    print(f"下载产出（{len(remote_files)} 个文件）…")
    for rf in remote_files:
        rel = rf[len(REMOTE_ROOT) + 1:]
        local_path = local_root / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = sbx.files.read(rf, user="root")
            mode = "w" if isinstance(content, str) else "wb"
            with open(local_path, mode) as f:
                f.write(content)
            print(f"  ✅ {rel}")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {rel} 下载失败：{e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", required=True, help="沙箱实例 ID（run_in_sandbox.py 启动时打印的那个）")
    ap.add_argument("--follow", action="store_true", help="持续每 15s 刷新一次状态与日志尾部，直到 ALL_DONE/FAILED")
    ap.add_argument("--download", action="store_true", help="下载 data/ 产出到本地项目根")
    ap.add_argument("--stop", action="store_true", help="回收沙箱实例（会终止里面仍在跑的任何进程，谨慎使用）")
    ap.add_argument("--log-lines", type=int, default=60)
    args = ap.parse_args()

    ags = AGSClient()
    sbx = _connect(args.instance)

    if args.follow:
        print(f"持续跟踪沙箱实例 {args.instance}（Ctrl-C 退出跟踪，不影响沙箱内任务继续跑）…\n")
        try:
            while True:
                print("=" * 72, time.strftime("%Y-%m-%d %H:%M:%S"), "=" * 20)
                status = show_status(sbx)
                show_progress(sbx)
                show_log_tail(sbx, args.log_lines)
                if status.startswith("ALL_DONE") or status.startswith("FAILED"):
                    print("\n流水线已结束。")
                    break
                time.sleep(15)
        except KeyboardInterrupt:
            print("\n（已停止跟踪，沙箱内任务不受影响，继续在后台运行）")
            return 0
    else:
        show_status(sbx)
        show_progress(sbx)
        show_log_tail(sbx, args.log_lines)

    if args.download:
        download_results(sbx, ROOT)

    if args.stop:
        ags.stop_instance(args.instance)
        print(f"已回收沙箱实例：{args.instance}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
