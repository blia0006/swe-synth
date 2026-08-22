"""本地双向 sanity 校验器（题目合法性的最终裁判）

一道题目要合法，必须同时满足三个条件 —— 缺一即 REJECT：

    1. stub 态：FAIL_TO_PASS 全红   ← 证明「题目有内容」（不是已经实现好了）
    2. stub 态：PASS_TO_PASS 全绿   ← 证明「挖空精准」（没有牵连无关功能）
    3. golden 态：全部变绿          ← 证明「题目可解」（原实现就是标准答案）

再加一条工程性校验：
    4. 确定性：同一状态连跑两次结果一致（剔除 flaky 测试，避免误判）

为什么必须在本地做（而不是全丢给沙箱）
------------------------------------
沙箱按运行时长计费，且启动、拉镜像都有开销。挖空过度、测试本身就红这类问题
在本地就能发现，没必要花钱去云上发现。**只有通过本地 sanity 的候选才值得打包成镜像。**

安全说明
--------
本模块会在临时目录里执行目标仓库的测试代码 —— 这是功能所必需（要跑测试才能判分），
但存在执行第三方代码的固有风险。因此：
  · 仓库来源限定为 `config/repos.yaml` 白名单，不接受任意用户输入的仓库
  · 所有子进程调用均使用**参数列表**形式（不经 shell），避免命令注入
  · 强制超时，防止死循环测试挂住流水线
  · 建议在容器/CI 等隔离环境中运行批量任务
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TestOutcome", "RunResult", "SanityReport", "TestRunner",
           "run_sanity", "run_sanity_edits", "safe_repo_join"]

# pytest 退出码语义（见 pytest 文档）
_PYTEST_OK = 0
_PYTEST_TESTS_FAILED = 1
_PYTEST_NO_TESTS = 5


@dataclass
class TestOutcome:
    """单条测试用例的结果。"""

    nodeid: str
    outcome: str        # passed / failed / error / skipped / xfailed / xpassed

    @property
    def is_pass(self) -> bool:
        return self.outcome in ("passed", "xfailed")

    @property
    def is_fail(self) -> bool:
        return self.outcome in ("failed", "error")


@dataclass
class RunResult:
    """一次测试运行的完整结果。"""

    returncode: int
    duration_sec: float
    outcomes: dict[str, str] = field(default_factory=dict)   # nodeid -> outcome
    stdout_tail: str = ""
    collect_error: bool = False       # 收集阶段就失败（通常是 SyntaxError / ImportError）
    timed_out: bool = False

    @property
    def passed(self) -> list[str]:
        return [n for n, o in self.outcomes.items() if o in ("passed", "xfailed")]

    @property
    def failed(self) -> list[str]:
        return [n for n, o in self.outcomes.items() if o in ("failed", "error")]

    def outcome_of(self, nodeid: str) -> str | None:
        return self.outcomes.get(nodeid)


@dataclass
class SanityReport:
    """双向 sanity 的最终结论。"""

    ok: bool
    reason: str = ""
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    stub_run: RunResult | None = None
    golden_run: RunResult | None = None
    deterministic: bool | None = None
    duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "fail_to_pass": self.fail_to_pass,
            "pass_to_pass": self.pass_to_pass,
            "deterministic": self.deterministic,
            "duration_sec": round(self.duration_sec, 2),
            "stub_summary": {
                "returncode": self.stub_run.returncode if self.stub_run else None,
                "n_passed": len(self.stub_run.passed) if self.stub_run else 0,
                "n_failed": len(self.stub_run.failed) if self.stub_run else 0,
            },
            "golden_summary": {
                "returncode": self.golden_run.returncode if self.golden_run else None,
                "n_passed": len(self.golden_run.passed) if self.golden_run else 0,
                "n_failed": len(self.golden_run.failed) if self.golden_run else 0,
            },
        }


class TestRunner:
    """在指定 Python 环境中运行 pytest，并解析出逐用例结果。

    结果解析策略（按可靠性降序）：
      1. `--report-log`（JSON Lines）—— 最可靠，但**并非所有 pytest 版本都支持**：
         它由 `pytest-reportlog` 插件提供；pytest 9 起已从核心移除。
         传入不支持的参数会导致 **整个命令以 usage error(退出码 4) 失败**，
         因此必须先探测可用性，不能盲传。
      2. `-v` 文本输出解析 —— 通用兜底，格式为 `nodeid PASSED [ 10%]`。
    """

    def __init__(
        self,
        repo_root: str | Path,
        python_bin: str | None = None,
        *,
        timeout: int = 900,
        env: dict[str, str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.python_bin = python_bin or sys.executable
        self.timeout = timeout
        self.env = env
        self._has_report_log: bool | None = None   # None = 尚未探测

    # -------------------------------------------------- 内部
    def _supports_report_log(self) -> bool:
        """探测当前环境的 pytest 是否支持 --report-log（结果缓存，只探一次）。"""
        if self._has_report_log is not None:
            return self._has_report_log
        try:
            p = subprocess.run(
                [self.python_bin, "-m", "pytest", "--help"],
                cwd=self.repo_root, env=self._base_env(),
                capture_output=True, text=True, errors="replace", timeout=120,
            )
            self._has_report_log = "--report-log" in (p.stdout or "")
        except (subprocess.SubprocessError, OSError):
            self._has_report_log = False
        return self._has_report_log

    def _base_env(self) -> dict[str, str]:
        e = dict(os.environ)
        # 让测试结果尽量确定：禁用哈希随机化、禁用字节码缓存、统一编码
        e["PYTHONHASHSEED"] = "0"
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        e["PYTHONIOENCODING"] = "utf-8"
        e["PYTHONUNBUFFERED"] = "1"
        # 避免测试读到开发者本地配置
        e.pop("PYTEST_ADDOPTS", None)
        # ⚠️ 隔离性修复：继承宿主机 PATH 会导致「裸命令名调用」（如测试内部
        # subprocess.run(["dotenv", ...])）优先命中本项目自己 .venv/bin 下的
        # 同名可执行文件，而不是目标仓库专用 venv 里刚装好的版本 —— 这是
        # 本地预筛与真实（完全隔离的）Docker 沙箱环境的一处行为差异，会把
        # 健康仓库误判为「基线不绿」。把目标解释器所在 bin 目录前置到 PATH，
        # 使裸命令查找优先落在目标 venv 内，贴近沙箱隔离语义。
        # 注意：不能用 Path(...).resolve() —— venv 内 python 通常是指向系统
        # 解释器的符号链接，resolve() 会一路追踪到系统安装目录，而不是 venv
        # 的 bin 目录，导致 venv 专属可执行文件（如上例的 dotenv）仍然找不到。
        py_bin_dir = str(Path(self.python_bin).absolute().parent)
        e["PATH"] = py_bin_dir + os.pathsep + e.get("PATH", "")
        if self.env:
            e.update(self.env)
        return e

    @staticmethod
    def _parse_report_log(path: Path) -> dict[str, str]:
        """解析 pytest --report-log 产生的 JSON Lines。"""
        outcomes: dict[str, str] = {}
        if not path.exists():
            return outcomes
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("$report_type") != "TestReport":
                    continue
                nodeid, when = rec.get("nodeid"), rec.get("when")
                outcome = rec.get("outcome")
                if not nodeid or not outcome:
                    continue
                # setup/teardown 出错也算这条用例失败
                if when in ("setup", "teardown") and outcome == "failed":
                    outcomes[nodeid] = "error"
                elif when == "call":
                    if outcome == "failed" and rec.get("wasxfail"):
                        outcomes[nodeid] = "xfailed"
                    elif outcome == "passed" and rec.get("wasxfail"):
                        outcomes[nodeid] = "xpassed"
                    else:
                        outcomes.setdefault(nodeid, outcome)
                elif when == "setup" and outcome == "skipped":
                    outcomes.setdefault(nodeid, "skipped")
        return outcomes

    _VERBOSE_RE = re.compile(
        r"^(?P<nodeid>\S+::\S+?)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
        re.M,
    )

    # ⚠️ pytest 9 即使在非 TTY 下也可能输出 ANSI 颜色码（实测 humanize@9.1.1：
    #   `tests/...::test_x \x1b[32mPASSED\x1b[0m`），导致 `^...\s+PASSED\b` 正则
    #   匹配失败、outcomes 解析为空，被误判成「未收集到任何用例」。
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

    @classmethod
    def _parse_verbose(cls, stdout: str) -> dict[str, str]:
        """退化方案：从 `pytest -v` 文本里提取结果（先剥离 ANSI 颜色码）。"""
        mapping = {
            "PASSED": "passed", "FAILED": "failed", "ERROR": "error",
            "SKIPPED": "skipped", "XFAIL": "xfailed", "XPASS": "xpassed",
        }
        clean = cls._ANSI_RE.sub("", stdout)
        out: dict[str, str] = {}
        for m in cls._VERBOSE_RE.finditer(clean):
            out[m.group("nodeid")] = mapping[m.group("outcome")]
        # 兼容「短摘要」格式：FAILED tests/x.py::test_y - AssertionError
        for m in re.finditer(r"^(?:FAILED|ERROR)\s+(\S+::\S+)", clean, re.M):
            out.setdefault(m.group(1), "failed")
        return out

    # -------------------------------------------------- 对外
    def run(self, targets: list[str] | None = None, *, extra_args: list[str] | None = None) -> RunResult:
        """运行测试。`targets` 为空则跑全量。"""
        use_log = self._supports_report_log()
        log_path = self.repo_root / f".swe_synth_report_{os.getpid()}.jsonl"
        cmd = [
            self.python_bin, "-m", "pytest",
            "-v", "-p", "no:cacheprovider", "--no-header",
        ]
        if use_log:
            cmd.append(f"--report-log={log_path}")
        if extra_args:
            cmd += extra_args
        if targets:
            cmd += targets

        t0 = time.time()
        timed_out = False
        try:
            # 不经 shell，参数以列表传入，避免命令注入
            proc = subprocess.run(
                cmd, cwd=self.repo_root, env=self._base_env(),
                capture_output=True, text=True, errors="replace",
                timeout=self.timeout,
            )
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            rc, timed_out = 124, True
            stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        except FileNotFoundError:
            return RunResult(returncode=127, duration_sec=0.0,
                             stdout_tail=f"找不到 Python 解释器：{self.python_bin}")
        dur = time.time() - t0

        outcomes = self._parse_report_log(log_path) if use_log else {}
        if not outcomes:
            outcomes = self._parse_verbose(stdout)
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass

        combined = (stdout + "\n" + stderr).strip()
        collect_error = bool(
            re.search(r"ERROR collecting|INTERNALERROR|ImportError while loading conftest", combined)
        )
        # pytest 退出码 4 = 使用方式错误（如传了不支持的参数）——必须显式识别，
        # 否则会被误判成「没收集到测试」，浪费大量排查时间
        usage_error = rc == 4 or "unrecognized arguments" in combined
        if usage_error:
            collect_error = True

        return RunResult(
            returncode=rc,
            duration_sec=dur,
            outcomes=outcomes,
            stdout_tail=combined[-4000:],
            collect_error=collect_error,
            timed_out=timed_out,
        )


# ------------------------------------------------------------------ 双向 sanity

def run_sanity(
    repo_root: str | Path,
    target_file_rel: str,
    stubbed_source: str,
    *,
    python_bin: str | None = None,
    test_targets: list[str] | None = None,
    timeout: int = 900,
    check_determinism: bool = True,
    max_fail_to_pass: int = 50,
) -> SanityReport:
    """执行双向 sanity 校验。

    流程
    ----
      1. 基线（golden 态，即仓库原始代码）跑一次 → 记录哪些用例本来就是绿的
      2. 写入 stub 后再跑一次 → 对比得出 FAIL_TO_PASS / PASS_TO_PASS
      3. 恢复原文件，确认 golden 态确实全绿
      4.（可选）stub 态再跑一次，验证结果确定性

    注意：这里的顺序是「先 golden 后 stub」，因为要先拿到基线。
    基线不绿的用例会被排除在 PASS_TO_PASS 之外（它们本来就红，与本题无关）。
    """
    root = Path(repo_root).resolve()
    target = root / target_file_rel
    if not target.is_file():
        return SanityReport(ok=False, reason=f"目标文件不存在：{target_file_rel}")

    runner = TestRunner(root, python_bin, timeout=timeout)
    original = target.read_text(encoding="utf-8")
    t_start = time.time()

    try:
        # ---------- 1) 基线（= golden 态）
        base = runner.run(test_targets)
        if base.timed_out:
            return SanityReport(ok=False, reason=f"基线测试超时（>{timeout}s），仓库不适合", golden_run=base)
        if base.collect_error:
            return SanityReport(ok=False, reason="基线测试收集失败（依赖缺失或 import 错误）", golden_run=base)
        if base.returncode == _PYTEST_NO_TESTS or not base.outcomes:
            return SanityReport(ok=False, reason="基线未收集到任何测试用例", golden_run=base)

        baseline_pass = set(base.passed)
        if not baseline_pass:
            return SanityReport(ok=False, reason="基线没有任何通过的用例，仓库基线不绿", golden_run=base)

        # ---------- 2) stub 态
        target.write_text(stubbed_source, encoding="utf-8")
        stub = runner.run(test_targets)
        if stub.timed_out:
            return SanityReport(ok=False, reason="stub 态测试超时", stub_run=stub, golden_run=base)
        if stub.collect_error:
            # 挖空导致 import/收集失败 → 挖空破坏了模块结构，题目不合法
            return SanityReport(
                ok=False,
                reason="stub 态测试收集失败（挖空破坏了模块可导入性）",
                stub_run=stub, golden_run=base,
            )

        # FAIL_TO_PASS：基线绿 → stub 红。这是题目的核心判据
        f2p = sorted(n for n in baseline_pass if stub.outcome_of(n) in ("failed", "error"))
        # PASS_TO_PASS：基线绿 → stub 仍绿。用于证明挖空没有牵连无关功能
        p2p = sorted(n for n in baseline_pass if stub.outcome_of(n) in ("passed", "xfailed"))

        if not f2p:
            return SanityReport(
                ok=False,
                reason="挖空后没有任何测试变红 —— 该函数无有效测试覆盖，无法自动判分",
                stub_run=stub, golden_run=base,
            )
        if len(f2p) > max_fail_to_pass:
            return SanityReport(
                ok=False,
                reason=f"挖空导致 {len(f2p)} 个用例变红（阈值 {max_fail_to_pass}），牵连过广",
                fail_to_pass=f2p, pass_to_pass=p2p, stub_run=stub, golden_run=base,
            )

        # ---------- 4) 确定性校验（仍在 stub 态）
        deterministic: bool | None = None
        if check_determinism:
            stub2 = runner.run(test_targets)
            same_f2p = sorted(n for n in baseline_pass if stub2.outcome_of(n) in ("failed", "error"))
            deterministic = same_f2p == f2p
            if not deterministic:
                flaky = set(f2p) ^ set(same_f2p)
                return SanityReport(
                    ok=False,
                    reason=f"测试结果不确定（flaky），两次 FAIL_TO_PASS 不一致，差异：{sorted(flaky)[:5]}",
                    fail_to_pass=f2p, pass_to_pass=p2p,
                    stub_run=stub, golden_run=base, deterministic=False,
                )

        # ---------- 3) 恢复 golden，确认 F2P 确实能全绿
        target.write_text(original, encoding="utf-8")
        golden = runner.run(sorted(set(f2p) | set(p2p)) or test_targets)
        still_red = [n for n in f2p if golden.outcome_of(n) not in ("passed", "xfailed")]
        if still_red:
            return SanityReport(
                ok=False,
                reason=f"golden 态仍有 {len(still_red)} 个 FAIL_TO_PASS 未通过：{still_red[:3]}",
                fail_to_pass=f2p, pass_to_pass=p2p,
                stub_run=stub, golden_run=golden, deterministic=deterministic,
            )

        return SanityReport(
            ok=True,
            reason="双向 sanity 通过：stub 态必红、golden 态必绿",
            fail_to_pass=f2p,
            pass_to_pass=p2p,
            stub_run=stub,
            golden_run=golden,
            deterministic=deterministic,
            duration_sec=time.time() - t_start,
        )
    finally:
        # 无论成功失败，务必恢复原文件，避免污染工作副本
        try:
            target.write_text(original, encoding="utf-8")
        except OSError:
            pass


# ------------------------------------------------------- 通用（多文件）双向 sanity
#
# `run_sanity` 只能处理「改一个已有文件」的 A 类挖空题。
# B 类（模块添加）与 C 类（重构）需要同时**新增测试文件**、**新增实现文件**，
# 甚至改多个文件，因此需要一个更通用的版本。
#
# 三类题型的判定规则被统一为一条（这是能低成本复用的关键）：
#     FAIL_TO_PASS = 题目态红 且 golden 态绿
#     PASS_TO_PASS = 题目态绿 且 golden 态绿 且（基线绿 或 属于本题新增的测试文件）
#
# 对 A 类，「基线绿」等价于原 `run_sanity` 的语义；
# 对 B/C 类，新增的测试文件在基线中并不存在，故对其豁免「基线绿」这一条。


def safe_repo_join(root: Path, rel: str) -> Path:
    """把相对路径安全地拼到仓库根下。

    ⚠️ 安全要求：B/C 题型的文件路径由 **LLM 生成**，属不可信输入。
    必须拒绝绝对路径与 `..` 逃逸，否则可能写到仓库外的任意位置。
    """
    if not rel or rel.startswith(("/", "\\")) or ":" in rel[:3]:
        raise ValueError(f"必须是相对路径：{rel!r}")
    p = (root / rel).resolve()
    root_r = root.resolve()
    if p != root_r and root_r not in p.parents:
        raise ValueError(f"路径逃逸仓库根目录：{rel!r}")
    return p


def _snapshot(root: Path, rels: Iterable[str]) -> dict[str, str | None]:
    """记录若干文件的当前内容（不存在记 None），用于事后精确还原。"""
    snap: dict[str, str | None] = {}
    for rel in rels:
        p = safe_repo_join(root, rel)
        snap[rel] = p.read_text(encoding="utf-8") if p.is_file() else None
    return snap


def _apply(root: Path, snap: dict[str, str | None], edits: dict[str, str]) -> None:
    """先把涉及的文件恢复到快照态，再写入本次状态的内容。

    「先恢复再写」而不是「直接覆盖」，是为了正确处理
    「某文件只在其中一个状态存在」的情况（如 golden 态才有实现文件）。
    """
    for rel, content in snap.items():
        p = safe_repo_join(root, rel)
        if rel in edits:
            continue
        if content is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(content, encoding="utf-8")
    for rel, content in edits.items():
        p = safe_repo_join(root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def run_sanity_edits(
    repo_root: str | Path,
    task_files: dict[str, str],
    golden_files: dict[str, str],
    *,
    new_files: Iterable[str] = (),
    python_bin: str | None = None,
    test_targets: list[str] | None = None,
    timeout: int = 900,
    check_determinism: bool = True,
    max_fail_to_pass: int = 50,
) -> SanityReport:
    """通用双向 sanity：适用于任意「题目态 / golden 态」文件集合差异。

    参数
    ----
    task_files:
        题目态下各文件应有的内容（交给被测 Agent 的初始状态）。
    golden_files:
        golden 态下各文件应有的内容（参考答案）。
    new_files:
        本题**新增**的文件（基线中不存在）。其中测试文件里的用例会豁免
        「基线必须为绿」的要求 —— 否则 B/C 类新写的测试永远无法成为判据。
    test_targets:
        本次要跑的测试目标。跑基线时会自动剔除尚不存在的文件，
        避免 pytest 因「找不到文件」以 usage error(退出码 4) 整体失败。
    """
    root = Path(repo_root).resolve()
    runner = TestRunner(root, python_bin, timeout=timeout)
    touched = sorted(set(task_files) | set(golden_files))
    new_set = {n.replace("\\", "/") for n in new_files}
    t_start = time.time()

    try:
        snap = _snapshot(root, touched)
    except ValueError as e:
        return SanityReport(ok=False, reason=f"文件路径不合法：{e}")

    def _exists_targets(targets: list[str] | None) -> list[str] | None:
        """剔除当前不存在的测试目标。"""
        if not targets:
            return None
        keep = []
        for t in targets:
            f = t.split("::", 1)[0]
            try:
                if safe_repo_join(root, f).exists():
                    keep.append(t)
            except ValueError:
                continue
        return keep or None

    def _is_new_test(nodeid: str) -> bool:
        return nodeid.split("::", 1)[0].replace("\\", "/") in new_set

    try:
        # ---------- 1) 基线（仓库原始状态）：用于剔除「本来就红」的用例
        base = runner.run(_exists_targets(test_targets))
        if base.timed_out:
            return SanityReport(ok=False, reason=f"基线测试超时（>{timeout}s）", golden_run=base)
        if base.collect_error:
            return SanityReport(ok=False, reason="基线测试收集失败（依赖缺失或 import 错误）",
                                golden_run=base)
        baseline_pass = set(base.passed)

        # ---------- 2) 题目态
        _apply(root, snap, task_files)
        task_run = runner.run(test_targets)
        if task_run.timed_out:
            return SanityReport(ok=False, reason="题目态测试超时", stub_run=task_run, golden_run=base)
        if task_run.collect_error:
            return SanityReport(
                ok=False,
                reason="题目态测试收集失败（骨架/测试文件破坏了模块可导入性，"
                       "或测试 import 了题目态尚不存在的符号）",
                stub_run=task_run, golden_run=base,
            )
        if not task_run.outcomes:
            return SanityReport(ok=False, reason="题目态未收集到任何测试用例",
                                stub_run=task_run, golden_run=base)

        # ---------- 3) 确定性（仍在题目态，避免来回切换文件带来的额外开销）
        deterministic: bool | None = None
        task_run2: RunResult | None = None
        if check_determinism:
            task_run2 = runner.run(test_targets)

        # ---------- 4) golden 态
        _apply(root, snap, golden_files)
        golden = runner.run(test_targets)
        if golden.timed_out:
            return SanityReport(ok=False, reason="golden 态测试超时",
                                stub_run=task_run, golden_run=golden)
        if golden.collect_error:
            return SanityReport(ok=False, reason="golden 态测试收集失败（参考实现有 import/语法问题）",
                                stub_run=task_run, golden_run=golden)

        def eligible(nid: str) -> bool:
            return nid in baseline_pass or _is_new_test(nid)

        def is_ok(run: RunResult, nid: str) -> bool:
            return run.outcome_of(nid) in ("passed", "xfailed")

        def is_red(run: RunResult, nid: str) -> bool:
            return run.outcome_of(nid) in ("failed", "error")

        universe = sorted(set(task_run.outcomes) | set(golden.outcomes))
        f2p = sorted(n for n in universe
                     if eligible(n) and is_red(task_run, n) and is_ok(golden, n))
        p2p = sorted(n for n in universe
                     if eligible(n) and is_ok(task_run, n) and is_ok(golden, n))

        if not f2p:
            # 区分两种失败原因，便于定位是「判据无效」还是「参考实现不对」
            red_in_task = [n for n in universe if is_red(task_run, n)]
            still_red = [n for n in red_in_task if not is_ok(golden, n)]
            if still_red:
                return SanityReport(
                    ok=False,
                    reason=f"golden 态仍有 {len(still_red)} 个用例不通过 —— "
                           f"参考实现/重构不正确：{still_red[:3]}",
                    pass_to_pass=p2p, stub_run=task_run, golden_run=golden,
                )
            return SanityReport(
                ok=False,
                reason="题目态没有任何用例变红 —— 该题没有有效判据，无法自动判分"
                       "（B 类：新测试可能没真正依赖待实现功能；C 类：守卫测试阈值可能过松）",
                pass_to_pass=p2p, stub_run=task_run, golden_run=golden,
            )
        if len(f2p) > max_fail_to_pass:
            return SanityReport(
                ok=False,
                reason=f"题目态有 {len(f2p)} 个用例变红（阈值 {max_fail_to_pass}），牵连过广",
                fail_to_pass=f2p, pass_to_pass=p2p, stub_run=task_run, golden_run=golden,
            )

        # golden 态必须让 P2P 也全绿（行为等价的核心保证，C 类尤其关键）
        p2p_broken = [n for n in baseline_pass
                      if n in golden.outcomes and not is_ok(golden, n)]
        if p2p_broken:
            return SanityReport(
                ok=False,
                reason=f"golden 态破坏了 {len(p2p_broken)} 个原本通过的用例 —— "
                       f"行为不等价：{p2p_broken[:3]}",
                fail_to_pass=f2p, pass_to_pass=p2p, stub_run=task_run, golden_run=golden,
            )

        if task_run2 is not None:
            f2p2 = sorted(n for n in universe
                          if eligible(n) and is_red(task_run2, n) and is_ok(golden, n))
            deterministic = f2p2 == f2p
            if not deterministic:
                return SanityReport(
                    ok=False,
                    reason=f"测试结果不确定（flaky），两次 FAIL_TO_PASS 不一致，"
                           f"差异：{sorted(set(f2p) ^ set(f2p2))[:5]}",
                    fail_to_pass=f2p, pass_to_pass=p2p,
                    stub_run=task_run, golden_run=golden, deterministic=False,
                )

        return SanityReport(
            ok=True,
            reason="双向 sanity 通过：题目态必红、golden 态必绿、原有用例未被破坏",
            fail_to_pass=f2p, pass_to_pass=p2p,
            stub_run=task_run, golden_run=golden,
            deterministic=deterministic,
            duration_sec=time.time() - t_start,
        )
    finally:
        try:
            _apply(root, snap, {})   # 全部还原到快照态
        except (OSError, ValueError):
            pass
