"""仓库分析：从代码库中找出「适合出题的挖空靶点」

判断一个函数是否适合作为 SWE 题目，需要同时满足两类条件：

1. **可挖空**（语法层面）—— 由 `stubber.stub_symbol` 判定
2. **有测试覆盖**（判据层面）—— 挖空后必须有测试会失败，否则无法自动判分

第 2 点是关键，也是本模块的主要工作。这里采用**静态引用分析**做粗筛：
在测试文件中检索对目标符号的引用（import / 属性访问 / 调用），
把「引用了该符号的测试文件」作为 `FAIL_TO_PASS` 的候选集合，
最终由 `local_validator` 真实跑一遍测试来确认（静态分析只负责减少无效尝试）。

为什么不直接用覆盖率工具（coverage.py）？
    覆盖率更精确，但需要先把仓库依赖装好并成功跑通全量测试，代价高、失败率高。
    静态粗筛 + 真实跑测试验证 的组合，在成功率与成本之间更平衡。
    （后续可选增强：对已通过基线的仓库跑一次 `coverage --context` 建立精确映射）
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .stubber import NotStubbable, SymbolNotFound, list_symbols, stub_symbol

__all__ = ["Candidate", "SourceFile", "analyze_repo", "find_test_files", "DEFAULT_EXCLUDES"]

# 这些目录不参与出题（要么不是业务代码，要么改动无意义）
DEFAULT_EXCLUDES = {
    ".git", ".github", ".venv", "venv", "env", "__pycache__", ".tox", ".nox",
    "build", "dist", "docs", "doc", "examples", "example", "benchmarks",
    "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    "migrations", "vendor", "third_party", "scripts",
}

# 测试文件命名约定
_TEST_FILE_RE = re.compile(r"(^test_.*\.py$)|(.*_test\.py$)|(^conftest\.py$)")

# 不适合出题的函数名模式：私有魔法方法、简单存取器、入口点
_SKIP_NAME_RE = re.compile(
    r"^(__\w+__|_{1,2}repr\w*|main|cli|setup|teardown)$"
)

# 这些装饰器意味着函数不是「纯实现」，挖空后行为不可预期
_SKIP_DECORATORS = {
    "abstractmethod", "abc.abstractmethod",
    "overload", "typing.overload",
    "abstractproperty",
}


@dataclass
class Candidate:
    """一个候选挖空靶点。"""

    rel_path: str                     # 相对仓库根的源文件路径
    symbol_path: str                  # 如 "utils.Session.request" 中的 "Session.request"
    kind: str                         # function / method / async_*
    body_line_count: int
    body_char_count: int
    docstring: str | None
    signature: str
    decorators: list[str] = field(default_factory=list)
    referencing_tests: list[str] = field(default_factory=list)  # 引用了该符号的测试文件
    score: float = 0.0                # 综合评分，越高越适合出题

    @property
    def has_test_coverage(self) -> bool:
        return bool(self.referencing_tests)

    def difficulty_hint(self) -> str:
        """按被挖掉的代码量粗略分档（最终难度还要结合跨文件情况）。"""
        n = self.body_line_count
        if n <= 5:
            return "easy"
        if n <= 20:
            return "medium"
        return "hard"


@dataclass
class SourceFile:
    rel_path: str
    source: str
    tree: ast.AST


# ------------------------------------------------------------------ 文件遍历

def _iter_py_files(root: Path, excludes: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地裁剪，避免进入被排除目录
        dirnames[:] = [d for d in dirnames if d not in excludes and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def find_test_files(root: Path, excludes: set[str] | None = None) -> list[Path]:
    """找出仓库中的测试文件。"""
    ex = (excludes or DEFAULT_EXCLUDES) - {"scripts"}  # 测试可能放在 scripts 下，放宽
    return [p for p in _iter_py_files(root, ex) if _TEST_FILE_RE.match(p.name)]


# ------------------------------------------------------------------ 测试引用索引

def _collect_referenced_names(source: str) -> set[str]:
    """收集一个测试文件里出现过的所有「名字」。

    覆盖三种引用形态：
        from mod import foo        → foo
        obj.method()               → method
        Klass(...)                 → Klass
    故意做得宽松（宁可多召回）—— 精确性由后续真实跑测试来保证。
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def _build_test_index(root: Path, excludes: set[str]) -> dict[str, set[str]]:
    """建立 {测试文件相对路径: 该文件引用的名字集合}。"""
    index: dict[str, set[str]] = {}
    for p in find_test_files(root, excludes):
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        index[str(p.relative_to(root))] = _collect_referenced_names(src)
    return index


