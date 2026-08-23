## 背景与上下文

`itsdangerous` 是一个用于对数据进行签名和校验的库。`Serializer` 类负责将 Python 对象序列化并签名，它依赖一个 signer 类为数据生成和校验签名。`make_signer` 是 `Serializer` 中用于创建签名器实例的工厂方法。

## 需要实现的功能

实现 `Serializer.make_signer` 方法，根据当前 `Serializer` 实例保存的配置创建一个新的签名器实例。该方法不应修改任何已有状态，只负责构造并返回 signer。

## 输入

- `salt`: `str | bytes | None`，可选。用于签名时的盐值。若为 `None`，使用构造 `Serializer` 时保存的默认 salt；否则使用调用方传入的值。构造 `Serializer` 时，salt 的默认值为 `b'itsdangerous'`。

## 输出

返回一个 `Signer` 实例。该实例的构造参数应来源于当前 `Serializer` 实例的 `secret_keys`、`salt`（或参数 `salt`）以及 `signer_kwargs`。

## 预期行为

1. 当 `salt` 参数为 `None` 时，使用 `self.salt` 作为签名器的 salt。
2. 当 `salt` 参数不为 `None` 时，使用传入的 `salt` 作为签名器的 salt。
3. 使用 `self.signer`（默认是 `Signer` 类）作为签名器类，将 `self.secret_keys` 作为密钥参数、上一步选择的 salt 作为 salt 关键字参数、`self.signer_kwargs` 中的键值对作为额外关键字参数，创建签名器实例。
4. 返回新创建的签名器实例。

## 约束条件

- 不得修改任何测试文件。
- 保持函数签名不变：`def make_signer(self, salt: str | bytes | None = None) -> Signer:`。
- 不得修改类的其他方法或测试代码。