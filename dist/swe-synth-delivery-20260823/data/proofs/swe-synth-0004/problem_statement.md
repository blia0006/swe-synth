## 背景与上下文
在 `pallets/itsdangerous` 项目的 `Serializer` 中，`iter_unsigners` 用于生成验证签名时需要尝试的所有 signer。它首先返回默认配置的 signer，然后遍历 `fallback_signers` 构造备用 signer。当前实现虽然正确，但将“解析 fallback 条目的不同格式”、“处理默认参数”和“逐个生成 signer”等多个职责混在一个函数里，导致圈复杂度偏高、难以理解和维护。该函数在反序列化验证过程中被频繁调用，清晰的结构有助于后续扩展和排查问题。

## 需要实现的功能
这是一道代码重构题，目标是改善 `iter_unsigners` 的内部结构，而不是增加或改变功能。你需要在不改变任何对外行为的前提下，将过于集中、分支较多的逻辑拆分为若干小函数，降低原函数的圈复杂度与有效行数，使代码更易读、易测试、易维护。

## 输入
`iter_unsigners` 的参数保持不变：
- `salt`：`str | bytes | None`，可选。若为 `None`，使用实例自己的 `self.salt`。

## 输出
该函数返回一个 `collections.abc.Iterator[Signer]`（生成器）。它会按顺序产出以下 signer：
1. 由 `self.make_signer(salt)` 生成的默认 signer；
2. 对于 `self.fallback_signers` 中的每一个条目，按条目解析规则生成 fallback signer class 和关键字参数，然后对 `self.secret_keys` 中的每一个 `secret_key`，用该 class 和参数实例化一个 signer 并产出。

异常语义也必须保持不变：例如，如果 fallback 条目是长度不为 2 的 tuple，解包时会抛出的 `ValueError` 以及后续的调用错误类型都不能改变。

## 预期行为
① 必须保持以下行为完全不变：
- 参数 `salt` 为 `None` 时回退到 `self.salt`；
- 默认 signer 必须最先被 yield；
- fallback 条目的解析规则：`dict` 形式使用当前 fallback dict 作为 kwargs，signer class 使用 `self.signer`；`tuple` 形式解包为 `(signer_class, kwargs)`；其他形式直接作为 signer class，kwargs 使用 `self.signer_kwargs`；
- 对每个 fallback 条目，必须按照 `self.secret_keys` 的顺序逐个 yield signer；
- 整个函数必须保持生成器惰性，即调用时不会立即执行循环体，只在迭代时产出。

② 要求消除的具体坏味道：
- 过高的圈复杂度与分支嵌套；
- 一个函数承担了参数解析、格式分派和对象构造等多个职责；
- 可读性差，难以快速理解 fallback 的不同格式；
- 重复不明显的逻辑，如多种分支都最终完成同样“构造 signer”的动作但分散在嵌套循环中。

③ 允许做的结构性调整：
- 提取模块级私有辅助函数，例如解析 fallback 条目、生成 fallback signers 的迭代器；
- 使用 `yield from` 委托给辅助生成器；
- 将类型联合或复杂条件拆成小函数中简单的 if/return。

注意：不能把逻辑压成复杂的长表达式或嵌套推导式来“凑”低复杂度；必须通过有意义的小函数拆分来降低复杂度。

## 约束条件
- `Serializer.iter_unsigners` 的签名必须逐字保持不变；
- 不得修改任何测试文件；
- 不得改变任何对外可见的行为，包括参数处理、返回值顺序、异常类型与触发条件、生成器惰性；
- 不得引入新的第三方依赖；只能使用标准库和该文件已经导入的模块；
- 提取出的辅助函数必须放在模块级，并以 `_` 开头表示私有；
- 不得删除或重命名 `Serializer.iter_unsigners`。

## 自动化质量门槛

除了「所有既有测试必须保持通过」之外，本题还有一组**静态指标**判据
（由 `tests/test_itsdangerous/test_refactor_guard_swe_synth_0004.py` 自动检查，该文件属于判据、不可修改）：

| 指标 | 重构前 | 要求 |
|---|---|---|
| `Serializer.iter_unsigners` 的有效代码行数 | 13 | ≤ **10** |
| `Serializer.iter_unsigners` 的圈复杂度 | 6 | ≤ **3** |
| 文件内任一函数的有效代码行数 | — | ≤ **21** |
| `Serializer.iter_unsigners` 的参数列表 | `self, salt` | 保持完全不变 |

说明：
- 「有效代码行数」不含 docstring、空行与纯注释行，所以删注释、压行都无助于达标
- 最后一项限制的用意是防止「把逻辑整体搬到另一个大函数」这种伪重构
- 达标的常规做法是：把目标函数中彼此独立的职责，提取为若干个小的私有辅助函数
