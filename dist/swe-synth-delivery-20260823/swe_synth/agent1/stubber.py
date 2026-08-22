"""AST 精准挖空器（题目可验证性的技术地基）

设计目标
--------
把一个函数/方法的**函数体**替换为 stub，同时**逐字节保留**其余所有内容：
签名、装饰器、类型注解、docstring、模块内其它代码、空行与注释一律不动。

为什么不用 `ast.unparse` 整体重写
--------------------------------
`ast.unparse` 会丢注释、重排格式、规范化引号，导致 diff 覆盖整个文件 ——
既让 `golden.patch` 变得巨大不可读，也会让「挖空」与「格式化」混在一起，
无法判断测试变红究竟是因为挖空还是格式变动。
因此这里采用**基于行范围的原地替换**：只替换函数体所占的那几行。

不可挖空的情况（主动拒绝，由上层跳过该候选）
--------------------------------------------
- 单行定义（`def f(): return 1`）：签名与函数体同行，替换会破坏结构
- 函数体只有 docstring / 只有 `pass` / 只有 `...`：本来就没有实现，挖了也不会变红
- 抽象方法、协议声明：同上
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass

__all__ = [
    "StubResult",
    "SymbolNotFound",
    "NotStubbable",
    "list_symbols",
    "stub_symbol",
    "make_patch",
    "make_added_file_patch",
    "find_symbol_span",
    "extract_symbol_def",
    "replace_symbol_def",
    "DEFAULT_STUB_BODY",
]

# stub 体：用 NotImplementedError 而非 pass，确保测试**明确失败**而非静默返回 None。
# 静默返回 None 有可能碰巧让某些断言通过，那样 FAIL_TO_PASS 就不可靠了。
DEFAULT_STUB_BODY = 'raise NotImplementedError("TODO: implement this function")'

# 只有 docstring / pass / ... 的函数体，视为「没有实现」
_TRIVIAL_BODY_MSG = "函数体没有实质实现（仅 docstring/pass/...），挖空后测试不会变红"


class SymbolNotFound(LookupError):
    """在源码中找不到指定的符号路径。"""


class NotStubbable(ValueError):
    """该符号存在，但不适合挖空（原因见异常消息）。"""


@dataclass
class StubResult:
    """一次挖空的完整结果，同时是「出题所需元数据」的载体。"""

    symbol_path: str          # 如 "MyClass.my_method"
    kind: str                 # function / method / async_function / async_method
    source_stubbed: str       # 挖空后的完整文件内容
    original_body: str        # 被挖掉的原始函数体（golden 实现，用于还原）
    signature: str            # 签名文本（含装饰器），供出题时展示
    docstring: str | None     # 原 docstring（保留在 stub 中）
    body_start_line: int      # 被替换区间起始行（1-based，含）
    body_end_line: int        # 被替换区间结束行（1-based，含）
    body_line_count: int      # 被挖掉的行数 —— 难度的粗略指标
    body_char_count: int      # 被挖掉的字符数
    decorators: list[str]     # 装饰器名列表（用于识别 property/abstractmethod 等）

    @property
    def is_method(self) -> bool:
        return "." in self.symbol_path


# --------------------------------------------------------------------- 符号遍历

def _walk_defs(node: ast.AST, prefix: str = ""):
    """深度优先遍历所有 def / class，产出 (symbol_path, node)。

    符号路径用点号连接，如 `Outer.Inner.method`。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            path = f"{prefix}{child.name}"
            yield path, child
            yield from _walk_defs(child, path + ".")


def list_symbols(source: str) -> list[tuple[str, ast.AST]]:
    """列出源码中所有可定位的符号（供候选靶点筛选）。"""
    return list(_walk_defs(ast.parse(source)))


def _find_symbol(tree: ast.AST, symbol_path: str) -> ast.AST:
    for path, node in _walk_defs(tree):
        if path == symbol_path:
            return node
    # 容错：允许只给函数名，但必须唯一命中，否则报错要求用完整路径
    tail_hits = [(p, n) for p, n in _walk_defs(tree) if p.rsplit(".", 1)[-1] == symbol_path]
    if len(tail_hits) == 1:
        return tail_hits[0][1]
    if len(tail_hits) > 1:
        raise SymbolNotFound(
            f"符号名 {symbol_path!r} 不唯一，候选：{[p for p, _ in tail_hits]}，请用完整路径"
        )
    raise SymbolNotFound(f"未找到符号 {symbol_path!r}")


# --------------------------------------------------------------------- 辅助判断

