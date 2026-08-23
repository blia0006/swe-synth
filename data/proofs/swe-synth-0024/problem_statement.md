## 背景与上下文
`Serializer.iter_unsigners` 是 itsdangerous 中用于在验签（unsigning）时生成待尝试签名器序列的核心生成器。它首先产出当前默认签名器，然后遍历 `fallback_signers` 并按条目形态（dict / tuple / 其他）构造不同配置的签名器。该函数目前混合了解析 fallback 条目、构造签名器两个层次的职责，圈复杂度偏高，且局部变量 `fallback` 在分支中被重新赋值，降低了可读性。因此需要在不改变行为的前提下，通过提取辅助函数来简化结构。

## 需要实现的功能
这是一道重构题：请重构 `Serializer.iter_unsigners` 方法。目标是在保持方法签名和所有外部可观察行为完全不变的前提下，降低方法的圈复杂度与有效行数。允许并鼓励提取模块级私有辅助函数，把“解析 fallback 条目为 signer class 与 kwargs”这一独立职责从主流程中分离出去。

## 输入
- `self`: `Serializer` 实例。
- `salt`: `str | bytes | None`，默认值为 `None`。若传入 `None`，则使用实例属性 `self.salt`。
（其余依赖的实例属性：`self.fallback_signers`、`self.signer`、`self.signer_kwargs`、`self.secret_keys`）

## 输出
该方法是生成器，返回 `cabc.Iterator[Signer]`。依次产出：
1. 使用当前 salt 通过 `self.make_signer(salt)` 创建的默认签名器。
2. 对 `self.fallback_signers` 中的每个条目，解析出签名器类和关键字参数后，再对 `self.secret_keys` 中的每个 secret key 依次构造签名器并产出。

## 预期行为
① 必须保持以下行为完全不变：
   - 当 `salt` 参数为 `None` 时，转向使用 `self.salt`。
   - 首次 yield 的签名器由 `self.make_signer(salt)` 生成。
   - 遍历 `self.fallback_signers` 的顺序、每个条目解析出的 signer class 与 kwargs 的规则均不变：
     * dict 条目：signer class 为 `self.signer`，kwargs 为该 dict 本身。
     * tuple 条目：解包 tuple 得到 signer class 和 kwargs（若长度不为 2，需在相同位置抛出相同异常）。
     * 其他类型条目：signer class 为该条目本身，kwargs 为 `self.signer_kwargs`。
   - 对 `self.secret_keys` 的遍历顺序及每个生成的 signer 的构造参数（`secret_key`、`salt=salt`、`**kwargs`）完全不变。
   - 生成器的惰性行为、异常传播、副作用顺序保持一致。
② 要求消除的具体坏味道：
   - `fallback` 变量在循环中被重新赋值为 signer class，命名与含义不一致。
   - 多层嵌套 + 多个 `isinstance` 分支导致圈复杂度偏高，应将解析职责独立出去。
   - “解析 fallback 条目”和“构造 signer”两层逻辑混杂，应分离到更小的单元。
   - 三元/多分支条件可以用更明确的函数返回值来代替重复赋值。
③ 允许的结构性调整：
   - 提取模块级私有辅助函数（例如 `_resolve_fallback_signer` 或类似命名）负责解析单个 fallback 条目，返回 `(signer_class, kwargs)` 二元组。
   - 简化主循环，仅保留遍历和 yield 逻辑。
   - 调整局部变量命名、拆分长行，但不得改变任何计算或条件判断。

## 约束条件
- 目标方法 `Serializer.iter_unsigners` 的签名（包括参数名、顺序、默认值、类型注解、返回类型注解）必须逐字保持不变。
- 不得修改任何测试文件。
- 不得改变任何对外可见行为，包括返回值、异常类型与触发条件、副作用顺序、生成器的惰性求值特性。
- 不得引入新的第三方依赖；只能使用标准库及该文件已导入的模块。
- 提取的辅助函数应为模块级私有函数（以下划线开头），放在文件末尾，并保持行为与主方法协调一致。
- 不得删除、重命名或改变 `Serializer.iter_unsigners` 的可访问性。
- 不得将重构变成单纯“压行”操作（如把多行压缩成难以读的长表达式），应通过分解职责来降低复杂度。

## 自动化质量门槛

除了「所有既有测试必须保持通过」之外，本题还有一组**静态指标**判据
（由 `tests/test_itsdangerous/test_refactor_guard_swe_synth_0029.py` 自动检查，该文件属于判据、不可修改）：

| 指标 | 重构前 | 要求 |
|---|---|---|
| `Serializer.iter_unsigners` 的有效代码行数 | 13 | ≤ **10** |
| `Serializer.iter_unsigners` 的圈复杂度 | 6 | ≤ **4** |
| 文件内任一函数的有效代码行数 | — | ≤ **21** |
| `Serializer.iter_unsigners` 的参数列表 | `self, salt` | 保持完全不变 |

说明：
- 「有效代码行数」不含 docstring、空行与纯注释行，所以删注释、压行都无助于达标
- 最后一项限制的用意是防止「把逻辑整体搬到另一个大函数」这种伪重构
- 达标的常规做法是：把目标函数中彼此独立的职责，提取为若干个小的私有辅助函数
