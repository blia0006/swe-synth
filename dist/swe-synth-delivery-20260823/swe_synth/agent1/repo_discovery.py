"""仓库池自动发现 —— 「10 道 → 1 万/100 万道」扩容链路的入口。

背景
----
`config/repos.yaml` 是人工逐个验证、逐个写入的候选池（截至本次改造只有
个位数仓库），这是当前产出题量小的**根因**：不是流水线跑不动，是喂给
它的仓库太少。人工核实每个仓库的 baseline（能装、能测）成本很高，
无法线性扩展到成百上千个。

本模块把"找候选仓库"这一步自动化：用 GitHub 搜索 API 按
`stars>100 + language + 近期活跃` 筛出候选，写入 `config/repos.discovered.yaml`
（与 `repos.yaml` 同构，`verified=False`）。**是否真的能用**（baseline
装得上、测试跑得过）仍然交给现有流水线在 `run_agent1()` 里的
`RepoWorkspace.prepare()` + baseline 检查去把关 —— 这一步本来就是自动化的，
发现阶段无需重复实现，只需把"喂给它多少候选"这个瓶颈打开。

产出规模测算见 `SCALE-OUT.md`：GitHub 上 Star>100 的 Python 仓库约 3 万个，
按当前 ~20% 通过率，1 万道题只需约 562 个仓库、100 万道题需约 5.6 万仓库
（逼近候选池上限，需叠加"多语言/多版本 commit/组合式挖空"等乘数策略，
本模块只解决「仓库来源」这一环，不是唯一扩容手段）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..clients.github import GitHubClient, GitHubError
from ..config.loader import RepoSpec

__all__ = ["DiscoveryFilter", "discover_candidates", "to_repo_specs"]


@dataclass
class DiscoveryFilter:
    """筛选条件（对应课题 T-6「Star>100」等硬性要求 + 工程上的可行性筛选）。"""

    languages: list[str] = field(default_factory=lambda: ["python"])
    min_stars: int = 100          # 课题硬性要求
    max_stars: int | None = None  # 避免只挖头部仓库导致「无重叠校验」总撞车
    pushed_after: str = "2022-01-01"  # 排除长期无人维护、baseline 大概率跑不起来的仓库
    exclude_archived: bool = True
    exclude_forks: bool = True
    exclude_names: set[str] = field(default_factory=set)  # 已在 repos.yaml 里的，避免重复
    max_results_per_lang: int = 300


def _build_query(lang: str, f: DiscoveryFilter) -> str:
    parts = [f"language:{lang}", f"stars:>{f.min_stars}", f"pushed:>{f.pushed_after}"]
    if f.max_stars:
        parts.append(f"stars:<{f.max_stars}")
    if f.exclude_archived:
        parts.append("archived:false")
    if f.exclude_forks:
        parts.append("fork:false")
    return " ".join(parts)


def discover_candidates(
    github: GitHubClient, f: DiscoveryFilter | None = None,
    *, on_progress: "callable" = print,
) -> list[dict]:
    """调用 GitHub 搜索 API，返回原始仓库 JSON 列表（已去重、已过滤已有仓库）。"""
    f = f or DiscoveryFilter()
    seen: set[str] = set()
    out: list[dict] = []
    for lang in f.languages:
        query = _build_query(lang, f)
        on_progress(f"  🔍 GitHub 搜索：{query}")
        try:
            items = github.search_repositories(query, max_results=f.max_results_per_lang)
        except GitHubError as e:
            on_progress(f"  ⚠️  {lang} 搜索失败：{e}")
            continue
        for it in items:
            full_name = it.get("full_name", "")
            if not full_name or full_name in seen or full_name in f.exclude_names:
                continue
            seen.add(full_name)
            out.append(it)
        on_progress(f"     → {len(items)} 条结果，累计去重后 {len(out)} 个候选")
        time.sleep(1.0)  # 温和让路给 search API 的 30 req/min 限流
    return out


def to_repo_specs(items: list[dict]) -> list[RepoSpec]:
    """把 GitHub API 原始 JSON 转成 `RepoSpec`（默认装机指令，`verified=False`）。

    默认只给最通用的 `pip install -e .`（`test_extras` 留空，pytest 由流水线
    统一强装，见 `RepoWorkspace.prepare()`）—— **故意不猜** `.[test]`/`.[dev]`
    等 extras，因为 `prepare()` 对 install 列表里的每条命令都是硬性要求
    （某条失败则整个仓库直接淘汰，没有「失败就跳到下一条」的回退逻辑），
    猜错 extras 名字只会让本来能用的仓库被误杀。真正「能不能装、能不能测」
    仍由 `run_agent1()` 首次跑该仓库时的 baseline 检查把关，跑不过的仓库
    会被流水线自然跳过，不会污染数据集，无需在发现阶段人工判断。
    """
    out: list[RepoSpec] = []
    for it in items:
        stars = int(it.get("stargazers_count", 0))
        if stars < 100:
            continue
        full_name = it.get("full_name", "")
        clone_url = it.get("clone_url") or f"https://github.com/{full_name}.git"
        lang = (it.get("language") or "python").lower()
        try:
            spec = RepoSpec(
                name=full_name,
                language=lang,
                stars=stars,
                clone_url=clone_url,
                install=["pip install -e ."],
                test_cmd="pytest -q",
                verified=False,
                priority="discovered",
                notes=f"自动发现（{it.get('description') or ''}）"[:120],
            )
        except ValueError:
            continue
        out.append(spec)
    return out
