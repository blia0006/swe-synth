## 背景与上下文
itsdangerous 仓库用于为数据签名和序列化提供安全工具，encoding 模块封装了多种编码转换，包括 Base64 和字节序处理。base64_decode 是该模块中负责将 URL-safe Base64 文本或字节串还原为原始 bytes 的函数。

## 需要实现的功能
实现 base64_decode 函数，使其能够接收 URL-safe Base64 编码的输入，返回解码后的 bytes。解码过程需支持 URL-safe Base64 变体（使用 `-` 和 `_` 替代标准 Base64 的 `+` 和 `/`），并能处理缺少末尾填充字符 `=` 的情况。

## 输入
- `string`：类型为 `str | bytes`，表示待解码的 URL-safe Base64 数据。
  - 当 `string` 为 `str` 时，内部需先将其按 ASCII 编码转换为 `bytes`，编码错误处理策略为 `errors='ignore'`；因此其中非 ASCII 字符会被忽略。
  - 当 `string` 为 `bytes` 时，直接使用该字节串。
  - 输入可能缺少 Base64 标准要求的 `=` 填充，也可能包含不完整的填充长度。

## 输出
- 正常情况下返回一个 `bytes` 对象，即输入 URL-safe Base64 数据解码后的原始字节。
- 成功解码只有一条返回路径；若输入无法作为 URL-safe Base64 解码，则通过异常表示失败，不返回任何值。

## 预期行为
1. 若 `string` 是 `str`，先以 ASCII 编码、`errors='ignore'` 策略将其转换为 `bytes`，非 ASCII 字符被忽略；若 `string` 是 `bytes`，则直接使用该字节串。
2. 对转换后的 `bytes` 进行 URL-safe Base64 解码；如果长度不是 4 的倍数，需补齐 `=` 填充后再解码。
3. 解码成功后返回对应的原始 `bytes`。
4. 如果输入不是有效的 URL-safe Base64 数据（例如包含非法字符或者补齐 `=` 后仍无法正确解码），抛出 `BadData` 异常，异常消息为 `'Invalid base64-encoded data'`。

## 约束条件
- 不得修改任何测试文件。
- 保持函数签名 `def base64_decode(string: str | bytes) -> bytes` 不变。
- 应使用 URL-safe Base64 解码能力（如标准库提供的方法），不要实现自定义的 Base64 解码算法。
- 异常类型必须是 `BadData`，且消息必须与给定字符串完全一致。