## 背景与上下文

tenacity 是一个 Python 重试库，允许用户通过回调（如 `after`、`before_sleep`）观察每次尝试。然而，库本身没有提供记录尝试结果并汇总统计的工具。新增 `tenacity/retry_history.py` 模块，提供一个简单的 `RetryHistory` 类，帮助用户在重试循环中收集成功/失败次数和等待时间等统计信息。

## 需要实现的功能

实现一个名为 `RetryHistory` 的类，用于记录每次重试尝试的结果（成功或失败）以及尝试前等待的时间，并能够返回汇总统计：总尝试次数、成功次数、失败次数、成功率、总等待时间和平均等待时间。

职责边界：只负责记录和统计，不执行任何重试逻辑，不存储每次尝试的具体细节（如异常对象、尝试编号），只计数和累加等待时间。

## 输入

- 构造函数：无参数。
- `record_success`：接受一个位置参数 `attempt_number`（整数，表示第几次尝试，1 开始），以及一个可选关键字参数 `idle_for`（浮点数或 `None`，表示此次尝试前等待的秒数，默认为 `None` 且视为 `0.0`）。
- `record_failure`：接受位置参数 `attempt_number` 和 `exception`（必须是 `BaseException` 实例，但仅用于类型提示，不影响统计），以及可选关键字参数 `idle_for`，含义同上。

## 输出

- `__init__` 返回 `None`。
- `attempts` 属性返回 `int`：成功与失败尝试之和。
- `successes` 属性返回 `int`：成功尝试次数。
- `failures` 属性返回 `int`：失败尝试次数。
- `success_rate()` 返回 `float`：成功次数除以总尝试次数；若总次数为 0，返回 `0.0`。
- `total_idle_time()` 返回 `float`：所有记录的 `idle_for` 之和（`None` 按 `0.0` 计）。
- `mean_idle_time()` 返回 `float`：总等待时间除以总尝试次数；若总次数为 0，返回 `0.0`。

## 预期行为

1. 新创建的 `RetryHistory` 实例各统计值均为 0。
2. 每次调用 `record_success` 或 `record_failure` 后，对应计数增加 1，总尝试次数增加 1。
3. `record_success` 不接收异常参数；`record_failure` 必须提供异常参数，但异常内容不影响统计。
4. `idle_for` 若为 `None` 或省略，按 `0.0` 累加到总等待时间；若为其他数值，按该数值累加。
5. `success_rate` 计算成功比例，当总次数为 0 时返回 `0.0`，避免除零错误。
6. `mean_idle_time` 返回平均等待时间，当总次数为 0 时返回 `0.0`。
7. 多次记录后，所有统计属性应反映累计值。

## 约束条件

- 新模块路径必须为 `tenacity/retry_history.py`，不得修改任何现有文件。
- 不得修改任何测试文件。
- 只允许使用 Python 标准库，不得引入新的第三方依赖。
- 不得在实现中使用随机数、当前时间、网络、文件读写等副作用操作。