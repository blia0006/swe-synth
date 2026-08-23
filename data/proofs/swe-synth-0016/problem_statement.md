## 背景与上下文

tenacity 是一个 Python 重试库，内置了 `wait_exponential`、`wait_incrementing` 等等待策略，但这些策略都返回可供重试调度使用的函数，不直接给出每次重试前的具体延迟序列。在实际使用中，用户可能需要提前计算、展示或分析退避延迟序列（例如生成文档、估算超时时间、调试重试配置），当前项目缺少一个公开的纯计算工具。

## 需要实现的功能

请新增模块 `tenacity/backoff_sequence.py`，提供三个纯函数，用于计算重试退避延迟序列：
`exponential_backoff_sequence` 计算指数退避序列；
`linear_backoff_sequence` 计算线性递增序列；
`total_delay` 计算给定延迟序列的总和。
这三个函数只做数值计算，不进行任何重试执行、时间格式化、网络或文件操作。

## 输入

`exponential_backoff_sequence(base_delay, multiplier, max_delay, retries)`：
- `base_delay`：float，首次重试前的等待时间，必须为非负数
- `multiplier`：float，指数增长因子，必须大于 1
- `max_delay`：Optional[float]，单个延迟的上限；传入 `None` 表示不设上限；传入非 `None` 时必须大于 0
- `retries`：int，需要生成的重试延迟个数，必须为非负整数

`linear_backoff_sequence(base_delay, increment, max_delay, retries)`：
- `base_delay`：float，首次重试前的等待时间，必须为非负数
- `increment`：float，每次重试延迟的固定增量，必须为非负数
- `max_delay`：Optional[float]，含义同上
- `retries`：int，含义同上

`total_delay(sequence)`：
- `sequence`：Iterable[float]，一个非负延迟值的可迭代对象

## 输出

- `exponential_backoff_sequence` 与 `linear_backoff_sequence` 均返回 `tuple[float, ...]`，长度等于 `retries`。元组索引 `i`（从 0 开始）对应第 `i+1` 次重试前的等待时间。
- 指数序列的第 `i` 个元素为 `min(base_delay * multiplier**i, max_delay)`（当 `max_delay` 为 `None` 时只取前半部分）；线性序列的第 `i` 个元素为 `min(base_delay + increment * i, max_delay)`（当 `max_delay` 为 `None` 时只取前半部分）。
- `total_delay` 返回 `float`，为输入序列中所有元素之和。
- 当任何参数违反上述输入约束时，抛出 `ValueError`，具体消息内容不限。`total_delay` 遇到负数元素时同样抛出 `ValueError`。

## 预期行为

1. 指数退避无上限时，元素严格按照 `base_delay * multiplier**i` 生成；例如 base=1.0、multiplier=2.0、max_delay=None、retries=5 应返回 `(1.0, 2.0, 4.0, 8.0, 16.0)`。
2. 指数退避有上限时，任何超过 `max_delay` 的计算值都会被截断为该上限；例如 base=1.0、multiplier=2.0、max_delay=3.0、retries=5 返回 `(1.0, 2.0, 3.0, 3.0, 3.0)`。
3. 线性退避无上限时，元素严格按照 `base_delay + increment * i` 生成；有上限时同样截断。
4. 当 `retries` 为 0 时，两个序列生成函数都返回空元组 `()`。
5. `total_delay` 对空序列返回 `0.0`。
6. `base_delay < 0`、`multiplier <= 1`、`increment < 0`、`retries < 0`、或 `max_delay` 非 `None` 且 `<= 0` 时，抛出 `ValueError`。
7. `total_delay` 的输入序列包含任意负数时，抛出 `ValueError`。
8. 当 `max_delay` 非 `None` 且 `max_delay < base_delay` 时，指数和线性序列的所有元素都等于 `max_delay`（因为首个元素即被截断，后续元素更大）。

## 约束条件

- 新模块路径必须为 `tenacity/backoff_sequence.py`，不能修改项目内任何现有文件。
- 不得修改测试文件。
- 只允许使用 Python 标准库和项目已有依赖；`typing` 已足够，禁止引入任何新的第三方依赖。
- 实现必须是纯逻辑功能，不得进行网络请求、文件读写、子进程、随机数生成，也不得依赖当前时间。
- 所有公开函数的名称、参数列表和返回值类型必须与题干描述完全一致。