## 背景与上下文
tenacity 是一个 Python 重试库，提供多种退避等待策略。在实际系统中，大量客户端同时重试可能造成雷群效应，因此等待时间需要加入随机抖动。虽然项目现有的 wait 模块提供了一些内置抖动能力，但缺少一组独立的、可复用的抖动计算函数。本模块将补充这一功能。

## 需要实现的功能
新增模块 `tenacity/jitter.py`，实现三个公开的纯函数 `full_jitter`、`equal_jitter` 和 `decorrelated_jitter`。它们接收基础延迟 `base_delay` 和一个返回 `[0, 1)` 浮点数的零参数 callable `random_func`，计算并返回抖动后的延迟。对于 `decorrelated_jitter` 还需要 `previous_delay`（上一次等待时间）和 `cap`（上限）。函数内部不得调用全局随机源或引入任何副作用，随机性完全由传入的 `random_func` 提供。所有非法参数必须抛出 `ValueError`。

## 输入
- `base_delay`: float，基础延迟（秒），必须非负。
- `previous_delay`: float，上一次延迟（秒），仅 `decorrelated_jitter` 使用，必须非负。
- `cap`: float，最大允许延迟（秒），仅 `decorrelated_jitter` 使用，必须大于等于 `base_delay`。
- `random_func`: `typing.Callable[[], float]`，零参数 callable，每次调用返回一个 `[0, 1)` 区间内的浮点数。返回值若不在该区间，必须抛出 `ValueError`。

## 输出
每个函数返回一个 float，表示抖动后的延迟（秒）。若任何参数不合法（见各函数说明），抛出 `ValueError`。

## 预期行为
1. `full_jitter(base_delay, random_func)` 返回 `base_delay * random_func()`。因此当 `base_delay` 为 0 时返回 0；当 `random_func` 返回 0 时返回 0。
2. `equal_jitter(base_delay, random_func)` 返回 `base_delay / 2 + (base_delay / 2) * random_func()`。因此结果总在 `[base_delay/2, base_delay)` 区间内。
3. `decorrelated_jitter(base_delay, previous_delay, cap, random_func)`：
   - 计算 `upper = min(cap, previous_delay * 3.0)`。
   - 如果 `upper <= base_delay`，返回 `base_delay`。
   - 否则返回 `base_delay + (upper - base_delay) * random_func()`。
   - 这保证返回不低于 `base_delay`，且当 `previous_delay` 为 0 时返回 `base_delay`。
4. 所有函数在以下情况下必须抛出 `ValueError`：
   - `base_delay < 0`；
   - `previous_delay < 0`（仅 `decorrelated_jitter`）；
   - `cap < base_delay`（仅 `decorrelated_jitter`）；
   - `random_func()` 返回值小于 0 或大于等于 1。

## 约束条件
- 新模块必须位于 `tenacity/jitter.py`。
- 不得修改项目内任何现有文件（包括 `tenacity/__init__.py`），只新增该模块。
- 不得修改任何测试文件。
- 不得引入新的第三方依赖；只能使用 Python 标准库和项目已有依赖（本模块实际仅需标准库）。
- 实现必须为纯逻辑，无网络、文件、随机数种子、时间等副作用；随机性只能来自传入的 `random_func` 参数。