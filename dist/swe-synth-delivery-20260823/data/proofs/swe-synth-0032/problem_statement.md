## 背景与上下文

CacheControl 是一个 HTTP 缓存库，本模块提供基于 Redis 的缓存后端 `RedisCache`。该缓存后端通过一个 Redis 连接对象保存和读取缓存条目，并支持为条目设置过期时间。过期的表示形式可以是整数秒、带时区或不带时区的 `datetime`，也可以完全不设置过期时间。

## 需要实现的功能

实现 `RedisCache.set` 方法，将指定 `key` 对应的字节串 `value` 写入 Redis。若调用方提供了 `expires`，则需要根据其类型计算 TTL，并以带过期时间的方式写入；未提供 `expires` 时执行永久写入。

## 输入

- `key`：`str`，缓存键名。
- `value`：`bytes`，要保存的字节内容。
- `expires`：`int | datetime | None`，可选过期时间。
  - `int`：表示 TTL 秒数。
  - `datetime`：表示过期时刻，可能是 naive 或 aware。
  - `None`：表示不设置过期时间。

## 输出

无返回值（`None`）。

## 预期行为

1. 当 `expires` 为 `None` 时，使用 Redis 连接对象的 `set` 操作写入 `key` 和 `value`，不设置 TTL。
2. 当 `expires` 为 `int` 时，将该整数直接作为 TTL 秒数，并使用 Redis 连接对象的 `setex` 操作写入 `key` 和 `value`。
3. 当 `expires` 为 `datetime` 且其 `tzinfo` 为 `None`（naive）时，需要把它视作 UTC 时间：先使用 `replace` 将该 `datetime` 的 `tzinfo` 设置为 `timezone.utc`，再与当前 UTC 时间比较，计算 `total_seconds` 后转换为 `int`，将结果作为 TTL 秒数，并使用 `setex` 操作写入。
4. 当 `expires` 为 aware `datetime` 时，直接与当前 UTC 时间比较，计算 `total_seconds` 后转换为 `int`，将结果作为 TTL 秒数，并使用 `setex` 操作写入。
5. 计算出的 TTL 以 `int` 类型参与写入，写入时使用 `setex` 操作。

## 约束条件

- 不得修改任何测试文件。
- 保持函数签名不变，即 `def set(self, key: str, value: bytes, expires: int | datetime | None = None) -> None`。
- 必须通过 `self.conn` 访问 Redis 连接对象，并分别使用其 `set` 与 `setex` 操作对应有 TTL 和无 TTL 的写入。
- 不要增加异常捕获逻辑。