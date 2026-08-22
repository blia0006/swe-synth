## 背景与上下文
cachecontrol 库为 requests 提供 HTTP 缓存能力。`wrapper.py` 是该库的入口封装模块，负责把缓存适配器安装到普通 `requests.Session` 上，让后续请求自动具备缓存行为。

## 需要实现的功能
实现 `CacheControl` 函数：根据传入的参数创建缓存适配器，并把它挂载到 Session 的 HTTP 与 HTTPS 协议前缀上，使该 Session 发出的对应请求经过缓存层。函数应返回完成安装后的 Session。

## 输入
- `sess: requests.Session`：需要安装缓存功能的 requests Session。
- `cache: BaseCache | None`：缓存后端实例；为 `None` 时使用默认内存缓存 `DictCache`。
- `cache_etags: bool`：是否缓存 ETag 相关响应。
- `serializer: Serializer | None`：响应序列化器；可为 `None`。
- `heuristic: BaseHeuristic | None`：缓存启发式策略；可为 `None`。
- `controller_class: type[CacheController] | None`：缓存控制器类；可为 `None`。
- `adapter_class: type[CacheControlAdapter] | None`：适配器类；为 `None` 时使用默认的 `CacheControlAdapter`。
- `cacheable_methods: Collection[str] | None`：可缓存 HTTP 方法集合；可为 `None`。

## 输出
返回传入的 `requests.Session` 对象；该对象上已安装好缓存适配器，后续通过 `'http://'` 和 `'https://'` 发出的请求会使用缓存策略。

## 预期行为
1. 当 `cache` 为 `None` 时，使用 `DictCache` 作为默认缓存后端。
2. 当 `adapter_class` 为 `None` 时，使用 `CacheControlAdapter` 作为默认适配器类。
3. 使用最终确定的 `adapter_class` 创建适配器，创建时传入缓存后端、`cache_etags`、`serializer`、`heuristic`、`controller_class` 和 `cacheable_methods`。
4. 将适配器实例挂载到 `sess` 的 `'http://'` 前缀。
5. 将适配器实例挂载到 `sess` 的 `'https://'` 前缀。
6. 返回原来的 `sess` 对象。

## 约束条件
- 保持函数签名不变。
- 不得修改任何测试文件。
- 无需捕获或处理异常。
- 实现应仅安装缓存适配器并返回 Session，不要擅自修改其他全局状态。