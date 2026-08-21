## 背景与上下文
CacheControl 是一个基于 requests 的 HTTP 缓存库，遵循 RFC 7234 缓存规范。判断缓存响应是否新鲜时，需要分别计算响应的新鲜度生命周期（freshness lifetime）和当前年龄（current age）。目前仓库缺少一个独立的纯函数模块来集中实现这些计算，因此需要新增该模块。

## 需要实现的功能
在 `cachecontrol/rfc7234_freshness.py` 中实现三个公开函数：
- `compute_freshness_lifetime(headers)`
- `compute_current_age(headers, request_time, response_time, now)`
- `is_fresh(headers, request_time, response_time, now)`

这些函数均为纯函数，只根据输入的头部和时间参数进行计算，不进行任何 I/O 操作。

## 输入
- `headers`: `Mapping[str, str]`，响应头字典，键大小写不敏感。常见头包括 `cache-control`、`expires`、`date`、`age`。
- `request_time`: `datetime`，请求发送时间。
- `response_time`: `datetime`，响应接收时间。
- `now`: `datetime`，当前时间。
所有时间参数均为 naive `datetime`，且处于同一时区。

## 输出
- `compute_freshness_lifetime` 返回 `int` 或 `None`：生命周期秒数，无法确定时返回 `None`。
- `compute_current_age` 返回 `int`：当前年龄秒数，非负。
- `is_fresh` 返回 `bool`：响应新鲜返回 `True`，否则 `False`。

## 预期行为
1. 对于 `compute_freshness_lifetime`：
   - 若 `Cache-Control` 头存在且包含指令 `no-store` 或 `no-cache`，返回 `0`。
   - 若 `Cache-Control` 头包含 `max-age=<seconds>`，且 `<seconds>` 是非负整数，返回该整数。如果值无效或为负数，忽略该指令并继续后续检查。
   - 若上述条件不满足，但存在有效的 `Expires` 和 `Date` 头，则计算两者差值秒数；若差值大于 0 返回该值，否则返回 `0`。
   - 若所有条件均不满足，返回 `None`。
   - HTTP 日期解析使用 `email.utils.parsedate_to_datetime`；失败或缺失视为无效。
2. 对于 `compute_current_age`：
   - `apparent_age` = `max(0, (response_time - date_header).total_seconds())`，无有效 `Date` 头时为 `0`。
   - `age_value` = 解析 `Age` 头，非负整数，否则 `0`。
   - `response_delay` = `max(0, (response_time - request_time).total_seconds())`。
   - `corrected_age_value` = `age_value + response_delay`。
   - `corrected_initial_age` = `max(apparent_age, corrected_age_value)`。
   - `resident_time` = `max(0, (now - response_time).total_seconds())`。
   - 返回 `corrected_initial_age + resident_time`。
3. 对于 `is_fresh`：
   - 调用 `compute_freshness_lifetime` 获取生命周期；若为 `None`，返回 `False`。
   - 否则调用 `compute_current_age` 获取当前年龄；若 `current_age < freshness_lifetime` 返回 `True`，否则返回 `False`（相等视为不新鲜）。

## 约束条件
- 新模块路径必须为 `cachecontrol/rfc7234_freshness.py`。
- 不得修改任何现有文件或测试文件。
- 只允许使用 Python 标准库（如 `datetime`, `email.utils`, `typing`, `re`），不得引入新的第三方依赖。
- 所有函数必须是纯函数，不得有网络请求、文件读写、随机数、获取系统当前时间等副作用。