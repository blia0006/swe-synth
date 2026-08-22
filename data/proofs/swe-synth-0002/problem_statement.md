## 背景与上下文
`itsdangerous` 是一个用于签名数据的库，`Signer` 类负责生成和验证签名。`derive_key` 方法从给定密钥或默认密钥列表中派生出最终用于签名的字节串密钥，支持多种派生策略（`concat`、`django-concat`、`hmac` 和 `none`）。当前实现将所有派生逻辑内联在一个方法中，导致该方法过长且圈复杂度高，不利于维护和扩展。

## 需要实现的功能
这是一道代码重构题：在不改变任何对外行为的前提下，重构 `Signer.derive_key` 方法，使其结构更清晰、复杂度更低。核心目标是将不同的密钥派生策略分离为独立的辅助函数，并使用统一的派发机制替换冗长的 `if-elif` 链。

## 输入
- `secret_key`：单个密钥，类型为 `str | bytes | None`。若为 `None`，则使用实例属性 `self.secret_keys` 的最后一个元素；否则通过 `want_bytes` 转换为 `bytes`。
- 方法还依赖于实例属性：`self.salt`、`self.key_derivation` 和 `self.digest_method`。这些属性的值在方法调用期间不应被修改。

## 输出
- 返回类型为 `bytes` 的派生密钥。
- 当 `self.key_derivation` 不是 `'concat'`、`'django-concat'`、`'hmac'`、`'none'` 之一时，抛出 `TypeError("Unknown key derivation method")`。

## 预期行为
① 必须保持以下行为完全不变：
- `secret_key` 为 `None` 时，从 `self.secret_keys[-1]` 取值，且不额外调用 `want_bytes`；
- `secret_key` 非 `None` 时，先调用 `want_bytes` 转换为 `bytes`；
- 对于 `'concat'`，返回 `self.digest_method(self.salt + secret_key).digest()` 的 `bytes` 结果；
- 对于 `'django-concat'`，返回 `self.digest_method(self.salt + b"signer" + secret_key).digest()` 的 `bytes` 结果；
- 对于 `'hmac'`，使用 `hmac.new(secret_key, digestmod=self.digest_method)`，更新 `self.salt`，返回 `mac.digest()`；
- 对于 `'none'`，直接返回 `secret_key`；
- 未知派生策略时抛出 `TypeError`，消息完全一致。

② 要求消除以下坏味道：
- 消除魔法字符串（应在模块级定义常量或映射）；
- 消除多个 `elif` 分支，降低圈复杂度；
- 将每种派生逻辑抽取为独立的辅助函数，使 `derive_key` 职责单一。

③ 允许做以下结构性调整：
- 在模块级定义私有辅助函数（以 `_` 开头）和映射字典；
- 在 `derive_key` 中使用字典查找替代 `if-elif` 链；
- 保留原 docstring 和类型标注，不得改变参数签名。

## 约束条件
- 不得修改 `Signer.derive_key` 的函数签名（参数名、顺序、默认值、返回类型标注必须逐字不变）。
- 不得修改任何测试文件。
- 不得改变任何对外可见的行为，包括异常类型、异常消息、返回值和副作用。
- 不得引入新的第三方依赖，只能使用标准库和文件中已导入的模块。
- 提取的辅助函数必须放在模块级，并以 `_` 开头表示私有。
- 不得删除或重命名 `Signer.derive_key`。

## 自动化质量门槛

除了「所有既有测试必须保持通过」之外，本题还有一组**静态指标**判据
（由 `tests/test_itsdangerous/test_refactor_guard_swe_synth_0002.py` 自动检查，该文件属于判据、不可修改）：

| 指标 | 重构前 | 要求 |
|---|---|---|
| `Signer.derive_key` 的有效代码行数 | 18 | ≤ **9** |
| `Signer.derive_key` 的圈复杂度 | 6 | ≤ **3** |
| 文件内任一函数的有效代码行数 | — | ≤ **26** |
| `Signer.derive_key` 的参数列表 | `self, secret_key` | 保持完全不变 |

说明：
- 「有效代码行数」不含 docstring、空行与纯注释行，所以删注释、压行都无助于达标
- 最后一项限制的用意是防止「把逻辑整体搬到另一个大函数」这种伪重构
- 达标的常规做法是：把目标函数中彼此独立的职责，提取为若干个小的私有辅助函数
