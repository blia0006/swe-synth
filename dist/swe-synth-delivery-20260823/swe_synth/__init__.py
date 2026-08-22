"""SWE-Synth：基于双 Agent 协作的 SWE 题目自动构建与验证。

核心思路（逆向消融法）：
    从长期稳定的开源仓库中，用 AST 把充分测试覆盖的函数体精准挖空成 stub，
    仓库自带测试即天然判据 —— 挖空态必红（FAIL_TO_PASS），原实现即 golden patch。
    因此题目「必然可解、必然可自动判分」，且不来源于任何 bugfix commit/PR，
    天然规避数据污染。
"""

__version__ = "0.1.0"
