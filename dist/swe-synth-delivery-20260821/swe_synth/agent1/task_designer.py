"""LLM 出题（Agent1 的核心）

职责
----
把「挖空靶点的静态信息」转换成一份**结构完整、不泄题**的题干。

关键设计：给 LLM 什么、不给什么
--------------------------------
给：函数签名、docstring、模块上下文（import + 类结构）、调用方式、测试**名称**
不给：被挖掉的原始实现、测试**断言内容**

原因：
  · 给实现 → LLM 会照抄进题干 → 直接泄题
  · 给测试断言 → 题干会变成"让断言通过"的作弊指南，而非真实需求描述
  · 给测试名称是必要的：能帮 LLM 判断该函数被期望有哪些行为分支

产出的题干必须通过 `schemas.task.SweTask` 的强制校验（含小节完整性 + 泄题审查），
校验不过就重试；连续失败则该候选 REJECT。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from ..clients.tokenhub import LLMError, TokenHubClient
from ..schemas.task import REQUIRED_SECTIONS, Difficulty, TaskType
from .stubber import StubResult

__all__ = ["TaskDraft", "DesignError", "build_context", "design_task", "PROMPT_VERSION",
           # 供同包的 B/C 题型出题器复用（题干清洗与泄题比对逻辑三类题型通用）
           "_clean_statement", "_looks_leaky"]

PROMPT_VERSION = "v1"


class DesignError(RuntimeError):
    """出题失败（LLM 输出始终不合格）。"""


@dataclass
class TaskDraft:
    """LLM 产出的题目草稿。"""

    problem_statement: str
    difficulty: Difficulty
    task_type: TaskType
    title: str = ""
    hints: str | None = None
    raw: dict | None = None


# --------------------------------------------------------------- 上下文抽取

def _module_skeleton(source: str, keep_symbol: str, max_lines: int = 120) -> str:
    """抽取模块骨架：import + 类/函数签名（不含函数体）。

    目的：让 LLM 了解代码风格与可用依赖，但看不到任何实现细节。
    """
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
            bases = ""
            try:
                bases = ", ".join(ast.unparse(b) for b in node.bases)
            except Exception:  # noqa: BLE001
                pass
            lines.append(f"\nclass {node.name}({bases}):")
            doc = ast.get_docstring(node)
            if doc:
                lines.append(f'    """{doc.strip().splitlines()[0]}"""')
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                    args = ""
                    try:
                        args = ast.unparse(sub.args)
                    except Exception:  # noqa: BLE001
                        pass
                    mark = "   # ← 本题要实现的方法" if f"{node.name}.{sub.name}" == keep_symbol else ""
                    lines.append(f"    {prefix} {sub.name}({args}): ...{mark}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            args = ""
            try:
                args = ast.unparse(node.args)
            except Exception:  # noqa: BLE001
                pass
            mark = "   # ← 本题要实现的函数" if node.name == keep_symbol else ""
            lines.append(f"{prefix} {node.name}({args}): ...{mark}")

    out = "\n".join(lines).splitlines()[:max_lines]
    return "\n".join(out)


def _test_names(fail_to_pass: list[str]) -> list[str]:
    """只取测试用例名（不含文件内容），用于提示行为分支。"""
    names = []
    for nid in fail_to_pass:
        names.append(nid.split("::", 1)[1] if "::" in nid else nid)
    return names


def build_context(
    repo: str,
    rel_path: str,
    stub: StubResult,
    stubbed_source: str,
    fail_to_pass: list[str],
    *,
    max_skeleton_lines: int = 120,
) -> dict[str, str]:
    """组装出题所需的上下文（**不含原始实现代码**）。

    ⚠️ 关键权衡：完全不给实现信息会导致 LLM「靠方法名猜行为」，产出不可解的废题
    （实测发生过）。因此提供**事实性线索**（调用了哪些方法、依赖哪些字面量），
    但**不给任何代码行** —— 让 LLM 有据可依，又无法照抄。
    """
    symbol_tail = stub.symbol_path.rsplit(".", 1)[-1]
    return {
        "repo": repo,
        "file": rel_path,
        "symbol": stub.symbol_path,
        "kind": stub.kind,
        "signature": stub.signature.strip(),
        "docstring": (stub.docstring or "").strip() or "（原代码无 docstring）",
        "decorators": ", ".join(stub.decorators) or "无",
        "body_lines": str(stub.body_line_count),
        # 用挖空后的源码抽骨架，从根上保证不可能泄露实现
        "skeleton": _module_skeleton(stubbed_source, symbol_tail, max_skeleton_lines),
        "test_names": "\n".join(f"- {n}" for n in _test_names(fail_to_pass)[:25]),
        "n_tests": str(len(fail_to_pass)),
        "behavior_hints": _behavior_hints(stub.original_body),
    }


def _behavior_hints(original_body: str) -> str:
    """从真实实现中抽取**事实性线索**（非代码），防止 LLM 臆造行为。

    只给「用到了什么」，不给「怎么用」：
      · 调用了哪些方法/函数
      · 依赖哪些字符串字面量（如协议前缀、字段名）
      · 是否有异常处理、是否有循环
    """
    facts = _impl_facts(original_body)
    lines: list[str] = []

    calls = sorted(c for c in facts["calls"] if not c.startswith("_") or c.count("_") <= 2)
    if calls:
        lines.append(f"- 实现中调用了这些方法/函数：{', '.join(calls[:10])}")
    consts = sorted(c for c in facts["consts"] if 2 <= len(c) <= 40)
    if consts:
        lines.append(f"- 实现中出现了这些字符串字面量（可能是协议前缀/字段名，务必在题干中体现）："
                     + ", ".join(repr(c) for c in consts[:8]))
    attrs = sorted(a for a in facts["attrs"] if a not in facts["calls"])
    if attrs:
        lines.append(f"- 访问了这些属性：{', '.join(attrs[:10])}")

    body = original_body
    if re.search(r"\btry\b", body):
        lines.append("- 实现中包含异常处理（try/except）")
    else:
        lines.append("- 实现中**没有**异常处理，不要在题干里要求捕获异常")
    if re.search(r"\bfor\b|\bwhile\b", body):
        lines.append("- 实现中包含循环")
    n_returns = len(re.findall(r"\breturn\b", body))
    if n_returns:
        lines.append(f"- 实现中有 {n_returns} 个 return 分支（题干的预期行为条数应与之匹配）")

    return "\n".join(lines) if lines else "（无额外线索）"


# --------------------------------------------------------------- Prompt

_SYSTEM = """你是一位资深软件工程面试官，负责把开源项目中的真实代码改造成高质量的编程题目。
你的题目将用于评测 AI Coding Agent 的能力，因此必须：
1. 描述清晰、信息完整，让人无需看答案就能理解要做什么
2. 只描述「需求与预期行为」，绝不包含任何实现代码或算法步骤的逐行翻译
3. 忠于原始代码的真实契约，不臆造不存在的行为"""

_USER_TMPL = """下面是一个开源仓库中被「挖空」的函数，你要为它写一份题目描述。

