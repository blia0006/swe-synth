## 背景与上下文

`itsdangerous` 提供带签名的数据序列化，防止篡改。`TimedSerializer` 继承自 `Serializer`，使用 `TimestampSigner` 在签名中记录时间戳。`loads` 方法用于验证并反序列化 `dumps` 生成的受保护数据，可限制数据的最大有效时间，并支持 fallback signers。

## 需要实现的功能

实现 `TimedSerializer.loads` 方法，对输入的已签名数据进行验证、时间有效性检查、以及负载反序列化。验证通过后返回原始 payload，或根据参数同时返回时间戳。

## 输入

- `s`：`str | bytes`，已签名的字符串或字节序列。
- `max_age`：`int | None`，可选，最大有效秒数。为 `None` 时不限制时间。
- `return_timestamp`：`bool`，可选，默认 `False`。为 `True` 时返回值中包含时间戳。
- `salt`：`str | bytes | None`，可选，用于选择签名者集合。

## 输出

- 当 `return_timestamp=False` 时，返回反序列化后的 payload，类型为任意（`t.Any`）。
- 当 `return_timestamp=True` 时，返回 `(payload, timestamp)` 元组，其中 `timestamp` 为签名时记录的时间戳（整数）。

## 预期行为

1. 将输入的 `s` 视为字节序列处理（`str` 与 `bytes` 均可）。
2. 遍历与给定 `salt` 对应的所有 signer（由 `iter_unsigners(salt)` 提供）。
3. 对每个 signer 执行签名验证与时间检查（使用 `signer.unsign`，传入 `max_age` 并请求返回时间戳），获得 Base64 解码后的负载数据和时间戳。
4. 对签名验证成功的数据调用 `load_payload` 进行反序列化，得到 payload。
5. 如果 `signer.unsign` 抛出 `SignatureExpired`，说明签名有效但已过期，立即向外抛出该异常，不再尝试后续 signer。
6. 如果 `signer.unsign` 或 `load_payload` 抛出 `BadSignature`，记录该异常作为最后异常，并继续尝试下一个 signer。
7. 成功时，如果 `return_timestamp=True`，返回 `(payload, timestamp)`；否则返回 `payload`。
8. 如果所有 signer 均验证失败，抛出最后记录的 `BadSignature`；如果没有可尝试的 signer，抛出 `BadSignature`，错误消息包含 `"No signer found for the given salt."`。

## 约束条件

- 不得修改任何测试文件。
- 保持函数签名不变。
- 不得修改 `TimestampSigner`、`Serializer` 等其他类的实现。
- 仅实现 `TimedSerializer.loads` 方法体。