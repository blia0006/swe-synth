## 背景与上下文
CacheControl 是一个给 requests 添加 HTTP 缓存透明层的库。缓存逻辑中经常需要解析响应头里的 Cache-Control 字段（例如判断 max-age、no-store 等）。当前项目中缺少一个独立、可复用的 Cache-Control 指令解析模块。

## 需要实现的功能
新增 `cachecontrol/cache_control_directives.py`。该模块负责解析 Cache-Control 头字段，并提供从 header 映射中提取指令、获取 max-age 数值、判断某指令是否存在等纯函数。

## 输入
- `parse_cache_control(value: str)`：参数是单个 Cache-Control 头的字符串值，可能为空。
- `cache_control_from_headers(headers: Mapping[str, str])`：参数是 HTTP 头的映射，键为头字段名，值为头字段值。
- `get_max_age(headers: Mapping[str, str])`：同上。
- `has_directive(headers: Mapping[str, str], directive: str)`：同上，外加指令名。

项目允许 headers 使用大小写不同的键，如 `Cache-Control` 或 `cache-control`。

## 输出
- `parse_cache_control` 返回 `dict[str, str | None]`，键为指令名（统一小写），值为指令的字符串值；对于没有取值的指令，返回 `None`。空字符串输入返回空 dict。
- `cache_control_from_headers` 返回从 headers 中取出的 Cache-Control 解析结果；若不存在该头或值为空，返回空 dict。若出现多个大小写不同的同名 header（实际不常见），以最后遍历到的一个为准。
- `get_max_age` 返回 `int | None`：当 max-age 指令存在且其值能被 `int()` 解析时返回该整数；否则返回 `None`（包括 max-age 不存在、值为空、不是合法整数）。
- `has_directive` 返回 `bool`：若指令名（不区分大小写）存在于 Cache-Control 中则返回 `True`，否则 `False`。

## 预期行为
1. 解析以逗号分隔指令，忽略指令前后的空白；空指令片段被忽略。
2. 指令名大小写不敏感，统一转为小写作为字典键。
3. 形如 `foo=bar` 的指令，值为 `bar`；形如 `foo="bar"` 的值要去掉两侧双引号，内部逗号保留。
4. 形如 `foo` 的指令，值为 `None`。
5. 若同一指令出现多次，后面的值覆盖前面的值。
6. 等号左边为空的无效片段（如 `=bar`）忽略；等号右边为空时，值为空字符串，不会被丢弃。
7. `cache_control_from_headers` 查找 header 时忽略键大小写。
8. `get_max_age` 只在 `max-age` 的值可被 `int()` 解析时返回整数，否则 `None`。
9. `has_directive` 忽略指令参数大小写。

## 约束条件
- 新模块必须放在 `cachecontrol/cache_control_directives.py`，不得修改任何测试文件。
- 只能使用 Python 标准库和项目已有依赖（Requests、urllib3、filelock、msgpack、redis、cherrypy），不得引入新的第三方依赖。
- 所有函数必须是纯函数，不得进行网络请求、文件读写、随机数或依赖当前时间。
- 测试文件为 `tests/test_cache_control_directives_synth.py`，不要修改它。