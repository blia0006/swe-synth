## 背景与上下文
tenacity 是一个 Python 重试库，用户可以配置停止条件、等待策略等。在重试策略的设计与验证中，经常需要预先计算多次尝试的指数退避等待时间，用于容量规划、测试断言或日志展示。当前项目中还没有一个独立的模块提供这种静态的退避延迟计算功能，因此需要新增一个纯逻辑的辅助模块。

## 需要实现的功能
新增模块 `tenacity/backoff_schedule.py`，该模块包含两个公开函数：
- `exponential_backoff_delay`：根据尝试次数、基础延迟、乘数因子和最大延迟，计算单次尝试前的退避延迟秒数。
- `build_exponential_backoff_schedule`：根据总尝试次数和其他参数，生成完整的退避延迟序列。

该模块不修改任何现有文件，只新增这一个模块。所有计算均为纯逻辑，不依赖当前时间、随机数或任何外部状态。

## 输入
- `attempt`：整数，表示第几次尝试，从 1 开始。必须为正整数，即大于 0；否则抛出 `ValueError`。
- `base_delay`：浮点数，表示第一次尝试前的基础延迟（单位：秒）。必须非负，即大于等于 0；否则抛出 `ValueError`。
- `multiplier`：浮点数，退避乘数因子。必须非负，即大于等于 0；否则抛出 `ValueError`。
- `max_delay`：浮点数，允许的最大延迟（单位：秒）。必须非负，即大于等于 0；否则抛出 `ValueError`。
- `total_attempts`：整数，表示要生成的延迟个数。必须非负，即大于等于 0；为 0 时返回空列表；为负数时抛出 `ValueError`。

## 输出
- `exponential_backoff_delay` 返回一个浮点数，表示第 `attempt` 次尝试前需要等待的秒数。
- `build_exponential_backoff_schedule` 返回一个浮点数列表，长度为 `total_attempts`，列表中第 `i` 个元素（索引 `i`）对应于第 `i+1` 次尝试的延迟。
- 所有非法参数都会抛出 `ValueError`。

## 预期行为
1. 对于 `exponential_backoff_delay`，延迟值按照以下规则计算：`base_delay` 乘以 `multiplier` 的 `(attempt - 1)` 次方；如果计算结果超过 `max_delay`，则直接返回 `max_delay`。
2. 当 `attempt` 为 1 时，任何非负的 `multiplier`（包括 0）都视为乘数 1，结果等于 `base_delay`，但仍受 `max_delay` 限制。
3. 当 `multiplier` 为 0 且 `attempt` 大于 1 时，结果应为 0（因为 0 的正数次方为 0），并且 0 不会超过非负的 `max_delay`，故返回 0。
4. 当 `base_delay` 为 0 时，无论 `attempt` 和 `multiplier` 如何（只要非负），结果应为 0，且不会超过 `max_delay`，故返回 0。
5. 当 `max_delay` 小于按公式计算的延迟时，必须返回 `max_delay`，不能返回未截断的计算值。
6. 对于 `build_exponential_backoff_schedule`，列表中的每个元素使用与 `exponential_backoff_delay` 相同的规则计算，元素顺序从 attempt 1 开始依次递增。
7. 若 `total_attempts` 为 0，返回空列表 `[]`。
8. 若 `total_attempts` 为负数，抛出 `ValueError`。
9. 所有非法参数（包括 `attempt` 小于等于 0、`base_delay`、`multiplier` 或 `max_delay` 小于 0、`total_attempts` 小于 0）必须抛出 `ValueError`，不允许静默返回错误结果。

## 约束条件
- 新模块必须位于 `tenacity/backoff_schedule.py`。
- 不得修改任何现有文件（包括 `tenacity` 包内其他模块和 `tests` 目录下已有测试）。
- 只能使用 Python 标准库，不得引入新的第三方依赖。
- 函数必须为纯逻辑实现，不得进行网络请求、文件操作、随机数生成、获取当前时间等副作用行为。
- 测试将直接从 `tenacity.backoff_schedule` 导入公开函数并进行断言，请确保函数名和签名与要求一致。