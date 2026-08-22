"""B 类题型「模块添加」出题器

课题原文要求题型覆盖「功能实现 / 重构 / **模块添加**」。本模块负责第三类。

与 A 类（AST 挖空）的本质差异
----------------------------
A 类的判据是**仓库自带测试**，标准答案是**原始实现** —— 两者都是既成事实，
所以 A 类天然可靠。B 类要「无中生有」一个新功能，判据和答案都得新造：

    判据（FAIL_TO_PASS）= LLM 新写的测试
    标准答案（golden）   = LLM 写的参考实现

风险显而易见：LLM 可能写出「测试与实现互相自洽但都是错的」，或者干脆写出
「不实现也能通过」的空洞测试。所以 B 类的质量完全依赖验证，而不依赖信任。

题目态与 golden 态怎么构造（关键设计）
------------------------------------
最朴素的做法是「题目态不放实现文件，让测试 import 失败」—— 但那样 pytest 在
**收集阶段**就报 ImportError，整个文件一个用例都不执行（无法产生逐用例判据），
和「测试变红」不是一回事。

因此这里沿用 A 类已验证的形态：

    题目态   = 骨架文件（签名 + docstring + raise NotImplementedError）+ 新测试
    golden 态 = 参考实现文件                                              + 新测试

于是新测试在题目态是**逐个 failed**（可精确成为 FAIL_TO_PASS），
在 golden 态全绿；且既有测试在两态都必须保持绿（PASS_TO_PASS）。

五道质量关卡（与 A 类同源，缺一不可）
----------------------------------
    ① 结构校验：路径合法、三段代码可解析、骨架与实现签名一致
    ② 骨架纯度：骨架必须只有 NotImplementedError，实现里不得残留
    ③ 双向 sanity：题目态必红、golden 态必绿、既有用例不受影响
    ④ solve-back：只看题干能否把功能实现出来 ← 终极判据
    ⑤ schema 强制校验 + 防作弊（测试文件列入 do_not_modify）
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..clients.tokenhub import LLMError, TokenHubClient
from ..schemas.task import REQUIRED_SECTIONS, Difficulty
from .repo_analyzer import DEFAULT_EXCLUDES
from .task_designer import DesignError, _clean_statement, _looks_leaky

__all__ = ["RepoLayout", "ModuleDraft", "detect_layout", "design_module_task",
           "MODULE_PROMPT_VERSION"]

MODULE_PROMPT_VERSION = "module-v1"

_TEST_FILE_RE = re.compile(r"(^test_.*\.py$)|(.*_test\.py$)")


# ------------------------------------------------------------------ 仓库结构探测


@dataclass
class RepoLayout:
    """出 B 类题所需的仓库结构信息。"""

    package_dir: str                       # 包目录的物理路径，如 "cachecontrol" 或 "src/itsdangerous"
    tests_dir: str                         # 测试目录，如 "tests"
    import_name: str = ""                  # 真正的 import 包名（src 布局时为 "itsdangerous"，非 src 时同 package_dir）
    module_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    sample_module: str = ""                # 代码风格样例（截断）
    sample_test: str = ""                  # 测试风格样例（截断）
    third_party: list[str] = field(default_factory=list)   # 仓库已有的外部依赖

    @property
    def import_prefix(self) -> str:
        # src 布局下物理路径是 "src/itsdangerous"，但 import 名是 "itsdangerous"
        return (self.import_name or self.package_dir).replace("/", ".")


def _iter_py(root: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in DEFAULT_EXCLUDES and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def _third_party_imports(root: Path, package_dir: str, import_name: str = "") -> list[str]:
    """粗略统计仓库用到的外部依赖顶层名（供 LLM 知道可以用什么）。"""
    import sys

    stdlib = set(getattr(sys, "stdlib_module_names", ()) or ())
    # 本仓库自身的顶层模块/包，以及测试框架，都不算「可用的第三方依赖」
    local = {p.stem if p.is_file() else p.name for p in root.iterdir()
             if p.name.endswith(".py") or p.is_dir()}
    local |= {package_dir.split("/")[0], import_name or "", "pytest", "setuptools", "tests"}
    seen: dict[str, int] = {}
    for p in _iter_py(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m and m not in stdlib and m not in local and not m.startswith("_"):
                    seen[m] = seen.get(m, 0) + 1
    return [m for m, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:10]]


def detect_layout(repo_root: str | Path) -> RepoLayout:
    """探测包目录、测试目录与代码风格样例。

    Raises
    ------
    DesignError
        找不到可用的包目录或测试目录 —— 该仓库不适合出 B 类题。
    """
    root = Path(repo_root).resolve()

    # 包目录：含 __init__.py 的目录，取「非测试模块数」最多的那个。
    # ⚠️ 必须排除测试目录（实测踩过）：`tests/` 常常也有 __init__.py，
    #    且文件数比业务包更多，按文件数排序会把 `tests` 误判为主包。
    # ⚠️ 支持 src 布局（实测 itsdangerous）：包在 `src/<name>/` 下，
    #    顶层只有 `src/`，此时物理路径为 "src/<name>"，但 import 名是 "<name>"。
    pkg_candidates: list[tuple[int, str, str]] = []   # (模块数, 物理路径, import 名)
    for base_name, base_dir in [(".", root), ("src", root / "src")]:
        if not base_dir.is_dir():
            continue
        for child in sorted(base_dir.iterdir()):
            if not child.is_dir() or child.name in DEFAULT_EXCLUDES or child.name.startswith("."):
                continue
            if "test" in child.name.lower():
                continue
            if not (child / "__init__.py").exists():
                continue
            n = sum(1 for p in child.rglob("*.py") if not _TEST_FILE_RE.match(p.name))
            if n:
                rel = child.relative_to(root).as_posix()
                pkg_candidates.append((n, rel, child.name))
    if not pkg_candidates:
        raise DesignError("找不到业务包目录（含 __init__.py 且非测试目录），"
                          "不适合出模块添加题")
    _, package_dir, import_name = max(pkg_candidates)

    # 测试目录：测试文件最集中的目录
    test_files = [str(p.relative_to(root)) for p in _iter_py(root)
                  if _TEST_FILE_RE.match(p.name)]
    if not test_files:
        raise DesignError("仓库没有测试目录，无法为新功能提供判据")
    dir_count: dict[str, int] = {}
    for t in test_files:
        d = str(Path(t).parent).replace("\\", "/")
        dir_count[d] = dir_count.get(d, 0) + 1
    tests_dir = max(dir_count.items(), key=lambda kv: kv[1])[0]
    if tests_dir == ".":
        tests_dir = "tests"

    module_files = sorted(
        str(p.relative_to(root)) for p in (root / package_dir).rglob("*.py")
        if not _TEST_FILE_RE.match(p.name)
    )

    def _head(rel: str, limit: int = 2500) -> str:
        try:
            return (root / rel).read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return ""

    # 样例挑「体量中等」的文件，既能体现风格又不至于过长
    def _pick(cands: list[str]) -> str:
        sized = []
        for rel in cands:
            try:
                sized.append(((root / rel).stat().st_size, rel))
            except OSError:
                continue
        sized = [s for s in sized if 800 <= s[0] <= 12000] or sized
        if not sized:
            return ""
        sized.sort()
        return sized[len(sized) // 2][1]

    sample_mod = _pick([m for m in module_files if not m.endswith("__init__.py")])
    sample_tst = _pick(test_files)

    return RepoLayout(
        package_dir=package_dir,
        tests_dir=tests_dir,
        import_name=import_name,
        module_files=module_files,
        test_files=test_files,
        sample_module=_head(sample_mod) if sample_mod else "",
        sample_test=_head(sample_tst, 2000) if sample_tst else "",
        third_party=_third_party_imports(root, package_dir, import_name),
    )


# ------------------------------------------------------------------ 草稿


@dataclass
class ModuleDraft:
    """B 类题目草稿（已通过结构校验）。"""

    title: str
    difficulty: Difficulty
    problem_statement: str
    module_path: str          # 新增实现文件（相对仓库根）
    skeleton_code: str        # 题目态内容
    impl_code: str            # golden 态内容
    test_path: str            # 新增测试文件
    test_code: str
    public_symbols: list[str] = field(default_factory=list)
    raw: dict | None = None


# ------------------------------------------------------------------ Prompt

_SYSTEM = """你是一位资深开源项目维护者，负责为项目设计「新增功能模块」类型的编程题目。
题目将用于评测 AI Coding Agent，因此必须做到：
1. 需求描述完整、无歧义，只看题干就能写出实现
2. 配套测试是可信判据：没有实现时必然失败，实现正确时必然通过
3. 骨架文件与参考实现的公开接口完全一致，只有函数体不同"""

_USER_TMPL = """请为下面这个开源项目设计一道「新增功能模块」题目。

