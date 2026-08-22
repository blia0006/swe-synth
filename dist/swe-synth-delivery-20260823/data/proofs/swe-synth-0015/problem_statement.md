## 背景与上下文

tenacity 是一个用于重试操作的 Python 库，其核心抽象包括 wait 策略（如指数退避）和 stop 策略。在某些场景下，用户希望在应用重试之前，根据配置计算出重试计划，例如预计的总等待时间或在给定时间预算内可进行的最大重试次数。当前项目缺少一个独立的、无副作用的退避计算工具模块，本模块将填补这一空白。

## 需要实现的功能

新增模块 `tenacity/backoff_calculator.py`，提供三个公开函数：
- `exponential_delay`：计算第 `attempt` 次重试前应等待的时间（指数退避，可选上限）。
- `total_delay_until_attempt`：计算截至第 `attempt` 次重试之前（不含该次重试的等待）累计的总等待时间。
- `attempts_possible_within_budget`：在给定的总时间预算内，计算最多可以执行多少次重试。

这些函数只做纯计算，不涉及任何 I/O、随机数或时间依赖。

## 输入

所有函数的参数如下，类型注解见签名（骨架文件已提供）：
- `attempt`：int，正整数，表示第几次重试，从 1 开始。
- `initial_delay`：float，第一次重试前的等待时间，必须大于 0。
- `multiplier`：float，默认 1.0，退避乘数，必须大于等于 1。
- `max_delay`：Optional[float]，默认 None，等待时间的上限；若提供，必须大于 0。
- `budget`：float，总时间预算（秒），必须大于等于 0。

## 输出

- `exponential_delay` 返回 float：第 `attempt` 次重试的等待时间。计算方式为 `initial_delay * (multiplier ** (attempt - 1))`，若提供 `max_delay` 则结果不超过 `max_delay`。
- `total_delay_until_attempt` 返回 float：从第 1 次到第 `attempt-1` 次重试的等待时间之和（即 `sum(exponential_delay(i) for i in 1..attempt-1)`）。当 `attempt=1` 时返回 0.0。
- `attempts_possible_within_budget` 返回 int：满足 `total_delay_until_attempt(N+1) <= budget` 的最大非负整数 N，即最多可进行的重试次数（初始调用不计入）。

所有函数在参数非法时抛出 `ValueError`，具体见“预期行为”。

## 预期行为

1. `exponential_delay`：
   - 当 `attempt` 小于 1 时，抛出 `ValueError`。
   - 当 `initial_delay` 小于等于 0 时，抛出 `ValueError`。
   - 当 `multiplier` 小于 1 时，抛出 `ValueError`。
   - 当 `max_delay` 提供且小于等于 0 时，抛出 `ValueError`。
   - 正常情况：`exponential_delay(1, 1.0, 1.0)` 返回 `1.0`；`exponential_delay(4, 1.0, 2.0)` 返回 `8.0`；`exponential_delay(3, 1.0, 2.0, max_delay=3.0)` 返回 `3.0`（4.0 被截断）。

2. `total_delay_until_attempt`：
   - 参数非法规则与 `exponential_delay` 相同。
   - 当 `attempt=1` 时返回 `0.0`。
   - 正常情况：初始延迟 1.0，乘数 2.0，则 `total_delay_until_attempt(4, 1.0, 2.0)` 返回 `1+2+4=7.0`。
   - 若设置了 `max_delay`，则每次延迟按截断后的值累加。

3. `attempts_possible_within_budget`：
   - 当 `budget` 小于 0 时，抛出 `ValueError`。
   - 其他参数非法规则与 `exponential_delay` 相同。
   - 当 `budget=0.0` 时，返回 `0`（因为 `total_delay_until_attempt(1)=0` 满足条件）。
   - 正常情况：初始延迟 1.0，乘数 2.0，预算 10.0，则返回 `3`，因为前 3 次重试总延迟为 7.0，前 4 次总延迟为 15.0 超过预算。
   - 预算边界恰好等于总延迟时，对应重试次数应被包含：如预算为 7.0 时，返回 `3`。

## 约束条件

- 新增模块路径：`tenacity/backoff_calculator.py`。
- 不得修改任何现有文件，包括 `tenacity/__init__.py`。
- 不得修改任何测试文件，仅新增测试 `tests/test_backoff_calculator_synth.py`。
- 只能使用 Python 标准库，不得引入新的第三方依赖。
- 所有函数必须保持纯函数特性，不产生任何副作用。