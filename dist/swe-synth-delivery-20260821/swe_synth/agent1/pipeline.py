"""Agent1 主流程：把「仓库 → 合格题目 + 构建上下文 + 通过证明」串成一条链路。

对应课题要求（见 TASK-SPEC.md）
------------------------------
    Agent1（出题+打包）：分析目标仓库结构 → 生成一道软件工程题目
                        （功能实现 / 重构 / 模块添加）
                        → 编写 Dockerfile + 构建脚本 → docker build → docker push

本模块负责到「构建上下文就绪」为止；真实 build/push 由 `packer.py` 完成
（分离原因：构建需要 Docker daemon，而出题不需要，便于在无 Docker 环境下开发与验证）。

三种题型，一套验证机制
--------------------
课题明文列举三种题型，本模块各有一个入口函数：

    A 功能实现  `make_task_from_candidate`   AST 挖空，判据 = 仓库自带测试
    B 模块添加  `make_module_task`           新模块骨架 + LLM 新写测试
    C 重构      `make_refactor_task`         原代码不动 + 自动生成的重构守卫测试

三类的差异只在「怎么构造题目态与 golden 态」，验证链路完全共用：

    ① 构造题目态 / golden 态       各题型自己的出题器
    ② 本地双向 sanity              local_validator（题目态必红 / golden 必绿 / 双跑一致）
    ③ 出题静态审查                 各出题器内置（小节完整 / 无泄题 / 无臆造 / 结构一致）
    ④ solve-back 可解性验证        solvability（只看题干能否做出来）← 终极判据
    ⑤ schema 强制校验              schemas.task.SweTask（含防作弊检查）

前 4 关都会把证据落盘到 `data/proofs/<task_id>/`，即课题要求的「通过证明」。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..clients.tokenhub import LLMError, TokenHubClient
from ..config.loader import RepoSpec, Settings
from ..schemas.task import (Difficulty, GeneratedBy, SweTask, TaskState,
                            TaskType, ValidationInfo)
from .dockerfile_gen import BuildContext, audit_dockerfile, render_dockerfile, write_build_context
from .local_validator import run_sanity, run_sanity_edits
from .module_designer import (ModuleDraft, RepoLayout, design_module_task, detect_layout)
from .refactor_designer import (RefactorDraft, RefactorTarget, design_refactor_task,
                                find_refactor_targets)
from .repo_analyzer import Candidate, analyze_repo
from .solvability import solve_back, solve_back_edits
from .stubber import (NotStubbable, StubResult, make_added_file_patch, make_patch,
                      stub_symbol)
from .task_designer import DesignError, build_context, design_task

__all__ = ["PipelineResult", "RepoWorkspace", "make_task_from_candidate",
           "make_module_task", "make_refactor_task", "run_agent1"]


@dataclass
class PipelineResult:
    """一次出题尝试的结果（成功或失败都留痕，便于统计通过率）。"""

    candidate: str                      # "file::symbol"
    accepted: bool
    stage: str                          # 走到哪一步
    reason: str = ""
    task: SweTask | None = None
    build_ctx_dir: Path | None = None
    proof_dir: Path | None = None
    duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate,
            "accepted": self.accepted,
            "stage": self.stage,
            "reason": self.reason,
            "task_id": self.task.task_id if self.task else None,
            "duration_sec": round(self.duration_sec, 1),
        }


class RepoWorkspace:
    """管理一个仓库的本地工作副本与专用 Python 环境。

    职责：shallow clone → 记录 base_commit → 建 venv → 装依赖（含测试依赖）
    → 跑基线确认全绿。基线不绿的仓库直接淘汰（否则无法区分挖空导致的红）。
    """

    def __init__(self, spec: RepoSpec, root: str | Path, python_bin: str | None = None) -> None:
        self.spec = spec
        self.root = Path(root).resolve()
        self.venv = self.root / ".venv_swe"
        self._python_bin = python_bin
        self.base_commit: str = ""

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
        """执行命令（参数列表形式，不经 shell，避免注入）。"""
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               errors="replace", timeout=timeout)
            return p.returncode, (p.stdout + p.stderr)[-4000:]
        except subprocess.TimeoutExpired:
            return 124, f"超时（>{timeout}s）：{' '.join(cmd[:3])}"
        except FileNotFoundError:
            return 127, f"命令不存在：{cmd[0]}"

    @property
    def python(self) -> str:
        """题目代码运行用的解释器（优先仓库专用 venv）。"""
        cand = self.venv / "bin" / "python"
        return str(cand) if cand.exists() else (self._python_bin or "python3")

    # ------------------------------------------------------------ 对外
    def prepare(self, *, base_python: str | None = None) -> tuple[bool, str]:
        """clone + 建 venv + 装依赖。返回 (是否成功, 说明)。"""
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.parent.mkdir(parents=True, exist_ok=True)

        # clone 带重试：实测批量运行时连续 clone 多个仓库，后几个会偶发
        # LibreSSL/SSL 握手失败（GitHub 短时限流/网络瞬断），重试即恢复。
        rc, out = -1, ""
        for attempt in range(1, 4):
            rc, out = self._run(["git", "clone", "--depth", "400", "-q",
                                 self.spec.clone_url, str(self.root)])
            if rc == 0:
                break
            if self.root.exists():
                shutil.rmtree(self.root)
            if attempt < 3:
                time.sleep(3 * attempt)
        if rc != 0:
            return False, f"clone 失败（重试 3 次）：{out[-300:]}"

        rc, out = self._run(["git", "rev-parse", "HEAD"], cwd=self.root)
        if rc != 0:
            return False, f"取 base_commit 失败：{out[-200:]}"
        self.base_commit = out.strip().splitlines()[-1][:40]

        # 仓库专用 venv：避免不同仓库的依赖互相污染
        py = base_python or self._python_bin or "python3"
        rc, out = self._run([py, "-m", "venv", str(self.venv)])
        if rc != 0:
            return False, f"创建 venv 失败：{out[-300:]}"

        self._run([self.python, "-m", "pip", "install", "-q", "--upgrade", "pip"])
        # 装依赖：先按 repos.yaml 的 install 指令，再补测试依赖
        for cmd in (self.spec.install or ["pip install -e ."]):
            parts = cmd.split()
            if parts[:2] == ["pip", "install"]:
                parts = [self.python, "-m", "pip", "install", "-q", *parts[2:]]
            rc, out = self._run(parts, cwd=self.root)
            if rc != 0:
                return False, f"依赖安装失败（{cmd}）：{out[-400:]}"
        # pytest 必装（判据靠它）
        self._run([self.python, "-m", "pip", "install", "-q", "pytest"], cwd=self.root)
        for extra in self.spec.test_extras:
            self._run([self.python, "-m", "pip", "install", "-q", extra], cwd=self.root)
        return True, f"就绪 @ {self.base_commit[:7]}"

    def baseline_green(self, targets: list[str] | None = None) -> tuple[bool, str, list[str]]:
        """跑基线，确认测试全绿。基线不绿的仓库不能用于出题。

        失败时给出**可操作**的诊断：基线失败绝大多数是「测试依赖没装全」，
        而缺失的模块名就藏在报错里。把它提取出来，比只说"基线不绿"有用得多。
        """
        from .local_validator import TestRunner

        # repos.yaml 里 test_cmd 可能带 --deselect（跳过环境相关的假阴性用例，
        # 如平台差异导致的用例，详见 python-dotenv 的配置注释）。baseline_green
        # 自己拼 pytest 命令、不解析 test_cmd 字符串，因此要把其中的
        # --deselect 显式提取出来透传，否则本地预筛会被这些已知噪音误判。
        extra_args = re.findall(r"--deselect(?:=|\s+)(\S+)", self.spec.test_cmd)
        extra_args = [a for pair in (("--deselect", v) for v in extra_args) for a in pair]

        runner = TestRunner(self.root, self.python, timeout=900)
        res = runner.run(targets or self.spec.fast_targets or None,
                          extra_args=extra_args or None)
        if res.timed_out:
            return False, "基线测试超时", []
        if res.collect_error:
            hint = self._missing_module_hint(res.stdout_tail)
            return False, f"基线收集失败{hint}", []
        if not res.outcomes:
            return False, "基线未收集到任何用例", []
        failed = res.failed
        if failed:
            hint = self._missing_module_hint(res.stdout_tail)
            return False, (f"基线有 {len(failed)} 个用例失败，如 {failed[:2]}{hint}"), res.passed
        return True, f"基线全绿（{len(res.passed)} 个用例）", res.passed

    @staticmethod
    def _missing_module_hint(log: str) -> str:
        """从测试日志里提取缺失的模块名，给出可直接执行的修复建议。"""
        import re

        mods = set(re.findall(r"No module named '([\w.]+)'", log))
        mods |= set(re.findall(r"ModuleNotFoundError: No module named ([\w.]+)", log))
        # cachecontrol 这类库会自己抛 ImportError 提示装 extras
        extras = set(re.findall(r"pip install ([\w\-]+\[[\w,]+\])", log))
        parts = []
        if mods:
            parts.append(f"缺少模块 {sorted(mods)}")
        if extras:
            parts.append(f"需安装 extras {sorted(extras)}")
        if not parts:
            return ""
        return (
            f" —— {'；'.join(parts)}。"
            f"请在 config/repos.yaml 的 {'install/test_extras'} 中补齐后重试"
        )

    def restore(self) -> None:
        """把工作副本恢复到干净状态（每道题验证后必做，避免相互污染）。"""
        self._run(["git", "checkout", "--", "."], cwd=self.root)
        self._run(["git", "clean", "-qfd", "--exclude=.venv_swe"], cwd=self.root)

    def recently_changed_files(self, months: float = 12.0) -> set[str]:
        """本地 git log 粗筛「近 N 个月内改动过」的文件集合（无重叠校验的前置过滤）。

        目的：Agent2 的「时间隔离」检查（`overlap_check.min_months_since_change`）
        要求目标文件 ≥12 个月未改动，此前每次都是等打包+沙箱验证跑完才在最后一步
        暴露撞车（浪费一整条链路的时间）。这里用**本地**已 clone 下来的 git 历史
        （浅克隆 --depth 50）提前把最近改过的文件挑出来，选靶点阶段就跳过它们。

        注意：因为是浅克隆，只能看到最近 50 个 commit——对活跃仓库这通常已覆盖
        远超 12 个月的时间跨度，足以当作有效的前置粗筛；但仍可能有极少数文件的
        最近改动落在第 50 个 commit 之外未被本地看到，因此不能替代 Agent2 最终
        基于 GitHub API 的权威时间隔离判定，只作为提前拦截、减少无效尝试。
        """
        since = f"{months:.0f} months ago"
        rc, out = self._run(
            ["git", "log", f"--since={since}", "--name-only", "--pretty=format:"],
            cwd=self.root, timeout=60,
        )
        if rc != 0:
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}


# ------------------------------------------------------------------ 单题流程

def make_task_from_candidate(
    ws: RepoWorkspace,
    cand: Candidate,
    task_id: str,
    client: TokenHubClient,
    settings: Settings,
    *,
    proofs_root: Path,
    build_root: Path,
    do_solve_back: bool = True,
) -> PipelineResult:
    """对一个候选靶点执行完整出题流程，产出合格题目 + 构建上下文 + 通过证明。"""
    t0 = time.time()
    label = f"{cand.rel_path}::{cand.symbol_path}"
    proof_dir = proofs_root / task_id
    proof_dir.mkdir(parents=True, exist_ok=True)

    def fail(stage: str, reason: str) -> PipelineResult:
        (proof_dir / "reject.json").write_text(
            json.dumps({"candidate": label, "stage": stage, "reason": reason},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return PipelineResult(candidate=label, accepted=False, stage=stage,
                              reason=reason, proof_dir=proof_dir,
                              duration_sec=time.time() - t0)

    # ---------- ① 挖空
    src_path = ws.root / cand.rel_path
    original = src_path.read_text(encoding="utf-8")
    try:
        stub: StubResult = stub_symbol(
            original, cand.symbol_path,
            keep_docstring=settings.get("stubbing.keep_docstring", True),
            min_body_lines=settings.get("stubbing.min_body_lines", 4),
        )
    except NotStubbable as e:
        return fail("STUB", f"不可挖空：{e}")

    # ---------- ② 本地双向 sanity（判据从这里产生）
    test_targets = [t for t in cand.referencing_tests if not t.endswith("conftest.py")][:4] or None
    sanity = run_sanity(
        ws.root, cand.rel_path, stub.source_stubbed,
        python_bin=ws.python, test_targets=test_targets,
        timeout=settings.get("local_validation.timeout_sec", 900),
        check_determinism=settings.get("local_validation.check_determinism", True),
        max_fail_to_pass=settings.get("stubbing.max_fail_to_pass", 50),
    )
    (proof_dir / "local_sanity.json").write_text(
        json.dumps(sanity.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if sanity.stub_run:
        (proof_dir / "stub_run.log").write_text(sanity.stub_run.stdout_tail, encoding="utf-8")
    if sanity.golden_run:
        (proof_dir / "golden_run.log").write_text(sanity.golden_run.stdout_tail, encoding="utf-8")
    if not sanity.ok:
        return fail("LOCAL_SANITY", sanity.reason)

    f2p, p2p = sanity.fail_to_pass, sanity.pass_to_pass

    # ---------- ③ LLM 出题（含静态审查）
    try:
        draft = design_task(
            client, ws.spec.name, cand.rel_path, stub, stub.source_stubbed, f2p,
            model=settings.model_task_design,
            max_tokens=settings.max_tokens,
        )
    except (DesignError, LLMError) as e:
        return fail("DESIGN", f"出题失败：{e}")
    (proof_dir / "problem_statement.md").write_text(draft.problem_statement, encoding="utf-8")

    # ---------- ④ solve-back 可解性验证（终极判据）
    ctx_info = build_context(ws.spec.name, cand.rel_path, stub, stub.source_stubbed, f2p)
    llm_attempt: dict | None = None
    if do_solve_back:
        sb = solve_back(
            client, ws.root, cand.rel_path, stub, draft.problem_statement,
            ctx_info["skeleton"], f2p, p2p,
            python_bin=ws.python, model=settings.model_repo_analyze,
            max_attempts=2,
        )
        llm_attempt = sb.to_dict()
        (proof_dir / "solve_back.json").write_text(
            json.dumps(llm_attempt, ensure_ascii=False, indent=2), encoding="utf-8")
        if not sb.solvable:
            return fail("SOLVE_BACK", sb.reason)

    # ---------- 生成 patch
    stub_patch = make_patch(original, stub.source_stubbed, cand.rel_path)
    golden_patch = make_patch(stub.source_stubbed, original, cand.rel_path)
    (proof_dir / "stub.patch").write_text(stub_patch, encoding="utf-8")
    (proof_dir / "golden.patch").write_text(golden_patch, encoding="utf-8")

    # ---------- ⑤ 组装 + 校验 + 构建上下文（三类题型共用）
    test_files = sorted({n.split("::", 1)[0] for n in f2p + p2p})
    task, err, build_dir = _finalize(
        ws, task_id, settings, client,
        task_type=TaskType.FEATURE_IMPLEMENTATION,
        difficulty=draft.difficulty,
        problem_statement=draft.problem_statement,
        hints=draft.hints,
        symbol=cand.symbol_path,
        modified_files=[cand.rel_path],
        do_not_modify=test_files,
        fail_to_pass=f2p, pass_to_pass=p2p,
        sanity=sanity, llm_attempt=llm_attempt,
        task_files={cand.rel_path: stub.source_stubbed},
        stub_patch=stub_patch, golden_patch=golden_patch,
        proof_dir=proof_dir, build_root=build_root,
        prompt_version="v1",
    )
    if task is None:
        return fail("FINALIZE", err)

    return PipelineResult(
        candidate=label, accepted=True, stage="BUILD_CTX_READY",
        reason="出题成功：本地 sanity + solve-back + schema 全部通过",
        task=task, build_ctx_dir=build_dir, proof_dir=proof_dir,
        duration_sec=time.time() - t0,
    )


# ------------------------------------------------------------------ 共用收尾
#
# 三种题型走到这里时，差异已经被各自的出题器消化完了，剩下的工作完全一致：
# 组装 SweTask → schema 强制校验 → 生成题目/答案两份构建上下文 → 落盘通过证明。
# 抽成一个函数，既避免三份重复代码，也保证三类题型的交付物格式严格一致。


def _finalize(
    ws: RepoWorkspace,
    task_id: str,
    settings: Settings,
    client: TokenHubClient,
    *,
    task_type: TaskType,
    difficulty: Difficulty,
    problem_statement: str,
    hints: str | None,
    symbol: str,
    modified_files: list[str],
    do_not_modify: list[str],
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    sanity,
    llm_attempt: dict | None,
    task_files: dict[str, str],
    stub_patch: str,
    golden_patch: str,
    proof_dir: Path,
    build_root: Path,
    prompt_version: str,
) -> tuple[SweTask | None, str, Path | None]:
    """组装并校验题目，生成构建上下文与通过证明。

    `task_files`：**题目态**下需要覆盖/新增到仓库副本里的文件（rel_path → 内容）。
    镜像里的仓库必须是题目态，绝不能含答案。
    """
    try:
        task = SweTask(
            task_id=task_id,
            task_type=task_type,
            difficulty=difficulty,
            state=TaskState.LOCAL_OK,
            repo=ws.spec.name,
            repo_stars=ws.spec.stars,
            base_commit=ws.base_commit,
            language=ws.spec.language,
            problem_statement=problem_statement,
            hints=hints,
            modified_files=modified_files,
            do_not_modify=do_not_modify,      # 防作弊：判据文件不可改
            test_cmd=ws.spec.test_cmd,
            FAIL_TO_PASS=fail_to_pass,
            PASS_TO_PASS=pass_to_pass,
            image=settings.image_ref(task_id),
            solution_image=settings.image_ref(task_id, solution=True),
            generated_by=GeneratedBy(
                model=settings.model_task_design,
                prompt_version=prompt_version,
                input_tokens=client.usage.prompt_tokens,
                output_tokens=client.usage.completion_tokens,
            ),
            validation=ValidationInfo(
                local_sanity_passed=True,
                local_duration_sec=sanity.duration_sec,
                deterministic=sanity.deterministic,
                llm_attempt=llm_attempt,
                proof_dir=str(proof_dir),
            ),
        )
    except Exception as e:  # noqa: BLE001  # pydantic ValidationError 等
        return None, f"未通过 schema 校验：{e}", None

    # 题目态仓库副本（镜像内容）
    task_repo = build_root / task_id / "_repo"
    if task_repo.exists():
        shutil.rmtree(task_repo)
    shutil.copytree(ws.root, task_repo,
                    ignore=shutil.ignore_patterns(".git", ".venv*", "__pycache__",
                                                  "*.pyc", ".pytest_cache", ".tox"))
    for rel, content in task_files.items():
        p = task_repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    bctx = BuildContext(
        task_id=task_id, repo=ws.spec.name, clone_url=ws.spec.clone_url,
        base_commit=ws.base_commit, language=ws.spec.language,
        test_cmd=ws.spec.test_cmd, fail_to_pass=fail_to_pass, pass_to_pass=pass_to_pass,
        problem_statement=problem_statement,
        stub_patch=stub_patch, golden_patch=golden_patch,
        modified_files=modified_files, do_not_modify=do_not_modify,
        install_cmds=list(ws.spec.install),
        base_image=settings.get("image.base",
                                "ccr.ccs.tencentyun.com/ags-image/sandbox-code:latest"),
        symbol=symbol,
        task_type=task_type.value,
    )
    try:
        for with_sol in (False, True):
            out_dir = build_root / task_id / ("sol" if with_sol else "task")
            write_build_context(bctx, out_dir, task_repo, with_solution=with_sol)
            problems = audit_dockerfile(
                render_dockerfile(bctx, with_solution=with_sol), expect_solution=with_sol)
            if problems:
                return None, f"Dockerfile 违反平台约束：{problems}", None
    finally:
        shutil.rmtree(task_repo, ignore_errors=True)

    # 通过证明：Dockerfile / metadata / 题目记录
    shutil.copy2(build_root / task_id / "task" / "Dockerfile", proof_dir / "Dockerfile")
    (proof_dir / "metadata.json").write_text(
        json.dumps(bctx.metadata(), ensure_ascii=False, indent=2), encoding="utf-8")
    (proof_dir / "task.json").write_text(task.to_jsonl_line(), encoding="utf-8")
    # 清掉同一 task_id 上一次失败留下的 reject.json，避免证据目录自相矛盾
    (proof_dir / "reject.json").unlink(missing_ok=True)
    return task, "", build_root / task_id


def _dump_sanity(proof_dir: Path, sanity, *, task_log: str = "task_run.log") -> None:
    """把双向 sanity 的证据落盘（课题要求的「通过证明」的核心部分）。"""
    (proof_dir / "local_sanity.json").write_text(
        json.dumps(sanity.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if sanity.stub_run:
        (proof_dir / task_log).write_text(sanity.stub_run.stdout_tail, encoding="utf-8")
    if sanity.golden_run:
        (proof_dir / "golden_run.log").write_text(sanity.golden_run.stdout_tail, encoding="utf-8")


def _existing_p2p_targets(ws: RepoWorkspace, limit: int = 3) -> list[str]:
    """挑几个既有测试文件作为 PASS_TO_PASS 来源。

    不跑全量的原因（已踩坑）：仓库里常有依赖缺失的无关测试文件，
    pytest 一旦在收集阶段报错就会 Interrupted，导致所有用例都不执行。
    """
    out = []
    for t in (ws.spec.fast_targets or []):
        if (ws.root / t.split("::", 1)[0]).exists():
            out.append(t)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ B 类：模块添加

_B_EXTRA = """## 额外约束
- 只需要改写上面列出的模块文件；测试文件已经写好且**不可修改**
- 保持文件中已有的函数签名、参数名与 docstring 不变，只把 `raise NotImplementedError`
  替换为真实实现
