"""重构题（C 类）的静态指标度量与「重构守卫测试」生成

为什么重构题需要单独设计判据
----------------------------
挖空题（A）与模块添加题（B）的判据很自然：功能没实现 → 测试红，实现了 → 测试绿。
但**重构题的定义就是「行为不变」** —— 既有测试在重构前后都应该是绿的，
于是「没有任何测试会由红变绿」，`FAIL_TO_PASS` 无从产生，题目也就无法自动判分。

解法：把「重构质量」本身变成可执行的测试
--------------------------------------
本模块自动生成一个 `重构守卫测试` 文件，用 AST 静态度量目标函数：

    · 目标函数有效代码行数        必须降到阈值以内
    · 目标函数圈复杂度            必须降到阈值以内
    · 模块内任一函数的体量上限    防止「把逻辑整体搬到另一个巨型函数」的伪重构
    · 目标函数签名保持不变        否则调用方全废，谈不上行为等价

于是判据变得完整且自动：
    FAIL_TO_PASS = 守卫测试（重构前必红，重构后必绿）
    PASS_TO_PASS = 仓库既有测试（全程必绿 → 这就是「行为等价」的保证）

⭐ 关键设计原则：**阈值由 golden 态实测反推，而不是拍脑袋定**
------------------------------------------------------------
阈值取「参考重构的实测指标」加一点余量，并强制小于重构前的指标。
这样在数学上保证：golden 态必绿（实测值 ≤ 阈值）、重构前必红（原值 > 阈值）。
若拍一个固定阈值，极可能出现「参考答案自己都过不了」的废题。

守卫测试与本模块共用同一套度量函数（通过 `inspect.getsource` 内联进生成的文件），
避免「出题时算的指标」与「判分时算的指标」不一致这类隐蔽错误。
"""

from __future__ import annotations

import ast
import inspect
import math
from dataclasses import dataclass, field

__all__ = [
    "CodeMetrics", "GuardThresholds", "measure_symbol", "measure_module_max",
    "derive_thresholds", "render_guard_test", "guard_test_path",
]


# ==================================================================== 度量函数
#
# ⚠️ 以下 5 个函数会被**整段内联**到生成的守卫测试文件中（inspect.getsource）。
#    因此它们必须自包含：只允许依赖 `ast` 标准库，不得引用本模块的其它名字。