## 项目信息
- 仓库：{repo}（{stars} stars）
- 包目录：`{package_dir}`（导入前缀 `{import_prefix}`）
- 测试目录：`{tests_dir}`
- 现有模块（部分）：{module_list}
- 项目已有的第三方依赖：{third_party}

## 现有模块的代码风格参考
```python
{sample_module}
```

## 现有测试的风格参考
```python
{sample_test}
```

---

## 你要产出什么

设计一个**与本项目领域高度相关、但项目当前还没有**的新功能模块，并同时给出：
骨架文件、参考实现、配套测试、题目描述。

输出一个 JSON 对象，字段如下：

- `feature_slug`：功能的英文短名，小写下划线，3~30 字符（如 `cache_key_normalizer`）
- `title`：一句话标题（不超过 30 字）
- `difficulty`：`"easy"` | `"medium"` | `"hard"`
- `module_path`：新模块路径，**必须**形如 `{package_dir}/<feature_slug>.py`
- `test_path`：新测试路径，**必须**形如 `{tests_dir}/test_<feature_slug>_synth.py`
- `skeleton_code`：骨架文件的**完整内容**
- `impl_code`：参考实现文件的**完整内容**
- `test_code`：测试文件的**完整内容**
- `problem_statement`：Markdown 题干，必须严格包含以下 6 个二级标题，顺序一致：
  `## 背景与上下文`（项目背景 + 为什么需要这个功能，2-4 句）
  `## 需要实现的功能`（要做什么，说清职责边界，但不给算法步骤）
  `## 输入`（每个公开函数/类的参数：含义、类型、特殊取值）
  `## 输出`（返回值类型与各种情况下的返回内容，异常语义也写在这里）
  `## 预期行为`（编号列出所有需要处理的情况与边界，这是最重要的部分）
  `## 约束条件`（至少包含：新模块路径、不得修改任何测试文件、不得引入新的第三方依赖）

