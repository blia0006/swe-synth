## 背景与上下文
`python-dotenv` 用于读取 `.env` 文件并解析其中的变量插值。`variables.py` 中定义了两类 `Atom`：`Literal` 表示普通文本，`Variable` 表示变量引用。`parse_variables` 负责把原始值切分成这些 `Atom`，后续再根据环境变量映射进行解析。

## 需要实现的功能
实现 `parse_variables`，将输入字符串按 dotenv 的变量占位符语法进行切分，普通文本片段包装为 `Literal`，变量占位符解析为 `Variable`，并保持原始顺序。不要在本函数中解析变量的最终值。

## 输入
- `value: str`：任意待解析字符串。它可能是空串、仅含普通字符、仅含变量占位符，或普通字符与变量占位符混合。
- 变量占位符支持两种形式：
  - `${VARIABLE}`
  - `${VARIABLE:-default}`

其中变量名语义上为 `name`，默认值语义上为 `default`；`:-default` 是可选的。

## 输出
返回 `Iterator[Atom]`，按顺序产出：
- 普通文本部分：构造为 `Literal`，传入该段文本。
- 变量占位符：构造为 `Variable`，第一个参数是变量名，第二参数是默认值；若占位符中省略了 `:-default`，默认值为 `None`。
- 输出中不应包含空的 `Literal` 节点。

## 预期行为
1. 输入为空字符串时，不产出任何 `Atom`。
2. 输入中没有任何变量占位符时，将整个输入作为单个 `Literal` 产出，例如 `"-"`、`"a"`。
3. 输入只包含一个变量占位符且位于起始位置时，只产出对应的 `Variable`，不产生空 `Literal`，例如 `${a}` 解析为变量名为 `a`、默认值为 `None` 的 `Variable`。
4. 输入包含 `${VARIABLE:-default}` 形式时，`Variable` 的 `default` 参数为 `:-` 后面的字符串；例如 `${a:-b}` 解析为变量名为 `a`、默认值为 `b` 的 `Variable`。
5. 变量占位符之前有普通文本时，先产出该段文本的 `Literal`，再产出变量对应的 `Variable`。
6. 变量占位符之后有普通文本时，在最后一个变量之后产出剩余文本的 `Literal`。
7. 多个变量和普通文本交错出现时，按输入顺序扁平产出对应的 `Literal` 或 `Variable`，不丢失、不合并相邻的同类段。

## 约束条件
- 保持函数签名 `def parse_variables(value: str) -> Iterator[Atom]:` 不变。
- 不得修改任何测试文件。
- 输入可视为格式正确的字符串，无需进行异常处理或错误恢复。