def _docstring_node(body: list[ast.stmt]) -> ast.Expr | None:
    """取函数体首条语句中的 docstring 节点（没有则 None）。"""
    if not body:
        return None
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first
    return None


def _is_trivial(stmt: ast.stmt) -> bool:
    """`pass` / `...` / 字符串常量表达式 —— 都算「没有实现」。"""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return stmt.value.value is Ellipsis or isinstance(stmt.value.value, str)
    return False


def _decorator_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for d in getattr(node, "decorator_list", []) or []:
        try:
            names.append(ast.unparse(d))
        except Exception:  # noqa: BLE001  # 极端语法容错
            names.append("<unparsable>")
    return names


def _leading_ws(line: str) -> str:
    m = re.match(r"[ \t]*", line)
    return m.group() if m else ""


# --------------------------------------------------------------------- 核心挖空

def stub_symbol(
    source: str,
    symbol_path: str,
    *,
    keep_docstring: bool = True,
    stub_body: str = DEFAULT_STUB_BODY,
    min_body_lines: int = 2,
) -> StubResult:
    """把 `symbol_path` 指向的函数体替换为 stub，返回完整结果。

    参数
    ----
    keep_docstring:
        保留 docstring。**强烈建议保持 True** —— docstring 是题干的重要信息来源，
        且保留它能让被测 Agent 知道该实现什么，符合「题目结构完整」的验收要求。
    min_body_lines:
        函数体（不含 docstring）至少要有几行才允许挖空。
        1 行的函数（如 `return self._x`）挖了也太简单，不适合作为 SWE 题目。
    """
    tree = ast.parse(source)
    node = _find_symbol(tree, symbol_path)

    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise NotStubbable(f"{symbol_path!r} 是 {type(node).__name__}，只支持挖空函数/方法")

    lines = source.splitlines(keepends=True)
    body = node.body
    if not body:
        raise NotStubbable(_TRIVIAL_BODY_MSG)

    doc_node = _docstring_node(body)
    doc_text = doc_node.value.value if doc_node is not None else None  # type: ignore[union-attr]

    # 真正的「实现语句」= 去掉 docstring 之后剩下的
    impl_stmts = body[1:] if doc_node is not None else body
    if not impl_stmts:
        raise NotStubbable(_TRIVIAL_BODY_MSG)
    if all(_is_trivial(s) for s in impl_stmts):
        raise NotStubbable(_TRIVIAL_BODY_MSG)

    # 单行定义（`def f(): return 1`）：函数体与签名同行，按行替换会破坏结构
    if body[0].lineno == node.lineno:
        raise NotStubbable("单行函数定义（签名与函数体同行），不支持精准挖空")

    first_impl = impl_stmts[0]
    # 保留 docstring 时，从第一条实现语句开始挖；否则从 docstring 开始一起挖
    start_line = first_impl.lineno if keep_docstring else body[0].lineno

    # 结束行取所有语句 end_lineno 的最大值：装饰器/多行表达式可能让顺序不严格单调
    end_line = max(
        (s.end_lineno or s.lineno) for s in (impl_stmts if keep_docstring else body)
    )

    if start_line > end_line:
        raise NotStubbable("函数体行范围异常，跳过")

    body_line_count = end_line - start_line + 1
    if body_line_count < min_body_lines:
        raise NotStubbable(
            f"函数体仅 {body_line_count} 行（阈值 {min_body_lines}），过于简单，不适合出题"
        )

    original_body = "".join(lines[start_line - 1 : end_line])

    # 缩进以「第一条实现语句所在行」的前导空白为准，避免用 col_offset 在 tab 缩进下算错
    indent = _leading_ws(lines[start_line - 1])
    # 行尾换行符跟随原文件风格（最后一行可能没有换行符）
    newline = "\r\n" if original_body.endswith("\r\n") else "\n"
    stub_lines = [f"{indent}{ln}" if ln.strip() else ln
                  for ln in stub_body.splitlines()]
    stub_text = newline.join(stub_lines) + (newline if original_body.endswith(("\n", "\r")) else "")

    new_lines = lines[: start_line - 1] + [stub_text] + lines[end_line:]
    source_stubbed = "".join(new_lines)

    # 挖空后必须仍是合法 Python —— 否则测试会因 SyntaxError 全红（含 PASS_TO_PASS），题目不合法
    try:
        ast.parse(source_stubbed)
    except SyntaxError as e:
        raise NotStubbable(f"挖空后语法错误（第 {e.lineno} 行：{e.msg}），跳过该候选") from e

    # 签名文本：从 def 行到 docstring/函数体之前
    sig_end = (doc_node.lineno - 1) if doc_node is not None else (body[0].lineno - 1)
    sig_start = min([node.lineno] + [d.lineno for d in (node.decorator_list or [])])
    signature = "".join(lines[sig_start - 1 : sig_end]).rstrip()

    is_async = isinstance(node, ast.AsyncFunctionDef)
    is_method = "." in symbol_path
    kind = ("async_" if is_async else "") + ("method" if is_method else "function")

    return StubResult(
        symbol_path=symbol_path,
        kind=kind,
        source_stubbed=source_stubbed,
        original_body=original_body,
        signature=signature,
        docstring=doc_text,
        body_start_line=start_line,
        body_end_line=end_line,
        body_line_count=body_line_count,
        body_char_count=len(original_body),
        decorators=_decorator_names(node),
    )


