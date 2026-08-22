## 背景与上下文
在 `pallets/itsdangerous` 的签名器 `Signer` 中，`derive_key` 负责从 `secret_key` 按 `key_derivation` 配置派生用于签名的密钥。当前实现把参数归一化和多种派生算法混在一个方法里，圈复杂度达到 6，新增算法需要修改核心分支，难以阅读和测试。为提高可维护性，需要在不改变外部行为的前提下重构该方法。

## 需要实现的功能
这是一道重构题：保持 `Signer.derive_key` 的对外签名与运行时行为完全不变，只优化其内部结构。目标是把不同的 key derivation 算法拆成独立的小函数，并用查表分发替代长 `if/elif`，使方法职责单一、圈复杂度显著降低。

## 输入
`secret_key: str | bytes | None = None`，表示要派生密钥的原始密钥。若为 `None`，则使用 `self.secret_keys` 的最后一个元素；否则通过 `want_bytes` 转为 `bytes`。该参数在重构后必须保持原样。

## 输出
返回 `bytes` 类型的派生密钥。对于不同 `self.key_derivation`，返回值语义：
- `"concat"`：`self.digest_method(self.salt + secret_key).digest()`
- `"django-concat"`：`self.digest_method(self.salt + b"signer" + secret_key).digest()`
- `"hmac"`：以 `secret_key` 为 key、`self.digest_method` 为摘要、更新 `self.salt` 后计算 HMAC 摘要
- `"none"`：直接返回 `secret_key`
- 其他任何值：抛出 `TypeError("Unknown key derivation method")`

## 预期行为
① 必须保持以下行为完全不变：
- 函数签名（参数名、顺序、默认值）逐字不变；
- 对 `secret_key` 为 `None` 和非 `None` 的处理逻辑、顺序、转换结果不变；
- 四种合法 `key_derivation` 的返回值、类型、异常类型和消息不变；
- 未知 `key_derivation` 抛出的 `TypeError` 类型和消息不变；
- `self.salt`、`self.digest_method`、`self.secret_keys` 等属性的访问结果和副作用不变。

② 要求消除以下具体坏味道：
- 长方法 / 职责混合；
- 多重 `if/elif` 分支；
- 重复的摘要 / HMAC 调用模式；
- 魔法字符串散落。

③ 允许做以下结构性调整：
- 提取模块级私有辅助函数，分别实现各派生算法；
- 使用一个模块级字典将算法名映射到对应辅助函数，并用查表替代长分支；
- 保留原 docstring 或等价文档。

## 约束条件
- 不得修改 `Signer.derive_key` 的函数签名；
- 不得修改任何测试文件；
- 不得改变对外的函数行为，包括返回值、异常类型与消息、参数处理；
- 不得引入新的第三方依赖（仅允许标准库及当前文件已有导入）；
- 新增的辅助函数必须放在模块级（文件末尾），且以下划线开头表示私有。

## 自动化质量门槛

除了「所有既有测试必须保持通过」之外，本题还有一组**静态指标**判据
（由 `tests/test_refactor_guard_swe_synth_0007.py` 自动检查，该文件属于判据、不可修改）：

| 指标 | 重构前 | 要求 |
|---|---|---|
| `Signer.derive_key` 的有效代码行数 | 18 | ≤ **10** |
| `Signer.derive_key` 的圈复杂度 | 6 | ≤ **3** |
| 文件内任一函数的有效代码行数 | — | ≤ **26** |
| `Signer.derive_key` 的参数列表 | `self, secret_key` | 保持完全不变 |

说明：
- 「有效代码行数」不含 docstring、空行与纯注释行，所以删注释、压行都无助于达标
- 最后一项限制的用意是防止「把逻辑整体搬到另一个大函数」这种伪重构
- 达标的常规做法是：把目标函数中彼此独立的职责，提取为若干个小的私有辅助函数
