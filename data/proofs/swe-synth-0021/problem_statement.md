## 背景与上下文
tldextract 是一个从 URL 和域名中提取子域名、域名和公共后缀的 Python 库。它依赖公共后缀列表（Public Suffix List, PSL）来判断域名后缀。PSL 文本中除了普通规则外，还包含通配符规则（`*.` 前缀）和异常规则（`!` 前缀），以及注释行和私有域标记。现有代码只能提取后缀字符串，无法区分规则类型，因此需要新增一个纯逻辑模块，将 PSL 文本解析为按类型分类的规则集合。

## 需要实现的功能
新增模块 `tldextract/suffix_rule_parser.py`，提供两个公开函数：
- `parse_suffix_rules(text: str) -> SuffixRules`：解析 PSL 文本，将其中的三类规则（普通、通配符、异常）提取并去重后返回。
- `format_suffix_rules(rules: SuffixRules) -> str`：将分类后的规则重新格式化为 PSL 文本。
该模块只做纯文本解析与格式化，不涉及网络、文件读写或任何副作用。

## 输入
- `parse_suffix_rules` 接收一个字符串 `text`，内容为 PSL 规则的原始文本。文本可能包含注释行（以 `//` 开头，包括 `// ===BEGIN PRIVATE DOMAINS===`）、空白行和规则行。规则行可能以 `!` 开头表示异常规则，以 `*.` 开头表示通配符规则，否则为普通规则。规则中不含空白字符。
- `format_suffix_rules` 接收一个 `SuffixRules` 对象，其中包含三个字符串列表：`regular`、`wildcard`、`exception`。

## 输出
- `parse_suffix_rules` 返回一个 `SuffixRules` 对象，其字段 `regular`、`wildcard`、`exception` 分别为按出现顺序去重后的普通规则、通配符规则、异常规则列表。对于空文本，三个列表均为空列表。
- `format_suffix_rules` 返回一个字符串，依次按 `regular`、`wildcard`、`exception` 的顺序输出每一条规则，每行一条规则，行与行之间用 `\n` 分隔。如果三个列表都为空，返回空字符串。不输出额外注释或私有域标记。

## 预期行为
1. 解析普通规则：输入 `"com\norg\n"`，`regular` 应包含 `"com"` 和 `"org"`，其他列表为空。
2. 解析通配符规则：输入包含 `"*.ck"`，该规则应归入 `wildcard`。
3. 解析异常规则：输入包含 `"!www.ck"`，该规则应归入 `exception`。
4. 注释行跳过：以 `//` 开头的行（包括私有域分隔行 `// ===BEGIN PRIVATE DOMAINS===`）应被忽略，不进入任何列表。
5. 空白行跳过：空字符串行应被忽略。
6. 去重：同一类型中重复出现的规则只保留第一次出现的位置，后续重复丢弃。
7. 保持顺序：规则在各自类型列表中按照在原始文本中首次出现的相对顺序排列。
8. 格式化输出：`format_suffix_rules` 对给定的 `SuffixRules` 对象输出字符串，顺序为 `regular`、`wildcard`、`exception`，每行一条规则；若所有列表为空，返回空字符串。
9. 空输入：`parse_suffix_rules("")` 返回三个空列表。

## 约束条件
- 必须将新模块实现于 `tldextract/suffix_rule_parser.py`，不要修改任何现有文件。
- 只能使用 Python 标准库，不得引入新的第三方依赖（项目已安装的依赖除外，但本模块无需使用）。
- 不得修改任何测试文件。
- 函数签名和 `SuffixRules` 定义需与提供骨架完全一致。
- 实现不得包含任何副作用（网络、文件读写等）。