# --------------------------------------------------------------------- 整体替换
#
# 「重构题」（C 类）与挖空题不同：它需要把**整个函数定义**换成另一份实现
# （而且重构常常会顺带提取出新的辅助函数）。因此需要「整体替换定义」的能力，
# 而不是「只替换函数体」。


def find_symbol_span(source: str, symbol_path: str) -> tuple[int, int, str]:
    """定位符号定义的完整行范围（含装饰器）。

    返回 `(start_line, end_line, indent)`，行号 1-based 且含端点，
    `indent` 是该定义所在行的前导空白（用于把替换文本重新缩进到同一层级）。
    """
    tree = ast.parse(source)
    node = _find_symbol(tree, symbol_path)
    lines = source.splitlines(keepends=True)
    start = min([node.lineno] + [d.lineno for d in (getattr(node, "decorator_list", None) or [])])
    end = getattr(node, "end_lineno", None) or node.lineno
    return start, end, _leading_ws(lines[start - 1])


def extract_symbol_def(source: str, symbol_path: str) -> str:
    """取出该符号的完整定义文本（含装饰器），并去掉公共缩进。

    用于重构题：必须把原始代码原样交给做题者（重构的对象就是这段代码，
    这不算泄题 —— 它本来就在仓库里）。
    """
    import textwrap

    start, end, _ = find_symbol_span(source, symbol_path)
    lines = source.splitlines(keepends=True)
    return textwrap.dedent("".join(lines[start - 1 : end]))


def replace_symbol_def(source: str, symbol_path: str, new_def: str) -> str:
    """用 `new_def` 整体替换该符号的定义（含装饰器）。

    `new_def` 可以包含多个同级定义（重构时常提取出辅助函数），
    会统一按原符号的缩进层级重新缩进。替换后会做语法校验。
    """
    import textwrap

    start, end, indent = find_symbol_span(source, symbol_path)
    lines = source.splitlines(keepends=True)

    text = textwrap.dedent(new_def.replace("\r\n", "\n")).strip("\n")
    if not text.strip():
        raise NotStubbable("替换内容为空")
    body = "\n".join(f"{indent}{ln}" if ln.strip() else "" for ln in text.splitlines())
    new_source = "".join(lines[: start - 1]) + body + "\n" + "".join(lines[end:])

    try:
        ast.parse(new_source)
    except SyntaxError as e:
        raise NotStubbable(f"替换后语法错误（第 {e.lineno} 行：{e.msg}）") from e
    return new_source


# --------------------------------------------------------------------- patch 生成

def make_patch(
    old: str,
    new: str,
    rel_path: str,
    *,
    context: int = 3,
) -> str:
    """生成 `git apply` 可用的 unified diff。

    用 `a/<path>` `b/<path>` 前缀，与 git 默认的 `-p1` 对齐。
    """
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        n=context,
    )
    text = "".join(diff)
    # 保证以换行结尾，否则 git apply 可能报 "corrupt patch"
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def make_added_file_patch(rel_path: str, content: str) -> str:
    """生成「新增文件」的 unified diff。

    B / C 题型都会往仓库里新增文件（新模块骨架、新测试、重构守卫测试），
    这类变更必须用 `--- /dev/null` 形式表达，否则 `git apply` 会因
    「找不到原文件」而拒绝。
    """
    if content and not content.endswith("\n"):
        content += "\n"
    lines = content.splitlines(keepends=True)
    head = [
        f"diff --git a/{rel_path} b/{rel_path}\n",
        "new file mode 100644\n",
        "--- /dev/null\n",
        f"+++ b/{rel_path}\n",
        f"@@ -0,0 +1,{len(lines)} @@\n",
    ]
    return "".join(head + [f"+{ln}" for ln in lines])
