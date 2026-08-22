## 背景与上下文
tldextract 解析域名时依赖公共后缀列表（Public Suffix List，PSL）。PSL 包含三类规则：普通后缀规则、通配符规则（以 `*.` 开头）和例外规则（以 `!` 开头）。现有代码没有独立提供按规则索引进行后缀查询的模块。本题目新增 `SuffixIndex` 类，用于根据一组 PSL 规则查询主机名对应的公共后缀。

## 需要实现的功能
在 `tldextract/suffix_index.py` 中实现 `SuffixIndex` 类。该类接收一组 PSL 规则，提供两个公开方法：
- `find_suffix(hostname)`：返回主机名匹配的最长公共后缀；如果没有匹配则返回 `None`。
- `is_public_suffix(hostname)`：判断主机名本身是否是一个公共后缀。

## 输入
- `SuffixIndex(rules: Iterable[str])`：`rules` 是可迭代的字符串，每个元素为一条规则。
  - 以 `//` 开头的行为注释，应忽略。
  - 完全为空或只包含空白字符的行应忽略。
  - 以 `!` 开头的行为例外规则；`!` 后面跟具体域名，例如 `!www.ck`。
  - 以 `*.` 开头的行为通配符规则；`*.` 后面跟域名，例如 `*.ck`。
  - 其他非空行为普通字符串规则，例如 `com`、`example.com`。
- `find_suffix(hostname: str)`：接受任意主机名字符串，可能包含大小写字母或末尾点。
- `is_public_suffix(hostname: str)`：接受任意主机名字符串。

## 输出
- `find_suffix` 返回 `str | None`。若找到匹配的公共后缀，返回该后缀字符串（不包含 `*.` 或 `!` 前缀，且与规范化后的主机名中对应部分相同）；若无匹配则返回 `None`。
- `is_public_suffix` 返回 `bool`。当 `find_suffix(hostname)` 的返回值与规范化后的完整主机名完全相同时返回 `True`，否则返回 `False`。

## 预期行为
1. 主机名在比较前应进行规范化：去除首尾空白、去除末尾点、转换为小写。若规范化后为空字符串、以点开头或包含连续两个点，视为无效主机名：`find_suffix` 返回 `None`，`is_public_suffix` 返回 `False`。
2. 初始化时忽略注释行和空白行。
3. 普通规则精确匹配。当多个普通规则匹配时，返回最长（标签数最多）的一个。例如规则包含 `com` 和 `example.com` 时，查询 `www.example.com` 返回 `example.com`，查询 `www.other.com` 返回 `com`。
4. 通配符规则 `*.foo` 表示“恰好一个任意标签后跟 `.foo` 的整个字符串”是公共后缀。例如规则 `*.ck` 时，`foo.ck` 返回 `foo.ck`，`ck` 返回 `None`，`bar.foo.ck` 返回 `foo.ck`。
5. 例外规则 `!foo` 表示主机名 `foo` 本身不是公共后缀，即使它可能匹配其他规则（包括通配符）。查询时，如果某个候选主机名（从长到短逐级去掉最左标签）恰好等于例外规则中的域名，则立即返回 `None`，不再尝试更短后缀。但不影响其他主机名。例如规则包含 `*.ck` 和 `!www.ck` 时，`www.ck` 返回 `None`，`foo.ck` 返回 `foo.ck`，`bar.www.ck` 返回 `None`。
6. `is_public_suffix` 严格基于 `find_suffix` 的结果：只有当 `find_suffix(hostname)` 的结果等于规范化后的完整主机名时才返回 `True`。
7. 如果没有规则匹配主机名，返回 `None`。

## 约束条件
- 新模块路径必须是 `tldextract/suffix_index.py`。
- 不得修改任何现有文件，包括但不限于 `tldextract/*.py` 和 `tests/*`。
- 不得修改任何测试文件。
- 只允许使用 Python 标准库（如 `collections.abc`），不得引入新的第三方依赖。
- 实现必须能被 `from tldextract.suffix_index import SuffixIndex` 导入。