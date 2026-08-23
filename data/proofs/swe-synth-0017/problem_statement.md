## 背景与上下文

`tenacity` 是一个 Python 重试库，提供多种退避等待策略，例如固定等待、指数退避等。但是，目前的模块中缺少斐波那契（Fibonacci）退避和多项式（Polynomial）退避这两种常见的确定性退避算法。本题目要求新增一个纯逻辑模块，用于生成这两种退避序列，帮助用户在重试策略中使用这些算法。

## 需要实现的功能

新增模块 `tenacity/backoff_policies.py`，提供两个公开函数：

- `fibonacci_backoff`：生成斐波那契风格的退避序列。
- `polynomial_backoff`：生成多项式风格的退避序列。

这些函数只负责生成数学序列，不涉及实际等待、随机数、时间或任何 I/O 操作。返回值为浮点数列表，长度为 `max_attempts`。

## 输入

### `fibonacci_backoff(max_attempts, initial=1.0, multiplier=1.0)`

- `max_attempts`：`int`，非负整数，表示要生成的序列长度。
- `initial`：`float`，序列前两个元素的初始值（也用作所有元素的基础值）。
- `multiplier`：`float`，应用于前两个元素之和的乘数。

### `polynomial_backoff(max_attempts, initial=1.0, degree=2, coefficient=1.0)`

- `max_attempts`：`int`，非负整数，表示要生成的序列长度。
- `initial`：`float`，常数偏移量，每个元素都加上这个值。
- `degree`：`int`，非负整数，多项式项的指数。
- `coefficient`：`float`，多项式项的乘数。

## 输出

两个函数都返回 `list[float]`，长度为 `max_attempts`。

- 当 `max_attempts == 0` 时，返回空列表 `[]`。
- 如果 `max_attempts` 不是 `int` 类型，抛出 `TypeError`。
- 如果 `max_attempts` 为负数，抛出 `ValueError`。
- `polynomial_backoff` 额外要求：如果 `degree` 不是 `int` 类型，抛出 `TypeError`；如果 `degree` 为负数，抛出 `ValueError`。
- 序列中的每个元素都必须是 `float` 类型（即使计算结果为整数也转换为浮点数）。

## 预期行为

1. **斐波那契序列定义**：序列的前两个元素均等于 `initial`。从第三个元素开始，每个元素等于前两个元素之和乘以 `multiplier`。即 `sequence[k] = (sequence[k-1] + sequence[k-2]) * multiplier`（`k >= 2`）。
2. 对于 `fibonacci_backoff`：
   - `max_attempts == 0`：返回 `[]`。
   - `max_attempts == 1`：返回 `[initial]`（注意即使只有一个元素，也是 float）。
   - `max_attempts == 2`：返回 `[initial, initial]`。
   - `max_attempts > 2`：按规则生成完整序列。
3. **多项式序列定义**：第 `k` 个元素（`k` 从 1 开始计数）等于 `initial + coefficient * (k ** degree)`。
4. 对于 `polynomial_backoff`：
   - `max_attempts == 0`：返回 `[]`。
   - `degree == 0`：所有元素都等于 `initial + coefficient`（因为 `k ** 0 == 1`）。
   - `degree` 和 `max_attempts` 必须是非负整数，否则抛出相应的异常。
5. 类型检查顺序：对于 `fibonacci_backoff`，先检查 `max_attempts` 是否为 `int`，再检查是否为负数；对于 `polynomial_backoff`，先检查 `max_attempts` 和 `degree` 是否为 `int`，再检查是否为负数。
6. 所有输入参数中的 `initial`、`multiplier`、`coefficient` 可以是 `int` 或 `float`，但输出序列中的值必须始终是 `float`。
7. 函数不得产生任何副作用，不得修改输入参数，不得依赖全局状态或系统时间。

## 约束条件

- 新模块路径必须为 `tenacity/backoff_policies.py`。
- 不得修改任何现有文件（包括 `tenacity` 包内的其他模块和测试目录下的任何文件）。
- 不得修改任何测试文件；评测将使用提供的 `tests/test_backoff_policies_synth.py` 进行验证。
- 不得引入新的第三方依赖；只允许使用 Python 标准库。
- 实现中不得出现 `NotImplementedError`。
- 函数签名必须与骨架文件中的签名完全一致。