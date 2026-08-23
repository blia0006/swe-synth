## 背景与上下文

`cachecontrol` 是一个为 HTTP 响应提供缓存策略的项目。`heuristics.py` 中的 `LastModified` 启发式用于在响应缺少明确 `Expires` 头时，根据 HTTP 的 `Last-Modified` 和 `Date` 头信息推断一个合理的过期时间。

该函数由缓存中间件调用，返回要合并到响应头中的字段映射；通常只返回 `expires` 字段。

## 需要实现的功能

实现 `LastModified.update_headers` 方法。该方法需要检查响应的已有头信息和状态码，决定是否推断 `expires` 头；如果可以推断，则返回包含 `expires` 的字典，否则返回空字典。

## 输入

- `resp`: 一个 `HTTPResponse` 对象，至少包含：
  - `resp.headers`: 映射类型的 HTTP 响应头，键为小写头字段名，值为字符串
  - `resp.status`: 整数状态码
- 需要关注的头字段包括：`last-modified`、`date`、`cache-control`、`expires`
- 当前类属性 `cacheable_by_default_statuses` 保存默认视为可缓存的状态码集合（包含 200、203、204、206、300、301、404、405、410、414、501）

## 输出

返回 `dict[str, str]`：
- 不推断任何缓存头时，返回空字典 `{}`
- 推断成功时，返回类似 `{"expires": "Tue, 15 Nov 1994 08:12:31 GMT"}` 的字典，`expires` 值为 GMT 格式的 HTTP 日期字符串

## 预期行为

请按以下规则处理响应：

1. 如果响应头中已经存在 `expires` 头，则不替换已有值，返回空字典。
2. 如果响应头中没有 `last-modified` 头，或该头值为空，则无法推断，返回空字典。
3. 如果响应头中存在 `cache-control` 头，且其值中不包含字符串 `public`，则不使用 `Last-Modified` 启发式，返回空字典。
4. 如果 `resp.status` 不在 `self.cacheable_by_default_statuses` 集合中，则返回空字典。
5. 将 `last-modified` 头值按 HTTP 日期格式解析为时间；如果无法解析，则返回空字典。
6. 确定参考时间：如果响应头中存在 `date` 头，则将其按 HTTP 日期格式解析并作为参考时间；如果没有 `date` 头，则使用当前系统时间作为参考时间。
7. 如果解析出的 `last-modified` 时间晚于参考时间，说明日期不可靠或来自未来，返回空字典。
8. 在正常路径下，计算 `age = max(0, 参考时间 - last_modified 时间)` 秒；再计算 `freshness_lifetime = min(age / 10, 86400)` 秒，即新鲜期为当前年龄的 10%，最长不超过 24 小时且不小于 0 秒。最后将 `expires` 设置为参考时间加上 `freshness_lifetime` 后的时间，并格式化为 GMT HTTP 日期字符串，返回 `{"expires": ...}`。

注意：第 5 条中的“按 HTTP 日期格式解析”应能处理带时区偏移的 RFC 1123/5322 日期；若带时区的解析失败，还应尝试不带时区偏移的标准 HTTP 日期格式。

## 约束条件

- 不得修改任何测试文件。
- 保持函数签名 `def update_headers(self, resp: HTTPResponse) -> dict[str, str]:` 不变。
- 不要改变类中其他方法或模块级函数的签名。
- 不允许调用当前线索之外的其他私有启发式方法。