## 硬性要求（违反任意一条，题目作废）

### 关于功能设计
1. 必须是**纯逻辑**功能：不得有网络请求、文件读写、子进程、随机数、
   依赖当前时间等副作用 —— 否则测试不确定，无法作为判据
2. 只允许使用 Python 标准库和上面列出的项目已有依赖，**不得引入新依赖**
3. 功能要自成一体：不修改项目里任何现有文件，只新增这一个模块
4. 规模适中：2~4 个公开函数（或 1 个类 + 若干方法），实现约 30~80 行

### 关于骨架文件 `skeleton_code`
5. 必须包含全部 import、全部公开符号的**完整签名**、完整 docstring、
   以及必要的模块级常量定义
6. 每个函数/方法的函数体**只能**是 `raise NotImplementedError(...)`，
   不得包含任何真实逻辑
7. 骨架与 `impl_code` 的公开符号名称、参数列表必须**逐字一致**

### 关于参考实现 `impl_code`
8. 必须是完整、正确、可直接运行的实现，**不得出现** `NotImplementedError`
9. 必须能让 `test_code` 全部通过

### 关于测试 `test_code`
10. 用 pytest 风格，包含 5~10 个 `test_` 函数，覆盖正常路径与边界情况
11. 必须 `from {import_prefix}.<feature_slug> import ...` 导入被测功能，
    并对返回值做**具体的断言**（不允许只断言"不抛异常"）
12. 断言必须严格到「不实现就一定失败」的程度
13. 不得使用 mock/monkeypatch 绕过待实现逻辑，不得出现 `NotImplementedError`

### 关于题干 `problem_statement`
14. 禁止包含实现代码、算法逐步伪代码
15. 禁止提及测试函数名，也不要写"让某个测试通过"
16. 必须把所有关键约定写清楚（返回值格式、异常类型、边界取值的处理），
    因为做题者看不到测试，只能依据题干实现
