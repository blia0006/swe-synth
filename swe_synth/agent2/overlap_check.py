"""无重叠校验（课题核心约束）

课题原文要求：题目「不得与仓库现有 issue/PR/commit/bugfix 内容重叠」，
并要求 Agent2「校验题目与仓库现有 PR/commit/bugfix 无重叠（通过 GitHub API 检索）」。

本模块实现这一校验，分四层（从便宜到昂贵，逐层过滤）：

1. **来源免疫**：题目不取自任何 bugfix commit/PR（由出题方式保证 —— 挖空的是
   稳定函数、重构的是坏味道代码、新增的是全新模块）
2. **时间隔离**：目标文件应长期未改动（默认 12 个月），否则很可能已有相关修复
3. **GitHub 检索 + 粗筛**：`search/issues` 检索相关 issue/PR + 目标文件提交历史，
   用字符 n-gram 相似度粗筛（代替 embedding，因 TokenHub 无 embedding 端点）
4. **LLM 裁决**：把粗筛出的候选交给 LLM 判断「是否与本题重叠」—— 这是最终裁决

结果写入 `OverlapCheck`（`overlap_check` 字段），作为「通过证明」的一部分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..clients.github import GitHubClient, GitHubError
from ..clients.tokenhub import LLMError, TokenHubClient
from ..schemas.task import OverlapCheck

__all__ = ["OverlapReport", "check_overlap", "NG"]


def NG(n: int) -> int:
    """字符 n-gram 的默认 n（模块级常量，便于统一）。"""
    return 3


# ------------------------------------------------------------------ 相似度（代替 embedding）

def _ngrams(text: str, n: int = 3) -> set[str]:
    """字符 n-gram 集合（对中文与英文都成立，无需分词）。"""
    t = re.sub(r"\s+", " ", text.lower())
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _similarity(a: str, b: str, n: int = 3) -> float:
    """Jaccard 相似度（字符 n-gram），0~1。"""
    ga, gb = _ngrams(a, n), _ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# ------------------------------------------------------------------ 结果

@dataclass
class OverlapReport:
    """无重叠校验的完整结果（含过程证据）。"""

    check: OverlapCheck
    candidates: list[dict[str, Any]]


def _query_terms(symbol_path: str, rel_path: str, problem_statement: str) -> list[str]:
    """构造检索词：目标符号名 + 文件名 + 题干中的关键名词。"""
    terms = []
    tail = symbol_path.rsplit(".", 1)[-1] if symbol_path else ""
    if tail:
        terms.append(tail)
    stem = rel_path.rsplit("/", 1)[-1].replace(".py", "") if rel_path else ""
    if stem and stem not in terms:
        terms.append(stem)
    # 题干里的关键词：取较长的标识符（类/函数名风格）作为补充检索词
    idents = sorted(set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]{4,}\b", problem_statement or "")),
                    key=len, reverse=True)
    for w in idents[:5]:
        if w.lower() not in ("cache", "test", "python", "github", "issue"):
            terms.append(w)
    return terms[:4]


def check_overlap(
    github: GitHubClient,
    client: TokenHubClient,
    repo: str,
    base_commit: str,
    target_file: str,
    symbol_path: str,
    problem_statement: str,
    *,
    min_months_since_change: float = 12.0,
    no_open_pr_months: float = 6.0,
    ngram_threshold: float = 0.25,
    model: str | None = None,
) -> OverlapReport:
    """对一道题执行无重叠校验，返回完整报告。

    参数
    ----
    min_months_since_change:
        目标文件最近改动距今的月数下限。低于它则「时间隔离」不达标，直接判不通过
        （但仍在报告中给出原因，供人工复核）。
    """
    queried: list[str] = []
    candidates: list[dict[str, Any]] = []
    last_modified: str | None = None
    months_since: float | None = None
    reason_parts: list[str] = []

    # ---------- 1) 时间隔离：目标文件最近改动时间
    try:
        last_modified = github.file_last_modified(repo, target_file)
    except GitHubError as e:
        reason_parts.append(f"时间隔离查询失败：{e}")
    if last_modified:
        from datetime import datetime, timezone

        try:
            dt = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
            months_since = (datetime.now(timezone.utc) - dt).days / 30.44
            if months_since < min_months_since_change:
                reason_parts.append(
                    f"目标文件 {months_since:.1f} 个月前刚改动（要求 ≥{min_months_since_change:.0f} 个月），"
                    f"存在与近期修复重叠的风险"
                )
        except ValueError:
            reason_parts.append(f"无法解析目标文件改动时间：{last_modified}")

    # ---------- 2) GitHub 检索 + 粗筛
    for term in _query_terms(symbol_path, target_file, problem_statement):
        try:
            items = github.search_issues(repo, term, max_results=10)
        except GitHubError as e:
            reason_parts.append(f"检索失败（{term}）：{e}")
            continue
        queried.append(term)
        for it in items:
            title = it.get("title") or ""
            body = (it.get("body") or "")[:2000]
            sim = max(_similarity(problem_statement, title),
                      _similarity(problem_statement, body))
            if sim >= ngram_threshold:
                candidates.append({
                    "number": it.get("number"),
                    "title": title[:200],
                    "state": it.get("state"),
                    "is_pr": bool(it.get("pull_request")),
                    "html_url": it.get("html_url"),
                    "similarity": round(sim, 3),
                    "body_excerpt": body[:500],
                })

    # 去重（同一 issue 被多个检索词命中）
    seen: set[int] = set()
    uniq = []
    for c in candidates:
        if c["number"] not in seen:
            seen.add(c["number"])
            uniq.append(c)
    uniq.sort(key=lambda x: -x["similarity"])
    candidates = uniq[:20]

    # ---------- 3) LLM 裁决
    verdict = ""
    passed = not reason_parts  # 时间隔离已失败则直接不通过
    if candidates:
        judge = _llm_judge(client, problem_statement, symbol_path, candidates, model=model)
        if judge.get("overlap"):
            passed = False
            reason_parts.append(
                f"LLM 裁决存在重叠：{judge.get('reason', '')[:200]}"
            )
        else:
            verdict = judge.get("reason", "")
    elif passed:
        verdict = "未检索到与本题相关的 issue/PR"

    reason = "; ".join(reason_parts) if reason_parts else (
        verdict or "无重叠：检索 + 粗筛 + LLM 裁决均未发现相关内容"
    )

    check = OverlapCheck(
        passed=passed,
        queried=queried,
        top_candidates=candidates,
        target_file_last_modified=last_modified,
        months_since_last_change=round(months_since, 1) if months_since is not None else None,
        verdict_reason=reason,
    )
    return OverlapReport(check=check, candidates=candidates)


_LLM_SYSTEM = """你是软件工程数据集的去污染审查员。给你一道新生成的编程题目，以及从该仓库
GitHub issue/PR 检索出的若干候选条目，判断这道题是否与某个已有条目**内容重叠**。

