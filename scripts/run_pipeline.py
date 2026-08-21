#!/usr/bin/env python3
"""SWE-Synth 流水线入口（课题验收要求的「流水线启动方式」）

用法示例
--------
    # 列出候选仓库池
    python scripts/run_pipeline.py list-repos

    # 只跑 Agent1（出题 + 生成构建上下文），三种题型轮转产 3 道题
    python scripts/run_pipeline.py agent1 --repo psf/cachecontrol --n 3

    # 指定题型：A=功能实现 B=模块添加 C=重构（可组合，如 --type BC）
    python scripts/run_pipeline.py agent1 --repo psf/cachecontrol --type C --n 1

    # 跑多个仓库、每个最多 3 道题
    python scripts/run_pipeline.py agent1 --n 12 --per-repo 3

    # 跳过 solve-back（更快，但题干准确性无保障，仅用于调试）
    python scripts/run_pipeline.py agent1 --repo psf/cachecontrol --n 1 --no-solve-back

    # 查看已产出的数据集并逐行校验
    python scripts/run_pipeline.py validate

参数说明见 README.md。所有可调参数在 config/settings.yaml，凭证在 .env。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from swe_synth.clients.tokenhub import TokenHubClient  # noqa: E402
from swe_synth.config.loader import load_repos, load_settings  # noqa: E402
from swe_synth.schemas.task import (SweTask, TaskState, TaskType,  # noqa: E402
                                    read_jsonl, write_jsonl)

# CLI 的题型简写 → 枚举（课题原文列举的三类）
TYPE_ALIASES = {
    "A": TaskType.FEATURE_IMPLEMENTATION,
    "B": TaskType.MODULE_ADDITION,
    "C": TaskType.REFACTORING,
}


def _log(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ list-repos

def cmd_list_repos(args: argparse.Namespace) -> int:
    repos = load_repos(only_verified=args.verified_only)
    print(f"候选仓库池（{len(repos)} 个）  ★=Star 数，课题要求 >100\n")
    print(f"{'状态':<6} {'仓库':<42} {'语言':<7} {'★':>7}  说明")
    print("-" * 100)
    for r in repos:
        mark = "✅验证" if r.verified else "  待验"
        print(f"{mark:<6} {r.name:<42} {r.language:<7} {r.stars:>7}  {r.notes[:40]}")
    return 0


# ------------------------------------------------------------------ agent1

def cmd_agent1(args: argparse.Namespace) -> int:
    from swe_synth.agent1.pipeline import run_agent1

    settings = load_settings()
    repos = load_repos()
    if args.repo:
        repos = [r for r in repos if r.name == args.repo]
        if not repos:
            print(f"❌ 仓库池中没有 {args.repo}，可用 list-repos 查看")
            return 1
    elif args.verified_only:
        repos = [r for r in repos if r.verified]

    # 题型选择：默认三类轮转（课题明文要求「功能实现/重构/模块添加」都要覆盖）
    spec_str = (args.type or "ABC").upper()
    bad = [c for c in spec_str if c not in TYPE_ALIASES]
    if bad:
        print(f"❌ --type 只接受 A/B/C 的组合（A=功能实现 B=模块添加 C=重构），非法字符：{bad}")
        return 1
    task_types = []
    for c in spec_str:                       # 去重但保持顺序
        if TYPE_ALIASES[c] not in task_types:
            task_types.append(TYPE_ALIASES[c])

    try:
        client = TokenHubClient(model=settings.model_task_design,
                                default_max_tokens=settings.max_tokens)
    except Exception as e:  # noqa: BLE001
        print(f"❌ TokenHub 初始化失败：{e}")
        return 1

    # ⚠️ 隔离性修复：工作副本必须放在项目树之外（系统临时目录），而不是
    # ROOT/".work"。原因：项目根目录本身含 .env（密钥）等文件，若仓库测试
    # 有「向上遍历父目录查找配置」的行为（如 python-dotenv 的
    # find_dotenv()），克隆在项目树内会一路网上读到我们自己的文件，
    # 产生与真实沙箱（容器内除仓库外空无一物）不一致的假阴性，
    # 错误淘汰本来健康的仓库（实测踩过：python-dotenv 16/17 失败为此污染）。
    work_root = Path(tempfile.gettempdir()) / "swe_synth_work"
    build_root = Path(tempfile.gettempdir()) / "swe_synth_build"
    proofs_root = settings.proofs_dir
    proofs_root.mkdir(parents=True, exist_ok=True)

    # 续跑：接着已有数据集的编号往下排，避免 task_id 冲突。
    # ⚠️ 序号必须同时考虑 tasks.jsonl 与 proofs 目录，二者取最大值：
    #   仅看 tasks.jsonl 时，若 proofs 里已有残留目录（如同一次运行被中断、
    #   或此前并发运行覆盖），新题会撞上已被占用的 task_id，导致证据目录被覆盖、
    #   tasks.jsonl 出现重复 task_id（实测踩过）。
    existing = read_jsonl(settings.tasks_jsonl) if settings.tasks_jsonl.exists() else []
    accepted_ids = {t.task_id for t in existing}
    proof_ids: set[int] = set()
    for d in proofs_root.iterdir() if proofs_root.is_dir() else []:
        if d.is_dir() and d.name.startswith("swe-synth-"):
            try:
                proof_ids.add(int(d.name.rsplit("-", 1)[1]))
            except ValueError:
                continue
    seq = 1 + max(
        [int(t.task_id.rsplit("-", 1)[1]) for t in existing] + sorted(proof_ids),
        default=0,
    )
    # 已出过题的靶点：避免对同一个函数/模块反复出题产生内容重复的题目。
    # 键的构造必须与 pipeline.run_agent1 内部一致，三种题型各有前缀/后缀。
    used_symbols: set[str] = set()
    for t in existing:
        meta = Path(t.validation.proof_dir or "") / "metadata.json"
        if not meta.exists():
            continue
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sym = m.get("symbol")
        tt = m.get("task_type") or t.task_type.value
        for f in m.get("modified_files", []):
            if tt == "module_addition":
                used_symbols.add(f"{f}::module_addition")
            elif tt == "refactoring":
                used_symbols.add(f"{f}::{sym}#refactor")
            else:
                used_symbols.add(f"{f}::{sym}" if sym else f)

    label = "+".join(f"{c}({TYPE_ALIASES[c].value})" for c in spec_str)
    _log(f"题型：{label}")
    _log(f"已有数据集 {len(existing)} 条，新题从 swe-synth-{seq:04d} 开始")
    if used_symbols:
        _log(f"已出题靶点 {len(used_symbols)} 个，将跳过以避免重复")
    _log("")

    all_results = []
    t0 = time.time()
    target = args.n

    for spec in repos:
        got = sum(1 for r in all_results if r.accepted)
        if got >= target:
            break
        res = run_agent1(
            spec, settings, client,
            work_root=work_root, build_root=build_root, proofs_root=proofs_root,
            task_id_start=seq,
            max_tasks=min(args.per_repo, target - got),
            max_candidates=args.max_candidates,
            base_python=args.python or sys.executable,
            do_solve_back=not args.no_solve_back,
            used_symbols=used_symbols,
            task_types=task_types,
            on_progress=_log,
        )
        all_results += res
        seq += sum(1 for r in res if r.accepted)

    # 落盘（JSON Lines，验收要求的格式）
    # ⚠️ 幂等保护：落盘前重新读取文件的最新 task_id 集合再过滤。
    #   若运行期间文件被其它进程修改（或本进程并发），只追加真正的新 task_id，
    #   避免同一 task_id 被写两次。
    latest_ids = {t.task_id for t in read_jsonl(settings.tasks_jsonl)} \
        if settings.tasks_jsonl.exists() else set()
    tasks = [r.task for r in all_results if r.accepted and r.task]
    new_tasks = [t for t in tasks if t.task_id not in latest_ids]
    if new_tasks:
        write_jsonl(new_tasks, settings.tasks_jsonl, append=bool(latest_ids))

    # 统计报告（用于结题报告的成本/通过率数据）
    n_try, n_ok = len(all_results), len(tasks)
    by_type: dict[str, int] = {}
    for t in tasks:
        by_type[t.task_type.value] = by_type.get(t.task_type.value, 0) + 1
    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_sec": round(time.time() - t0, 1),
        "task_types_requested": [t.value for t in task_types],
        "attempts": n_try,
        "accepted": n_ok,
        "accepted_by_type": by_type,
        "pass_rate": round(n_ok / n_try, 3) if n_try else 0.0,
        "llm_usage": client.usage.summary(),
        "llm_cost_estimate_cny": client.usage.cost_estimate(
            *settings.price_of(settings.model_task_design)),
        "by_stage": {},
        "results": [r.to_dict() for r in all_results],
    }
    for r in all_results:
        if not r.accepted:
            report["by_stage"][r.stage] = report["by_stage"].get(r.stage, 0) + 1
    report_path = ROOT / str(settings.get("output.report", "data/report.json"))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"Agent1 完成：尝试 {n_try} 次，产出 {n_ok} 道合格题目"
          f"（通过率 {report['pass_rate']:.0%}），耗时 {report['duration_sec']:.0f}s")
    if by_type:
        print("题型分布：" + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    if report["by_stage"]:
        print("失败分布：" + ", ".join(f"{k}={v}" for k, v in report["by_stage"].items()))
    print(f"LLM 用量：{client.usage.calls} 次调用，"
          f"成本≈{report['llm_cost_estimate_cny']} 元")
    print(f"数据集：{settings.tasks_jsonl}（共 {len(existing) + len(new_tasks)} 条）")
    print(f"通过证明：{proofs_root}/<task_id>/")
    print(f"构建上下文：{build_root}/<task_id>/{{task,sol}}/")
    print(f"统计报告：{report_path}")
    print("=" * 72)
    return 0 if n_ok else 1


# ------------------------------------------------------------------ agent2

def cmd_agent2(args: argparse.Namespace) -> int:
    """Agent2：沙箱双向验证 + 无重叠校验；两者皆通过则 state → ACCEPTED。"""
    import os as _os

    from swe_synth.agent2.overlap_check import check_overlap
    from swe_synth.agent2.sandbox_runner import SandboxVerifyError, verify_task
    from swe_synth.clients.github import GitHubClient, GitHubError
    from swe_synth.clients.tokenhub import LLMError

    settings = load_settings()
    if not settings.tasks_jsonl.exists():
        print(f"❌ 数据集不存在：{settings.tasks_jsonl}（先跑 agent1 + pack）")
        return 1
    try:
        tasks = read_jsonl(settings.tasks_jsonl)
    except ValueError as e:
        print(f"❌ 数据集存在不合法的行：{e}")
        return 1

    ids = set(args.task_id) if args.task_id else None
    targets = [
        t for t in tasks
        if (ids is None or t.task_id in ids)
        and (args.force or t.state != TaskState.ACCEPTED)
    ]
    if not targets:
        print("没有需要验证的题目（全部已 ACCEPTED；用 --force 可指定题目重跑）")
        return 0

    github = None
    llm = None
    if not args.skip_overlap:
        try:
            github = GitHubClient(
                cache_dir=settings.get("overlap_check.github.cache_dir", ".cache/github"),
                max_retries=int(settings.get("overlap_check.github.max_retries", 5)),
                backoff_base_sec=float(settings.get("overlap_check.github.backoff_base_sec", 2)),
            )
            llm = TokenHubClient(model=settings.model_overlap_judge,
                                 default_max_tokens=settings.max_tokens)
        except Exception as e:  # noqa: BLE001
            print(f"❌ 无重叠校验依赖初始化失败：{e}（可加 --skip-overlap 先只跑沙箱验证）")
            return 1

    sandbox_timeout = int(settings.get("sandbox.timeout_sec", 900))
    reuse_tool = bool(settings.get("sandbox.reuse_tool", True))
    registry_type = _os.environ.get("TCR_REGISTRY_TYPE", "personal")

    n_accepted, n_pending = 0, 0
    for t in targets:
        _log(f"\n{'=' * 72}\n{t.task_id}  ({t.task_type.value}/{t.difficulty.value})  {t.repo}")
        if not t.image or not t.solution_image:
            _log("  ⏭  缺少镜像地址，跳过")
            n_pending += 1
            continue

        proof_dir = Path(t.validation.proof_dir or (settings.proofs_dir / t.task_id))
        proof_dir.mkdir(parents=True, exist_ok=True)

        # ---------- 1) 沙箱双向验证（空解必败 + golden 必过）
        sandbox_ok = t.validation.fully_verified
        if args.skip_sandbox:
            _log(f"  ⏭  跳过沙箱验证（沿用历史结果：{'通过' if sandbox_ok else '未通过'}）")
        else:
            try:
                vres = verify_task(
                    t, image_registry_type=registry_type,
                    timeout=sandbox_timeout, reuse_tool=reuse_tool,
                )
            except SandboxVerifyError as e:
                _log(f"  ❌ 沙箱验证环境错误：{e}")
                n_pending += 1
                continue
            t.validation.sandbox_tool = t.task_id
            t.validation.sandbox_instance_id = vres.empty_sandbox_id
            t.validation.empty_solution_result = "pass" if (vres.empty_run or {}).get("passed") else "fail"
            t.validation.golden_solution_result = "pass" if (vres.golden_run or {}).get("passed") else "fail"
            t.validation.duration_sec = vres.duration_sec
            sandbox_ok = vres.passed
            (proof_dir / "verification.json").write_text(
                json.dumps(vres.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _log(f"  {'✅' if sandbox_ok else '❌'} 沙箱双向验证：{vres.reason}")
            if sandbox_ok and t.state in (TaskState.LOCAL_OK, TaskState.IMAGE_PUSHED):
                t.state = TaskState.SANDBOX_OK

        # ---------- 2) 无重叠校验
        overlap_ok = t.overlap_check.passed
        if not sandbox_ok:
            _log("  ⏭  沙箱未通过，跳过无重叠校验")
        elif args.skip_overlap:
            _log(f"  ⏭  跳过无重叠校验（沿用历史结果：{'通过' if overlap_ok else '未通过'}）")
        else:
            meta_path = proof_dir / "metadata.json"
            symbol = ""
            if meta_path.exists():
                try:
                    symbol = json.loads(meta_path.read_text(encoding="utf-8")).get("symbol") or ""
                except (OSError, json.JSONDecodeError):
                    pass
            target_file = t.modified_files[0] if t.modified_files else ""
            try:
                report = check_overlap(
                    github, llm, t.repo, t.base_commit, target_file, symbol,
                    t.problem_statement,
                    min_months_since_change=float(settings.get("overlap_check.min_months_since_change", 12)),
                    no_open_pr_months=float(settings.get("overlap_check.no_open_pr_months", 6)),
                    model=settings.model_overlap_judge,
                )
            except GitHubError as e:
                _log(f"  ❌ 无重叠校验失败（GitHub API）：{e}")
                n_pending += 1
                continue
            except LLMError as e:
                _log(f"  ❌ 无重叠校验失败（LLM 裁决）：{e}")
                n_pending += 1
                continue
            t.overlap_check = report.check
            overlap_ok = report.check.passed
            (proof_dir / "overlap_check.json").write_text(
                json.dumps(report.check.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _log(f"  {'✅' if overlap_ok else '❌'} 无重叠校验：{report.check.verdict_reason}")
            if overlap_ok and t.state == TaskState.SANDBOX_OK:
                t.state = TaskState.OVERLAP_OK

        # ---------- 3) 终判
        if sandbox_ok and overlap_ok:
            t.state = TaskState.ACCEPTED
            n_accepted += 1
            _log(f"  🎉 {t.task_id} → ACCEPTED")
        else:
            n_pending += 1
            _log(f"  ⚠️  {t.task_id} 尚未满足 ACCEPTED 条件（保留状态 {t.state.value}），需复核")

    write_jsonl(tasks, settings.tasks_jsonl)
    print("\n" + "=" * 72)
    print(f"Agent2 完成：{len(targets)} 道题验证，{n_accepted} 道 → ACCEPTED，{n_pending} 道待复核")
    print(f"数据集已更新：{settings.tasks_jsonl}")
    print("=" * 72)
    return 0 if n_pending == 0 else 1


# ------------------------------------------------------------------ validate

def cmd_validate(args: argparse.Namespace) -> int:
    """逐行校验数据集，并核对是否满足课题的交付标准。"""
    settings = load_settings()
    path = settings.tasks_jsonl
    if not path.exists():
        print(f"❌ 数据集不存在：{path}")
        return 1
    try:
        tasks = read_jsonl(path)
    except ValueError as e:
        print(f"❌ 数据集存在不合法的行：{e}")
        return 1

    print(f"✅ {path} 共 {len(tasks)} 条，全部通过 schema 校验\n")
    print(f"{'task_id':<18} {'题型':<24} {'难度':<8} {'状态':<14} {'F2P':>4} {'P2P':>4}  仓库")
    print("-" * 108)
    for t in tasks:
        print(f"{t.task_id:<18} {t.task_type.value:<24} {t.difficulty.value:<8} "
              f"{t.state.value:<14} {len(t.FAIL_TO_PASS):>4} {len(t.PASS_TO_PASS):>4}  {t.repo}")

    # 对照 TASK-SPEC.md 的交付标准逐项核对
    print("\n=== 课题交付标准核对（TASK-SPEC.md）===")
    n_accepted = sum(1 for t in tasks if t.state.value == "ACCEPTED")
    types = {t.task_type.value for t in tasks}
    diffs = {t.difficulty.value for t in tasks}
    checks = [
        ("≥10 道题目", len(tasks) >= 10, f"当前 {len(tasks)} 道"),
        ("全部通过双 Agent 验证（state=ACCEPTED）", n_accepted >= 10, f"当前 {n_accepted} 道"),
        ("每道含题干描述", all(len(t.problem_statement) > 200 for t in tasks), "problem_statement"),
        ("每道含镜像地址", all(t.image for t in tasks), "image"),
        ("每道含验证脚本", all(t.verify_script for t in tasks), "verify_script"),
        ("每道含通过证明", all(t.validation.proof_dir for t in tasks), "proof_dir"),
        ("题型覆盖三类", len(types) >= 3, f"当前 {sorted(types)}"),
        ("难度覆盖三档", len(diffs) >= 3, f"当前 {sorted(diffs)}"),
        ("仓库 Star>100", all(t.repo_stars > 100 for t in tasks), "repo_stars"),
        ("无重叠校验通过", all(t.overlap_check.passed for t in tasks), "overlap_check"),
    ]
    for name, ok, detail in checks:
        print(f"  {'✅' if ok else '❌'} {name:<38} {detail}")
    return 0


# ------------------------------------------------------------------ pack

def cmd_pack(args: argparse.Namespace) -> int:
    """把 .build 里的构建上下文打包成镜像并推送到 TCR。"""
    from swe_synth.agent1.packer import DockerError, pack_all

    settings = load_settings()
    # 必须与 cmd_agent1 里的 build_root 保持一致（均已迁到系统临时目录，
    # 原因见 cmd_agent1 内的隔离性修复说明），否则 pack 会找不到刚出的题。
    build_root = Path(tempfile.gettempdir()) / "swe_synth_build"
    if not build_root.is_dir() or not any(build_root.iterdir()):
        print(f"❌ 构建上下文为空：{build_root}（先跑 agent1）")
        return 1

    task_ids = args.task_id or None
    try:
        results = pack_all(build_root, settings, task_ids=task_ids,
                           platform=args.platform)
    except DockerError as e:
        print(f"❌ {e}")
        return 1

    print("=" * 72)
    print(f"打包完成：{len(results)} 个镜像已推送")
    for r in results:
        print(f"  ✅ {r.image}  （{r.duration_sec}s）")
    print("=" * 72)
    return 0


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="SWE-Synth：双 Agent 协作的 SWE 题目自动构建与验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-repos", help="列出候选仓库池")
    p_list.add_argument("--verified-only", action="store_true", help="只显示已验证的仓库")
    p_list.set_defaults(func=cmd_list_repos)

    p1 = sub.add_parser("agent1", help="出题 + 生成构建上下文（不含 build/push）")
    p1.add_argument("--repo", help="只处理指定仓库（如 psf/cachecontrol）")
    p1.add_argument("--n", type=int, default=1, help="本次目标产出题数（默认 1）")
    p1.add_argument("--type", default="ABC",
                    help="题型组合：A=功能实现 B=模块添加 C=重构（默认 ABC 轮转）")
    p1.add_argument("--per-repo", type=int, default=3, help="每个仓库最多产出几道（默认 3）")
    p1.add_argument("--max-candidates", type=int, default=8, help="每仓库最多尝试候选数")
    p1.add_argument("--python", help="用于创建仓库 venv 的基础解释器（默认当前解释器）")
    p1.add_argument("--no-solve-back", action="store_true",
                    help="跳过 solve-back 可解性验证（更快但题干准确性无保障，仅调试用）")
    p1.add_argument("--verified-only", action="store_true", help="只用已验证的仓库")
    p1.set_defaults(func=cmd_agent1)

    p_pack = sub.add_parser("pack", help="docker build + push 到 TCR（需 Docker）")
    p_pack.add_argument("--task-id", action="append", dest="task_id",
                        help="只打包指定题目（可多次；默认全部）")
    p_pack.add_argument("--platform", default=None, help="构建平台（默认取 settings.yaml）")
    p_pack.set_defaults(func=cmd_pack)

    p_a2 = sub.add_parser("agent2", help="沙箱双向验证 + 无重叠校验（通过则 state -> ACCEPTED）")
    p_a2.add_argument("--task-id", action="append", dest="task_id",
                      help="只验证指定题目（可多次；默认全部未 ACCEPTED 的题目）")
    p_a2.add_argument("--force", action="store_true", help="即使已 ACCEPTED 也重跑（配合 --task-id 使用）")
    p_a2.add_argument("--skip-sandbox", action="store_true", help="跳过沙箱验证，沿用历史结果")
    p_a2.add_argument("--skip-overlap", action="store_true", help="跳过无重叠校验，沿用历史结果")
    p_a2.set_defaults(func=cmd_agent2)

    p_val = sub.add_parser("validate", help="校验数据集并核对课题交付标准")
    p_val.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
