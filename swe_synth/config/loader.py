"""配置加载（settings.yaml + repos.yaml + .env）

约定
----
· 可调参数放 YAML（可提交、可复现）
· 凭证只从环境变量 / .env 读（绝不入库）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Settings", "RepoSpec", "load_settings", "load_repos", "PROJECT_ROOT"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("缺少 PyYAML：pip install -r requirements.txt") from e
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_dotenv_if_present(root: Path | None = None) -> None:
    """加载 .env（不覆盖已存在的环境变量）。"""
    env_path = (root or PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except ImportError:
        with env_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


@dataclass
class RepoSpec:
    """一个候选仓库的规格。"""

    name: str
    language: str
    stars: int
    clone_url: str
    install: list[str] = field(default_factory=list)
    test_cmd: str = "pytest -q"
    test_extras: list[str] = field(default_factory=list)
    fast_targets: list[str] = field(default_factory=list)
    verified: bool = False
    priority: str = "normal"
    notes: str = ""

    def __post_init__(self) -> None:
        # 课题硬性要求，配置层就拦住
        if self.stars < 100:
            raise ValueError(f"{self.name} Star={self.stars} < 100，不符合课题要求")


@dataclass
class Settings:
    """流水线全局配置。"""

    raw: dict[str, Any]

    # ---- 便捷访问器（带默认值，避免配置缺项直接崩）
    def get(self, path: str, default: Any = None) -> Any:
        """按 `a.b.c` 路径取值。"""
        cur: Any = self.raw
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def model_task_design(self) -> str:
        return self.get("models.task_design", "deepseek-v4-pro-202606")

    @property
    def model_repo_analyze(self) -> str:
        return self.get("models.repo_analyze", "deepseek-v4-flash")

    @property
    def model_overlap_judge(self) -> str:
        return self.get("models.overlap_judge", "deepseek-v4-flash")

    @property
    def max_tokens(self) -> int:
        return int(self.get("llm.max_tokens", 8192))

    @property
    def target_total(self) -> int:
        return int(self.get("task_mix.target_total", 12))

    @property
    def tasks_jsonl(self) -> Path:
        return PROJECT_ROOT / str(self.get("output.tasks_jsonl", "data/tasks.jsonl"))

    @property
    def proofs_dir(self) -> Path:
        return PROJECT_ROOT / str(self.get("output.proofs_dir", "data/proofs"))

    @property
    def state_db(self) -> Path:
        return PROJECT_ROOT / str(self.get("output.state_db", "data/state.db"))

    def price_of(self, model: str) -> tuple[float, float]:
        """返回 (输入价, 输出价)，单位：元/百万 tokens。"""
        p = self.get(f"models.pricing.{model}") or {}
        return float(p.get("input", 0.0)), float(p.get("output", 0.0))

    # ---- 镜像地址拼装（registry 类型可配置，便于个人版/企业版切换）
    def image_ref(self, task_id: str, *, solution: bool = False) -> str:
        registry = os.environ.get("TCR_REGISTRY", "").rstrip("/")
        namespace = os.environ.get("TCR_NAMESPACE", "")
        if not registry or not namespace:
            raise RuntimeError("未配置 TCR_REGISTRY / TCR_NAMESPACE（见 .env）")
        tag = self.get("image.tag_solution", "v1-sol") if solution else self.get("image.tag_task", "v1")
        return f"{registry}/{namespace}/{task_id}:{tag}"


def load_settings(path: str | Path | None = None) -> Settings:
    load_dotenv_if_present()
    p = Path(path) if path else PROJECT_ROOT / "config" / "settings.yaml"
    return Settings(raw=_load_yaml(p))


def load_repos(path: str | Path | None = None, *, only_verified: bool = False) -> list[RepoSpec]:
    p = Path(path) if path else PROJECT_ROOT / "config" / "repos.yaml"
    data = _load_yaml(p)
    defaults = data.get("defaults") or {}
    out: list[RepoSpec] = []
    for item in data.get("repos") or []:
        merged = {**{k: v for k, v in defaults.items() if k in RepoSpec.__dataclass_fields__}, **item}
        merged.pop("python_version", None)
        merged.pop("test_framework", None)
        merged.pop("baseline_required", None)
        spec = RepoSpec(**merged)
        if only_verified and not spec.verified:
            continue
        out.append(spec)
    return out
