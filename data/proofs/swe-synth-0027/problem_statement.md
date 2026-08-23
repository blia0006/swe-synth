## 背景与上下文
在 `cachecontrol` 项目中，`heuristics.py` 模块提供了一组缓存启发式策略，用于在没有明确缓存头时为 HTTP 响应补充缓存控制信息。`OneDayCache` 是其中一种策略：它通过为响应添加或调整 `expires` 头，让响应缓存一天。

## 需要实现的功能
实现 `OneDayCache.update_headers` 方法。当响应中已经存在 `expires` 头时，应保留该值；当响应中没有该头时，应基于响应中的 `date` 头计算一天后的时间并格式化为 `expires` 值。最终返回一个包含 `cache-control` 和 `expires` 两个键的头部字典，其中 `cache-control` 的值固定为 `public`。本模块已有的日期解析与格式化辅助函数可用于完成时间处理。

## 输入
- `response`：类型为 `HTTPResponse` 的对象。该对象含有 `headers` 属性，它是一个可索引的头部映射，可能包含 `date` 和/或 `expires` 这类 HTTP 头字段。
- 当 `headers` 中不存在 `expires` 头时，假定 `date` 头存在且其格式能被 `parsedate` 正常解析。

## 输出
返回 `dict[str, str]`，包含以下键：
- `cache-control`：值固定为 `public`。
- `expires`：如果输入响应已有 `expires` 头，则为该头的原值；否则为根据 `date` 头计算出的、一天后的 HTTP 日期字符串。

## 预期行为
1. 若 `response.headers` 中已经存在 `expires` 头，直接使用该头部的原值作为返回字典中 `expires` 的值。
2. 若不存在 `expires` 头，则使用 `parsedate` 解析 `response.headers` 中的 `date` 头部，并以 UTC 时区构造 `datetime` 对象，表示该响应日期。
3. 在解析出的时间基础上加上一天（`timedelta(days=1)`），再将其转换为适合 HTTP `expires` 头的日期字符串格式。
4. 不论上述哪种情况，返回字典的 `cache-control` 都必须是 `public`。

## 约束条件
- 保持函数签名不变：`def update_headers(self, response: HTTPResponse) -> dict[str, str]:`
- 不得修改任何测试文件。
- 不需要处理输入头部缺失或日期解析失败等异常路径；实现应假定在需要解析 `date` 时该字段存在且格式合法。