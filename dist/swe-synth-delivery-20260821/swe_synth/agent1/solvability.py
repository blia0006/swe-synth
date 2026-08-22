"""可解性验证（solve-back）—— 题干质量的终极判据

为什么必须有这一步
------------------
实测发现了一个比泄题更致命、也更隐蔽的失效模式：
**LLM 会写出「看起来专业、结构完整、通过所有格式校验，但描述的行为与真实实现不符」的题干。**

真实案例（`Serializer.loads`）：
    真实实现：data 不以 `cc=4,` 开头 → 返回 None（旧格式已移除）
    LLM 题干：data 不以 `cc=` 开头 → 视为当前版本正常处理   ← 逻辑完全反了

这种题目按题干实现永远无法让 FAIL_TO_PASS 变绿 —— **题目不可解**，
但静态规则校验（检查小节完整性、是否调用不存在的方法、字面量是否提及）**全都能通过**。

结论：**规则永远追不上语义错误**。唯一可靠的判据是「让人（或模型）只看题干去做，能不能做出来」。

做法
----
    1. 只把 `problem_statement` + 函数签名 + 模块骨架给 LLM（**不给原实现、不给测试**）
    2. 让它产出函数体
    3. 把该实现写入仓库，跑 FAIL_TO_PASS + PASS_TO_PASS
    4. 全绿 → 题干准确且可解；否则 → 题干有缺陷，打回重写

附带收益（正好覆盖课题要求）
--------------------------
· 课题要求 Agent2「执行题目 → 验证解的正确性」，这一步就是「执行题目」的字面实现
· 产出**难度标定**证据：LLM 一次通过 = easy 偏简单；多次才通过 = 合适；始终不通过 = 过难
· 这些记录写入 `validation.llm_attempt`，作为「通过证明」的一部分
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from ..clients.tokenhub import LLMError, TokenHubClient
from .local_validator import TestRunner, safe_repo_join
from .stubber import StubResult

__all__ = ["SolveAttempt", "SolveBackReport", "solve_back", "solve_back_edits"]


_SYSTEM = """你是一位资深 Python 工程师。你将收到一份任务需求和一个待实现的函数签名。
请严格按照需求实现该函数，只输出函数体代码。

要求：
- 只输出函数体（不含 def 行、不含签名），保持正确的相对缩进
- 不要输出 Markdown 代码块标记，不要任何解释文字
- 只能使用需求中提到的、以及模块中已存在的方法和依赖
- 严格遵循需求描述的每一条预期行为"""

_USER = """## 任务需求
{statement}

## 你要实现的函数签名（已给出，不要重复输出）
```python
{signature}
```

## 所在模块的结构（仅签名，供你了解可用的方法）
```python
{skeleton}
```

