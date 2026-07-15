# 阶段 2 第一批：股票主数据、交易日历与 Provider 设计

## 1. 目标与范围

本批在阶段 1A、1B 已验收的公共底座上，并行建设三个可独立验收的业务基础模块：

1. `securities`：股票主数据、变更历史、证券范围校验和范围快照。
2. `calendar`：交易日历版本、交易日、交易时段、覆盖检查、导入、人工覆盖和版本恢复。
3. `providers`：统一市场数据契约、东方财富和新浪适配、受控外部调用、能力路由、限流、熔断和健康状态。

需求依据为当前唯一生效的 V3.1 实施基线第 6、7、21、25.5、25.6、25.7.1、25.7.2、25.7.10、25.18.1、25.18.4、25.18.6 节。

本批不实现统一调度器、实时行情批次、全市场日线、前复权数据集、数据质量人工复核和正式信号分发。这些能力依赖本批的公开服务，在阶段 2 后续批次建设。

## 2. 总体架构

采用“领域模块独立、主流程统一接线”的结构。三个模块各自拥有模型、服务、仓储、契约和测试，不直接读取或修改其他模块的表。

- `securities` 通过自身定义的 `SecurityMasterSource` 端口接收标准主数据，不识别第三方字段。
- `calendar` 通过 `TradingCalendarService` 向其他模块提供唯一的交易日判断能力，其他模块禁止自行使用 `weekday()` 推断交易日。
- `providers` 对外只暴露标准 DTO 和 `ProviderRouter`，第三方 URL、字段名、请求头、解析规则和错误格式不得越过模块边界。
- 任务、审计和可靠事件继续使用阶段 1 的公开服务，并与业务修改处于同一 PostgreSQL 事务。
- 主流程串行维护 FastAPI 主路由、CLI 主入口、Worker 注册、Compose、Alembic 主链、OpenAPI 文件和前端生成类型。

## 3. 股票主数据模块

### 3.1 数据归属

`security` 保存统一代码、交易所原始代码、名称、市场、证券类型、上市和退市日期、上市状态、ST 状态、停牌状态、东方财富和新浪代码映射、主数据版本及更新时间。

`security_revision` 追加保存名称、类型、市场、上市状态、ST 状态、停牌状态和 Provider 映射的变化。相同内容重复提交不创建新修订。

范围快照保存任务启动时的股票代码、当时状态、筛选条件、数量和主数据版本。执行过程中主数据变化不能修改既有快照。

内部统一代码仅使用 `600000.SH`、`000001.SZ`、`430047.BJ` 形式。SH、SZ、BJ 的 A 股可进入正式监控范围；ETF、可转债、B 股、基金、指数和港美股保留主数据时不得被误判为可监控 A 股。停牌不是 Provider 故障；退市记录保留历史但不能新建正式监控。

### 3.2 公开接口

公开服务提供：

- 按统一代码查询股票。
- 分页搜索股票。
- 校验股票是否属于正式监控范围。
- 将完整标准主数据快照应用到当前版本。
- 冻结符合条件的股票范围快照。
- 创建幂等的主数据刷新任务。

HTTP 接口为：

```text
GET  /api/v1/securities
GET  /api/v1/securities/search
GET  /api/v1/securities/{symbol}
POST /api/v1/securities/refresh
```

刷新操作需要有效 Session、Origin、CSRF、确认和幂等键。刷新接口创建任务，不在 API 请求内等待第三方网络。

### 3.3 事件与错误

主数据版本切换成功后写入 `security_master.updated` 事务发件箱事件。

稳定错误至少包括：股票不存在、代码格式错误、不支持的证券类型、退市股票不可新增监控、主数据快照不完整、幂等键内容冲突和数据库不可用。

## 4. 交易日历模块

### 4.1 数据归属

日历模块拥有不可变日历版本、版本内交易日、每日交易时段、人工覆盖原因和版本切换历史。

交易日状态为 `CONFIRMED`、`PROVISIONAL`、`OVERRIDDEN`、`MISSING`。只有 `CONFIRMED` 和 `OVERRIDDEN` 允许正式自动任务。时间按 `Asia/Shanghai` 解释，数据库时间保存为 UTC。

默认连续交易时段为 09:30～11:30、13:00～15:00，但每个日期保存冻结时段，支持特殊交易安排。