## 仓库信息
- 仓库：{repo}
- 文件：{file}
- 目标符号：{symbol}（类型：{kind}）
- 装饰器：{decorators}
- 原实现约 {body_lines} 行

## 函数签名（必须保持不变）
```python
{signature}
```

## 原有文档字符串
```
{docstring}
```

## 所在模块的结构骨架（仅签名，无实现）
```python
{skeleton}
```

## 该函数被以下 {n_tests} 个测试用例覆盖（仅提供名称，供你推断需要覆盖哪些行为分支）
{test_names}

## 关于真实实现的事实线索（**必须严格遵守，不得臆造**）
{behavior_hints}

---

请输出一个 JSON 对象，字段如下：

- `title`: 一句话标题（不超过 30 字）
- `difficulty`: "easy" | "medium" | "hard"（依据逻辑复杂度与分支数量判断）
- `problem_statement`: Markdown 题干，**必须严格包含以下 6 个二级标题，顺序一致**：
  `## 背景与上下文`（项目与该模块的作用，2-4 句）
  `## 需要实现的功能`（要做什么，不说怎么做）
  `## 输入`（逐个参数说明含义、类型、可能的特殊值）
  `## 输出`（返回值类型与各种情况下的返回内容）
  `## 预期行为`（编号列出需要处理的情况与分支，这是最重要的部分）
  `## 约束条件`（至少包含"不得修改任何测试文件"和"保持函数签名不变"）

### ⚠️ 最重要的要求：忠于真实实现，不要推测
上面「事实线索」列出了真实实现调用的方法与依赖的字面量。你必须：
- **只描述线索支持的行为**；线索里没有的方法，不要要求调用
- 线索中列出的**字符串字面量必须在题干中明确写出**（它们通常是协议前缀、
  版本标识或字段名，缺了这些信息，做题者不可能实现正确）
