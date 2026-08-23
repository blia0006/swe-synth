## 背景与上下文

tldextract 是一个用于从 URL 或域名中提取子域、域和公共后缀的 Python 库。在处理国际化域名（IDN）时，需要将 Unicode 域名转换为 ASCII 兼容编码（即 IDNA 编码，俗称 punycode），或者将 punycode 编码的域名解码回人类可读的 Unicode 形式。当前项目缺少一个独立、可复用的 IDNA 编码/解码工具模块，本功能旨在填补这一空白。

## 需要实现的功能

新增一个模块 `tldextract/idna_utils.py`，提供两个公开函数：
- `to_ascii(hostname: str) -> str`：将可能包含 Unicode 字符的域名（主机名）转换为 ASCII 的 IDNA 编码形式。
- `to_unicode(hostname: str) -> str`：将 ASCII 的 IDNA 编码域名解码为 Unicode 形式。

两个函数在转换前都需要对输入进行规范化处理：去除首尾空白字符、去除末尾的所有点、将整个字符串转换为小写形式。处理后若字符串为空，则直接返回空字符串。转换过程中使用项目已依赖的 `idna` 库，并负责将 `idna.IDNAError` 异常转换为 `ValueError`。

## 输入

### `to_ascii(hostname: str)`
- `hostname`：字符串，可能包含 Unicode 字符的域名，例如 `"münich.com"`。可以包含首尾空白、尾部点、大写字母。也允许为空字符串或仅包含空白和点。

### `to_unicode(hostname: str)`
- `hostname`：字符串，预期为 ASCII 编码的域名，可能包含 punycode 标签，例如 `"xn--mnich-kva.com"`。同样可以包含首尾空白、尾部点、大写字母（大写字母仅影响 ASCII 部分）。也允许为空字符串或仅包含空白和点。

## 输出

### `to_ascii`
- 返回 `str`：纯 ASCII 的 IDNA 编码域名，不包含尾部点。例如 `"münich.com"` 应返回 `"xn--mnich-kva.com"`。
- 如果规范化后的输入为空字符串，返回空字符串 `""`。
- 如果输入不是有效的域名（如包含空格、非法 Unicode 字符、标签长度超限等），抛出 `ValueError`。

### `to_unicode`
- 返回 `str`：Unicode 域名，不包含尾部点。例如 `"xn--mnich-kva.com"` 应返回 `"münich.com"`。
- 如果规范化后的输入为空字符串，返回空字符串 `""`。
- 如果输入不是有效的 IDNA 域名（如包含无效 punycode 标签、非法字符等），抛出 `ValueError`。

## 预期行为

1. **规范化处理**：两个函数在调用 IDNA 处理前，必须对输入执行以下步骤（顺序可以不同但结果应一致）：
   - 去除字符串首尾的所有空白字符（使用 `str.strip()`）。
   - 去除字符串末尾的所有点（使用 `str.rstrip('.')`）。
   - 将字符串中的所有字符转换为小写（使用 `str.lower()`）。
2. **空输入处理**：如果规范化后的字符串为空，直接返回空字符串 `""`，不调用 `idna` 库。
3. **`to_ascii` 正常转换**：对于有效的 Unicode 域名，返回其对应的 ASCII IDNA 编码字符串。编码必须使用 IDNA 2008 标准（由 `idna` 库实现）。例如 `"münich.com"` 应得到 `"xn--mnich-kva.com"`。对于已经是 ASCII 的域名（如 `"example.com"`），返回原样（但已小写且无尾点）。
4. **`to_ascii` 错误处理**：如果 `idna.encode()` 抛出 `idna.IDNAError`（或其子类），将其捕获并重新抛出为 `ValueError`，原始异常作为 `__cause__`。不得让原始 `IDNAError` 泄漏到函数外。
5. **`to_unicode` 正常转换**：对于有效的 ASCII IDNA 域名，返回对应的 Unicode 字符串。例如 `"xn--mnich-kva.com"` 应得到 `"münich.com"`。对于不包含 punycode 的 ASCII 域名（如 `"example.com"`），返回原样（已小写且无尾点）。
6. **`to_unicode` 错误处理**：如果 `idna.decode()` 抛出 `idna.IDNAError`（或其子类），将其捕获并重新抛出为 `ValueError`，原始异常作为 `__cause__`。不得让原始 `IDNAError` 泄漏到函数外。
7. **边界情况**：
   - 输入 `""`、`"   "`、`"."` 均视为空输入，返回 `""`。
   - 输入包含多个尾点（如 `"example.com.."`）时，去除所有尾点后处理。
   - 输入包含大写 Unicode 字符（如 `"MÜNICH.COM"`）时，应先转换为小写再编码，确保结果正确。
   - 输入包含无效字符（如空格、表情符号）时，应抛出 `ValueError`。

## 约束条件

- 新模块路径必须为 `tldextract/idna_utils.py`，不得修改项目中的任何现有文件。
- 不得引入新的第三方依赖，只允许使用 Python 标准库和项目已依赖的 `idna` 库。
- 函数签名必须与题目描述完全一致，不得修改参数名、类型注解或返回值类型。
- 禁止在网络、文件系统、子进程、随机数、当前时间等有副作用的场景中使用本模块。
- 不得在实现中使用 `NotImplementedError`（骨架代码除外）。