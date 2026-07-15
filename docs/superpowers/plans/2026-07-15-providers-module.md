# Market Data Providers Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成带真实东方财富和新浪适配、统一标准 DTO、有限重试、限流、熔断和能力路由的 Provider 模块。

**Architecture:** 第三方传输与解析封装在适配器内，Router 只返回标准 DTO 和逐项失败。调用管线统一执行截止时间、响应保护、有限重试、共享限流和按能力隔离熔断；正式行情持久化不属于本批。

**Tech Stack:** Python 3.12、HTTPX、Redis、SQLAlchemy 2、FastAPI、pytest。

---

### Task 1: Provider 标准契约

**Files:**
- Create: `backend/src/long_invest/modules/providers/contracts.py`
- Create: `backend/tests/modules/providers/test_contracts.py`

- [ ] 测试五种能力、Provider 代码、内部股票代码、Decimal 价格和带时区时间。
- [ ] 测试报价不能混合来源，日线 OHLC/数量字段和代码范围必须有效。
- [ ] 实现 `ProviderCapability`、`ProviderCode`、`SecurityMasterRecord`、`RealtimeQuote`、`DailyBar`、`DailyBarRequest`、`ProbeResult`、`ProviderItemFailure`、泛型 `ProviderBatchResult` 和 Provider Protocol。

```python
class MarketDataProvider(Protocol):
    @property
    def code(self) -> ProviderCode: ...
    @property
    def capabilities(self) -> frozenset[ProviderCapability]: ...
    async def security_master(self, deadline: datetime) -> tuple[SecurityMasterRecord, ...]: ...
    async def realtime_quotes(self, symbols: tuple[str, ...], deadline: datetime) -> ProviderBatchResult[RealtimeQuote]: ...
    async def daily_bars(self, request: DailyBarRequest, deadline: datetime) -> ProviderBatchResult[DailyBar]: ...
    async def probe(self, capability: ProviderCapability, deadline: datetime) -> ProbeResult: ...
```
- [ ] 运行测试并提交 `feat: define provider contracts`。

### Task 2: 受控 HTTP 调用管线

**Files:**
- Create: `backend/src/long_invest/modules/providers/http_client.py`
- Create: `backend/src/long_invest/modules/providers/retry.py`
- Create: `backend/tests/modules/providers/test_http_client.py`
- Create: `backend/tests/modules/providers/test_retry.py`

- [ ] 测试不跟随重定向、TLS 校验、允许的固定主机、内容类型、响应大小和总截止时间。
- [ ] 测试最多三次请求，只重试连接、临时 DNS、读取超时、429/502/503/504。
- [ ] 测试 TLS、4xx 参数错误、Schema 错误、HTML、验证码和超大响应不重试。
- [ ] 使用 `httpx.AsyncClient`、事件钩子和流式读取实现受控客户端；日志不得包含完整 URL 查询、Header 或原始响应。

```python
async def request_json(
    self,
    request: ProviderHttpRequest,
    *,
    deadline: datetime,
) -> dict[str, object]: ...
```
- [ ] 运行测试并提交 `feat: add bounded provider http client`。

### Task 3: 限流、熔断与持久化健康状态

**Files:**
- Create: `backend/src/long_invest/modules/providers/resilience.py`
- Create: `backend/src/long_invest/modules/providers/models.py`
- Create: `backend/src/long_invest/modules/providers/repository.py`
- Create: `backend/tests/modules/providers/test_resilience.py`
- Create: `backend/tests/modules/providers/test_models.py`

- [ ] 测试全局、能力和实时预留额度，Redis 故障退化为本地保守额度。
- [ ] 测试连续三次失败打开，60/180/300 秒冷却，半开单探测，成功恢复，禁用后只能探测恢复。
- [ ] 测试 Provider 与能力隔离，停牌或单股偶发缺失不计全局失败。
- [ ] 实现配置版本、能力设置、健康状态、熔断历史和脱敏失败样本模型。
- [ ] 实现服务在调用方事务内追加历史和发件箱事件，不自行提交。
- [ ] 运行测试并提交 `feat: persist provider resilience state`。

### Task 4: 东方财富与新浪离线契约适配

**Files:**
- Create: `backend/src/long_invest/modules/providers/eastmoney.py`
- Create: `backend/src/long_invest/modules/providers/sina.py`
- Create: `backend/tests/modules/providers/fixtures/eastmoney/*.json`
- Create: `backend/tests/modules/providers/fixtures/sina/*.txt`
- Create: `backend/tests/modules/providers/test_eastmoney.py`
- Create: `backend/tests/modules/providers/test_sina.py`

- [ ] 先建立正常、空数据、部分缺失、缺字段、错误码、HTML、验证码、超大响应、时间异常和多市场代码样本测试。
- [ ] 实现东方财富股票主数据、批量实时、不复权日线和前复权历史标准化。
- [ ] 实现新浪批量实时行情标准化，不声明其不支持的能力。
- [ ] 所有 Schema 改变返回 `PROVIDER_SCHEMA_INCOMPATIBLE`，不猜测字段、不写正式表。
- [ ] 运行离线契约测试并提交 `feat: adapt eastmoney and sina providers`。

### Task 5: Router、诊断和 API

**Files:**
- Create: `backend/src/long_invest/modules/providers/router.py`
- Create: `backend/src/long_invest/modules/providers/service.py`
- Create: `backend/src/long_invest/modules/providers/api.py`
- Create: `backend/tests/modules/providers/test_router.py`
- Create: `backend/tests/modules/providers/test_api.py`

- [ ] 测试实时主源整批失败切新浪，部分缺失只请求缺失项，每条结果记录唯一来源。
- [ ] 测试历史能力无备用源，不能按天拼接。
- [ ] 测试诊断返回标准化字段差异但不写正式行情、不改变优先级。
- [ ] 测试设置接口只允许启停、优先级、并发、速率、超时和自动切换，拒绝 URL、代理、Header、Cookie 和脚本。
- [ ] 实现 Provider V3.1 九个接口，读接口要求 Session，写接口要求 Origin、CSRF、确认、原因、幂等和审计。
- [ ] 运行模块测试和 Ruff，提交 `feat: route and expose market providers`。
