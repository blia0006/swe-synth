"""TokenHub LLM 客户端（OpenAI 兼容协议）

针对实测踩到的坑做了防御
------------------------
1. **推理模型的思维链会吃掉 max_tokens**（已实测）：
   `deepseek-v4-pro*` / `glm-5` 等先生成 `reasoning_content` 再生成 `content`。
   若 `max_tokens` 太小，思维链占满后 `content` 返回**空字符串**，
   而 HTTP 200 + usage 正常 —— 看起来"调用成功"实际拿不到内容。
   → 因此：默认 `max_tokens` 给足；**空内容或 finish_reason=length 一律视为失败并重试**。

2. **QPM=60 限流**（已实测）：所有模型每分钟 60 次，需退避重试。

3. **成本可观测**：思维链也计入 `completion_tokens` 计费，必须累计统计，
   否则实际花费会显著超出按输出长度的预估。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["LLMError", "EmptyContentError", "LLMUsage", "TokenHubClient"]


class LLMError(RuntimeError):
    """LLM 调用失败（已重试耗尽）。"""


class EmptyContentError(LLMError):
    """调用成功但 content 为空 —— 通常是推理模型思维链占满了 max_tokens。"""


@dataclass
class LLMUsage:
    """累计用量与成本统计。

    多 Key 并发场景下，多个 worker 线程可能共享同一个底层 `_client`
    （或该对象被 `TokenHubClientPool` 聚合），故所有写操作都加锁，
    避免计数竞态导致统计失真。
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_chars: int = 0
    retries: int = 0
    failures: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, model: str, pt: int, ct: int, reasoning: int = 0) -> None:
        with self._lock:
            self.calls += 1
            self.prompt_tokens += pt
            self.completion_tokens += ct
            self.reasoning_chars += reasoning
            self.by_model[model] = self.by_model.get(model, 0) + 1

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1

    def merge_from(self, other: "LLMUsage") -> None:
        """把另一份用量累加进来（多 Key 池聚合各 Key 的统计用）。"""
        with self._lock, other._lock:
            self.calls += other.calls
            self.prompt_tokens += other.prompt_tokens
            self.completion_tokens += other.completion_tokens
            self.reasoning_chars += other.reasoning_chars
            self.retries += other.retries
            self.failures += other.failures
            for k, v in other.by_model.items():
                self.by_model[k] = self.by_model.get(k, 0) + v

    def cost_estimate(self, price_in: float, price_out: float) -> float:
        """按「元/百万 tokens」价格估算总成本。"""
        return round(
            self.prompt_tokens / 1e6 * price_in + self.completion_tokens / 1e6 * price_out, 4
        )

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_chars": self.reasoning_chars,
            "retries": self.retries,
            "failures": self.failures,
            "by_model": dict(self.by_model),
        }


class TokenHubClient:
    """TokenHub 封装：重试、退避、JSON 结构化输出、用量统计。"""

    # 已实测为推理模型（会返回 reasoning_content）的前缀
    _REASONING_PREFIXES = ("deepseek-v4", "deepseek-v3", "glm-5", "kimi", "hy3", "minimax")

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        *,
        timeout: int = 300,
        max_retries: int = 4,
        default_max_tokens: int = 8192,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise LLMError("缺少 openai 包：pip install -r requirements.txt") from e

        key = api_key or os.environ.get("TOKENHUB_API_KEY", "")
        if not key:
            raise LLMError(
                "未配置 TOKENHUB_API_KEY。"
                "控制台自助创建：https://console.cloud.tencent.com/tokenhub/apikey"
            )
        self.base_url = base_url or os.environ.get(
            "TOKENHUB_BASE_URL", "https://tokenhub.tencentmaas.com/v1"
        )
        self.model = model or os.environ.get("TOKENHUB_MODEL", "deepseek-v4-pro-202606")
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_max_tokens = default_max_tokens
        self.usage = LLMUsage()
        self._client = OpenAI(base_url=self.base_url, api_key=key, timeout=timeout)

    # ------------------------------------------------------------ 内部
    def is_reasoning_model(self, model: str | None = None) -> bool:
        m = (model or self.model).lower()
        return any(m.startswith(p) for p in self._REASONING_PREFIXES)

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        # QPM=60 → 触发限流时等待要够久；指数退避 + 上限
        time.sleep(min(2 ** attempt, 30))

    # ------------------------------------------------------------ 对外
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        allow_empty: bool = False,
    ) -> str:
        """发起一次对话，返回 content 文本。

        与裸 SDK 的区别：**空内容视为失败**并自动重试（提高 max_tokens），
        因为空内容在批量出题时会静默产出废题。
        """
        mdl = model or self.model
        mt = max_tokens or self.default_max_tokens
        last_err: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                kwargs: dict[str, Any] = {
                    "model": mdl,
                    "messages": messages,
                    "max_tokens": mt,
                }
                if temperature is not None:
                    kwargs["temperature"] = temperature
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                rsp = self._client.chat.completions.create(**kwargs)
                choice = rsp.choices[0]
                content = (choice.message.content or "").strip()
                reasoning = getattr(choice.message, "reasoning_content", None) or ""
                u = getattr(rsp, "usage", None)
                self.usage.add(
                    mdl,
                    getattr(u, "prompt_tokens", 0) or 0,
                    getattr(u, "completion_tokens", 0) or 0,
                    len(reasoning),
                )

                if not content and not allow_empty:
                    # 思维链占满额度的典型症状：finish_reason == "length"
                    self.usage.record_retry()
                    last_err = EmptyContentError(
                        f"content 为空（finish_reason={choice.finish_reason}，"
                        f"reasoning {len(reasoning)} 字符）"
                    )
                    mt = min(mt * 2, 32768)   # 加倍 max_tokens 后重试
                    continue
                return content

            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e).lower()
                # 限流 / 服务端瞬时错误 → 退避重试；其余直接失败，避免无谓等待
                if any(k in msg for k in ("rate", "429", "timeout", "502", "503", "504", "overload")):
                    self.usage.record_retry()
                    self._sleep_backoff(attempt)
                    continue
                if isinstance(e, EmptyContentError):
                    continue
                break

        self.usage.record_failure()
        raise LLMError(f"LLM 调用失败（重试 {self.max_retries} 次）：{last_err}") from last_err

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """要求模型返回 JSON 对象，并解析为 dict。

        即使开了 `response_format=json_object`，推理模型偶尔仍会带 ```json 包裹，
        故做一层容错提取。
        """
        raw = self.chat(
            messages, model=model, max_tokens=max_tokens,
            temperature=temperature, json_mode=True,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 容错：从 ```json ... ``` 或首个 {...} 中提取
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
        if not m:
            m = re.search(r"(\{.*\})", raw, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError as e:
                raise LLMError(f"模型返回的 JSON 无法解析：{raw[:300]}") from e
        raise LLMError(f"模型未返回 JSON：{raw[:300]}")
