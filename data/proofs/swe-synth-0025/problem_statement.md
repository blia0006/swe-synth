## 背景与上下文

`psf/cachecontrol` 是一个 HTTP 缓存库，`cachecontrol/heuristics.py` 中的 `LastModified.update_headers` 负责在响应没有显式 `Expires` 或 `Cache-Control: public` 时，根据 `Date` 和 `Last-Modified` 头计算一个启发式的 `Expires` 值。当前实现将所有前置条件检查、日期解析和过期时间计算都堆在一个方法里，圈复杂度为 10，结构不清晰，也难以针对子逻辑进行测试。本次任务要求在不改变对外行为的前提下，重构该方法，使其结构更清晰、职责更单一。

## 需要实现的功能

这是一道代码重构题，目标是改善结构，而不是增加或修改功能。你需要将 `LastModified.update_headers` 中多个相互独立的逻辑块提取为模块级私有辅助函数，使目标方法本身变得简短、可读，同时保持其签名和所有对外行为完全不变。

## 输入

目标方法的签名为 `update_headers(self, resp: HTTPResponse) -> dict[str, str]`，其中：

- `resp` 是 `HTTPResponse` 对象，包含 `headers`（响应头映射）和 `status`（整数状态码）。
- `self` 是 `LastModified` 实例，其 `cacheable_by_default_statuses` 属性定义了默认可缓存的状态码集合。

重构后该签名不得有任何变化。

## 输出

该方法返回一个 `dict[str, str]`：

- 若满足所有缓存前置条件并且计算出的新鲜度大于当前年龄，返回 `{"expires": "<HTTP-date>"}`，其中 `<HTTP-date>` 是根据启发式过期时间格式化的日期字符串。
- 否则返回空字典 `{}`。

异常语义：如果 `Date` 头存在但无法被 `parsedate_tz` 解析，应继续抛出 `AssertionError`；其余不满足条件的情况均返回空字典，不抛异常。

## 预期行为

① 必须保持以下行为完全不变：

- 所有条件的判断顺序和返回值：先检查响应头是否已有 `Expires`，再检查 `Cache-Control` 是否不是 `public`，再检查状态码是否不在 `self.cacheable_by_default_statuses` 中，再检查是否缺少 `Date` 或 `Last-Modified`；任一不满足直接返回 `{}`。
- 日期解析与时间计算规则：先用 `parsedate_tz` 解析 `Date`，`assert time_tuple is not None`，再用 `parsedate` 解析 `Last-Modified`；若后者为 `None` 返回 `{}`；然后计算 `current_age = max(0, now - date)`，`delta = date - calendar.timegm(last_modified)`，`freshness_lifetime = max(0, min(delta / 10, 24 * 3600))`；若 `freshness_lifetime <= current_age` 返回 `{}`；否则返回 `{"expires": time.strftime(TIME_FMT, time.gmtime(expires))}`。
- 返回的 `expires` 字符串格式必须与原来的 `TIME_FMT` 和 `time.gmtime` 用法完全一致。
- 异常触发条件不变：`Date` 无法解析时触发 `AssertionError`。

② 要求消除以下具体坏味道：

- 长方法和连续提前返回导致的复杂控制流；将守卫条件抽取为独立辅助函数。
- 日期解析和过期时间计算与缓存策略判断耦合；提取为独立辅助函数。
- 魔法数字 `10` 和 `24 * 3600` 应被放入具有语义的辅助函数（或常量）中。
- 圈复杂度应显著降低（例如目标方法主体的圈复杂度降到 3 左右）。

③ 允许的结构性调整：

- 在模块级添加以下划线开头的私有辅助函数，并将目标方法中的逻辑块迁移进去。
- 对辅助函数增加类型标注。
- 调整局部变量名，只要不改变语义。
- 目标方法内部可以调用这些辅助函数，但不能改变签名。

## 约束条件

- `LastModified.update_headers` 的签名必须逐字保持不变，包括参数名、顺序、默认值、返回类型标注。
- 不得修改任何测试文件。
- 不得改变任何对外可见行为：所有返回值、异常类型与触发条件、副作用顺序都必须与原实现完全一致。
- 不得引入新的第三方依赖；只能使用标准库以及该文件已经导入的模块。
- 不得删除或重命名 `LastModified.update_headers`。
- 提取的辅助函数必须放在模块级，并使用下划线前缀表示私有。

## 自动化质量门槛

除了「所有既有测试必须保持通过」之外，本题还有一组**静态指标**判据
（由 `tests/test_refactor_guard_swe_synth_0030.py` 自动检查，该文件属于判据、不可修改）：

| 指标 | 重构前 | 要求 |
|---|---|---|
| `LastModified.update_headers` 的有效代码行数 | 23 | ≤ **14** |
| `LastModified.update_headers` 的圈复杂度 | 10 | ≤ **5** |
| 文件内任一函数的有效代码行数 | — | ≤ **13** |
| `LastModified.update_headers` 的参数列表 | `self, resp` | 保持完全不变 |

说明：
- 「有效代码行数」不含 docstring、空行与纯注释行，所以删注释、压行都无助于达标
- 最后一项限制的用意是防止「把逻辑整体搬到另一个大函数」这种伪重构
- 达标的常规做法是：把目标函数中彼此独立的职责，提取为若干个小的私有辅助函数
