## 背景与上下文
`itsdangerous` 用于对不可信环境中的数据进行签名，`TimedSerializer` 在 `Serializer` 的基础上使用 `TimestampSigner` 为签名加入时间戳。目标函数是 `TimedSerializer.loads`，它是 `dumps` 的反向操作：验证签名、检查时效并恢复原始数据。该模块已提供 `iter_unsigners`、`load_payload`、`want_bytes` 以及 `TimestampSigner.unsign`、`TimestampSigner.timestamp_to_datetime` 等可复用方法。

## 需要实现的功能
实现 `loads`：接收已签名字符串，验证签名是否有效；若提供 `max_age`，检查签名是否过期；返回反序列化后的原始 payload。需要支持通过 `salt` 生成的多个 signer 进行回退验证，并可按需返回签名中的时间戳。

## 输入
- `s`：`str | bytes`，由 `dumps` 生成的已签名数据。实现需同时支持 `str` 和 `bytes` 输入。
- `max_age`：`int | None`，签名允许的最大年龄（秒）。`None` 表示不检查是否过期。
- `return_timestamp`：`bool`，为 `True` 时除 payload 外还要返回签名时间；默认 `False`。
- `salt`：`str | bytes | None`，用于派生 signer 的盐。`None` 表示使用默认盐。

## 输出
- `return_timestamp=False`：返回反序列化后的 payload，其类型与 `dumps` 输入一致。
- `return_timestamp=True`：返回二元组，第一个元素是 payload，第二个元素是由签名时间戳转换得到的 `datetime` 对象。
- 验证失败时抛出异常，不返回正常值。

## 预期行为
1. 将 `s` 规范化为 `bytes`（使用 `want_bytes`），以便后续签名验证处理。
2. 通过 `iter_unsigners(salt)` 获得候选 signer，并逐个尝试。
3. 对每个 signer，调用其 `unsign` 方法并传入 `max_age`，同时要求返回时间戳（`return_timestamp=True`）。如果提供了 `max_age`，`unsign` 会负责检查签名是否过期。
4. `unsign` 成功后，对其返回的 base64 payload 调用 `load_payload`，恢复原始数据。
5. 若 `unsign` 抛出 `SignatureExpired`，说明签名本身有效但已过期，必须立即将该异常重新抛出，不再尝试后续 signer。
6. 若 `unsign` 或 `load_payload` 抛出 `BadSignature`（不包括 `SignatureExpired`），记录最近一次异常，然后继续尝试下一个 signer。
7. 成功完成验证与解析后：
   - 如果 `return_timestamp=False`，仅返回 payload；
   - 如果 `return_timestamp=True`，返回一个二元组，包含 payload 和 `signer.timestamp_to_datetime(timestamp)` 得到的 `datetime`。
8. 如果所有候选 signer 都未能成功验证：
   - 若曾记录到至少一个 `BadSignature` 异常，则重新抛出最后一次记录到的异常；
   - 若没有任何异常记录（例如候选 signer 列表为空），抛出 `BadSignature`，消息为 `"No signer found"`。

## 约束条件
- 不得修改任何测试文件。
- 保持函数签名不变。
- 必须使用项目中已有的异常类型（如 `BadSignature`、`SignatureExpired`）来报告相应错误。
- 不要改变 `dumps`/`unsign` 既有语义或绕过签名检查。