# ------------------------------------------------------------------ 评分

def _score(c: Candidate) -> float:
    """候选靶点评分：偏好「有测试覆盖、体量适中、有 docstring」的函数。

    评分只用于排序（优先尝试最有希望的候选），不做硬性过滤。
    """
    s = 0.0

    # 有测试引用是最重要的信号 —— 没有测试就无法自动判分
    if c.referencing_tests:
        s += 50.0
        # 被多个测试文件引用 → 判据更充分，但收益递减
        s += min(len(c.referencing_tests), 3) * 5.0

    # 体量适中最好：太短没难度，太长容易牵连过多、题干难写清楚
    n = c.body_line_count
    if 4 <= n <= 25:
        s += 20.0
    elif n < 4:
        s += 5.0
    else:
        s += max(0.0, 15.0 - (n - 25) * 0.3)

    # 有 docstring：题干信息更充分，且能保留给被测 Agent 作为规格说明
    if c.docstring:
        s += 15.0
        if len(c.docstring) > 80:
            s += 5.0

    # 纯函数（非方法）通常依赖更少、更容易独立实现
    if "." not in c.symbol_path:
        s += 5.0

    # ⚠️ 单下划线开头的私有方法/函数：文档通常更少、上下文更隐晦，
    #    LLM 出题时更容易臆造不存在的行为（实测 `_cache_set` 连续 3 次出题
    #    都被一致性校验拦下，浪费 12 分钟）。降权让公开方法优先。
    tail = c.symbol_path.rsplit(".", 1)[-1]
    if tail.startswith("_") and not (tail.startswith("__") and tail.endswith("__")):
        s -= 12.0

    # 装饰器越多，行为越可能受外部影响（缓存/注册/重试），扣分
    s -= len(c.decorators) * 3.0

    return round(s, 2)


# ------------------------------------------------------------------ 主入口

def analyze_repo(
    repo_root: str | Path,
    *,
    excludes: set[str] | None = None,
    min_body_lines: int = 4,
    max_body_lines: int = 60,
    require_test_coverage: bool = True,
    limit: int | None = None,
) -> list[Candidate]:
    """扫描仓库，返回按评分降序排列的候选挖空靶点。

    参数
    ----
    min_body_lines / max_body_lines:
        函数体行数区间。下限避免出「一行题」，上限避免牵连过大、题干无法写清。
    require_test_coverage:
        只保留「有测试文件引用」的候选。**默认 True** —— 没有测试判据的题目
        无法自动判分，不符合课题要求。
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"仓库路径不存在：{root}")
    ex = excludes or DEFAULT_EXCLUDES

    test_index = _build_test_index(root, ex)
    candidates: list[Candidate] = []

    for path in _iter_py_files(root, ex):
        rel = str(path.relative_to(root))
        # 测试文件本身不出题（改测试等于改判据）
        if _TEST_FILE_RE.match(path.name):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            symbols = list_symbols(source)
        except SyntaxError:
            continue  # 语法不兼容当前 Python 版本的文件，跳过

        for symbol_path, node in symbols:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = symbol_path.rsplit(".", 1)[-1]
            if _SKIP_NAME_RE.match(name):
                continue
            # 私有函数（单下划线）通常不是公开契约，测试也少，优先级低但不排除
            try:
                res = stub_symbol(
                    source, symbol_path,
                    keep_docstring=True,
                    min_body_lines=min_body_lines,
                )
            except (NotStubbable, SymbolNotFound):
                continue

            if res.body_line_count > max_body_lines:
                continue
            if any(d in _SKIP_DECORATORS for d in res.decorators):
                continue

            refs = [tf for tf, names in test_index.items() if name in names]
            if require_test_coverage and not refs:
                continue

            c = Candidate(
                rel_path=rel,
                symbol_path=symbol_path,
                kind=res.kind,
                body_line_count=res.body_line_count,
                body_char_count=res.body_char_count,
                docstring=res.docstring,
                signature=res.signature,
                decorators=res.decorators,
                referencing_tests=refs,
            )
            c.score = _score(c)
            candidates.append(c)

    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates[:limit] if limit else candidates