- 不得引入新的第三方依赖，不得做网络/文件/时间/随机等有副作用的操作"""


def make_module_task(
    ws: RepoWorkspace,
    layout: RepoLayout,
    task_id: str,
    client: TokenHubClient,
    settings: Settings,
    *,
    proofs_root: Path,
    build_root: Path,
    do_solve_back: bool = True,
    avoid_slugs: set[str] | None = None,
) -> PipelineResult:
    """出一道「模块添加」题（课题三类题型之 B）。

    与 A 类最大的不同：判据（新测试）与标准答案（参考实现）都由 LLM 产出，
    因此**完全依靠验证而非信任** —— 结构校验、双向 sanity、solve-back 逐层过筛，
    任一不过即丢弃。通过率低于 A 类是正常的。
    """
    t0 = time.time()
    proof_dir = proofs_root / task_id
    proof_dir.mkdir(parents=True, exist_ok=True)
    label = f"{ws.spec.name}::module_addition"

    def fail(stage: str, reason: str) -> PipelineResult:
        (proof_dir / "reject.json").write_text(
            json.dumps({"candidate": label, "task_type": "module_addition",
                        "stage": stage, "reason": reason},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return PipelineResult(candidate=label, accepted=False, stage=stage,
                              reason=reason, proof_dir=proof_dir,
                              duration_sec=time.time() - t0)

    # ---------- ① LLM 设计新模块（含结构校验与自我修正重试）
    try:
        draft: ModuleDraft = design_module_task(
            client, ws.spec.name, ws.spec.stars, ws.root, layout,
            model=settings.model_task_design,
            max_tokens=settings.max_tokens,
            avoid_slugs=avoid_slugs,
        )
    except (DesignError, LLMError) as e:
        return fail("DESIGN", f"模块添加题设计失败：{e}")

    label = f"{draft.module_path}::module_addition"
    (proof_dir / "problem_statement.md").write_text(draft.problem_statement, encoding="utf-8")

    task_files = {draft.module_path: draft.skeleton_code, draft.test_path: draft.test_code}
    golden_files = {draft.module_path: draft.impl_code, draft.test_path: draft.test_code}
    existing = _existing_p2p_targets(ws)
    test_targets = [draft.test_path, *existing]

    # ---------- ② 双向 sanity：题目态（骨架）必红、golden 态（实现）必绿
    sanity = run_sanity_edits(
        ws.root, task_files, golden_files,
        new_files=[draft.module_path, draft.test_path],
        python_bin=ws.python, test_targets=test_targets,
        timeout=settings.get("local_validation.timeout_sec", 900),
        check_determinism=settings.get("local_validation.check_determinism", True),
        max_fail_to_pass=settings.get("stubbing.max_fail_to_pass", 50),
    )
    _dump_sanity(proof_dir, sanity)
    if not sanity.ok:
        return fail("LOCAL_SANITY", sanity.reason)
    f2p, p2p = sanity.fail_to_pass, sanity.pass_to_pass

    # 判据必须落在新测试文件上，否则说明红的是别的东西（题目不成立）
    if not all(n.split("::", 1)[0] == draft.test_path for n in f2p):
        return fail("LOCAL_SANITY",
                    f"FAIL_TO_PASS 出现在非本题测试文件中：{f2p[:3]} —— 新模块牵连了既有功能")

    # ---------- ③ solve-back 可解性验证（只看题干能否把功能实现出来）
    llm_attempt: dict | None = None
    if do_solve_back:
        sb = solve_back_edits(
            client, ws.root, task_files, [draft.module_path],
            draft.problem_statement, f2p, p2p,
            extra_instructions=_B_EXTRA,
            python_bin=ws.python, model=settings.model_repo_analyze, max_attempts=2,
        )
        llm_attempt = sb.to_dict()
        (proof_dir / "solve_back.json").write_text(
            json.dumps(llm_attempt, ensure_ascii=False, indent=2), encoding="utf-8")
        if not sb.solvable:
            return fail("SOLVE_BACK", sb.reason)

    # ---------- ④ patch（题目态 = 新增骨架 + 新增测试；答案 = 骨架→实现）
    stub_patch = (make_added_file_patch(draft.module_path, draft.skeleton_code)
                  + make_added_file_patch(draft.test_path, draft.test_code))
    golden_patch = make_patch(draft.skeleton_code, draft.impl_code, draft.module_path)
    (proof_dir / "stub.patch").write_text(stub_patch, encoding="utf-8")
    (proof_dir / "golden.patch").write_text(golden_patch, encoding="utf-8")

    # ---------- ⑤ 收尾
    protected = sorted({draft.test_path, *(n.split("::", 1)[0] for n in f2p + p2p)})
    task, err, build_dir = _finalize(
        ws, task_id, settings, client,
        task_type=TaskType.MODULE_ADDITION,
        difficulty=draft.difficulty,
        problem_statement=draft.problem_statement,
        hints=None,
        symbol=draft.module_path,
        modified_files=[draft.module_path],
        do_not_modify=protected,
        fail_to_pass=f2p, pass_to_pass=p2p,
        sanity=sanity, llm_attempt=llm_attempt,
        task_files=task_files,
        stub_patch=stub_patch, golden_patch=golden_patch,
        proof_dir=proof_dir, build_root=build_root,
        prompt_version="module-v1",
    )
    if task is None:
        return fail("FINALIZE", err)

    return PipelineResult(
        candidate=label, accepted=True, stage="BUILD_CTX_READY",
        reason=f"模块添加题出题成功（新模块 {draft.module_path}，判据 {len(f2p)} 个）",
        task=task, build_ctx_dir=build_dir, proof_dir=proof_dir,
        duration_sec=time.time() - t0,
    )


# ------------------------------------------------------------------ C 类：重构

_C_EXTRA_TMPL = """## 额外约束（这是一道重构题）
- 目标符号：`{symbol}`，位于上面给出的文件中
- **行为必须完全等价**：所有分支、返回值、异常类型与触发条件都不能变
- 函数签名必须逐字不变：参数为 `{args}`
- 必须把 `{symbol}` 的有效代码行数降到 **≤ {max_body}** 行、圈复杂度降到 **≤ {max_cx}**，
  且文件内任何函数的有效代码行数都不得超过 **{max_any}** 行