### 4.2 公开接口

`TradingCalendarService` 提供：

- 查询指定日期。
- 查询前一或后一交易日。
- 判断日期是否允许正式自动执行。
- 检查未来确认覆盖天数。
- 完整导入一个不可变日历版本。
- 通过新版本完成单日人工覆盖。
- 恢复历史版本并形成新的当前版本指针。

HTTP 接口为：

```text
GET  /api/v1/trading-calendar
GET  /api/v1/trading-calendar/{date}
GET  /api/v1/trading-calendar/coverage
GET  /api/v1/trading-calendar/next-trading-day
GET  /api/v1/trading-calendar/previous-trading-day
PATCH /api/v1/trading-calendar/{date}
POST /api/v1/trading-calendar/import
GET  /api/v1/trading-calendar/versions
POST /api/v1/trading-calendar/versions/{id}/restore
```

CLI 增加隐藏文件输入或标准输入方式的日历导入命令，不接受无法审计的网页任意脚本。

### 4.3 事务、事件与错误

导入先完整解析并校验所有日期、状态、交易时段、重复项和覆盖范围。任意条目错误时整个版本拒绝，并返回逐项错误；全部通过后才创建版本并原子切换当前指针。

人工覆盖和版本恢复不修改旧记录，只创建新版本或新的当前版本切换事实。高风险修改、审计和事件在同一事务完成。

事件包括 `trading_calendar.updated`、`trading_calendar.coverage_low`、`trading_calendar.missing`。

稳定错误至少包括：日期不存在、版本不存在、乐观锁冲突、日期重复、交易时段重叠、交易时段越界、导入不完整、版本内容非法和数据库不可用。

## 5. Provider 模块

### 5.1 标准契约

能力枚举包括：

```text
SECURITY_MASTER
REALTIME_QUOTE_BATCH
DAILY_BAR_UNADJUSTED
HISTORICAL_DAILY_UNADJUSTED
HISTORICAL_DAILY_QFQ
```

标准 DTO 使用内部股票代码、`Decimal` 和带时区时间。Provider 返回标准股票主数据、批量实时行情、日线历史、逐项失败和能力探测结果。上层只依赖 `ProviderRouter`。

能力路由固定为：

```text
REALTIME_QUOTE_BATCH: EASTMONEY -> SINA
SECURITY_MASTER: EASTMONEY
DAILY_BAR_UNADJUSTED: EASTMONEY
HISTORICAL_DAILY_UNADJUSTED: EASTMONEY
HISTORICAL_DAILY_QFQ: EASTMONEY
```

实时主源整批失败时切换新浪；部分缺失时仅向新浪请求缺失股票。单条标准行情必须完全来自一个 Provider，不允许混合字段。历史数据没有备用源，不按日拼接不同来源。

### 5.2 数据归属与公开服务

Provider 模块拥有配置版本、能力配置、健康状态、熔断历史和最长保留 7 天的脱敏失败样本。

公开服务提供：

- Provider 与能力列表。
- 标准股票主数据获取。
- 批量实时行情获取和缺失项回退。
- 不复权及前复权日线历史获取。
- 固定安全股票的小范围能力探测。
- 不写正式行情的诊断比较。
- Provider 设置变更、半开探测和受控熔断重置。

HTTP 接口为：

```text
GET   /api/v1/providers
GET   /api/v1/providers/{provider_code}
GET   /api/v1/providers/{provider_code}/capabilities
GET   /api/v1/providers/{provider_code}/health
PATCH /api/v1/providers/{provider_code}/settings
GET   /api/v1/providers/circuits
POST  /api/v1/providers/circuits/{circuit_id}/probe
POST  /api/v1/providers/circuits/{circuit_id}/reset
POST  /api/v1/providers/quote-diagnostics
```

### 5.3 外部调用保护

每个进程按上游类型复用 HTTPX Client。请求设置连接、读取、写入、连接池等待和总截止时间；默认不跟随重定向，强制 TLS 校验，并限制响应大小和内容类型。

一次 Provider 操作最多三次 HTTP 请求。只重试连接、临时 DNS、读取超时、429、502、503、504 和上游明确临时错误。参数错误、TLS 失败、Schema 不兼容、响应过大和永久鉴权错误不重试。总截止时间不足时不得启动下一次重试。

