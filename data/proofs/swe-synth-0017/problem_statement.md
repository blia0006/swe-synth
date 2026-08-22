## 背景与上下文

tenacity 是一个 Python 重试库，广泛应用于网络请求、数据库操作等可能瞬时失败的场景。在实际使用中，开发者往往需要监控重试过程：多少次尝试成功、多少次失败、失败时的异常类型分布、平均耗时等。目前项目缺少一个独立的统计记录模块来聚合这些信息，因此需要新增一个纯逻辑的重试尝试统计器。

## 需要实现的功能

新增 `tenacity/retry_stats.py` 模块，实现一个 `RetryStatsTracker` 类。该类负责记录每次重试尝试的 outcome（成功或失败）、耗时以及失败时的异常类型，并提供 `summary()` 方法返回聚合统计信息。该类不负责执行实际重试、调用回调或进行任何 I/O 操作，只负责数据记录与统计计算。

## 输入

`RetryStatsTracker` 提供以下公开方法：

- `__init__(self) -> None`：无参数，创建一个空的跟踪器。
- `record_attempt(self, outcome: str, error_type: str | None = None, duration: float = 0.0) -> None`：记录一次尝试。
  - `outcome`：字符串，必须为 `'success'` 或 `'failure'`，否则抛出 `ValueError`。
  - `error_type`：当 `outcome` 为 `'failure'` 时，必须是非空字符串，表示异常类型名；当 `outcome` 为 `'success'` 时，必须为 `None`。违反约束抛出 `ValueError`。
  - `duration`：非负浮点数，表示该次尝试耗时（秒）。负数会抛出 `ValueError`。
- `record_success(self, duration: float = 0.0) -> None`：便捷方法，等价于调用 `record_attempt('success', None, duration)`。
- `record_failure(self, error_type: str, duration: float = 0.0) -> None`：便捷方法，等价于调用 `record_attempt('failure', error_type, duration)`。
- `summary(self) -> dict[str, typing.Any]`：无参数，返回统计字典。

## 输出

`summary()` 返回一个字典，包含以下键：
- `total_attempts`：总尝试次数（整数）。
- `success_count`：成功次数（整数）。
- `failure_count`：失败次数（整数）。
- `total_duration`：所有尝试耗时总和（浮点数）。
- `average_duration`：平均耗时（浮点数），如果没有尝试则为 `0.0`。
- `success_rate`：成功率（浮点数，0.0 到 1.0），如果没有尝试则为 `0.0`。
- `error_type_counts`：字典，键为异常类型字符串，值为该异常类型出现的次数。

所有数值均基于已记录的数据计算，不依赖外部状态。

## 预期行为

1. 初始状态：新创建的 `RetryStatsTracker` 调用 `summary()` 返回全零字典：`total_attempts=0`、`success_count=0`、`failure_count=0`、`total_duration=0.0`、`average_duration=0.0`、`success_rate=0.0`、`error_type_counts={}`。
2. 记录成功：调用 `record_success(duration)` 后，总尝试次数和成功次数各加 1，总耗时增加 `duration`，平均耗时和成功率相应更新。
3. 记录失败：调用 `record_failure(error_type, duration)` 后，总尝试次数和失败次数各加 1，总耗时增加 `duration`，异常类型计数中对应类型加 1。
4. 混合记录：多次成功和失败后，`summary()` 返回正确的总次数、各自计数、总耗时、平均耗时（总耗时/总次数）、成功率（成功次数/总次数）以及异常类型分布。
5. `record_attempt` 的 `outcome` 参数若既不是 `'success'` 也不是 `'failure'`，抛出 `ValueError`，且不修改任何内部状态。
6. `record_attempt` 的 `duration` 参数若为负数，抛出 `ValueError`，且不修改任何内部状态。
7. 当 `outcome` 为 `'success'` 时，`error_type` 必须为 `None`，否则抛出 `ValueError`；当 `outcome` 为 `'failure'` 时，`error_type` 必须是非空字符串（`None` 或空字符串均抛出 `ValueError`）。
8. `record_success` 和 `record_failure` 必须分别正确委托给 `record_attempt`，其参数校验规则与 `record_attempt` 一致。

## 约束条件

- 新模块路径必须为 `tenacity/retry_stats.py`。
- 不得修改任何现有文件，包括 `tenacity` 包内的其他模块和 `tests` 目录下的现有测试文件。
- 只允许使用 Python 标准库（如 `typing`、`collections`），不得引入新的第三方依赖。
- 实现必须为纯逻辑，不能有网络请求、文件读写、子进程、随机数、依赖当前时间等副作用。
- 公开接口（类名、方法名、参数列表、类型注解、docstring）必须与骨架文件完全一致。