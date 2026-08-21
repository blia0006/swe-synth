"""多 Key 轮转客户端池 —— 出题吞吐的水平扩展点。

背景（为什么需要它）
--------------------
TokenHub 每个 Key 限流 **QPM=60**（已实测，见 `tokenhub.py`）。
单 Key 场景下，Agent1 的出题吞吐上限被这个数字锁死，跟仓库池大小、
worker 并发数都无关 —— 这是「10 道 → 1 万/100 万道」链路里最先被
撑爆的瓶颈，必须先解决，否则加多少 worker 都只是在同一个限流桶里排队。

方案
----
每个 Key 独立计入平台的 QPM 配额，因此 **N 个 Key = N 倍吞吐上限**
（线性扩展，无需修改限流逻辑本身）。`TokenHubClientPool` 按「租给
哪个 worker」做粘性分配（sticky lease）：同一个 worker 在其生命周期内
固定使用同一个 Key 对应的 `TokenHubClient`，避免多线程共享同一个
`openai.OpenAI` 实例时的隐藏状态风险，同时让每个 Key 的用量/失败率
可独立观测（`per_key_summary()`）。

Key 来源：环境变量 `TOKENHUB_API_KEYS`（逗号分隔多个 Key）。
未设置时回退到单 Key `TOKENHUB_API_KEY`（即池大小=1，行为与旧代码一致，
完全向后兼容）。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from .tokenhub import LLMUsage, TokenHubClient

__all__ = ["TokenHubClientPool"]


class TokenHubClientPool:
    """按 Key 数量线性扩展吞吐的客户端池。"""

    def __init__(
        self,
        *,
        keys: list[str] | None = None,
        keys_env: str = "TOKENHUB_API_KEYS",
        single_key_env: str = "TOKENHUB_API_KEY",
        model: str | None = None,
        default_max_tokens: int = 8192,
        base_url: str | None = None,
        timeout: int = 300,
        max_retries: int = 4,
    ) -> None:
        if keys is None:
            raw = os.environ.get(keys_env, "").strip()
            if raw:
                keys = [k.strip() for k in raw.split(",") if k.strip()]
            else:
                single = os.environ.get(single_key_env, "").strip()
                keys = [single] if single else []
        if not keys:
            raise ValueError(
                f"未配置任何 Key：设 {keys_env}（逗号分隔多个）或 {single_key_env}（单个）"
            )

        self._clients: list[TokenHubClient] = [
            TokenHubClient(
                api_key=k, base_url=base_url, model=model,
                timeout=timeout, max_retries=max_retries,
                default_max_tokens=default_max_tokens,
            )
            for k in keys
        ]
        self._key_suffixes = [_mask(k) for k in keys]
        self._n = len(self._clients)
        self._lock = threading.Lock()
        self._next_idx = 0
        # 聚合口径的用量对象：供 report.json 里"看起来像单客户端"的旧字段兼容
        self.usage = LLMUsage()

    def __len__(self) -> int:
        return self._n

    # ------------------------------------------------------------ 对外
    def lease(self) -> TokenHubClient:
        """粘性租借一个 Key 对应的客户端（轮询分配，一个 worker 用到底）。

        调用方（如 ThreadPoolExecutor 里的每个 repo worker）应在自己的
        生命周期开始时调用一次并复用返回的 client，而不是每次 LLM 调用
        都重新 lease —— 否则失去了「Key 粘性」带来的可观测性与稳定性。
        """
        with self._lock:
            c = self._clients[self._next_idx % self._n]
            self._next_idx += 1
            return c

    def refresh_usage(self) -> LLMUsage:
        """把各 Key 客户端的用量聚合进 `self.usage` 并返回（调用侧统计报告用）。"""
        agg = LLMUsage()
        for c in self._clients:
            agg.merge_from(c.usage)
        self.usage = agg
        return agg

    def per_key_summary(self) -> list[dict[str, Any]]:
        """逐 Key 用量（用于发现某个 Key 被限流/失败率异常高）。"""
        out = []
        for i, c in enumerate(self._clients):
            s = c.usage.summary()
            s["key_index"] = i
            s["key_suffix"] = self._key_suffixes[i]
            out.append(s)
        return out

    def cost_estimate(self, price_in: float, price_out: float) -> float:
        return self.refresh_usage().cost_estimate(price_in, price_out)


def _mask(key: str) -> str:
    if not key or len(key) < 8:
        return "****"
    return f"...{key[-4:]}"