限流使用共享令牌桶和并发限制，至少区分全局额度、能力额度和实时预留额度。熔断按 Provider 与能力隔离，状态为 `CLOSED`、`OPEN`、`HALF_OPEN`、`DISABLED`，连续三次失败后打开，冷却时间依次为 60、180、300 秒。

Redis 故障时退化为本进程保守限流和熔断状态；数据库故障时不把内存或 Redis 状态当作正式持久化结果。

允许的目标地址、Header 和解析规则只能来自代码或受控启动配置。HTML、验证码、登录页、错误内容类型、超大响应和 Schema 变化必须拒绝解析并形成稳定错误。

### 5.4 事件与错误

事件包括 `provider.request_succeeded`、`request_failed`、`degraded`、`circuit_opened`、`half_opened`、`recovered`、`auto_switched`、`schema_changed`、`rate_limited`、`config_changed`。

稳定错误至少包括：Provider 超时、限流、熔断、能力不可用、响应过大、内容类型错误、Schema 不兼容、上游临时失败、上游永久失败和所有来源均失败。

## 6. 跨模块数据流

### 6.1 股票主数据刷新

1. API 通过公开任务服务创建幂等刷新任务。
2. Worker 通过 `ProviderRouter` 请求东方财富股票主数据。
3. Provider 层解析并返回标准 DTO。
4. `SecurityMasterService` 完整校验快照。
5. 同一事务更新当前记录、追加真实变化历史、记录审计并写 `security_master.updated` 发件箱事件。
6. 相同快照重复提交返回既有版本，不产生重复历史。

### 6.2 日历导入

1. API 或 CLI 接收结构化年度日历。
2. 日历模块完整解析并返回全部条目错误。
3. 校验通过后创建不可变版本和日期、时段记录。
4. 同一事务切换当前版本、记录审计并写更新事件。
5. 后续业务只通过 `TradingCalendarService` 读取当前正式日历。

### 6.3 Provider 请求

1. 调用方提交能力、标准代码和总截止时间。
2. Router 按冻结配置选择 Provider。
3. 调用经过限流、熔断、超时、响应保护和有限重试。
4. Provider 解析为标准 DTO 并执行契约校验。
5. Router 返回每条实际来源和逐项失败，不直接写股票、行情或日线表。

## 7. 并行施工与集成

三个子任务使用独立分支和工作区，只修改自身后端模块、对应测试和模块内离线样本。

- 股票子任务不得修改 Provider 或日历模块。
- 日历子任务不得修改调度器、股票或 Provider 模块。
- Provider 子任务不得写股票、行情和日历表。

子任务若需要改变任务、审计、HTTP 响应、配置或发件箱公共契约，必须停止并提交偏差分析，由主流程统一处理。

主流程逐个接入三个模块，统一创建 Alembic 迁移，接入路由、CLI 和 Worker，重新生成 OpenAPI 与前端类型。整批通过全量测试和容器验收后才能开始实时行情批次与日线模块。

## 8. 测试与验收

所有模块使用测试先行方式，至少覆盖正常、空数据、重复提交、非法输入、权限、超时、失败隔离、恢复、并发和迁移场景。

股票模块额外覆盖 ETF、可转债、B 股、基金、指数、港美股、退市、停牌、代码映射变化、相同快照重放和范围快照不随主数据漂移。

日历模块额外覆盖未来覆盖不足、当天缺失、日期重复、时段重叠、特殊时段、整个版本原子拒绝、乐观锁冲突、单日覆盖、版本恢复和并发导入。

Provider 模块使用固定离线样本覆盖正常、空数据、部分缺失、缺字段、错误码、HTML、验证码、超大响应、时间异常、多市场代码、临时重试、永久失败、限流、熔断、半开探测、Redis 降级和 Schema 变化。线上探测只用于受控小范围验收，不能替代离线契约测试。

整批验收要求：

- 后端全量测试通过，Ruff 无错误。
- Alembic 单一最新版本，模型与迁移无差异。
- 三个模块没有跨模块内部导入或直接改表。
- 刷新和导入操作具备 Session、Origin、CSRF、确认、幂等和审计保护。
- Redis 故障不丢失 PostgreSQL 正式事实，数据库故障不产生仅存在队列或内存的正式事实。
- Compose 中 API、数据库、Redis、分发器、看门狗和 Worker 保持健康。
