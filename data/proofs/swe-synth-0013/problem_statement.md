## 背景与上下文
CacheControl 是一个 HTTP 缓存库，RedisCache 是其基于 Redis 的缓存后端实现。该模块负责将缓存条目写入 Redis，并支持为键设置过期时间。RedisCache.set 是缓存写入路径的核心方法。

## 需要实现的功能
实现 RedisCache.set 方法，将给定的 key 和 value 写入 Redis；当提供了 expires 参数时，为键设置过期时间。expires 可能是整数秒数或 datetime 时间点，需要按不同类型分别处理。

## 输入
- key: str，缓存键。
- value: bytes，缓存值，原始字节内容。
- expires: int | datetime | None，过期时间。为 None 表示不设置过期；为 int 表示相对当前时间的秒数；为 datetime 表示绝对过期时间点，可能是 naive 或 aware。

## 输出
返回值类型为 None；方法不产生返回内容，仅产生 Redis 写入副作用。

## 预期行为
1. 若 expires 为 None，使用 conn.set 写入 key-value，不带任何过期时间。
2. 若 expires 为 int 类型，将其视为过期秒数，使用 conn.setex 写入 key-value 并设置该秒数过期。
3. 若 expires 为 datetime 类型：
   - 若其 tzinfo 属性为 None（naive datetime），先将其替换为 UTC 时区（timezone.utc），得到 aware datetime。
   - 计算该过期时间点与当前 UTC 时间 datetime.now(timezone.utc) 的差值。
   - 对该差值调用 total_seconds 得到浮点秒数，再用 int 转换为整数秒数。
   - 使用 conn.setex 以该整数秒数设置过期时间写入 key-value。
4. 对于 aware datetime（tzinfo 不为 None），直接与当前 UTC 时间计算差值，不需要替换时区。
5. 方法不应捕获或处理任何异常。

## 约束条件
- 保持函数签名不变。
- 不得修改任何测试文件。
- 严禁在实现中捕获或吞掉异常。
- 必须使用 Redis 客户端的 set 和 setex 方法完成写入。