17. 用中文书写，专业术语保留英文"""


# ------------------------------------------------------------------ 结构校验

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,29}$")


def _public_api(source: str) -> dict[str, list[str]]:
    """抽取模块的公开 API：{符号路径: 参数名列表}。用于比对骨架与实现是否一致。"""
    out: dict[str, list[str]] = {}

    def args_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        a = node.args
        names = [p.arg for p in (a.posonlyargs or [])] + [p.arg for p in a.args]
        if a.vararg:
            names.append("*" + a.vararg.arg)
        names += [p.arg for p in a.kwonlyargs]
        if a.kwarg:
            names.append("**" + a.kwarg.arg)
        return names

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                out[node.name] = args_of(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            out[node.name] = []
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not sub.name.startswith("_") or sub.name == "__init__":
                        out[f"{node.name}.{sub.name}"] = args_of(sub)
    return out


def _skeleton_is_hollow(source: str) -> str | None:
    """确认骨架里每个函数体都只是 NotImplementedError（不含真实逻辑）。"""
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        raises_ni = any(
            isinstance(s, ast.Raise) and (
                (isinstance(s.exc, ast.Call) and getattr(s.exc.func, "id", "") == "NotImplementedError")
                or getattr(s.exc, "id", "") == "NotImplementedError"
            )
            for s in body
        )
        if not raises_ni or len(body) > 1:
            offenders.append(node.name)
    return (f"骨架中这些函数体不是单条 raise NotImplementedError：{offenders}"
            if offenders else None)


def _count_tests(source: str) -> int:
    tree = ast.parse(source)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            n += 1
    return n


def _n_assertions(source: str) -> int:
    return sum(1 for node in ast.walk(ast.parse(source))
               if isinstance(node, (ast.Assert,))
               or (isinstance(node, ast.With)
                   and any("raises" in ast.dump(i.context_expr) for i in node.items)))


def _validate(data: dict, layout: RepoLayout, repo_root: Path) -> tuple[ModuleDraft | None, list[str]]:
    """对 LLM 输出做全面结构校验，返回 (草稿, 问题列表)。"""
    problems: list[str] = []

    slug = str(data.get("feature_slug") or "").strip()
    if not _SLUG_RE.match(slug):
        problems.append(f"feature_slug 不合法：{slug!r}（需小写字母/数字/下划线，3~30 字符）")

    module_path = str(data.get("module_path") or "").replace("\\", "/").strip()
    test_path = str(data.get("test_path") or "").replace("\\", "/").strip()

    def _path_ok(p: str, field: str, must_prefix: str, must_name: str = "") -> bool:
        if not p.endswith(".py"):
            problems.append(f"{field} 必须以 .py 结尾：{p!r}")
            return False
        if p.startswith("/") or ".." in Path(p).parts:
            problems.append(f"{field} 必须是仓库内的相对路径且不含 ..：{p!r}")
            return False
        if not p.startswith(must_prefix.rstrip("/") + "/"):
            problems.append(f"{field} 必须位于 `{must_prefix}` 目录下：{p!r}")
            return False
        if must_name and Path(p).name != must_name:
            problems.append(f"{field} 文件名必须是 `{must_name}`：{p!r}")
            return False
        if (repo_root / p).exists():
            problems.append(f"{field} 指向的文件已存在，不能覆盖仓库现有文件：{p!r}")
            return False
        return True

    ok_mod = _path_ok(module_path, "module_path", layout.package_dir,
                      f"{slug}.py" if _SLUG_RE.match(slug) else "")
    ok_tst = _path_ok(test_path, "test_path", layout.tests_dir,
                      f"test_{slug}_synth.py" if _SLUG_RE.match(slug) else "")

    skeleton = str(data.get("skeleton_code") or "")
    impl = str(data.get("impl_code") or "")
    tests = str(data.get("test_code") or "")

    for name, src in (("skeleton_code", skeleton), ("impl_code", impl), ("test_code", tests)):
        if not src.strip():
            problems.append(f"{name} 为空")
            continue
        try:
            ast.parse(src)
        except SyntaxError as e:
            problems.append(f"{name} 语法错误：第 {e.lineno} 行 {e.msg}")

    if problems:
        return None, problems

    # 骨架必须是空壳；实现必须没有残留
    hollow = _skeleton_is_hollow(skeleton)
    if hollow:
        problems.append(hollow)
    if "NotImplementedError" in impl:
        problems.append("impl_code 中出现 NotImplementedError —— 参考实现必须完整")
    if "NotImplementedError" in tests:
        problems.append("test_code 中出现 NotImplementedError —— 测试不得迁就未实现状态")

    # 公开 API 必须逐字一致，否则 solve-back 与判分都会错位
    api_s, api_i = _public_api(skeleton), _public_api(impl)
    if api_s != api_i:
        only_s = {k: v for k, v in api_s.items() if api_i.get(k) != v}
        only_i = {k: v for k, v in api_i.items() if api_s.get(k) != v}
        problems.append(f"骨架与实现的公开接口不一致：骨架 {only_s}，实现 {only_i}")
    if not api_s:
        problems.append("骨架没有任何公开符号")

    # 测试必须真的 import 新模块
    if ok_mod:
        import_name = module_path[:-3].replace("/", ".")
        if import_name not in tests:
            problems.append(f"test_code 未导入新模块 `{import_name}`，测试与实现无关")
    n_tests = _count_tests(tests)
    if n_tests < 3:
        problems.append(f"test_code 只有 {n_tests} 个测试函数，判据不充分（要求 ≥3）")
    if _n_assertions(tests) < n_tests:
        problems.append("test_code 的断言数少于测试函数数 —— 存在没有具体断言的空测试")
    for bad in ("monkeypatch", "unittest.mock", "MagicMock", "mocker"):
        if bad in tests:
            problems.append(f"test_code 使用了 {bad} —— 不得用 mock 绕过待实现逻辑")
            break

    # 题干校验
    stmt = _clean_statement(str(data.get("problem_statement") or ""))
    if len(stmt) < 400:
        problems.append(f"题干过短（{len(stmt)} 字符），做题者无法据此实现")
    missing = [k for k in REQUIRED_SECTIONS if k not in stmt]
    if missing:
        problems.append(f"题干缺少必需小节：{missing}")
    leak = _looks_leaky(stmt, impl)
    if leak:
        problems.append(leak)
    for m in re.findall(r"\bdef\s+(test_\w+)", tests):
        if m in stmt:
            problems.append(f"题干提及了测试名 {m}，请删除")
            break
    # 题干必须写清新模块路径，否则做题者不知道把代码放哪
    if ok_mod and module_path not in stmt and Path(module_path).name not in stmt:
        problems.append(f"题干未说明新模块应创建在 `{module_path}`")

    if problems:
        return None, problems

    diff_raw = str(data.get("difficulty") or "").lower()
    if diff_raw not in ("easy", "medium", "hard"):
        diff_raw = "medium"

    return ModuleDraft(
        title=str(data.get("title") or "")[:60],
        difficulty=Difficulty(diff_raw),
        problem_statement=stmt,
        module_path=module_path,
        skeleton_code=skeleton if skeleton.endswith("\n") else skeleton + "\n",
        impl_code=impl if impl.endswith("\n") else impl + "\n",
        test_path=test_path,
        test_code=tests if tests.endswith("\n") else tests + "\n",
        public_symbols=sorted(api_i),
        raw=data,
    ), []


# ------------------------------------------------------------------ 主入口


def design_module_task(
    client: TokenHubClient,
    repo: str,
    stars: int,
    repo_root: str | Path,
    layout: RepoLayout,
    *,
    model: str | None = None,
    max_attempts: int = 3,
    max_tokens: int = 8192,
    avoid_slugs: set[str] | None = None,
) -> ModuleDraft:
    """让 LLM 设计一道「模块添加」题，并做全面结构校验，失败自动回灌重试。

    `avoid_slugs`：已出过的功能短名，避免同一仓库反复出相同功能的题。
    """
    root = Path(repo_root).resolve()
    user = _USER_TMPL.format(
        repo=repo,
        stars=stars,
        package_dir=layout.package_dir,
        import_prefix=layout.import_prefix,
        tests_dir=layout.tests_dir,
        module_list=", ".join(f"`{m}`" for m in layout.module_files[:20]) or "（无）",
        third_party=", ".join(layout.third_party) or "（无，仅标准库）",
        sample_module=layout.sample_module or "（无样例）",
        sample_test=layout.sample_test or "（无样例）",
    )
    if avoid_slugs:
        user += ("\n\n## 已经出过的功能（请设计一个明显不同的功能，不要重复）\n"
                 + ", ".join(sorted(avoid_slugs)))

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
            data = client.chat_json(messages, model=model, max_tokens=max_tokens, temperature=0.4)
        except LLMError as e:
            problems = [f"调用失败：{e}"]
            continue

        draft, problems = _validate(data if isinstance(data, dict) else {}, layout, root)
        if draft is not None:
            return draft

    raise DesignError(f"模块添加题设计失败（尝试 {max_attempts} 次），最后的问题：{problems}")
