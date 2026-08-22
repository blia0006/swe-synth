#!/usr/bin/env python3
"""临时脚本：检查当前流水线状态、agent2重试情况、AGS工具/配额状态。用完即删。"""
from __future__ import annotations
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
os.environ.setdefault("E2B_VALIDATE_API_KEY", "false")
os.environ.setdefault("E2B_DOMAIN", "ap-guangzhou.tencentags.com")
from scripts.sandbox_status import _connect
from scripts import run_in_sandbox as ris

INSTANCE_ID = "pfz4lxt46zp2fvtmicovwjaeppx2wsosxsq2w2lz"
sbx = _connect(INSTANCE_ID)
print("=== 状态文件 ===")
r = sbx.commands.run(f"cat {ris.PIPELINE_STATUS} 2>/dev/null || echo NONE", timeout=10, user="root")
print(r.stdout)

print("=== 日志尾部 ===")
r = sbx.commands.run(f"tail -60 {ris.PIPELINE_LOG} 2>/dev/null", timeout=10, user="root")
print(r.stdout)

print("=== 进程是否还在跑 ===")
r = sbx.commands.run("ps aux | grep run_pipeline | grep -v grep", timeout=10, user="root")
print(r.stdout)
