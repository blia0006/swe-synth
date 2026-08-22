"""C 类题型「重构」出题器

课题原文要求题型覆盖「功能实现 / **重构** / 模块添加」。本模块负责第二类。

重构题的核心矛盾与解法
--------------------
重构的定义就是「**行为不变**、结构变好」。于是既有测试在重构前后都是绿的，
「由红变绿的测试」不存在 —— `FAIL_TO_PASS` 无从产生，题目无法自动判分。

解法是把「重构质量」本身变成可执行判据（详见 `refactor_metrics`）：

    FAIL_TO_PASS = 自动生成的**重构守卫测试**（静态指标；重构前必红、重构后必绿）
    PASS_TO_PASS = 仓库**既有测试**（全程必绿 → 这就是「行为等价」的机器证明）

两者合起来，才完整表达了重构题的验收含义：既要真的改好，又不能改坏。

题目态 / golden 态
------------------
    题目态   = 原始源文件（一个字都不改）+ 守卫测试
    golden 态 = 参考重构后的源文件          + 守卫测试

注意题目态**不动业务代码** —— 这正是重构题的自然形态：代码就在那里，请把它改好。

阈值的可行性保证
--------------
守卫阈值由「参考重构的实测指标」反推（`derive_thresholds`），
因此数学上保证「参考答案一定能过、原始代码一定过不了」。
若 LLM 给出的重构没有实质简化，`NotRefactorable` 会直接打回重试 ——
绝不会产出「连参考答案自己都过不了」的废题。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..clients.tokenhub import LLMError, TokenHubClient
from ..schemas.task import REQUIRED_SECTIONS, Difficulty
from .refactor_metrics import (GuardThresholds, NotRefactorable, derive_thresholds,
                               measure_symbol, render_guard_test)
from .repo_analyzer import DEFAULT_EXCLUDES, _TEST_FILE_RE, _build_test_index
from .stubber import (NotStubbable, SymbolNotFound, extract_symbol_def, list_symbols,
                      replace_symbol_def)
from .task_designer import DesignError, _clean_statement, _looks_leaky

__all__ = ["RefactorTarget", "RefactorDraft", "find_refactor_targets",
           "design_refactor_task", "REFACTOR_PROMPT_VERSION"]

REFACTOR_PROMPT_VERSION = "refactor-v1"


# ------------------------------------------------------------------ 选靶点


@dataclass
class RefactorTarget:
    """一个「坏味道」重构靶点。"""

    rel_path: str
    symbol_path: str
    body_lines: int
    cyclomatic: int
    file_lines: int
    referencing_tests: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.rel_path}::{self.symbol_path}"


def find_refactor_targets(
    repo_root: str | Path,
    *,
    min_body_lines: int = 12,
    min_cyclomatic: int = 6,
    max_file_lines: int = 600,
    require_test_coverage: bool = True,
    limit: int | None = None,
) -> list[RefactorTarget]:
    """找出适合出重构题的「长且复杂」的函数。

    筛选条件的用意
    --------------
    · `min_body_lines` / `min_cyclomatic`：太简单的函数没有重构空间，
      改完指标降不下来，守卫阈值区间会退化成空集
    · `max_file_lines`：solve-back 与做题者都需要整体理解该文件；
      文件过大不仅费 token，也容易让重构失控
    · `require_test_coverage`：**必须**有既有测试，否则「行为等价」无从验证 ——
      这是重构题不可妥协的前提
    """
    import os

    root = Path(repo_root).resolve()
    test_index = _build_test_index(root, DEFAULT_EXCLUDES)
    out: list[RefactorTarget] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in DEFAULT_EXCLUDES and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py") or _TEST_FILE_RE.match(fn):
                continue
            path = Path(dirpath) / fn
            rel = str(path.relative_to(root))
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            n_lines = source.count("\n") + 1
            if n_lines > max_file_lines:
                continue
            try:
                symbols = list_symbols(source)
            except SyntaxError:
                continue

            for symbol_path, node in symbols:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                name = symbol_path.rsplit(".", 1)[-1]
                if name.startswith("__") and name.endswith("__"):
                    continue
                m = measure_symbol(source, symbol_path)
                if not m.exists:
                    continue
                if m.body_lines < min_body_lines or m.cyclomatic < min_cyclomatic:
                    continue
                refs = [tf for tf, names in test_index.items() if name in names]
                if require_test_coverage and not refs:
                    continue
                t = RefactorTarget(
                    rel_path=rel, symbol_path=symbol_path,
                    body_lines=m.body_lines, cyclomatic=m.cyclomatic,
                    file_lines=n_lines, referencing_tests=refs,
                )
                # 越复杂越值得重构；测试越多，行为等价的保障越强
                t.score = round(t.cyclomatic * 3.0 + t.body_lines * 1.0
                                + min(len(refs), 3) * 6.0, 2)
                out.append(t)

    out.sort(key=lambda x: x.score, reverse=True)
    return out[:limit] if limit else out


# ------------------------------------------------------------------ 草稿


@dataclass
class RefactorDraft:
    """C 类题目草稿（已通过指标校验，阈值已确定）。"""

    title: str
    difficulty: Difficulty
    problem_statement: str
    rel_path: str
    symbol_path: str
    original_source: str          # 题目态文件内容（= 原文，不改）
    refactored_source: str        # golden 态文件内容
    guard_test_path: str
    guard_test_code: str
    thresholds: GuardThresholds
    raw: dict | None = None


# ------------------------------------------------------------------ Prompt

_SYSTEM = """你是一位资深软件工程师，擅长在不改变行为的前提下重构遗留代码。
你现在要做两件事：
1. 给出一份**行为完全等价**、但结构显著更清晰的参考重构
2. 为这次重构写一份题目描述，让别人能独立完成同样的重构

