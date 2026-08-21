## 背景与上下文
CacheControl 是一个为 requests 添加 HTTP 缓存能力的库。在处理缓存决策时，经常需要解析和合并 HTTP 头 `Cache-Control` 中的指令（如 `max-age`、`no-cache`、`private`）。目前项目内没有独立、可复用的解析模块，需要新增一个模块来统一处理这些指令。

## 需要实现的功能
新增 `cachecontrol/cache_control_parser.py` 模块，提供对 `Cache-Control` 头字段值的解析、合并与序列化功能。具体包括：
- `parse_cache_control(value: str) -> CacheControlDirectives`：将头字段字符串解析为结构化对象。
- `merge_cache_control(*directives: CacheControlDirectives) -> CacheControlDirectives`：合并多个解析结果。
- `CacheControlDirectives` 类：继承 `dict`，提供规范化字符串表示。
不得修改项目现有文件，只能新增该模块。

## 输入
- `parse_cache_control` 的 `value` 是 `str` 类型，表示一个或多个 `Cache-Control` 指令，指令之间用英文逗号分隔。空字符串或只含空白字符的字符串是合法输入。
- `merge_cache_control` 接受零个或多个 `CacheControlDirectives` 对象作为位置参数。
- `CacheControlDirectives` 继承自 `dict`，键为小写指令名字符串，值为以下三种之一：`True`（无参数指令）、`int`（整数参数）、`str`（带引号的字符串参数或未加引号的 token 参数）。

## 输出
- `parse_cache_control` 返回 `CacheControlDirectives` 对象。
- `merge_cache_control` 返回一个新的 `CacheControlDirectives` 对象。
- `CacheControlDirectives.__str__` 返回规范化字符串：指令按字典序排序；无参数指令输出为 `name`；整数参数输出为 `name=value`；字符串参数输出为 `name="value"`；不同指令之间用 `, `（逗号加空格）分隔。
- 当输入非法时（如未闭合的引号、空的指令名），`parse_cache_control` 抛出 `ValueError`。

## 预期行为
1. `parse_cache_control` 能够解析基本指令，例如 `"max-age=3600, no-cache, private"`，返回包含三个键的 `CacheControlDirectives`，其中 `max-age` 的值为整数 `3600`，`no-cache` 和 `private` 为 `True`。
2. 解析时应忽略每个指令前后的空白字符，指令名大小写不敏感（统一转为小写），`=` 两侧允许空白。
3. 如果参数以双引号开头，则必须配对闭合，提取内部内容作为字符串值，内部即使包含逗号也不能被当作指令分隔符。
4. 如果参数不是带引号字符串，则尝试转换为整数；转换成功则存储为 `int`，失败则按原样存储为字符串（去除首尾空白）。
5. 遇到重复指令名时，后出现的指令覆盖先出现的，不影响指令顺序。
6. 空字符串或只含空白的输入返回空的 `CacheControlDirectives` 对象。
7. 输入中存在未闭合的引号或 `=` 前没有有效指令名时，抛出 `ValueError`。
8. `merge_cache_control` 将多个 `CacheControlDirectives` 合并为一个，合并时后一个对象中的同名指令覆盖前一个对象中的值；如果不传任何参数，返回空对象。
9. `str(directives)` 输出按指令名字母顺序排列，遵循输出格式约定；即使解析时指令顺序随意，序列化结果也要稳定。

## 约束条件
- 新模块路径必须为 `cachecontrol/cache_control_parser.py`。
- 不得修改任何现有项目文件（包括测试文件），仅新增该模块。
- 不得引入新的第三方依赖，只能使用 Python 标准库。
- 不得进行网络请求、文件读写、子进程调用、随机数生成或依赖当前时间等副作用操作，保持纯逻辑。