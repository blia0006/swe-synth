"""GitHub 客户端：仓库信息检索 + 目标文件的提交历史（供无重叠校验使用）

安全与合规
----------
· 只调用 GitHub 公开只读 API（`search/issues`、`commits?path=`），
  需要的 token 只需 `public_repo` 读权限
· 所有查询参数经 requests 的 `params` 传入（自动 URL 编码），避免注入
· ETag 缓存：GitHub search API 有 30 req/min 限流，用 ETag 命中时不计入配额，
  把结果缓存到本地 `cache_dir`，既能限流又能断点续查
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

__all__ = ["GitHubClient", "GitHubError"]


class GitHubError(RuntimeError):
    """GitHub API 调用失败。"""


class GitHubClient:
    """GitHub 公开 API 的轻量封装（带 ETag 缓存与退避）。"""

    BASE = "https://api.github.com"

    def __init__(
        self,
        token: str | None = None,
        *,
        cache_dir: str | Path = ".cache/github",
        max_retries: int = 5,
        backoff_base_sec: float = 2.0,
    ) -> None:
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise GitHubError("缺少 requests：pip install -r requirements.txt") from e

        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._requests = requests
        self._cache = Path(cache_dir)
        self._cache.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.backoff_base = backoff_base_sec
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "swe-synth",
        })
        if self._token:
            self._session.headers["Authorization"] = f"Bearer {self._token}"

    # ------------------------------------------------------------ 内部
    def _get(self, url: str, *, params: dict[str, Any] | None = None,
             cache_key: str | None = None) -> tuple[dict | list, str]:
        """GET 请求，带 ETag 缓存与退避重试。返回 (json, etag)。"""
        if cache_key:
            cached = self._cache / (cache_key + ".json")
            meta = self._cache / (cache_key + ".etag")
            headers = {}
            if meta.exists():
                headers["If-None-Match"] = meta.read_text(encoding="utf-8").strip()
            if cached.exists():
                # 命中本地缓存但无 ETag 信息时，优先用缓存（离线可查）
                if not meta.exists():
                    try:
                        return json.loads(cached.read_text(encoding="utf-8")), ""
                    except (OSError, json.JSONDecodeError):
                        pass
        else:
            headers = {}
            cached = meta = None

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                rsp = self._session.get(url, params=params, headers=headers, timeout=30)
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(min(self.backoff_base * (2 ** attempt), 30))
                continue

            if rsp.status_code == 304 and cached and cached.exists():
                # ETag 命中：内容未变，直接用本地缓存，不计限流配额
                try:
                    return json.loads(cached.read_text(encoding="utf-8")), \
                        meta.read_text(encoding="utf-8").strip() if meta.exists() else ""
                except (OSError, json.JSONDecodeError):
                    pass
            if rsp.status_code == 403 and "rate limit" in rsp.text.lower():
                # 限流：按退避等待（GitHub 会返回 Retry-After / 重置时间）
                reset = rsp.headers.get("X-RateLimit-Reset")
                wait = 30
                if reset:
                    wait = max(1.0, float(reset) - time.time())
                time.sleep(min(wait, 60))
                continue
            if rsp.status_code >= 400:
                last_err = GitHubError(f"{url} → HTTP {rsp.status_code}: {rsp.text[:200]}")
                if rsp.status_code in (401, 403, 404):
                    break  # 认证/权限/资源不存在，重试无意义
                time.sleep(min(self.backoff_base * (2 ** attempt), 30))
                continue
            try:
                data = rsp.json()
            except ValueError as e:
                last_err = GitHubError(f"{url} 返回非 JSON：{e}")
                continue
            etag = rsp.headers.get("ETag", "")
            if cache_key and cached is not None and meta is not None:
                cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                if etag:
                    meta.write_text(etag, encoding="utf-8")
            return data, etag

        raise GitHubError(f"GitHub API 失败（重试 {self.max_retries} 次）：{last_err}")

    # ------------------------------------------------------------ 对外
    def search_issues(self, repo: str, query: str, *, max_results: int = 30) -> list[dict]:
        """检索仓库的 issue / PR（含 closed），返回条目列表。"""
        q = f"repo:{repo} {query}"
        out: list[dict] = []
        page = 1
        while len(out) < max_results and page <= 3:
            data, _ = self._get(
                f"{self.BASE}/search/issues",
                params={"q": q, "per_page": min(max_results, 100), "page": page},
                cache_key=f"search_{repo.replace('/', '_')}_{abs(hash(q)) % 10**9}",
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                break
            out += items
            page += 1
        return out[:max_results]

    def file_commits(self, repo: str, path: str, *, per_page: int = 10) -> list[dict]:
        """目标文件的提交历史（用于「时间隔离」与「无重叠」判断）。"""
        data, _ = self._get(
            f"{self.BASE}/repos/{repo}/commits",
            params={"path": path, "per_page": per_page},
            cache_key=f"commits_{repo.replace('/', '_')}_{path.replace('/', '_')[:80]}",
        )
        return data if isinstance(data, list) else []

    def file_last_modified(self, repo: str, path: str) -> str | None:
        """目标文件最近一次提交的时间（ISO 8601），无提交则 None。"""
        commits = self.file_commits(repo, path, per_page=1)
        if not commits:
            return None
        c = commits[0].get("commit", {}).get("committer", {})
        return c.get("date")