请只输出 `{symbol}` 的函数体代码（不含 def 行），使用 4 空格作为函数体的基础缩进。"""


@dataclass
class SolveAttempt:
    """一次解题尝试。"""

    attempt: int
    passed: bool
    n_f2p_passed: int = 0
    n_f2p_total: int = 0
    n_p2p_passed: int = 0
    n_p2p_total: int = 0
    error: str | None = None
    body_preview: str = ""


@dataclass
class SolveBackReport:
    """可解性验证结论。"""

    solvable: bool
    attempts: list[SolveAttempt] = field(default_factory=list)
    reason: str = ""
    first_pass_attempt: int | None = None

    @property
    def difficulty_signal(self) -> str:
        """按「解出所需尝试次数」给难度信号（用于校准 LLM 自评的难度）。"""
        if not self.solvable:
            return "too_hard_or_ambiguous"
        if self.first_pass_attempt == 1:
            return "easy_for_llm"
        if self.first_pass_attempt == 2:
            return "moderate"
        return "challenging"

    def to_dict(self) -> dict:
        return {
            "solvable": self.solvable,
            "reason": self.reason,
            "first_pass_attempt": self.first_pass_attempt,
            "difficulty_signal": self.difficulty_signal,
            "n_attempts": len(self.attempts),
            "attempts": [
                {
                    "attempt": a.attempt,
                    "passed": a.passed,
                    "f2p": f"{a.n_f2p_passed}/{a.n_f2p_total}",
                    "p2p": f"{a.n_p2p_passed}/{a.n_p2p_total}",
                    "error": a.error,
                }
                for a in self.attempts
            ],
        }


def _clean_body(raw: str, indent: str = "        ") -> str:
    """清洗 LLM 输出，得到可直接插入的函数体。"""
    text = raw.strip()
    # 去掉可能的 Markdown 代码块
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    # 去掉模型可能多输出的 def 行
    lines = text.splitlines()
    while lines and re.match(r"^\s*(async\s+)?def\s+", lines[0]):
        lines.pop(0)
    if not lines:
        return ""
    # 统一缩进：先去掉公共缩进，再套上目标缩进
    body = textwrap.dedent("\n".join(lines)).strip("\n")
    out = []
    for ln in body.splitlines():
        out.append(indent + ln if ln.strip() else "")
    return "\n".join(out) + "\n"


def _splice(stubbed_source: str, stub: StubResult, body: str) -> str:
    """把 LLM 写的函数体替换掉 stub 体，得到候选实现。"""
    lines = stubbed_source.splitlines(keepends=True)
    # stub 占据的行：从 body_start_line 起、长度为 stub_body 的行数（默认 1 行）
    start = stub.body_start_line - 1
    # 找到 stub 那一行（含 NotImplementedError），只替换它
    end = start
    for i in range(start, min(start + 5, len(lines))):
        if "NotImplementedError" in lines[i]:
            end = i
            break
    return "".join(lines[:start] + [body] + lines[end + 1:])


def solve_back(
    client: TokenHubClient,
    repo_root: str | Path,
    rel_path: str,
    stub: StubResult,
    problem_statement: str,
    skeleton: str,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    *,
    python_bin: str | None = None,
    model: str | None = None,
    max_attempts: int = 2,
    timeout: int = 600,
) -> SolveBackReport:
    """只依据题干让 LLM 实现该函数，跑测试验证题干是否准确、题目是否可解。

    ⚠️ 只给题干 + 签名 + 骨架，**绝不给原实现或测试代码** ——
    否则验证就失去意义（等于让它抄答案）。
    """
    root = Path(repo_root).resolve()
    target = root / rel_path
    original = target.read_text(encoding="utf-8")
    runner = TestRunner(root, python_bin, timeout=timeout)
    test_files = sorted({n.split("::", 1)[0] for n in (fail_to_pass + pass_to_pass)})

    report = SolveBackReport(solvable=False)
    # 函数体缩进：方法为 8 空格，模块级函数为 4 空格
    indent = "        " if stub.is_method else "    "

    try:
        for i in range(1, max_attempts + 1):
            messages = [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER.format(
                    statement=problem_statement,
                    signature=stub.signature.strip(),
                    skeleton=skeleton,
                    symbol=stub.symbol_path,
                )},
            ]
            if report.attempts:
                last = report.attempts[-1]
                messages.append({
                    "role": "user",
                    "content": (
                        f"上一次实现未通过测试（FAIL_TO_PASS {last.n_f2p_passed}/{last.n_f2p_total}）。"
                        f"{('错误信息：' + last.error) if last.error else ''}\n"
                        "请重新审视需求描述，给出修正后的函数体。"
                    ),
                })

            try:
                raw = client.chat(messages, model=model, max_tokens=4096, temperature=0.2)
            except LLMError as e:
                report.attempts.append(SolveAttempt(attempt=i, passed=False, error=f"LLM 调用失败：{e}"))
                continue

            body = _clean_body(raw, indent)
            if not body.strip():
                report.attempts.append(SolveAttempt(attempt=i, passed=False, error="LLM 未输出有效函数体"))
                continue

            candidate = _splice(stub.source_stubbed, stub, body)
            # 语法必须合法，否则跳过本次
            try:
                compile(candidate, str(target), "exec")
            except SyntaxError as e:
                report.attempts.append(SolveAttempt(
                    attempt=i, passed=False,
                    error=f"生成的代码语法错误：第 {e.lineno} 行 {e.msg}",
                    body_preview=body[:200],
                ))
                continue

            target.write_text(candidate, encoding="utf-8")
            res = runner.run(test_files)

            f2p_ok = [n for n in fail_to_pass if res.outcome_of(n) in ("passed", "xfailed")]
            p2p_ok = [n for n in pass_to_pass if res.outcome_of(n) in ("passed", "xfailed")]
            ok = (
                len(f2p_ok) == len(fail_to_pass)
                and len(p2p_ok) == len(pass_to_pass)
                and not res.collect_error
            )
            err = None
            if not ok:
                # 摘取首个断言/异常信息，便于回灌给模型修正
                m = re.search(r"^E\s+(\w*(?:Error|Exception|AssertionError)[^\n]*)",
                              res.stdout_tail, re.M)
                err = m.group(1)[:200] if m else None

            report.attempts.append(SolveAttempt(
                attempt=i, passed=ok,
                n_f2p_passed=len(f2p_ok), n_f2p_total=len(fail_to_pass),
                n_p2p_passed=len(p2p_ok), n_p2p_total=len(pass_to_pass),
                error=err, body_preview=body[:200],
            ))
            if ok:
                report.solvable = True
                report.first_pass_attempt = i
                report.reason = f"题干准确：LLM 第 {i} 次尝试即依据题干实现并通过全部判据"
                return report

        best = max((a.n_f2p_passed for a in report.attempts), default=0)
        report.reason = (
            f"题干可能不准确或过于含糊：{max_attempts} 次尝试均未通过"
            f"（最好一次 FAIL_TO_PASS {best}/{len(fail_to_pass)}）。"
            "常见原因是题干描述的行为与真实实现不一致。"
        )
        return report
    finally:
        target.write_text(original, encoding="utf-8")


# ------------------------------------------------- 通用版：整文件改写型 solve-back
#
# A 类挖空题只需要 LLM 补一个函数体，用 `solve_back` 即可。
# B 类（模块添加）与 C 类（重构）要改的是**整个文件**，需要另一种交互形式：
#     给「题目态的文件内容」→ 让 LLM 输出「改写后的完整文件内容」
#
# 与 A 类一致的核心纪律不变：**只给题干与可编辑文件，绝不给测试代码**。
# 否则 solve-back 就退化成「照着测试写」，无法检验题干本身是否说清楚了需求。

_EDIT_SYSTEM = """你是一位资深 Python 工程师，负责按需求文档完成一项代码任务。