铁律：重构不得改变任何对外可见的行为，也不得改变目标函数的签名。"""

_USER_TMPL = """请为下面这段代码设计一道「代码重构」题目，并给出参考重构。

## 项目信息
- 仓库：{repo}（{stars} stars）
- 文件：`{rel_path}`（共 {file_lines} 行）
- 目标符号：`{symbol}`
- 当前指标：有效代码 {body_lines} 行，圈复杂度 {cyclomatic}

## 目标符号的当前实现（这就是要被重构的代码）
```python
{target_code}
```

## 该文件的其余上下文（仅签名，帮你了解可用的方法与命名风格）
```python
{skeleton}
```

---

## 输出格式

输出一个 JSON 对象，字段如下：

- `title`：一句话标题（不超过 30 字）
- `difficulty`：`"easy"` | `"medium"` | `"hard"`
- `smells`：字符串数组，列出这段代码的具体坏味道（3~5 条，每条一句话）
- `refactored_symbol`：重构后 `{symbol}` 的**完整定义**（含 `def` 行与装饰器；
  若是方法，请按**类内方法**的形式书写，但**不要**包含 `class` 行；
  使用 4 空格为一个缩进层级，最外层不要额外缩进）
- `helpers`：可选。重构中提取出的**模块级**辅助函数/常量的完整代码
  （会被追加到文件末尾；没有则给空字符串）
- `problem_statement`：Markdown 题干，必须严格包含以下 6 个二级标题，顺序一致：
  `## 背景与上下文`（项目与该函数的职责，以及为什么需要重构，3-5 句）
  `## 需要实现的功能`（说明这是一道重构题：目标是改善结构而非改变功能）
  `## 输入`（目标函数的参数说明 —— 重构后必须保持不变）
  `## 输出`（返回值与异常语义 —— 重构后必须保持不变）
  `## 预期行为`（编号列出：① 必须保持哪些行为完全不变；
    ② 要求消除哪些具体坏味道；③ 允许做哪些结构性调整）
  `## 约束条件`（至少包含：签名不得改变、不得修改任何测试文件、
    不得改变对外行为、不得引入新的第三方依赖）

## 硬性要求（违反任意一条，题目作废）

### 关于参考重构
1. **行为必须完全等价**：所有分支、返回值、异常类型与触发条件、副作用顺序都不能变
2. **签名必须逐字不变**：参数名、顺序、默认值、`*args`/`**kwargs` 一律不动
3. 必须**显著**降低目标函数的有效行数与圈复杂度（把独立职责提取成小函数），
   而不是把代码压成长表达式 —— 压行不算重构
4. 提取出的辅助函数请放在 `helpers` 里（模块级、以下划线开头表示私有），
   不要把大段逻辑塞进另一个巨型函数
5. 只能使用标准库与该文件已经导入的依赖，不得新增第三方依赖
6. 不得删除或重命名 `{symbol}`

### 关于题干
7. 禁止在题干里贴出参考重构的代码，也不要写逐步操作指令 ——
   要描述「要达到什么状态」，而不是「照着敲什么代码」
