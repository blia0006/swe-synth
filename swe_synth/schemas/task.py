"""任务数据模型（交付物 `data/tasks.jsonl` 的唯一契约）

设计要点
--------
1. **强制校验题干结构**：课题验收标准明文要求题目「含上下文、输入输出、预期行为」。
   这里用 pydantic 把它变成**硬性校验** —— 缺任何一节直接拒绝，不靠人工检查。

2. **强制泄题审查**：题干若包含被挖函数的实现代码，题目就失去了意义。
   `problem_statement` 的校验器会拒绝含有实现特征的题干。

3. **字段与验收标准一一对应**（见 `requirements-check.md` §1.3）：
   题干描述 → `problem_statement`；TCR 镜像地址 → `image`/`solution_image`；
   验证脚本 → `verify_script`；通过证明 → `proof_dir` + `validation`。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "TaskType", "Difficulty", "TaskState", "Language",
    "ValidationInfo", "OverlapCheck", "GeneratedBy", "SweTask",
    "REQUIRED_SECTIONS", "write_jsonl", "read_jsonl",
]


class TaskType(str, Enum):
    """课题原文列举的三种题型，最终数据集需三类都有覆盖。"""

    FEATURE_IMPLEMENTATION = "feature_implementation"   # A 功能实现（AST 挖空）
    MODULE_ADDITION = "module_addition"                 # B 模块添加
    REFACTORING = "refactoring"                         # C 重构


class Difficulty(str, Enum):
    EASY = "easy"        # 单函数、逻辑集中
    MEDIUM = "medium"    # 单函数但逻辑较复杂，或跨 2~3 处
    HARD = "hard"        # 新模块 / 多文件协作


class Language(str, Enum):
    PYTHON = "python"
    GO = "go"
    TYPESCRIPT = "typescript"
    RUST = "rust"


class TaskState(str, Enum):
    """流水线状态机（支持断点续跑，落库于 data/state.db）。"""

    SELECTED = "SELECTED"              # 仓库已选定
    DESIGNED = "DESIGNED"              # LLM 已出题
    PATCHED = "PATCHED"                # stub / golden patch 已生成
    LOCAL_OK = "LOCAL_OK"              # 本地双向 sanity 通过
    IMAGE_PUSHED = "IMAGE_PUSHED"      # 镜像已推送到仓库
    SANDBOX_OK = "SANDBOX_OK"          # 沙箱内验证通过
    OVERLAP_OK = "OVERLAP_OK"          # 与现有 PR/commit 无重叠
    ACCEPTED = "ACCEPTED"              # 全流程通过，入数据集
    REJECTED = "REJECTED"              # 任一环节失败


# 题干必须包含的小节标题（与验收标准逐条对应）
REQUIRED_SECTIONS: dict[str, str] = {
    "背景与上下文": "上下文（验收标准明文要求）",
    "需要实现的功能": "任务描述",
    "输入": "输入说明（验收标准明文要求）",
    "输出": "输出说明（验收标准明文要求）",
    "预期行为": "预期行为（验收标准明文要求）",
    "约束条件": "约束（如不得修改测试文件）",
}

# 泄题特征：题干里出现完整实现代码
_LEAK_PATTERNS = [
    # 题干中不应出现 def/class 定义体（签名可以，但不应有实现）
    (re.compile(r"^\s*(def|async def)\s+\w+\s*\([^)]*\)\s*(->[^:]+)?:\s*\n\s+(?!\.\.\.|pass|\"\"\")\S",
                re.M), "题干包含函数实现代码"),
    (re.compile(r"\breturn\s+\w+.*\n.*\breturn\s+", re.M), "题干疑似包含多条 return 语句（实现细节）"),
]


class GeneratedBy(BaseModel):
    """出题溯源信息（可复核性要求）。"""

    model: str
    agent: str = "agent1"
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ValidationInfo(BaseModel):
    """Agent2 的验证结果 —— 即验收要求的「通过证明」。"""

    # 本地阶段（Agent1 自校验，省沙箱成本）
    local_sanity_passed: bool = False
    local_duration_sec: float | None = None

    # 沙箱阶段（Agent2）
    sandbox_tool: str | None = None
    sandbox_instance_id: str | None = None
    empty_solution_result: str | None = None    # 期望 "fail"：不改代码必须跑不过
    golden_solution_result: str | None = None   # 期望 "pass"：打入 golden patch 必须过
    deterministic: bool | None = None
    duration_sec: float | None = None

    # 可选加分项：让 LLM 真实解一次，用于难度标定
    llm_attempt: dict[str, Any] | None = None

    proof_dir: str | None = None

    @property
    def fully_verified(self) -> bool:
        """双向 sanity 全部满足才算真正验证通过。"""
        return (
            self.local_sanity_passed
            and self.empty_solution_result == "fail"
            and self.golden_solution_result == "pass"
        )


class OverlapCheck(BaseModel):
    """去污染校验结果（核心约束：题目不得与现有 issue/PR/commit/bugfix 重叠）。"""

    passed: bool = False
    method: str = "github_search+bm25+embedding+llm_judge"
    queried: list[str] = Field(default_factory=list)
    top_candidates: list[dict[str, Any]] = Field(default_factory=list)
    target_file_last_modified: str | None = None
    months_since_last_change: float | None = None
    verdict_reason: str | None = None


class SweTask(BaseModel):
    """一道 SWE 题目 —— `tasks.jsonl` 的一行。"""

    task_id: str = Field(pattern=r"^swe-synth-\d{4}$")
    task_type: TaskType
    difficulty: Difficulty
    state: TaskState = TaskState.SELECTED

    # ---- 来源仓库（课题要求 Star>100，此处留证）
    repo: str = Field(pattern=r"^[\w.\-]+/[\w.\-]+$")
    repo_stars: int = Field(ge=100, description="课题硬性要求 Star>100")
    base_commit: str = Field(min_length=7)
    language: Language = Language.PYTHON

    # ---- 题目本体
    problem_statement: str = Field(min_length=200)
    hints: str | None = None
    modified_files: list[str] = Field(min_length=1)
    do_not_modify: list[str] = Field(default_factory=list)

    # ---- 判据
    test_cmd: str
    FAIL_TO_PASS: list[str] = Field(min_length=1, description="挖空态必须失败的用例")
    PASS_TO_PASS: list[str] = Field(default_factory=list, description="全程必须通过的用例")

    # ---- 镜像与验证入口
    image: str | None = None
    solution_image: str | None = None
    verify_script: str = "/task/verify.sh"

    # ---- 元信息
    generated_by: GeneratedBy | None = None
    validation: ValidationInfo = Field(default_factory=ValidationInfo)
    overlap_check: OverlapCheck = Field(default_factory=OverlapCheck)
    reject_reason: str | None = None

    # ---------------------------------------------------------- 校验器

    @field_validator("problem_statement")
    @classmethod
    def _check_structure_and_leak(cls, v: str) -> str:
        """强制题干结构完整 + 无泄题。这是验收标准的硬性要求，不能只靠人工抽检。"""
        missing = [
            desc for key, desc in REQUIRED_SECTIONS.items()
            if key not in v
        ]
        if missing:
            raise ValueError(
                "题干缺少必需小节：" + "; ".join(missing)
                + "。验收标准要求题目必须含上下文、输入输出、预期行为"
            )
        for pat, msg in _LEAK_PATTERNS:
            if pat.search(v):
                raise ValueError(f"题干疑似泄题：{msg}")
        return v

    @field_validator("FAIL_TO_PASS", "PASS_TO_PASS")
    @classmethod
    def _no_dup(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("测试用例列表存在重复项")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> SweTask:
        # F2P 与 P2P 不能有交集 —— 同一个用例不可能既要红又要绿
        overlap = set(self.FAIL_TO_PASS) & set(self.PASS_TO_PASS)
        if overlap:
            raise ValueError(f"FAIL_TO_PASS 与 PASS_TO_PASS 重叠：{sorted(overlap)[:3]}")

        # 防作弊：**三种题型一律**要求判据测试文件列入不可修改清单，
        # 否则改测试就能"通过"。B 类的新测试、C 类的重构守卫测试同样必须受保护。
        test_files = {n.split("::", 1)[0] for n in self.FAIL_TO_PASS}
        not_protected = test_files - set(self.do_not_modify)
        if not_protected:
            raise ValueError(
                f"判据测试文件未列入 do_not_modify（可被改测试作弊）：{sorted(not_protected)}"
            )

        # 待修改文件不能同时被声明为不可修改 —— 这样的题目自相矛盾，做题者无从下手
        contradiction = set(self.modified_files) & set(self.do_not_modify)
        if contradiction:
            raise ValueError(
                f"modified_files 与 do_not_modify 冲突：{sorted(contradiction)}"
            )

        # 重构题的行为等价必须有机器证明，即至少要有既有测试作为 PASS_TO_PASS
        if self.task_type == TaskType.REFACTORING and not self.PASS_TO_PASS:
            raise ValueError(
                "重构题必须有 PASS_TO_PASS（既有测试全程保持通过），"
                "否则无法证明重构前后行为等价"
            )

        # ACCEPTED 状态必须具备完整证据链
        if self.state == TaskState.ACCEPTED:
            if not self.image:
                raise ValueError("ACCEPTED 的题目必须有镜像地址（验收要求）")
            if not self.validation.fully_verified:
                raise ValueError("ACCEPTED 的题目必须通过双向 sanity（空解 fail + golden pass）")
            if not self.overlap_check.passed:
                raise ValueError("ACCEPTED 的题目必须通过无重叠校验（课题核心约束）")
            if not self.validation.proof_dir:
                raise ValueError("ACCEPTED 的题目必须有通过证明目录（验收要求）")
        return self

    # ---------------------------------------------------------- 序列化

    def to_jsonl_line(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)


def write_jsonl(tasks: list[SweTask], path: str | Path, *, append: bool = False) -> int:
    """把任务写入 JSON Lines 文件（验收要求的落盘格式）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8") as f:
        for t in tasks:
            f.write(t.to_jsonl_line() + "\n")
    return len(tasks)


def read_jsonl(path: str | Path) -> list[SweTask]:
    """读回并逐行校验（用于交付前自检：确保每行都合法）。"""
    out: list[SweTask] = []
    p = Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(SweTask.model_validate_json(line))
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"{p}:{i} 行不是合法的 SweTask：{e}") from e
    return out
