## 背景与上下文

tldextract 是一个从 URL 和域名中提取子域、注册域和公共后缀的库。在提取过程中，经常需要对域名进行预处理，例如去除尾随点、统一小写、将国际化域名（IDN）转换为 ASCII（Punycode），以及校验域名是否符合 DNS 主机名规则。目前项目还缺少一个独立的、可复用的域名处理工具模块。本题目要求新增一个纯逻辑模块，提供域名的规范化、拆分和有效性检查，以辅助后续的域名提取和分析。

## 需要实现的功能

在 `tldextract/domain_utils.py` 中实现三个公开函数：

- `normalize_domain(domain: str) -> str`
- `split_domain(domain: str) -> list[str]`
- `is_valid_domain(domain: str) -> bool`

该模块不进行任何网络请求、文件读写、随机数、时间获取或其他 I/O 操作，仅对传入的字符串进行纯内存处理。实现时只能使用 Python 标准库和项目已有的 `idna` 依赖。

## 输入

- `normalize_domain(domain: str)`：`domain` 是一个可能包含 Unicode 字符和尾随点的域名字符串。如果传入的不是字符串，应抛出 `TypeError`。
- `split_domain(domain: str)`：`domain` 是一个域名字符串，可以为空或包含任意字符。如果传入的不是字符串，应抛出 `TypeError`。
- `is_valid_domain(domain: str)`：`domain` 是一个域名字符串，可以为空或包含任意字符。如果传入的不是字符串，应抛出 `TypeError`（内部调用的 `normalize_domain` 会抛出该异常）。

## 输出

- `normalize_domain` 返回规范化后的 ASCII 小写域名，且不包含任何尾随点。如果输入是空字符串或仅由点组成（例如 `""`、`"."`、`"..."`），则返回空字符串 `""`。如果输入包含无法通过 IDNA 编码的非法字符（例如空字符 `\u0000`），则抛出 `ValueError`。
- `split_domain` 返回一个字符串列表，表示域名拆分后的标签。如果规范化后的域名为空字符串，则返回空列表 `[]`；否则返回按 `.` 拆分后的列表。该列表可能包含空字符串，例如当域名中存在连续点（`"example..com"`）或开头点（`".example.com"`）时。`split_domain` 不做有效性检查，也不过滤空标签。
- `is_valid_domain` 返回布尔值 `True` 或 `False`。如果域名有效，返回 `True`；否则返回 `False`。对于 `normalize_domain` 抛出的 `ValueError`，`is_valid_domain` 会捕获并返回 `False`；但不会捕获 `TypeError`，因此非字符串输入会传播 `TypeError`。

## 预期行为

1. **尾随点处理**：`normalize_domain("example.com.")` 返回 `"example.com"`；多个尾随点如 `"example.com..."` 也会被全部去除。
2. **空输入处理**：`normalize_domain("")` 返回 `""`；`normalize_domain("...")` 返回 `""`；`split_domain("")` 返回 `[]`；`is_valid_domain("")` 返回 `False`；`is_valid_domain(".")` 返回 `False`。
3. **大小写归一化**：`normalize_domain("WWW.Example.COM")` 返回 `"www.example.com"`。
4. **IDN 转换**：`normalize_domain("bücher.example")` 返回 `"xn--bcher-kva.example"`；`is_valid_domain("bücher.example")` 返回 `True`。
5. **非法字符处理**：`normalize_domain` 对包含无法 IDNA 编码的字符（如空字符 `\u0000`）的域名抛出 `ValueError`；`is_valid_domain` 捕获该异常并返回 `False`。
6. **域名拆分**：`split_domain("www.example.com")` 返回 `["www", "example", "com"]`；`split_domain("example.com.")` 返回 `["example", "com"]`；`split_domain(".example.com")` 返回 `["", "example", "com"]`；`split_domain("example..com")` 返回 `["example", "", "com"]`。
7. **有效域名判断**：以下域名应返回 `True`：`"example.com"`、`"localhost"`、`"foo-bar.example.com"`。
8. **无效域名判断**：以下域名应返回 `False`：空字符串、仅由点组成的字符串、以连字符开头或结尾的标签（如 `"-example.com"`、`"example.com-"`）、包含连续点（`"example..com"`）、包含下划线（`"exa_mple.com"`）、标签长度超过 63 个字符或整体长度超过 253 个字符、标签包含非法字符（如空格）。其中，连续点和下划线等非法字符在 `normalize_domain` 后仍保留，后续校验会返回 `False`。
9. **类型错误**：`normalize_domain`、`split_domain`、`is_valid_domain` 对非字符串输入（如 `None`、整数）会抛出 `TypeError`。

## 约束条件

- 新模块路径必须为 `tldextract/domain_utils.py`，不得修改项目任何现有文件。
- 不得修改任何测试文件。
- 不得引入新的第三方依赖；只能使用 Python 标准库和项目已有依赖 `idna`。
- 代码必须为纯逻辑，不得包含网络请求、文件读写、随机数、时间获取、子进程等副作用。