- 达标做法是把独立职责提取为模块级私有辅助函数，而不是压行或删注释
- 不得修改任何测试文件，不得引入新的第三方依赖"""


def make_refactor_task(
    ws: RepoWorkspace,
    target: RefactorTarget,
    task_id: str,
    client: TokenHubClient,
    settings: Settings,
    *,
    proofs_root: Path,
    build_root: Path,
    tests_dir: str = "tests",
    do_solve_back: bool = True,
) -> PipelineResult:
    """出一道「重构」题（课题三类题型之 C）。

    判据组合：自动生成的重构守卫测试（必须由红变绿）+ 既有测试（全程保持绿）。
    前者证明「确实重构了」，后者证明「行为没变」——
    两者合起来才完整表达重构题的验收含义。
    """
    t0 = time.time()
    proof_dir = proofs_root / task_id
    proof_dir.mkdir(parents=True, exist_ok=True)
    label = target.label

    def fail(stage: str, reason: str) -> PipelineResult:
        (proof_dir / "reject.json").write_text(
            json.dumps({"candidate": label, "task_type": "refactoring",
                        "stage": stage, "reason": reason},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return PipelineResult(candidate=label, accepted=False, stage=stage,
                              reason=reason, proof_dir=proof_dir,
                              duration_sec=time.time() - t0)

    # ---------- ① LLM 给参考重构 + 题干；程序据实测指标反推守卫阈值
    try:
        draft: RefactorDraft = design_refactor_task(
            client, ws.spec.name, ws.spec.stars, ws.root, target, task_id,
            tests_dir=tests_dir,
            model=settings.model_task_design,
            max_tokens=settings.max_tokens,
        )
    except (DesignError, LLMError) as e:
        return fail("DESIGN", f"重构题设计失败：{e}")

    (proof_dir / "problem_statement.md").write_text(draft.problem_statement, encoding="utf-8")
    (proof_dir / "refactor_metrics.json").write_text(
        json.dumps(draft.thresholds.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    # 题目态：业务代码**一个字都不改**，只放入守卫测试
    task_files = {draft.rel_path: draft.original_source,
                  draft.guard_test_path: draft.guard_test_code}
    golden_files = {draft.rel_path: draft.refactored_source,
                    draft.guard_test_path: draft.guard_test_code}
    behavior_tests = [t for t in target.referencing_tests if not t.endswith("conftest.py")][:4]
    test_targets = [draft.guard_test_path, *behavior_tests]

    # ---------- ② 双向 sanity
    sanity = run_sanity_edits(
        ws.root, task_files, golden_files,
        new_files=[draft.guard_test_path],
        python_bin=ws.python, test_targets=test_targets,
        timeout=settings.get("local_validation.timeout_sec", 900),
        check_determinism=settings.get("local_validation.check_determinism", True),
        max_fail_to_pass=settings.get("stubbing.max_fail_to_pass", 50),
    )
    _dump_sanity(proof_dir, sanity)
    if not sanity.ok:
        return fail("LOCAL_SANITY", sanity.reason)
    f2p, p2p = sanity.fail_to_pass, sanity.pass_to_pass

    # 重构题的判据必须来自守卫测试；若既有测试变红，说明"重构"改变了行为
    stray = [n for n in f2p if n.split("::", 1)[0] != draft.guard_test_path]
    if stray:
        return fail("LOCAL_SANITY",
                    f"既有测试在题目态就是红的：{stray[:3]} —— 无法作为行为等价的基准")
    if not p2p:
        return fail("LOCAL_SANITY",
                    "没有任何既有测试可作为 PASS_TO_PASS —— 重构题必须有行为等价的机器证明")

    # ---------- ③ solve-back（重构题会把原代码给做题者，这不算泄题：代码本就在仓库里）
    llm_attempt: dict | None = None
    if do_solve_back:
        th = draft.thresholds
        # 重构题要求模型输出整个目标文件，额度必须按文件规模放大 ——
        # 否则输出被截断，表现为「语法错误」，会被误判成题目不可解
        est_tokens = max(8192, min(32768, int(len(draft.original_source) / 2.5) + 6000))
        sb = solve_back_edits(
            client, ws.root, task_files, [draft.rel_path],
            draft.problem_statement, f2p, p2p,
            extra_instructions=_C_EXTRA_TMPL.format(
                symbol=draft.symbol_path,
                args=", ".join(th.expected_args),
                max_body=th.max_target_body_lines,
                max_cx=th.max_target_complexity,
                max_any=th.max_any_func_body_lines,
            ),
            python_bin=ws.python, model=settings.model_repo_analyze,
            max_attempts=2, max_tokens=est_tokens,
        )
        llm_attempt = sb.to_dict()
        (proof_dir / "solve_back.json").write_text(
            json.dumps(llm_attempt, ensure_ascii=False, indent=2), encoding="utf-8")
        if not sb.solvable:
            return fail("SOLVE_BACK", sb.reason)

    # ---------- ④ patch
    stub_patch = make_added_file_patch(draft.guard_test_path, draft.guard_test_code)
    golden_patch = make_patch(draft.original_source, draft.refactored_source, draft.rel_path)
    (proof_dir / "stub.patch").write_text(stub_patch, encoding="utf-8")
    (proof_dir / "golden.patch").write_text(golden_patch, encoding="utf-8")

    # ---------- ⑤ 收尾
    protected = sorted({draft.guard_test_path,
                        *(n.split("::", 1)[0] for n in f2p + p2p)})
    task, err, build_dir = _finalize(
        ws, task_id, settings, client,
        task_type=TaskType.REFACTORING,
        difficulty=draft.difficulty,
        problem_statement=draft.problem_statement,
        hints=None,
        symbol=draft.symbol_path,
        modified_files=[draft.rel_path],
        do_not_modify=protected,
        fail_to_pass=f2p, pass_to_pass=p2p,
        sanity=sanity, llm_attempt=llm_attempt,
        task_files=task_files,
        stub_patch=stub_patch, golden_patch=golden_patch,
        proof_dir=proof_dir, build_root=build_root,
        prompt_version="refactor-v1",
    )
    if task is None:
        return fail("FINALIZE", err)

    return PipelineResult(
        candidate=label, accepted=True, stage="BUILD_CTX_READY",
        reason=(f"重构题出题成功（{draft.symbol_path}："
                f"行 {draft.thresholds.before.body_lines}→≤{draft.thresholds.max_target_body_lines}，"
                f"复杂度 {draft.thresholds.before.cyclomatic}→≤{draft.thresholds.max_target_complexity}）"),
        task=task, build_ctx_dir=build_dir, proof_dir=proof_dir,
        duration_sec=time.time() - t0,
    )


# ------------------------------------------------------------------ 仓库级流程

def run_agent1(
    spec: RepoSpec,
    settings: Settings,
    client: TokenHubClient,
    *,
    work_root: Path,
    build_root: Path,
    proofs_root: Path,
    task_id_start: int = 1,
    max_tasks: int = 2,
    max_candidates: int = 8,
    base_python: str | None = None,
    do_solve_back: bool = True,
    used_symbols: set[str] | None = None,
    task_types: list[TaskType] | None = None,
    on_progress=None,
) -> list[PipelineResult]:
    """对一个仓库出若干道题。返回全部尝试结果（含失败，用于统计通过率）。

    参数
    ----
    task_types:
        本次要出的题型，按给定顺序轮转。默认三类都出（课题明文要求题型覆盖
        「功能实现 / 重构 / 模块添加」）。
    used_symbols:
        已出过题的 `file::symbol` 集合。**必须传入**，否则重复运行会对同一个靶点
        反复出题，产生内容重复的题目（实测踩过：连续两次都选中评分最高的
        `CacheControl`，得到两道一模一样的题）。
    """
    results: list[PipelineResult] = []
    used = set(used_symbols or ())
    types = list(task_types or [TaskType.FEATURE_IMPLEMENTATION,
                                TaskType.MODULE_ADDITION,
                                TaskType.REFACTORING])

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    ws = RepoWorkspace(spec, work_root / spec.name.replace("/", "__"), base_python)
    log(f"[{spec.name}] 准备工作副本…")
    ok, msg = ws.prepare(base_python=base_python)
    if not ok:
        return [PipelineResult(candidate=spec.name, accepted=False,
                               stage="REPO_PREPARE", reason=msg)]
    log(f"[{spec.name}] {msg}")

    ok, msg, _ = ws.baseline_green()
    if not ok:
        return [PipelineResult(candidate=spec.name, accepted=False,
                               stage="BASELINE", reason=msg)]
    log(f"[{spec.name}] {msg}")

    # ---- 各题型的靶点池（惰性准备，用不到就不花时间）
    a_pool: list[Candidate] | None = None
    c_pool: list[RefactorTarget] | None = None
    layout: RepoLayout | None = None
    layout_err = ""

    # 无重叠校验要求目标文件长期未改动（默认 12 个月），提前用本地 git log
    # 粗筛掉「最近改过」的文件，避免打包+沙箱验证跑完才在最后一步撞车。
    min_months = float(settings.get("overlap_check.min_months_since_change", 12))
    recent_files = ws.recently_changed_files(months=min_months)
    if recent_files:
        log(f"[{spec.name}] 近 {min_months:.0f} 个月内改动过 {len(recent_files)} 个文件，"
            f"选靶点时将跳过（避免撞无重叠校验）")

    def ensure_a() -> list[Candidate]:
        nonlocal a_pool
        if a_pool is None:
            cands = analyze_repo(
                ws.root,
                min_body_lines=settings.get("stubbing.min_body_lines", 4),
                max_body_lines=settings.get("stubbing.max_body_lines", 60),
                require_test_coverage=settings.get("stubbing.require_test_coverage", True),
                limit=max_candidates,
            )
            n_before = len(cands)
            cands = [c for c in cands if c.rel_path not in recent_files]
            a_pool = [c for c in cands if f"{c.rel_path}::{c.symbol_path}" not in used]
            log(f"[{spec.name}] A 类候选靶点 {len(a_pool)}/{n_before} 个可用"
                f"（已排除近期改动文件）")
        return a_pool

    def ensure_c() -> list[RefactorTarget]:
        nonlocal c_pool
        if c_pool is None:
            tgts = find_refactor_targets(
                ws.root,
                min_body_lines=settings.get("refactoring.min_body_lines", 12),
                min_cyclomatic=settings.get("refactoring.min_cyclomatic", 6),
                max_file_lines=settings.get("refactoring.max_file_lines", 600),
                limit=max_candidates,
            )
            n_before = len(tgts)
            tgts = [t for t in tgts if t.rel_path not in recent_files]
            c_pool = [t for t in tgts if f"{t.label}#refactor" not in used]
            log(f"[{spec.name}] C 类重构靶点 {len(c_pool)}/{n_before} 个可用"
                f"（已排除近期改动文件）")
        return c_pool

    def ensure_layout() -> RepoLayout | None:
        nonlocal layout, layout_err
        if layout is None and not layout_err:
            try:
                layout = detect_layout(ws.root)
                log(f"[{spec.name}] 包目录 `{layout.package_dir}`，"
                    f"测试目录 `{layout.tests_dir}`（B 类可用）")
            except DesignError as e:
                layout_err = str(e)
        return layout

    seq = task_id_start
    # 已用过的 B 类功能短名，避免同一仓库反复出相同功能
    used_slugs = {u.split("::", 1)[0].rsplit("/", 1)[-1][:-3]
                  for u in used if u.endswith(".py::module_addition")}

    # 按题型轮转（而不是先出完 A 再出 B），保证少量产出时也能覆盖多种题型
    ti = 0
    guard = 0
    while sum(r.accepted for r in results) < max_tasks and guard < max_tasks * len(types) * 3:
        guard += 1
        ttype = types[ti % len(types)]
        ti += 1
        task_id = f"swe-synth-{seq:04d}"

        if ttype == TaskType.FEATURE_IMPLEMENTATION:
            pool = [c for c in ensure_a() if f"{c.rel_path}::{c.symbol_path}" not in used]
            if not pool:
                if all(t == ttype for t in types):
                    break
                continue
            cand = pool[0]
            log(f"  → [A 功能实现] {cand.symbol_path}（score={cand.score}）as {task_id}")
            res = make_task_from_candidate(
                ws, cand, task_id, client, settings,
                proofs_root=proofs_root, build_root=build_root,
                do_solve_back=do_solve_back,
            )
            used.add(f"{cand.rel_path}::{cand.symbol_path}")

        elif ttype == TaskType.MODULE_ADDITION:
            lay = ensure_layout()
            if lay is None:
                log(f"  ⏭ [B 模块添加] 跳过：{layout_err}")
                if all(t == ttype for t in types):
                    break
                continue
            log(f"  → [B 模块添加] 设计新模块 as {task_id}")
            res = make_module_task(
                ws, lay, task_id, client, settings,
                proofs_root=proofs_root, build_root=build_root,
                do_solve_back=do_solve_back, avoid_slugs=used_slugs,
            )
            if res.accepted and res.task:
                mp = res.task.modified_files[0]
                used.add(f"{mp}::module_addition")
                used_slugs.add(Path(mp).stem)

        else:  # 重构
            pool = [t for t in ensure_c() if f"{t.label}#refactor" not in used]
            if not pool:
                if all(t == ttype for t in types):
                    break
                continue
            tgt = pool[0]
            log(f"  → [C 重构] {tgt.symbol_path}"
                f"（{tgt.body_lines} 行/复杂度 {tgt.cyclomatic}）as {task_id}")
            res = make_refactor_task(
                ws, tgt, task_id, client, settings,
                proofs_root=proofs_root, build_root=build_root,
                tests_dir=(ensure_layout().tests_dir if ensure_layout() else "tests"),
                do_solve_back=do_solve_back,
            )
            used.add(f"{tgt.label}#refactor")

        ws.restore()
        results.append(res)
        log(f"     {'✅ 通过' if res.accepted else '❌ ' + res.stage}：{res.reason[:110]}")
        if res.accepted:
            seq += 1

    if not results:
        return [PipelineResult(candidate=spec.name, accepted=False, stage="NO_CANDIDATE",
                               reason="该仓库没有可用靶点（或全部已出过题），"
                                      "可增大 --max-candidates 或换仓库")]
    return results
