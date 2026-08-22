## 背景与上下文
CacheControl 是一个为 requests 提供 HTTP 缓存功能的库。在 HTTP 缓存语义中，解析和格式化 HTTP 日期（如 Last-Modified、Expires 响应头中的日期）是常见需求。本模块将提供一个独立的日期工具，用于在缓存逻辑中处理日期字符串，它不依赖网络或文件，只处理输入参数并返回结果。

## 需要实现的功能
在 `cachecontrol/http_date_utils.py` 中实现两个公共函数：`parse_http_date` 和 `format_http_date`。`parse_http_date` 将符合 HTTP/1.1 日期格式的字符串解析为 Unix 时间戳（整数秒）；`format_http_date` 将 Unix 时间戳格式化为 IMF-fixdate 格式的日期字符串。两者都遵循 RFC 7231 对 HTTP 日期的定义，所有日期均视为 GMT。

## 输入
- `parse_http_date(date_str: str)`：`date_str` 是待解析的 HTTP 日期字符串，可能是以下三种格式之一：
    - IMF-fixdate（如 `"Sun, 06 Nov 1994 08:49:37 GMT"`）
    - RFC 850 legacy（如 `"Sunday, 06-Nov-94 08:49:37 GMT"`）
    - asctime（如 `"Sun Nov  6 08:49:37 1994"`，注意日期部分的空格可能被省略或保留）
  其他任意字符串（包括空字符串）均视为无效。
- `format_http_date(timestamp: int)`：`timestamp` 是 Unix 时间戳，表示自 1970-01-01 00:00:00 UTC 以来的秒数，可以是任意整数（包括负数）。

## 输出
- `parse_http_date` 返回 `int`：解析得到的 Unix 时间戳（秒）。如果 `date_str` 无法被识别为上述任一格式，抛出 `ValueError`，异常消息需包含原始输入字符串。
- `format_http_date` 返回 `str`：形如 `"Sun, 06 Nov 1994 08:49:37 GMT"` 的字符串，星期和月份使用英文缩写，日期部分为两位数（不足补零），时区固定为 GMT。

## 预期行为
1. 解析标准 IMF-fixdate 日期字符串，返回对应时间戳。
2. 解析 RFC 850 格式，正确处理两位年份的世纪推断：年份 70–99 视为 19xx，00–69 视为 20xx。
3. 解析 asctime 格式，注意日期字段可能是一个空格加一位数字（如 `" 6"`），解析时必须正确处理；该格式没有时区指示，应视为 GMT。
4. 三种格式解析同一时刻时，返回完全相同的时间戳。
5. 对于所有无法识别或格式错误的字符串（例如空字符串、随机文本、缺失字段、错误的时区缩写等），`parse_http_date` 应抛出 `ValueError`，且不返回部分结果。
6. `format_http_date` 对于任意整数时间戳（包括 0 和负数）都能生成格式正确的字符串，并确保输出可以被 `parse_http_date` 解析回原始时间戳（在秒级精度下）。
7. 星期和月份缩写必须使用英文（不可受本地化影响）。

## 约束条件
- 新模块路径必须为 `cachecontrol/http_date_utils.py`，不得修改项目中任何现有文件。
- 测试文件路径为 `tests/test_http_date_utils_synth.py`，实现者不得修改该文件。
- 只允许使用 Python 标准库和项目已声明的依赖（requests、urllib3、filelock、msgpack、redis、cherrypy），本题实现中不得引入任何新的第三方依赖。
- 实现必须为纯逻辑函数，不得进行网络请求、文件读写、子进程调用、随机数生成或依赖当前系统时间等副作用操作。