def _iter_defs(node, prefix: str = ""):
    """深度优先遍历所有 def / class，产出 (symbol_path, node)。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            path = f"{prefix}{child.name}"
            yield path, child
            yield from _iter_defs(child, path + ".")


def _find_def(tree, symbol_path: str):
    """按点分路径定位符号，找不到返回 None。"""
    for path, node in _iter_defs(tree):
        if path == symbol_path:
            return node
    return None


def _effective_body_lines(node, src_lines) -> int:
    """函数体的**有效代码行数**：排除 docstring、空行与纯注释行。

    用有效行而非物理行，是为了让指标反映真实复杂度，
    避免「把代码压成一行」或「删注释」这类作弊式优化。
    """
    body = list(getattr(node, "body", []) or [])
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return 0
    start = min(s.lineno for s in body)
    end = max((getattr(s, "end_lineno", None) or s.lineno) for s in body)
    n = 0
    for raw in src_lines[start - 1 : end]:
        s = raw.strip()
        if s and not s.startswith("#"):
            n += 1
    return n


def _cyclomatic(node) -> int:
    """圈复杂度（McCabe 近似）：1 + 判定点个数。

    统计 if / for / while / except / assert / 三元表达式 / 布尔短路 /
    推导式（含其 if 子句）/ match 分支。
    嵌套函数计入 —— 它们仍属于本函数的复杂度；
    而重构时把逻辑提取到**模块级**函数则不再计入，这正是我们要鼓励的方向。
    """
    n = 1
    for sub in ast.walk(node):
        if isinstance(sub, (ast.If, ast.For, ast.AsyncFor, ast.While,
                            ast.ExceptHandler, ast.Assert, ast.IfExp)):
            n += 1
        elif isinstance(sub, ast.BoolOp):
            n += max(0, len(sub.values) - 1)
        elif isinstance(sub, ast.comprehension):
            n += 1 + len(sub.ifs)
        elif sub.__class__.__name__ == "match_case":
            n += 1
    return n


def _arg_names(node) -> list[str]:
    """签名的参数名序列（`*args` / `**kwargs` 带前缀，保留顺序）。"""
    a = getattr(node, "args", None)
    if a is None:
        return []
    names = [p.arg for p in (getattr(a, "posonlyargs", None) or [])]
    names += [p.arg for p in a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [p.arg for p in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return names


_METRIC_FUNCS = (_iter_defs, _find_def, _effective_body_lines, _cyclomatic, _arg_names)


# ==================================================================== 数据结构


@dataclass
class CodeMetrics:
    """一个函数的静态复杂度画像。"""

    body_lines: int
    cyclomatic: int
    args: list[str] = field(default_factory=list)
    exists: bool = True

    def to_dict(self) -> dict:
        return {"body_lines": self.body_lines, "cyclomatic": self.cyclomatic,
                "args": list(self.args)}


@dataclass
class GuardThresholds:
    """守卫测试的阈值（由 before/after 实测反推）。"""

    max_target_body_lines: int
    max_target_complexity: int
    max_any_func_body_lines: int
    expected_args: list[str]
    before: CodeMetrics
    after: CodeMetrics

    def to_dict(self) -> dict:
        return {
            "max_target_body_lines": self.max_target_body_lines,
            "max_target_complexity": self.max_target_complexity,
            "max_any_func_body_lines": self.max_any_func_body_lines,
            "expected_args": list(self.expected_args),
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


class NotRefactorable(ValueError):
    """该重构不满足「实质性简化」的最低要求，不能作为题目。"""


# ==================================================================== 度量入口


def measure_symbol(source: str, symbol_path: str) -> CodeMetrics:
    """度量源码中某个符号的复杂度。符号不存在时返回 `exists=False`。"""
    tree = ast.parse(source)
    node = _find_def(tree, symbol_path)
    if node is None or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return CodeMetrics(body_lines=0, cyclomatic=0, exists=False)
    lines = source.splitlines()
    return CodeMetrics(
        body_lines=_effective_body_lines(node, lines),
        cyclomatic=_cyclomatic(node),
        args=_arg_names(node),
    )


def measure_module_max(source: str) -> int:
    """模块内所有函数的最大有效体行数（用于「防搬家」阈值）。"""
    tree = ast.parse(source)
    lines = source.splitlines()
    return max(
        (_effective_body_lines(n, lines)
         for _, n in _iter_defs(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        default=0,
    )


# ==================================================================== 阈值推导


def derive_thresholds(
    before_source: str,
    after_source: str,
    symbol_path: str,
    *,
    min_body_drop: int = 3,
    slack_ratio: float = 0.25,
) -> GuardThresholds:
    """由「重构前 / 重构后」的实测指标反推守卫阈值。

    阈值落在 `[after, before)` 区间内（偏向 after 一侧），从而同时保证：
      · golden 态必绿：`after <= 阈值`
      · 重构前必红：`before > 阈值`

    `slack_ratio` 给做题者留出「不必和参考重构一样极致」的余量。

    Raises
    ------
    NotRefactorable
        参考重构没有带来实质性简化（行数与复杂度都没明显下降），
        或改动了签名 / 删除了目标函数 —— 这样的重构不能作为题目。
    """
    before = measure_symbol(before_source, symbol_path)
    after = measure_symbol(after_source, symbol_path)

    if not before.exists:
        raise NotRefactorable(f"重构前找不到目标符号 {symbol_path}")
    if not after.exists:
        raise NotRefactorable(f"重构后目标符号 {symbol_path} 消失了（不允许删除或改名）")
    if after.args != before.args:
        raise NotRefactorable(
            f"重构改变了签名：{before.args} → {after.args}。"
            f"签名必须保持不变，否则调用方全部失效"
        )

    body_drop = before.body_lines - after.body_lines
    cx_drop = before.cyclomatic - after.cyclomatic
    if body_drop < min_body_drop and cx_drop < 2:
        raise NotRefactorable(
            f"参考重构没有实质性简化目标函数："
            f"有效行 {before.body_lines}→{after.body_lines}（需减少 ≥{min_body_drop}），"
            f"圈复杂度 {before.cyclomatic}→{after.cyclomatic}（需减少 ≥2）"
        )

    def pick(b: int, a: int, floor_gap: int = 1) -> int:
        """在 [a, b-1] 内取阈值，偏向 a 一侧留少量余量。"""
        if b - a <= floor_gap:
            return max(a, b - 1)
        return min(b - 1, a + int(math.floor((b - a) * slack_ratio)))

    max_body = pick(before.body_lines, after.body_lines)
    max_cx = pick(before.cyclomatic, after.cyclomatic)
    # 「防搬家」阈值只需保证 golden 态绿：取重构后模块内实测最大值 + 15% 余量
    after_module_max = measure_module_max(after_source)
    max_any = max(after_module_max + 2, int(math.ceil(after_module_max * 1.15)))

    return GuardThresholds(
        max_target_body_lines=max_body,
        max_target_complexity=max_cx,
        max_any_func_body_lines=max_any,
        expected_args=list(before.args),
        before=before,
        after=after,
    )


# ==================================================================== 守卫测试生成

_GUARD_TMPL = '''\
"""重构质量守卫（自动生成，请勿修改）—— {task_id}

