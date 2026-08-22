#!/usr/bin/env python3
"""生成交付包（只挑课题要求的产物，严格排除密钥与研发残留）

用法
----
    .venv/bin/python scripts/make_delivery.py

产出
----
    dist/swe-synth-delivery-<日期>/      交付目录
    dist/swe-synth-delivery-<日期>.zip   交付压缩包

设计原则
--------
1. **只增不改**：不动原项目任何文件，全部操作在 `dist/` 内完成
2. **密钥零容忍**：`.env` 等敏感文件从不拷贝，且打包前后各扫一遍确认
3. **证据只留 ACCEPTED**：`data/proofs/` 下 35 个目录里混着 16 个失败尝试的
   残留（研发过程记录，本地保留但不进交付包），只拷贝 `tasks.jsonl` 里
   真实存在的 19 道题，避免验收方困惑「为什么 19 道题有 35 个证据目录」
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 交付清单

# 根目录文件：课题点名要的 + 复现必需 + 过程记录
FILES = [
    "交付说明.md",            # 验收入口（从这里开始看）
    "SCALE-OUT.md",           # Scale Out 方案：如何从 19 道扩到 1 万/100 万道
    "review-feedback-report.md",  # 外部评审意见的逐条实测验证与整改方案
    "README.md",              # 验收标准明文要求
    "TASK-SPEC.md",           # 课题原文（对照基准）
    "requirements-check.md",  # 验收逐条核对（验收方省时间用）
    "PROGRESS.md",            # 研发过程与踩坑记录
    "requirements.txt",       # 依赖清单
    ".env.example",           # 凭证模板（只有键名，无真实值）
]

# 实验脚本（评审意见的验证依据，随交付一起提供以便复核）
EXPERIMENT_DIR = "experiments"

# 目录：双 Agent 源码与配置
DIRS = ["swe_synth", "scripts", "config", "experiments"]

# data 下只拷这两个（tasks_archive.jsonl 是研发残留，不进交付包）
DATA_FILES = ["tasks.jsonl", "report.json"]

# 拷贝时一律排除（缓存、虚拟环境、系统垃圾）
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".DS_Store",
    ".venv*", "venv", ".pytest_cache", ".cache", ".git",
)

# 绝不允许出现在交付包里的文件名（打包前后都会扫）
FORBIDDEN_NAMES = {".env"}

# 疑似密钥的内容特征（值本身不打印，只报告命中位置）
SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "疑似 TokenHub/OpenAI API Key"),
    (re.compile(r"\bark[_-][A-Za-z0-9]{8,}"), "疑似 AGS API Key"),
    (re.compile(r"\bAKID[A-Za-z0-9]{10,}"), "疑似腾讯云 SecretId"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "疑似 GitHub Token"),
]

# 内部信息特征（public 仓库发布前必须清零；由 sanitize 环节负责替换，这里做兜底门禁）
# 用正则动态匹配，不写他人资源名明文。
# 注意：只匹配「团队共享账号的既有规模」这类表述，不能误伤本项目的扩容规划
# （如「构建机集群 2~8 台 CVM」是我们的方案建议，不是内部信息）。
INTERNAL_PATTERNS = [
    (re.compile(r"\b10005\d{7}\b"), "子用户 UIN"),
    (re.compile(r"账号[^\n]{0,10}\d+ 台 CVM"), "团队账号资源规模"),
    (re.compile(r"\d+ 台 CVM\s*/\s*\d+ VPC"), "团队账号资源规模"),
    (re.compile(r"已有 \d+ 个 Key"), "账号内 Key 数量"),
]

# 扫描时跳过的文件（这些文件按设计就包含密钥的「占位符/键名」，不是真实值）
SCAN_SKIP = {".env.example"}


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 步骤

def accepted_task_ids(tasks_jsonl: Path) -> list[str]:
    """从数据集读出全部 ACCEPTED 题号（交付证据的唯一依据）。"""
    ids: list[str] = []
    with tasks_jsonl.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("state") != "ACCEPTED":
                raise SystemExit(
                    f"❌ {rec.get('task_id')} 的 state={rec.get('state')}，"
                    "非 ACCEPTED 记录不应出现在主交付文件中"
                )
            ids.append(rec["task_id"])
    return ids


def copy_payload(out: Path, task_ids: list[str]) -> dict[str, int]:
    """按清单拷贝，返回统计。"""
    stats = {"files": 0, "dirs": 0, "proofs": 0}

    for name in FILES:
        src = ROOT / name
        if not src.is_file():
            log(f"  ⚠️ 跳过（不存在）：{name}")
            continue
        shutil.copy2(src, out / name)
        stats["files"] += 1
        log(f"  ✅ {name}")

    for name in DIRS:
        src = ROOT / name
        if not src.is_dir():
            log(f"  ⚠️ 跳过（不存在）：{name}/")
            continue
        ignore = IGNORE
        if name == "scripts":
            # 交付/脱敏脚本本身不进交付包：
            #   · make_delivery.py  仅内部使用，验收方不需要
            #   · sanitize_delivery.py 的规则里必然含待脱敏的关键词
            #     （否则无法匹配），若随包发布反而成为泄露源
            ignore = shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.pyo", ".DS_Store",
                ".venv*", "venv", ".pytest_cache", ".cache", ".git",
                "make_delivery.py", "sanitize_delivery.py",
            )
        shutil.copytree(src, out / name, ignore=ignore)
        stats["dirs"] += 1
        log(f"  ✅ {name}/")

    data_out = out / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    for name in DATA_FILES:
        src = ROOT / "data" / name
        if not src.is_file():
            log(f"  ⚠️ 跳过（不存在）：data/{name}")
            continue
        shutil.copy2(src, data_out / name)
        stats["files"] += 1
        log(f"  ✅ data/{name}")

    # 通过证明：只拷 ACCEPTED 的那些
    proofs_out = data_out / "proofs"
    proofs_out.mkdir(exist_ok=True)
    for tid in task_ids:
        src = ROOT / "data" / "proofs" / tid
        if not src.is_dir():
            raise SystemExit(f"❌ {tid} 缺少通过证明目录：{src}")
        shutil.copytree(src, proofs_out / tid, ignore=IGNORE)
        stats["proofs"] += 1
    log(f"  ✅ data/proofs/（{stats['proofs']} 个 ACCEPTED 题目的证据）")

    return stats


def scan_secrets(target: Path) -> list[str]:
    """扫描交付目录，返回问题列表（空 = 干净）。"""
    problems: list[str] = []

    for p in target.rglob("*"):
        if p.is_dir():
            continue
        if p.name in FORBIDDEN_NAMES:
            problems.append(f"存在禁止交付的文件：{p.relative_to(target)}")
            continue
        if p.name in SCAN_SKIP:
            continue
        # 只扫文本类文件，二进制跳过
        if p.suffix.lower() in {".png", ".jpg", ".gz", ".zip", ".whl", ".so"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, desc in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{desc}：{p.relative_to(target)}")
        for pattern, desc in INTERNAL_PATTERNS:
            m = pattern.search(text)
            if m:
                problems.append(
                    f"内部信息未脱敏（{desc}）：{p.relative_to(target)} → 「{m.group()}」"
                )

    return problems


def verify(target: Path, expect_ids: list[str]) -> list[str]:
    """交付完整性自检，返回问题列表（空 = 合格）。"""
    problems: list[str] = []

    tasks = target / "data" / "tasks.jsonl"
    if not tasks.is_file():
        return ["缺少 data/tasks.jsonl"]

    got_ids = accepted_task_ids(tasks)
    if len(got_ids) < 10:
        problems.append(f"题目数 {len(got_ids)} < 课题要求的 10 道")
    if sorted(got_ids) != sorted(expect_ids):
        problems.append("tasks.jsonl 的题号与拷贝的证据目录不一致")

    proofs_dir = target / "data" / "proofs"
    got_proofs = sorted(d.name for d in proofs_dir.iterdir() if d.is_dir())
    if got_proofs != sorted(expect_ids):
        extra = set(got_proofs) - set(expect_ids)
        missing = set(expect_ids) - set(got_proofs)
        if extra:
            problems.append(f"证据目录有多余项：{sorted(extra)}")
        if missing:
            problems.append(f"证据目录有缺失项：{sorted(missing)}")

    # 每道题的关键证据文件必须齐备（课题要求「通过证明」）
    required = ["problem_statement.md", "task.json", "verification.json",
                "overlap_check.json", "Dockerfile"]
    for tid in expect_ids:
        for fn in required:
            if not (proofs_dir / tid / fn).is_file():
                problems.append(f"{tid} 缺少证据文件 {fn}")

    # README 必须含课题要求的三节
    readme = (target / "README.md").read_text(encoding="utf-8")
    for section in ["启动方式", "参数配置", "结果文件格式"]:
        if section not in readme:
            problems.append(f"README.md 缺少「{section}」相关章节")

    return problems


def main() -> int:
    stamp = date.today().strftime("%Y%m%d")
    dist = ROOT / "dist"
    name = f"swe-synth-delivery-{stamp}"
    out = dist / name

    log("=" * 72)
    log(f"生成交付包：{name}")
    log("=" * 72)

    tasks_src = ROOT / "data" / "tasks.jsonl"
    if not tasks_src.is_file():
        log("❌ 找不到 data/tasks.jsonl，无法交付")
        return 1

    task_ids = accepted_task_ids(tasks_src)
    log(f"\n[1/6] 读取数据集：{len(task_ids)} 道 ACCEPTED 题目")

    # 幂等：重跑时清掉上次的产物
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    log("\n[2/6] 拷贝交付物")
    stats = copy_payload(out, task_ids)

    log("\n[3/6] 脱敏（移除他人云资源名与团队账号规模等内部信息）")
    sys.path.insert(0, str(ROOT / "scripts"))
    from sanitize_delivery import sanitize_text  # noqa: E402

    sanitized = 0
    for p in sorted(out.rglob("*.md")):
        original = p.read_text(encoding="utf-8")
        new, hits = sanitize_text(original)
        if new != original:
            p.write_text(new, encoding="utf-8")
            sanitized += 1
            log(f"  ✅ {p.relative_to(out)}（{sum(hits.values())} 处）")
    if not sanitized:
        log("  ✅ 无需脱敏")

    log("\n[4/6] 密钥与内部信息扫描")
    leaks = scan_secrets(out)
    if leaks:
        log("  ❌ 发现敏感内容，已中止并清理：")
        for x in leaks:
            log(f"     · {x}")
        shutil.rmtree(out)
        return 1
    log("  ✅ 未发现密钥或禁止交付的文件")

    log("\n[5/6] 完整性自检")
    problems = verify(out, task_ids)
    if problems:
        log("  ❌ 自检未通过：")
        for x in problems:
            log(f"     · {x}")
        return 1
    log(f"  ✅ {len(task_ids)} 道题 + {stats['proofs']} 份通过证明齐备，README 三节完整")

    log("\n[6/6] 打包")
    zip_base = dist / name
    archive = shutil.make_archive(str(zip_base), "zip", root_dir=dist, base_dir=name)
    archive_path = Path(archive)

    # 打包后二次确认：压缩包内不含 .env
    import zipfile
    with zipfile.ZipFile(archive_path) as zf:
        bad = [n for n in zf.namelist() if Path(n).name in FORBIDDEN_NAMES]
    if bad:
        log(f"  ❌ 压缩包内发现禁止文件：{bad}")
        archive_path.unlink()
        return 1

    size_mb = archive_path.stat().st_size / 1024 / 1024
    total_files = sum(1 for _ in out.rglob("*") if _.is_file())

    log("=" * 72)
    log("✅ 交付包生成完成")
    log(f"   目录：{out}")
    log(f"   压缩包：{archive_path}（{size_mb:.2f} MB）")
    log(f"   文件总数：{total_files}")
    log(f"   题目数：{len(task_ids)} 道（全部 ACCEPTED）")
    log("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
