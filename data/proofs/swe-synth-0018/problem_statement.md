## 背景与上下文
tenacity 是一个通用的 Python 重试库，广泛用于需要自动重试失败操作的场景。在 HTTP 客户端重试中，服务器经常通过 `Retry-After` 响应头告知客户端应该等待多久再发起下一次请求。为方便集成，tenacity 需要一个纯逻辑模块来解析该头部并计算重试延迟。

## 需要实现的功能
新增 `tenacity/retry_after.py` 模块，提供两个公开函数：`parse_retry_after` 和 `retry_after_delay`。前者解析单个 `Retry-After` 值并计算从给定时刻起的延迟秒数；后者从一组 HTTP 头中提取 `Retry-After` 并计算延迟。所有实现不得进行任何 I/O 或依赖当前时间，需要的当前时间作为参数传入。

## 输入
- `parse_retry_after(value: str, now: datetime.datetime) -> float | None`
  - `value`：`Retry-After` 头部的原始字符串。
  - `now`：参考时刻，应为 naive `datetime.datetime` 对象（不带时区），通常表示当前 UTC 时间。
- `retry_after_delay(headers: typing.Mapping[str, str], now: datetime.datetime) -> float | None`
  - `headers`：HTTP 头部映射，键为头部名称（大小写不敏感），值为字符串。
  - `now`：同上，naive `datetime.datetime` 对象。

## 输出
- 两函数返回 `float | None`。
- 当成功计算出延迟时返回非负 `float`（单位秒）。数字形式直接转为 `float`；HTTP-date 形式返回 `max(0.0, (parsed_date - now).total_seconds())`。
- 当值缺失、非法、或无法解析时返回 `None`。

## 预期行为
1. `parse_retry_after` 对去除首尾空白后的字符串进行判断。
2. 若字符串仅由数字组成（非负整数），解析为对应秒数并返回 `float`。例如 `"120"` 返回 `120.0`，`"0"` 返回 `0.0`。
3. 若字符串包含负号、小数点、或其他非数字字符（如 `"-5"`、`"1.5"`、`"abc"`），不能按数字解析，进入下一步尝试按 HTTP-date 解析。
4. 使用标准库中的日期解析工具（例如 `email.utils.parsedate_to_datetime`）尝试将字符串解析为 HTTP-date（支持 RFC 1123、RFC 850、asctime 等常见格式）。若解析成功，计算 `(parsed_date - now).total_seconds()`，若差值为负则返回 `0.0`，否则返回差值。
5. 若解析失败，返回 `None`。
6. `retry_after_delay` 遍历 `headers` 的键，忽略大小写查找 `"retry-after"`。若找到，取其值调用 `parse_retry_after` 并返回结果；若未找到，返回 `None`。
7. 注意：`headers` 的值应为字符串；不符合类型约定的输入行为未定义。
8. 两函数不抛出异常（除类型错误等调用约定错误外），所有非法值均返回 `None`。

## 约束条件
- 模块路径必须为 `tenacity/retry_after.py`。
- 不得修改任何测试文件或项目内已有文件。
- 不得引入新的第三方依赖，仅使用 Python 标准库。
- 实现必须为纯逻辑，不访问网络、文件、随机数、当前时间等副作用。
- 公开接口必须与文档一致，函数体需完整实现。