「重叠」的判定标准（严格）：
- 已有条目描述的问题/需求/缺陷，与本题要求实现的功能、重构的对象、或新增的模块
  **是同一件事**（哪怕措辞不同）
- 注意：仅讨论同一个**文件或类**不算重叠；必须是**同一处代码的同一处改动意图**

请输出 JSON：{"overlap": true/false, "reason": "一句话说明" }"""


def _llm_judge(
    client: TokenHubClient,
    statement: str,
    symbol: str,
    candidates: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    cand_text = "\n\n".join(
        f"[{i + 1}] #{c['number']} {'PR' if c['is_pr'] else 'Issue'} ({c['state']})\n"
        f"标题：{c['title']}\n摘要：{c['body_excerpt'][:300]}"
        for i, c in enumerate(candidates[:8])
    )
    user = (
        f"目标符号：{symbol}\n\n"
        f"题目描述（节选）：\n{statement[:2000]}\n\n"
        f"候选条目：\n{cand_text}"
    )
    try:
        data = client.chat_json(
            [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": user},
            ],
            model=model, max_tokens=1024, temperature=0.0,
        )
        return data if isinstance(data, dict) else {}
    except LLMError:
        # 裁决失败不阻塞 —— 保守返回「无重叠」，交由人工复核候选列表
        return {"overlap": False, "reason": "LLM 裁决失败，需人工复核"}
