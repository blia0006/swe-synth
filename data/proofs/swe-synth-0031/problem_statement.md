## 背景与上下文
`cachecontrol.serialize` 负责将 HTTP 响应及请求相关信息序列化为缓存条目。`Serializer.dumps` 是序列化入口，它可能从响应对象读取 body、构造包含响应元数据和 Vary 头的字典，最终交给 `serialize` 并返回字节串。随着逻辑加入，该方法混合了读取副作用、元数据组装、Vary 头展开和字节编码，结构逐渐不清晰。现在希望对它做一次纯结构重构，保持行为完全一致，并将独立职责拆成模块级私有辅助函数。

## 需要实现的功能
这是一道重构题：你需要调整 `Serializer.dumps` 的实现，使其结构更清晰、可维护。重点是提取辅助函数、降低有效行数和圈复杂度，而不是改变任何可见行为或返回结果。签名必须逐字保持不变，不得改变函数对外契约。

## 输入
- `self`：`Serializer` 实例，提供 `serde_version` 和 `serialize`。
- `request`：`requests.PreparedRequest` 对象，其 `headers` 用于计算 Vary 头。
- `response`：`urllib3.HTTPResponse` 对象，包含状态、headers、version 等信息；当 body 未提供时可能被读取和修改。
- `body`：`bytes | None`，可选响应体；默认 `None`，表示需要从 `response` 读取。

## 输出
- 始终返回 `bytes`：由 `b"cc=<serde_version>"` 与 `self.serialize(data)` 通过 `b","` 连接而成。
- 无新增异常：当输入合法时，异常会与当前实现中自然出现的异常完全一致（例如 `response.read`、`self.serialize` 内部可能抛出的异常）。
- 对 `response` 的副作用保持不变：当 `body is None` 时，会读取 body 并替换 `response._fp`、更新 `response.length_remaining`；否则不触碰。

## 预期行为
① 必须保持以下行为完全不变（逐项）：
   - 构造 `response_headers = CaseInsensitiveDict(response.headers)` 及其后的大小写不敏感 Vary 判断。
   - 当 `body is None` 时，先读取 `decode_content=False`，然后修改 `response._fp` 和 `response.length_remaining`。
   - 响应元数据字典 `data["response"]` 的键、值转换方式与顺序不变。
   - `data["vary"]` 为空字典或在命中 Vary 头时包含展开后的 header 键值；`header = str(header).strip()`，`header_value` 先 `request.headers.get(header, None)` 再在非 `None` 时 `str()` 转换。
   - 最终返回字节串的拼接方式、顺序不变。
② 要求消除的具体坏味道：降低 `dumps` 的有效行数与圈复杂度；将读取 body、构造响应数据、构造 Vary 字典等职责拆成模块级私有函数。
③ 允许的结构性调整：提取模块级 `_` 开头辅助函数；调整局部变量命名以更清晰；调整类型标注；在保持语义不变的前提下重新组织辅助函数的表达方式。禁止压行或用复杂表达式折叠逻辑。

## 约束条件
- `Serializer.dumps` 签名必须逐字不变（参数名、顺序、默认值、类型注释、装饰器均不变）。
- 不得修改任何测试文件。
- 不得改变任何对外可见行为：相同输入必须产生相同输出，且对 `request`、`response` 产生的所有副作用及其顺序必须与当前实现一致。
- 只能使用标准库以及该文件已经导入的依赖（如 `io`、`typing`、`requests.structures.CaseInsensitiveDict`、`urllib3.HTTPResponse` 等），不得新增第三方依赖。
- 提取出的辅助函数应为模块级、以下划线开头，并放在文件末尾；不得将大段逻辑塞进另一个巨型函数。
- 不得删除或重命名 `Serializer.dumps`。

## 自动化质量门槛

除了「所有既有测试必须保持通过」之外，本题还有一组**静态指标**判据
（由 `tests/test_refactor_guard_swe_synth_0031.py` 自动检查，该文件属于判据、不可修改）：

| 指标 | 重构前 | 要求 |
|---|---|---|
| `Serializer.dumps` 的有效代码行数 | 27 | ≤ **12** |
| `Serializer.dumps` 的圈复杂度 | 6 | ≤ **3** |
| 文件内任一函数的有效代码行数 | — | ≤ **26** |
| `Serializer.dumps` 的参数列表 | `self, request, response, body` | 保持完全不变 |

说明：
- 「有效代码行数」不含 docstring、空行与纯注释行，所以删注释、压行都无助于达标
- 最后一项限制的用意是防止「把逻辑整体搬到另一个大函数」这种伪重构
- 达标的常规做法是：把目标函数中彼此独立的职责，提取为若干个小的私有辅助函数