- 线索说「没有异常处理」，就不要写"解析失败时捕获异常"
- 预期行为的分支数应与 return 分支数大致吻合，不要凭空增加分支

### 硬性禁止（违反则题目作废）
- 禁止在题干中出现完整的函数实现、代码块形式的实现步骤
- 禁止出现 `return xxx` 这类具体代码语句
- 禁止提及测试函数名或"让某个测试通过"之类的表述
- 禁止臆造签名中不存在的参数
- 禁止臆造事实线索中不存在的方法调用或行为分支

### 写作要求
- 用中文书写，专业术语保留英文
- `## 预期行为` 用编号列表，覆盖正常路径与边界情况
- 从"需求方"视角描述，就像一份任务单，而不是代码讲解"""


# --------------------------------------------------------------- 主流程

def _clean_statement(text: str) -> str:
    """规范化题干，修正 LLM 常见的小偏差。"""
    text = text.replace("\r\n", "\n").strip()
    # 统一标题层级：偶尔会输出 ### 或 #
    for key in REQUIRED_SECTIONS:
        text = re.sub(rf"^#{{1,4}}\s*{re.escape(key)}", f"## {key}", text, flags=re.M)
    return text


def _looks_leaky(text: str, original_body: str) -> str | None:
    """额外的泄题检查：题干与原实现的代码行是否高度重合。

    `schemas` 里已有正则规则，这里再做一次「与真实答案比对」的检查 ——
    这是规则匹配做不到的。
    """
    body_lines = [
        ln.strip() for ln in original_body.splitlines()
        if len(ln.strip()) > 15 and not ln.strip().startswith("#")
    ]
    if not body_lines:
        return None
    hits = [ln for ln in body_lines if ln in text]
    if len(hits) >= 2:
        return f"题干包含 {len(hits)} 行原始实现代码，例如：{hits[0][:60]}"
    # 单行但很长的实现也算泄露
    long_hits = [ln for ln in hits if len(ln) > 40]
    if long_hits:
        return f"题干包含原始实现代码：{long_hits[0][:60]}"
    return None


# ---- 题干与真实实现的一致性校验 ---------------------------------------------
#
# ⚠️ 这是实测发现的**最危险的失效模式**：
#    LLM 会根据方法名「合理臆测」出并不存在的行为。
#    例如为 `Serializer.loads` 臆造出「msgpack 反序列化后读 version 字段分发」，
#    而真实实现只是「检查字节前缀 cc=4,」。
#    这种题干看起来专业、结构完整、能通过所有格式校验，
#    但**按它实现永远无法让 FAIL_TO_PASS 变绿 —— 题目不可解**。
#    比泄题更隐蔽，也更致命。
#
# 校验思路：从真实实现里抽取「可验证的事实」（调用了哪些方法、用了哪些字面量），
# 与题干宣称的内容比对：
#   · 题干提到了实现中根本没调用的本类方法 → 疑似臆造
#   · 实现中的关键字面量（如 "cc=" 前缀）在题干中完全没有体现 → 描述可能偏离

def _impl_facts(original_body: str) -> dict[str, set[str]]:
    """从原始实现中抽取可核对的事实。"""
    facts: dict[str, set[str]] = {"calls": set(), "attrs": set(), "consts": set()}
    try:
        # 函数体不是完整语法单元，包一层壳再解析
        wrapped = "def _w():\n" + "\n".join(
            "    " + ln for ln in original_body.splitlines()
        )
        tree = ast.parse(wrapped)
    except SyntaxError:
        return facts
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                facts["calls"].add(f.attr)
            elif isinstance(f, ast.Name):
                facts["calls"].add(f.id)
        elif isinstance(node, ast.Attribute):
            facts["attrs"].add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) >= 3:
                facts["consts"].add(node.value)
    return facts


def _check_consistency(
    text: str,
    original_body: str,
    sibling_methods: set[str],
) -> str | None:
    """检查题干是否臆造了实现中不存在的行为。返回问题描述，None 表示通过。"""
    facts = _impl_facts(original_body)
    actually_called = facts["calls"] | facts["attrs"]

    # 1) 题干点名要求调用某个同类方法，但实现里根本没调用它
    fabricated = []
    for m in sibling_methods:
        if m.startswith("__") or m in actually_called:
            continue
        # 题干中以 `m` 或 m( 形式明确提及才算（避免误伤普通词汇）
        if re.search(rf"`{re.escape(m)}`|\b{re.escape(m)}\s*\(|\b{re.escape(m)}\s*方法", text):
            fabricated.append(m)
    if fabricated:
        return (
            f"题干要求调用 {fabricated}，但真实实现并未调用它们 —— "
            f"疑似臆造行为，按此题干实现将无法通过测试。"
            f"真实实现实际调用的是：{sorted(actually_called)[:6]}"
        )

    # 2) 实现中的关键字符串字面量（如协议前缀）在题干里毫无体现
    key_consts = [c for c in facts["consts"] if 3 <= len(c) <= 40]
    if key_consts:
        mentioned = any(c in text for c in key_consts)
        if not mentioned:
            return (
                f"真实实现依赖关键字面量 {key_consts[:3]}，题干完全未提及 —— "
                f"缺少这一信息将导致题目无法解出"
            )
    return None


def _sibling_methods(stubbed_source: str, symbol_path: str) -> set[str]:
    """取出目标符号所在类/模块的其它方法名（用于识别臆造调用）。"""
    names: set[str] = set()
    try:
        tree = ast.parse(stubbed_source)
    except SyntaxError:
        return names
    target_tail = symbol_path.rsplit(".", 1)[-1]
    owner = symbol_path.rsplit(".", 1)[0] if "." in symbol_path else None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and (owner is None or node.name == owner):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name != target_tail:
                    names.add(sub.name)
        elif owner is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != target_tail:
                names.add(node.name)
    return names


def design_task(
    client: TokenHubClient,
    repo: str,
    rel_path: str,
    stub: StubResult,
    stubbed_source: str,
    fail_to_pass: list[str],
    *,
    model: str | None = None,
    max_attempts: int = 3,
    max_tokens: int = 8192,
) -> TaskDraft:
    """调用 LLM 出题，并做多重质量校验，失败自动重试。

    校验项（任一不过就重试）：
      1. JSON 结构完整、含必需字段
      2. 题干包含全部 6 个必需小节（对应验收标准"含上下文、输入输出、预期行为"）
      3. 题干未泄露实现（正则规则 + 与真实答案比对）
      4. 题干长度足够（过短说明信息不完整）
    """
    ctx = build_context(repo, rel_path, stub, stubbed_source, fail_to_pass)
    siblings = _sibling_methods(stubbed_source, stub.symbol_path)
    user = _USER_TMPL.format(**ctx)
    problems: list[str] = []

    for attempt in range(max_attempts):
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ]
        if problems:
            # 把上一轮的问题回灌，让模型自我修正
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

        problems = []
        stmt = _clean_statement(str(data.get("problem_statement") or ""))

        if len(stmt) < 300:
            problems.append(f"题干过短（{len(stmt)} 字符），信息不完整")
        missing = [k for k in REQUIRED_SECTIONS if k not in stmt]
        if missing:
            problems.append(f"缺少必需小节：{missing}")
        leak = _looks_leaky(stmt, stub.original_body)
        if leak:
            problems.append(leak)
        # ⚠️ 最关键的一道校验：题干宣称的行为必须与真实实现一致，
        #    否则会产出「看起来专业但根本解不出」的废题（实测发生过）
        inconsistent = _check_consistency(stmt, stub.original_body, siblings)
        if inconsistent:
            problems.append(inconsistent)
        # 不得提及测试名（会变成作弊指南）
        for tn in _test_names(fail_to_pass):
            if tn in stmt:
                problems.append(f"题干提及了测试名 {tn}，请删除")
                break

        diff_raw = str(data.get("difficulty") or "").lower()
        if diff_raw not in ("easy", "medium", "hard"):
            # 兜底：按被挖行数推断，不因这个小问题浪费一次重试
            n = stub.body_line_count
            diff_raw = "easy" if n <= 5 else ("medium" if n <= 20 else "hard")

        if problems:
            continue

        return TaskDraft(
            problem_statement=stmt,
            difficulty=Difficulty(diff_raw),
            task_type=TaskType.FEATURE_IMPLEMENTATION,
            title=str(data.get("title") or "")[:60],
            hints=(str(data["hints"]) if data.get("hints") else None),
            raw=data,
        )

    raise DesignError(
        f"出题失败（尝试 {max_attempts} 次），最后的问题：{problems}"
    )