8. 禁止提及任何测试函数名
9. 用中文书写，专业术语保留英文"""


# 追加到题干末尾的自动化门槛说明。
# ⚠️ 数值由程序填入而非 LLM 生成 —— 判分阈值必须与守卫测试逐字一致，
#    交给 LLM 写极易出现「题干说 10 行、测试卡 8 行」这类不可解陷阱。
_GATE_TMPL = """

## 自动化质量门槛

除了「所有既有测试必须保持通过」之外，本题还有一组**静态指标**判据
（由 `{guard_file}` 自动检查，该文件属于判据、不可修改）：

| 指标 | 重构前 | 要求 |
|---|---|---|
| `{symbol}` 的有效代码行数 | {before_body} | ≤ **{max_body}** |
| `{symbol}` 的圈复杂度 | {before_cx} | ≤ **{max_cx}** |
| 文件内任一函数的有效代码行数 | — | ≤ **{max_any}** |
| `{symbol}` 的参数列表 | `{args}` | 保持完全不变 |

说明：
- 「有效代码行数」不含 docstring、空行与纯注释行，所以删注释、压行都无助于达标
- 最后一项限制的用意是防止「把逻辑整体搬到另一个大函数」这种伪重构
- 达标的常规做法是：把目标函数中彼此独立的职责，提取为若干个小的私有辅助函数
"""


def _module_skeleton(source: str, exclude_symbol: str, max_lines: int = 90) -> str:
    """抽取文件骨架（import + 类/函数签名），不含函数体。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            try:
                lines.append(ast.unparse(node))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(node, ast.ClassDef):
            lines.append(f"\nclass {node.name}:")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = ""
                    try:
                        args = ast.unparse(sub.args)
                    except Exception:  # noqa: BLE001
                        pass
                    mark = ("   # ← 本题要重构的目标"
                            if f"{node.name}.{sub.name}" == exclude_symbol else "")
                    lines.append(f"    def {sub.name}({args}): ...{mark}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ""
            try:
                args = ast.unparse(node.args)
            except Exception:  # noqa: BLE001
                pass
            mark = "   # ← 本题要重构的目标" if node.name == exclude_symbol else ""
            lines.append(f"def {node.name}({args}): ...{mark}")
    return "\n".join("\n".join(lines).splitlines()[:max_lines])


def _impl_body_only(def_text: str) -> str:
    """提取函数定义的「实现部分」（去掉 def 行与 docstring）。

    用于重构题的泄题检查：题干**允许**出现方法签名（约束条件要写明"签名不变"，
    做题者也需要知道重构哪个方法），但**不允许**泄露重构后的实现代码。
    故泄题比对只针对函数体，签名行不算泄题。
    """
    try:
        tree = ast.parse(def_text)
    except SyntaxError:
        return def_text
    if not tree.body or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        return def_text
    fn = tree.body[0]
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if not body:
        return ""
    lines = def_text.splitlines()
    start = min(s.lineno for s in body)
    end = max((s.end_lineno or s.lineno) for s in body)
    return "\n".join(lines[start - 1:end])


def _build_refactored_source(original: str, symbol_path: str,
                             refactored_symbol: str, helpers: str) -> str:
    """把 LLM 的重构结果合成完整文件内容。

    目标符号做**同层级替换**（保持类内/模块级的原有缩进），
    辅助函数一律追加到文件末尾（模块级）—— Python 的名字在调用时才解析，
    因此定义顺序不影响运行。
    """
    out = replace_symbol_def(original, symbol_path, refactored_symbol)
    helpers = (helpers or "").strip("\n")
    if helpers:
        import textwrap

        block = textwrap.dedent(helpers).strip("\n")
        if not out.endswith("\n"):
            out += "\n"
        out += "\n\n" + block + "\n"
        try:
            ast.parse(out)
        except SyntaxError as e:
            raise NotStubbable(f"追加 helpers 后语法错误（第 {e.lineno} 行：{e.msg}）") from e
    return out


def _validate(
    data: dict,
    original: str,
    target: RefactorTarget,
    task_id: str,
    tests_dir: str,
) -> tuple[RefactorDraft | None, list[str]]:
    problems: list[str] = []

    refactored_symbol = str(data.get("refactored_symbol") or "").strip()
    helpers = str(data.get("helpers") or "")
    if not refactored_symbol:
        return None, ["refactored_symbol 为空"]
    if "class " in refactored_symbol.splitlines()[0]:
        return None, ["refactored_symbol 不应包含 class 行，只给方法定义本身"]

    try:
        refactored_source = _build_refactored_source(
            original, target.symbol_path, refactored_symbol, helpers)
    except (NotStubbable, SymbolNotFound, SyntaxError) as e:
        return None, [f"重构结果无法合成为合法文件：{e}"]

    if "NotImplementedError" in refactored_source and "NotImplementedError" not in original:
        problems.append("重构结果引入了 NotImplementedError —— 参考重构必须是完整实现")

    # ⭐ 关键校验：必须有实质简化，且签名不变。不达标直接打回（绝不产出不可解的题）
    try:
        th = derive_thresholds(original, refactored_source, target.symbol_path)
    except NotRefactorable as e:
        return None, [str(e)]

    stmt = _clean_statement(str(data.get("problem_statement") or ""))
    if len(stmt) < 400:
        problems.append(f"题干过短（{len(stmt)} 字符），说不清重构要求")
    missing = [k for k in REQUIRED_SECTIONS if k not in stmt]
    if missing:
        problems.append(f"题干缺少必需小节：{missing}")
    leak = _looks_leaky(stmt, _impl_body_only(refactored_symbol))
    if leak:
        problems.append(f"题干泄露了参考重构方案：{leak}")
    smells = data.get("smells")
    if not isinstance(smells, list) or len(smells) < 2:
        problems.append("smells 至少要列出 2 条具体坏味道")
    # 题干必须点明目标符号，否则做题者不知道重构谁
    tail = target.symbol_path.rsplit(".", 1)[-1]
    if tail not in stmt:
        problems.append(f"题干未指明目标符号 `{target.symbol_path}`")

    if problems:
        return None, problems

    from .refactor_metrics import guard_test_path

    guard_rel = guard_test_path(task_id, tests_dir)
    guard_code = render_guard_test(task_id, target.rel_path, target.symbol_path, th)

    diff_raw = str(data.get("difficulty") or "").lower()
    if diff_raw not in ("easy", "medium", "hard"):
        diff_raw = "medium" if target.cyclomatic < 12 else "hard"

    stmt += _GATE_TMPL.format(
        guard_file=guard_rel,
        symbol=target.symbol_path,
        before_body=th.before.body_lines,
        before_cx=th.before.cyclomatic,
        max_body=th.max_target_body_lines,
        max_cx=th.max_target_complexity,
        max_any=th.max_any_func_body_lines,
        args=", ".join(th.expected_args),
    )

    return RefactorDraft(
        title=str(data.get("title") or "")[:60],
        difficulty=Difficulty(diff_raw),
        problem_statement=stmt,
        rel_path=target.rel_path,
        symbol_path=target.symbol_path,
        original_source=original,
        refactored_source=refactored_source,
        guard_test_path=guard_rel,
        guard_test_code=guard_code,
        thresholds=th,
        raw=data,
    ), []


# ------------------------------------------------------------------ 主入口


def design_refactor_task(
    client: TokenHubClient,
    repo: str,
    stars: int,
    repo_root: str | Path,
    target: RefactorTarget,
    task_id: str,
    *,
    tests_dir: str = "tests",
    model: str | None = None,
    max_attempts: int = 3,
    max_tokens: int = 8192,
) -> RefactorDraft:
    """让 LLM 给出参考重构与题干，并用静态指标反推可行的守卫阈值。"""
    root = Path(repo_root).resolve()
    src_path = root / target.rel_path
    original = src_path.read_text(encoding="utf-8")

    try:
        target_code = extract_symbol_def(original, target.symbol_path)
    except (SymbolNotFound, SyntaxError) as e:
        raise DesignError(f"取不到目标符号源码：{e}") from e

    user = _USER_TMPL.format(
        repo=repo, stars=stars,
        rel_path=target.rel_path, file_lines=target.file_lines,
        symbol=target.symbol_path,
        body_lines=target.body_lines, cyclomatic=target.cyclomatic,
        target_code=target_code,
        skeleton=_module_skeleton(original, target.symbol_path),
    )

    problems: list[str] = []
    for _ in range(max_attempts):
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ]
        if problems:
            messages.append({
                "role": "user",
                "content": "上一次输出存在以下问题，请修正后重新输出完整 JSON：\n"
                           + "\n".join(f"- {p}" for p in problems),
            })
        try:
            data = client.chat_json(messages, model=model, max_tokens=max_tokens, temperature=0.3)
        except LLMError as e:
            problems = [f"调用失败：{e}"]
            continue

        draft, problems = _validate(
            data if isinstance(data, dict) else {}, original, target, task_id, tests_dir)
        if draft is not None:
            return draft

    raise DesignError(f"重构题设计失败（尝试 {max_attempts} 次），最后的问题：{problems}")