要求：
- 严格依据需求文档实现，不要臆测文档之外的行为
- 输出 JSON 对象，形如 {"files": {"<文件路径>": "<该文件改写后的完整内容>"}}
- 文件内容必须是完整、可直接运行的 Python 源码，不要用 Markdown 代码块包裹
- 只修改被允许编辑的文件，不要新增或删除其它文件"""

_EDIT_USER = """## 任务需求
{statement}

## 你可以修改的文件（下面是它们当前的内容）
{files_block}

{extra}

请输出 JSON：`{{"files": {{"路径": "改写后的完整文件内容"}}}}`，
只包含上面列出的可修改文件。"""


def _strip_fence(text: str) -> str:
    """去掉 LLM 可能加上的 Markdown 代码块包裹。"""
    t = text.strip()
    m = re.match(r"^```(?:python|py)?\s*\n(.*?)\n?```\s*$", t, re.S)
    return m.group(1) if m else t


def solve_back_edits(
    client: TokenHubClient,
    repo_root: str | Path,
    task_files: dict[str, str],
    editable: list[str],
    problem_statement: str,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    *,
    extra_instructions: str = "",
    python_bin: str | None = None,
    model: str | None = None,
    max_attempts: int = 2,
    max_tokens: int = 8192,
    timeout: int = 600,
) -> SolveBackReport:
    """整文件改写型可解性验证（B / C 题型）。

    参数
    ----
    task_files:
        题目态的完整文件集合（起点，会先落到工作副本上）。
    editable:
        允许 LLM 改写的文件（必须是 `task_files` 的子集）。
        测试文件**不得**列入，否则等于让它改判据作弊。
    extra_instructions:
        题型相关的附加约束，例如重构题的「签名必须不变、指标必须下降」。
    max_tokens:
        必须留足 —— 这里要求模型输出**整个文件**，而推理模型的思维链还会额外占用额度。
        重构题的目标文件可达数百行，`8192` 往往不够（会被截断成语法错误），
        调用方应按文件规模放大。
    """
    root = Path(repo_root).resolve()
    runner = TestRunner(root, python_bin, timeout=timeout)
    test_files = sorted({n.split("::", 1)[0] for n in (fail_to_pass + pass_to_pass)})
    report = SolveBackReport(solvable=False)

    editable = [e for e in editable if e in task_files]
    if not editable:
        report.reason = "没有可编辑文件，无法执行 solve-back"
        return report

    # 快照 + 落题目态；结束后无条件还原
    touched = sorted(set(task_files))
    snap: dict[str, str | None] = {}
    for rel in touched:
        p = safe_repo_join(root, rel)
        snap[rel] = p.read_text(encoding="utf-8") if p.is_file() else None

    def _write(state: dict[str, str]) -> None:
        for rel, content in state.items():
            p = safe_repo_join(root, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    try:
        _write(task_files)
        files_block = "\n\n".join(
            f"### `{rel}`\n```python\n{task_files[rel]}\n```" for rel in editable
        )
        base_user = _EDIT_USER.format(
            statement=problem_statement,
            files_block=files_block,
            extra=extra_instructions or "",
        )

        for i in range(1, max_attempts + 1):
            messages = [
                {"role": "system", "content": _EDIT_SYSTEM},
                {"role": "user", "content": base_user},
            ]
            if report.attempts:
                last = report.attempts[-1]
                messages.append({
                    "role": "user",
                    "content": (
                        f"上一次实现未通过验证（判据 {last.n_f2p_passed}/{last.n_f2p_total} 通过）。"
                        f"{('错误信息：' + last.error) if last.error else ''}\n"
                        "请重新审视需求文档，输出修正后的完整文件内容。"
                    ),
                })

            try:
                data = client.chat_json(messages, model=model, max_tokens=max_tokens,
                                        temperature=0.2)
            except LLMError as e:
                report.attempts.append(SolveAttempt(attempt=i, passed=False,
                                                    error=f"LLM 调用失败：{e}"))
                continue

            files = data.get("files") if isinstance(data, dict) else None
            if not isinstance(files, dict) or not files:
                report.attempts.append(SolveAttempt(attempt=i, passed=False,
                                                    error="LLM 未按约定返回 files 字段"))
                continue

            # 只接受可编辑文件的改写；其余一律忽略（防止越权改测试）
            candidate = dict(task_files)
            bad: str | None = None
            for rel, content in files.items():
                rel_n = str(rel).replace("\\", "/")
                if rel_n not in editable:
                    continue
                text = _strip_fence(str(content))
                try:
                    compile(text, rel_n, "exec")
                except SyntaxError as e:
                    bad = f"{rel_n} 语法错误：第 {e.lineno} 行 {e.msg}"
                    break
                candidate[rel_n] = text
            if bad:
                report.attempts.append(SolveAttempt(attempt=i, passed=False, error=bad))
                continue
            if all(candidate[r] == task_files[r] for r in editable):
                report.attempts.append(SolveAttempt(attempt=i, passed=False,
                                                    error="LLM 未对可编辑文件做任何改动"))
                continue

            _write(candidate)
            res = runner.run(test_files)

            f2p_ok = [n for n in fail_to_pass if res.outcome_of(n) in ("passed", "xfailed")]
            p2p_ok = [n for n in pass_to_pass if res.outcome_of(n) in ("passed", "xfailed")]
            ok = (
                len(f2p_ok) == len(fail_to_pass)
                and len(p2p_ok) == len(pass_to_pass)
                and not res.collect_error
            )
            err = None
            if not ok:
                m = re.search(r"^E\s+(\w*(?:Error|Exception|AssertionError)[^\n]*)",
                              res.stdout_tail, re.M)
                err = m.group(1)[:300] if m else None
                if err is None:
                    m2 = re.search(r"^\s*(?:assert|AssertionError)[^\n]*", res.stdout_tail, re.M)
                    err = m2.group(0)[:300] if m2 else None

            report.attempts.append(SolveAttempt(
                attempt=i, passed=ok,
                n_f2p_passed=len(f2p_ok), n_f2p_total=len(fail_to_pass),
                n_p2p_passed=len(p2p_ok), n_p2p_total=len(pass_to_pass),
                error=err,
            ))
            if ok:
                report.solvable = True
                report.first_pass_attempt = i
                report.reason = f"题干准确：LLM 第 {i} 次尝试即依据题干完成任务并通过全部判据"
                return report
            # 下一轮回到题目态起点重试
            _write(task_files)

        best = max((a.n_f2p_passed for a in report.attempts), default=0)
        report.reason = (
            f"题干可能不准确或过于含糊：{max_attempts} 次尝试均未通过"
            f"（最好一次判据 {best}/{len(fail_to_pass)}）"
        )
        return report
    finally:
        for rel, content in snap.items():
            p = safe_repo_join(root, rel)
            try:
                if content is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(content, encoding="utf-8")
            except OSError:
                pass
