## 背景与上下文
`itsdangerous` 用于对数据进行签名，以保证数据完整性。`TimedSerializer` 在普通序列化器基础上加入时间戳支持，能够验证签名是否过期。本任务是实现其 `loads` 方法，完成带时间戳签名数据的验证与反序列化。

## 需要实现的功能
实现 `TimedSerializer.loads` 方法，使其能够验证输入数据的签名与时效性，并在验证成功后返回反序列化后的 payload；当调用者需要时，同时返回签名中的时间戳。

## 输入
- `s`：待验证的序列化数据，类型为 `str` 或 `bytes`。其中包含签名、时间戳以及序列化后的 payload。
- `max_age`：可选整数，表示签名允许的最大年龄（秒）。若为 `None`，则不检查过期；否则签名时间戳距当前时间超过该值时视为过期。
- `return_timestamp`：布尔值，控制是否在返回结果中包含时间戳。默认 `False`。
- `salt`：可选参数，类型为 `str` 或 `bytes` 或 `None`，用于选择或派生用于验证的签名者（signer）。

## 输出
- 当 `return_timestamp=False` 时，返回反序列化后的 payload，类型由序列化器决定，可能是任意 Python 对象。
- 当 `return_timestamp=True` 时，返回一个二元组 `(payload, timestamp)`，其中 `payload` 同上，`timestamp` 为签名时记录的时间戳（整数）。

## 预期行为
1. 将输入 `s` 统一视为字节串处理（若为 `str` 则编码为 UTF-8 字节）。
2. 根据 `salt` 获取签名者迭代器；可能存在多个签名者（例如配置了回退签名者），需要按顺序尝试。
3. 对每个签名者，请求其验证签名并返回 base64 解码后的 payload 和签名时间戳；若提供了 `max_age`，该签名者会检查时间戳是否过期。
4. 若签名有效且未过期，使用序列化器的 `load_payload` 方法将 payload 反序列化为原始对象。
5. 当 `return_timestamp=False` 时，仅返回反序列化后的 payload。
6. 当 `return_timestamp=True` 时，返回 `(payload, timestamp)` 元组，`timestamp` 为整数时间戳。
7. 若某个签名者验证时抛出 `BadSignature`（签名无效），记录该异常并继续尝试下一个签名者。
8. 若某个签名者验证时抛出 `SignatureExpired`（签名有效但已过期），立即抛出该异常，不再尝试后续签名者。
9. 若所有签名者都因 `BadSignature` 失败，抛出 `BadSignature` 异常（通常为最后一个捕获到的异常）。
10. 若签名有效但 payload 反序列化失败，相应异常直接传播，不进行捕获或转换。

## 约束条件
- 不得修改任何测试文件。
- 保持函数签名不变。
- 只实现 `TimedSerializer.loads` 方法，不改变其他模块行为。