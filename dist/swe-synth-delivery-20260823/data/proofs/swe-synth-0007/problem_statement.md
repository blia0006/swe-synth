## 背景与上下文

tldextract 在提取域名后缀时，需要先得到规范化的主机名。当前源码中主机名处理逻辑分散在调用处，缺少一个可复用的纯逻辑模块来从 URL 或普通字符串中提取主机名，并进行 Unicode/IDNA 规范化与标签拆分。请新增该模块，方便其他模块复用并减少重复代码。

## 需要实现的功能

新增 `tldextract/hostname_utils.py`，提供三个公开函数：`extract_hostname`、`normalize_hostname`、`split_domain`。职责边界：本模块只负责主机名字符串的解析、规范化和拆分，不涉及网络请求、文件读写或其他副作用；调用方需自行保证传入的是字符串。

## 输入

- `extract_hostname(text: str) -> str`
  - `text`：任意字符串，可能是完整 URL（含 scheme，如 `http://`、`https://`）或裸主机名（如 `example.com`）。
- `normalize_hostname(hostname: str, *, keep_unicode: bool = False) -> str`
  - `hostname`：任意字符串，表示一个 DNS 主机名，**不包含 IP 地址字面量**。
  - `keep_unicode`：布尔关键字参数，若为 `True` 则保留/解码为 Unicode 形式；若为 `False` 则编码为 ASCII punycode。
- `split_domain(hostname: str) -> list[str]`
  - `hostname`：任意字符串，表示一个 DNS 主机名。

## 输出

- `extract_hostname` 返回规范化后的主机名字符串。
  - 若 `text` 是空字符串或只含空白，返回空字符串 `''`。
  - 若 `text` 被识别为 URL，但无法从 URL 中提取出主机名（例如 `http://`），抛出 `ValueError`。
  - 若提取出的主机名是 IPv4 或 IPv6 地址字面量，返回其小写形式，不再做域名规范化。
- `normalize_hostname` 返回规范化后的主机名字符串。
  - 若输入为空或只含空白，返回 `''`。
  - 若输入是 IP 地址字面量，抛出 `ValueError`。
  - 若 `keep_unicode=False` 且输入含非 ASCII 字符，返回 IDNA/punycode 编码后的 ASCII 字符串；若编码失败，抛出 `ValueError`。
  - 若 `keep_unicode=True` 且输入是 punycode 编码，返回解码后的 Unicode 字符串；若解码失败但输入本身就是 Unicode，则原样返回小写形式。
- `split_domain` 返回域名标签列表（`list[str]`），按从左到右的原域名顺序排列；空输入返回 `[]`。若输入无法规范化，抛出 `ValueError`。

## 预期行为

1. URL 提取：`extract_hostname('https://www.Example.COM:8080/path?q=1')` 返回 `'www.example.com'`。
2. 裸域名提取：`extract_hostname('sub.example.com')` 返回 `'sub.example.com'`。
3. 空白与尾点处理：`extract_hostname('  example.com.  ')` 返回 `'example.com'`；`normalize_hostname` 对任意输入都需去除两端空白和所有尾部点。
4. IP 地址：`extract_hostname('http://127.0.0.1:8000/')` 返回 `'127.0.0.1'`；`extract_hostname('http://[::1]:8000/')` 返回 `'::1'`。`normalize_hostname('127.0.0.1')` 或 `normalize_hostname('::1')` 必须抛出 `ValueError`。
5. 无法提取的 URL：`extract_hostname('http://')` 抛出 `ValueError`。
6. Unicode 到 ASCII：`normalize_hostname('münchen.com')` 返回 `'xn--mnchen-3ya.com'`（punycode）。
7. punycode 到 Unicode：`normalize_hostname('xn--mnchen-3ya.com', keep_unicode=True)` 返回 `'münchen.com'`。
8. 标签拆分：`split_domain('www.example.com')` 返回 `['www', 'example', 'com']`；`split_domain('')` 返回 `[]`。
9. 大小写统一：所有非 IP 的主机名规范化结果必须是小写。

## 约束条件

- 新模块路径必须为 `tldextract/hostname_utils.py`。
- 不得修改项目内任何现有文件，包括 `tests/` 下的任何文件。
- 不得引入新的第三方依赖；只允许使用 Python 标准库和项目已有依赖 `idna`。
- 不得进行网络请求、文件读写、子进程、随机数或依赖当前时间的操作。
- 不得修改任何测试文件；测试将作为独立判据。