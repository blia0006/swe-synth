## 背景与上下文
itsdangerous 用于对数据进行签名，确保数据完整性。`TimedSerializer` 是 `Serializer` 的子类，使用 `TimestampSigner` 在签名中额外记录时间信息。`loads` 方法负责验证签名、检查数据是否过期并还原原始数据。本任务需要在给定骨架中实现 `TimedSerializer.loads`。

## 需要实现的功能
实现 `loads` 方法：接收签名后的数据 `s`，验证签名有效性；如果提供了 `max_age`，检查签名是否过期；验证通过后反序列化得到原始 payload，并根据 `return_timestamp` 决定是否同时返回时间戳。允许存在多个签名者（例如 fallback signers），应按顺序尝试。

## 输入
- `s`：`str | bytes`，经过签名后的序列化数据。
- `max_age`：`int | None`，签名允许的最大有效秒数。`None` 表示不检查是否过期；否则签名时间戳与当前时间的差值不得超过该值。
- `return_timestamp`：`bool`，默认 `False`。为 `True` 时返回值中包含签名时的时间戳。
- `salt`：`str | bytes | None`，用于选择/派生签名者的盐值，默认 `None`。

## 输出
- 当 `return_timestamp=False` 时，返回反序列化后的 payload（类型由 `load_payload` 决定）。
- 当 `return_timestamp=True` 时，返回元组 `(payload, timestamp)`，其中 `timestamp` 是签名时记录的时间戳（整数）。
- 如果签名无效且所有签名者都验证失败，抛出最后一个 `BadSignature` 异常。
- 如果签名有效但已超过 `max_age` 指定的期限，抛出 `SignatureExpired` 异常。

## 预期行为
1. 将输入 `s` 统一转换为字节串后再处理。
2. 根据 `salt` 获取签名者列表，并按顺序尝试每一个签名者。
3. 对每个签名者，验证 `s` 的签名并提取出签名时的时间戳；同时根据 `max_age` 检查是否过期。
4. 若某次验证成功，则将签名数据中携带的原始数据部分反序列化为 payload。
5. 若外部传入的 `return_timestamp` 为 `False`，只返回该 payload。
6. 若外部传入的 `return_timestamp` 为 `True`，返回 `(payload, timestamp)`。
7. 若在验证过程中遇到 `SignatureExpired`（签名已过期），立即将其抛出，不再尝试后续签名者。
8. 若遇到其他 `BadSignature` 错误，记录该异常并继续尝试下一个签名者；如果所有签名者都失败，抛出最后一次记录的 `BadSignature` 异常。

## 约束条件
- 不得修改任何测试文件。
- 保持函数签名不变。
- 不要修改 `TimestampSigner`、`Serializer` 或其他模块中的既有实现。