本文件由 SWE-Synth 自动生成，为「重构题」提供可自动判分的判据。

分工：
  · 仓库**既有测试**保证「行为等价」（重构前后都必须全绿）
  · 本文件用**静态指标**保证「确实重构了」（重构前必红，重构后必绿）

阈值来自参考重构的实测值加余量，因此一定存在可行解。
"""

import ast
from pathlib import Path

# ---- 判据参数（由出题流水线依据 before/after 实测值反推）
TARGET_FILE = {target_file!r}
SYMBOL = {symbol!r}
MAX_TARGET_BODY_LINES = {max_body}
MAX_TARGET_COMPLEXITY = {max_cx}
MAX_ANY_FUNC_BODY_LINES = {max_any}
EXPECTED_ARGS = {expected_args!r}
BEFORE = {before!r}


{metrics}

def _repo_root() -> Path:
    """从本文件位置向上找到含有 TARGET_FILE 的目录，即仓库根。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / TARGET_FILE).is_file():
            return parent
    raise AssertionError("定位不到仓库根目录：找不到 " + TARGET_FILE)


def _load():
    src = (_repo_root() / TARGET_FILE).read_text(encoding="utf-8")
    return src.splitlines(), ast.parse(src)


def _target():
    lines, tree = _load()
    node = _find_def(tree, SYMBOL)
    assert node is not None, (
        "目标符号 " + SYMBOL + " 不存在了。重构不得删除或重命名它。"
    )
    return lines, tree, node


def test_signature_unchanged():
    """签名必须保持不变 —— 否则所有调用方失效，谈不上行为等价。"""
    _, _, node = _target()
    got = _arg_names(node)
    assert got == EXPECTED_ARGS, (
        "签名被改动了：期望 %r，实际 %r。重构必须保持对外契约不变。"
        % (EXPECTED_ARGS, got)
    )


def test_target_function_is_simplified():
    """目标函数的有效代码行数必须降下来（把职责拆分到更小的单元中）。"""
    lines, _, node = _target()
    n = _effective_body_lines(node, lines)
    assert n <= MAX_TARGET_BODY_LINES, (
        "%s 的函数体仍有 %d 行有效代码（重构前 %d 行），要求不超过 %d 行。"
        "请将独立职责提取为单独的函数/方法，而不是原地压缩代码。"
        % (SYMBOL, n, BEFORE["body_lines"], MAX_TARGET_BODY_LINES)
    )


def test_target_function_complexity_reduced():
    """目标函数的圈复杂度必须降下来（分支/循环/短路判断更少）。"""
    _, _, node = _target()
    cx = _cyclomatic(node)
    assert cx <= MAX_TARGET_COMPLEXITY, (
        "%s 的圈复杂度仍为 %d（重构前 %d），要求不超过 %d。"
        "请减少嵌套分支，把条件判断收敛到辅助函数中。"
        % (SYMBOL, cx, BEFORE["cyclomatic"], MAX_TARGET_COMPLEXITY)
    )


def test_no_new_monolithic_function():
    """防伪重构：不允许把复杂度整体搬运到另一个巨型函数里。"""
    lines, tree = _load()
    offenders = [
        (path, _effective_body_lines(node, lines))
        for path, node in _iter_defs(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _effective_body_lines(node, lines) > MAX_ANY_FUNC_BODY_LINES
    ]
    assert not offenders, (
        "以下函数体量超过 %d 行：%r。"
        "把代码整体搬到另一个大函数不算重构，请真正拆分职责。"
        % (MAX_ANY_FUNC_BODY_LINES, offenders)
    )
'''


def guard_test_path(task_id: str, tests_dir: str = "tests") -> str:
    """守卫测试的落盘路径。

    文件名带 `task_id`，既避免与仓库既有测试冲突，也便于在证据里追溯。
    """
    return f"{tests_dir}/test_refactor_guard_{task_id.replace('-', '_')}.py"


def render_guard_test(task_id: str, target_file: str, symbol: str,
                      th: GuardThresholds) -> str:
    """生成守卫测试文件内容。

    度量函数直接内联自本模块（`inspect.getsource`），
    确保「出题时算的指标」与「判分时算的指标」逐字节一致。
    """
    metrics = "\n\n".join(inspect.getsource(f).rstrip() for f in _METRIC_FUNCS)
    return _GUARD_TMPL.format(
        task_id=task_id,
        target_file=target_file.replace("\\", "/"),
        symbol=symbol,
        max_body=th.max_target_body_lines,
        max_cx=th.max_target_complexity,
        max_any=th.max_any_func_body_lines,
        expected_args=th.expected_args,
        before=th.before.to_dict(),
        metrics=metrics,
    )
