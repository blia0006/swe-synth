#!/usr/bin/env python3
"""仓库池自动发现 —— 把手工维护的个位数仓库池扩展到成百上千个。

用法示例
--------
    # 试运行：只打印会发现多少候选，不写文件（不消耗太多 GitHub API 配额）
    python scripts/discover_repos.py --languages python --max-per-lang 50 --dry-run

    # 正式运行：写入 config/repos.discovered.yaml（与 repos.yaml 同构、自动去重）
    python scripts/discover_repos.py --languages python,javascript --max-per-lang 300

    # 之后让 agent1 / list-repos 一并使用发现出的仓库：
    python scripts/run_pipeline.py list-repos --include-discovered
    python scripts/run_pipeline.py agent1 --include-discovered --n 50 --workers 8

需要 GITHUB_TOKEN（见 .env），否则 GitHub 搜索 API 限流极低（未认证 10 req/min）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from swe_synth.agent1.repo_discovery import (DiscoveryFilter, discover_candidates,  # noqa: E402
                                              to_repo_specs)
from swe_synth.clients.github import GitHubClient  # noqa: E402
from swe_synth.config.loader import load_dotenv_if_present, load_repos  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--languages", default="python", help="逗号分隔，如 python,javascript")
    ap.add_argument("--min-stars", type=int, default=100, help="课题硬性要求 >100")
    ap.add_argument("--max-stars", type=int, default=None,
                    help="上限（避免只挖头部超大仓库导致重叠风险集中）")
    ap.add_argument("--pushed-after", default="2022-01-01", help="仓库最近一次 push 不早于此日期")
    ap.add_argument("--max-per-lang", type=int, default=300, help="每个语言最多拉取的候选数")
    ap.add_argument("--out", default=str(ROOT / "config" / "repos.discovered.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args()

    load_dotenv_if_present()
    existing = load_repos()  # repos.yaml 里已有的，发现结果要排除，避免重复
    exclude = {r.name for r in existing}

    github = GitHubClient(cache_dir=str(ROOT / ".cache" / "github"))
    f = DiscoveryFilter(
        languages=[s.strip() for s in args.languages.split(",") if s.strip()],
        min_stars=args.min_stars,
        max_stars=args.max_stars,
        pushed_after=args.pushed_after,
        exclude_names=exclude,
        max_results_per_lang=args.max_per_lang,
    )
    print(f"排除 repos.yaml 中已有的 {len(exclude)} 个仓库\n")
    items = discover_candidates(github, f)
    specs = to_repo_specs(items)
    print(f"\n共发现 {len(specs)} 个新候选仓库（Star>{args.min_stars}，去重后）")

    if args.dry_run:
        print("（--dry-run，未写文件）前 20 个预览：")
        for s in specs[:20]:
            print(f"  ★{s.stars:>6}  {s.name:<45} {s.language}")
        return 0

    out_path = Path(args.out)
    # 与已有的 repos.discovered.yaml 合并去重（多次运行会累积扩大候选池）
    prior_names: set[str] = set()
    if out_path.exists():
        prior_names = {r.name for r in load_repos(out_path)}
    new_specs = [s for s in specs if s.name not in prior_names]

    import yaml

    prior_repos = []
    if out_path.exists():
        data = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
        prior_repos = data.get("repos") or []

    all_repos = prior_repos + [
        {
            "name": s.name, "language": s.language, "stars": s.stars,
            "clone_url": s.clone_url, "install": s.install, "test_cmd": s.test_cmd,
            "verified": False, "priority": s.priority, "notes": s.notes,
        }
        for s in new_specs
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 自动生成：python scripts/discover_repos.py\n"
        "# 与 repos.yaml 同构；verified 全为 false —— 是否真的可用（能装/能测）\n"
        "# 由 run_agent1() 首次跑到该仓库时的 baseline 检查决定，非本文件职责。\n"
        "# 不建议手工编辑；重跑发现脚本会在此基础上累积追加、自动去重。\n"
    )
    out_path.write_text(
        header + yaml.safe_dump({"repos": all_repos}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"新增 {len(new_specs)} 个（累计 {len(all_repos)} 个）→ 已写入 {out_path}")
    print("下一步：python scripts/run_pipeline.py agent1 --include-discovered --n <N> --workers <W>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
