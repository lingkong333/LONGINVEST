# 阶段 2 行情采集第一批设计

## 1. 目标与需求依据

本批在股票主数据、交易日历和 Provider 公共能力已经验收的基础上，建设行情采集公共底座，并完成两条可以独立运行的数据链：

1. 实时行情批次屏障。
2. 全市场不复权日线采集。

需求依据为当前唯一生效的 V3.1 实施基线第 7、8、21、22.2、25.7、25.18 和 25.20.7 节。其中第 25.7 节的状态、事务、异常分支和 API 是本批的直接验收基线。

本批不实现正式信号判断、监控分组和调度定义、策略目标计算、监控股前复权数据集、全市场回测和网页行情管理页面。监控股前复权在下一批实现，直接使用 Provider 返回的完整前复权历史；MVP 不增加独立复权因子系统。

## 2. 方案选择

考虑过三种施工方式：

1. 实时、日线、前复权三线同时施工。并行度最高，但前复权依赖当日日线和质量门槛，会迫使两个子任务同时修改同一条数据链，不采用。
2. 先完成整个行情大模块再一次接线。边界容易统一，但反馈周期长，实时和日线无法独立验收，不采用。
3. 先串行建设公共契约和质量底座，再并行建设实时与日线，前复权进入下一依赖批次。依赖清晰，失败可隔离，采用此方案。

## 3. 模块和所有权

新增三个领域目录：

- `market_data`：只拥有跨行情类型复用的质量问题、标准质量状态、质量裁决和公开事件契约。
- `quotes`：拥有实时批次、实时批次条目、批次冻结范围、报价冲突证据和批次终态。
- `daily_data`：拥有日线批次、暂存条目、正式不复权日线、缺失清单和日线修订。

模块之间只通过公开 Service、只读 DTO、事务发件箱事件和集成端口协作。禁止导入其他模块的 SQLAlchemy 模型、Repository 或直接修改其他模块的表。

共享入口、主路由、任务注册、Alembic 主链、OpenAPI 和前端生成类型由主流程串行接入。行情公共契约先由主流程落定，两个子模块不得自行改变公共契约。

## 4. 行情公共底座

### 4.1 数据归属

`data_quality_issue` 保存问题类型、关联对象类型与编号、股票、状态、严重程度、结构化证据、首次及最近发现时间、处理方式、处理人和说明。状态固定为：

```text
OPEN
REVIEW_REQUIRED
RESOLVED
INVALIDATED
```

证据可以引用已经持久化的标准报价或日线版本，但不能保存任意网页输入的正式价格。失败原始响应继续由 Provider 模块拥有并最多保留 7 天，质量模块只保存脱敏样本引用。

### 4.2 公开接口

公共服务只提供创建或合并质量问题、查询问题、选择已有报价来源、请求重抓、解决或判定无效。`select-source` 只能选择问题证据中已经存在的来源结果。

稳定错误至少包括：

```text
QUALITY_ISSUE_NOT_FOUND
QUALITY_ISSUE_STATE_CONFLICT
QUALITY_SOURCE_NOT_AVAILABLE
QUALITY_ACTION_NOT_ALLOWED
QUALITY_EVIDENCE_INVALID
```

### 4.3 事务与事件

质量问题必须与引发它的批次事实处于同一数据库事务。状态变化通过事务发件箱发布，Redis 故障不影响 PostgreSQL 正式事实提交。

本批使用的事件包括：

```text
quote_conflict.detected
quote_item.missing
daily_batch.partial
daily_batch.completed
daily_bar.corrected
data_quality_issue.resolved
```

## 5. 实时行情批次模块

### 5.1 对应规格和数据

对应 V3.1 第 8.2、8.3、25.7.2～25.7.5、25.7.10 节。

`quote_cycle` 保存计划时间、开始和截止时间、最终确定时间、订阅快照版本、预期/有效/缺失/冲突/失败数量及状态。状态固定为：

```text
PENDING
FETCHING
FINALIZING
READY
PARTIAL
FAILED
MISSED
CANCELED
```

`quote_cycle_item` 对冻结范围中的每只股票最多一条，保存标准行情字段、行情时间、接收时间、实际 Provider、质量状态、稳定错误码、冲突证据和是否允许正式判断。条目状态固定为：

```text
VALID
MISSING
STALE
CONFLICT
INVALID
TIMEOUT
PROVIDER_FAILED
NOT_EXPECTED_TO_TRADE
```

范围快照通过 `SecurityScopeService` 的公开接口取得并冻结。当前监控订阅模块尚未建设时，正式自动批次入口保持关闭；手工和测试可显式传入合法 A 股范围并创建同样不可变的快照。后续订阅模块只需提供范围端口，不改变批次核心。

### 5.2 公开服务和接口

公开 Service 提供：创建批次、开始抓取、提交来源结果、一次性最终确定、按编号查询批次和条目、创建诊断任务。

HTTP 接口为：

```text
GET  /api/v1/quote-cycles
GET  /api/v1/quote-cycles/{id}/items
POST /api/v1/quote-cycles/manual
POST /api/v1/quotes/diagnose
```

写接口具备 Session、Origin、CSRF、确认、幂等键和审计。API 只创建任务，不在请求中等待外部网络。

### 5.3 批次屏障和质量规则

执行顺序固定为：

1. 在数据库中创建批次并冻结全部预期股票。
2. 通过 Provider Router 批量请求东方财富。
3. 只对失败或缺失股票请求新浪。
4. 将每只股票写为终态；达到整体截止时将剩余股票写为 `TIMEOUT`。
5. 在数据库事务中锁定批次、计算聚合数量并且只 finalize 一次。
6. 写入 `quote_cycle.finalized` 发件箱事件，事件负载只包含 `VALID` 项目编号。

禁止在 finalize 前逐股发布正式判断事件。迟到响应只能保存诊断引用，不能重新打开批次或补造正式事件。

正式行情至少校验代码匹配、当前价大于零、时间存在且不过度未来、新鲜度不超过 3 分钟、OHLC 关系合理、成交量额非负、字段类型和大小符合契约。

主备来源均有效时比较当前价；超过 `max(0.02 元, 0.2%)` 时保存两个完整标准 DTO，条目标记 `CONFLICT` 并创建待复核质量问题。未超过时按固定优先级选择一个完整来源，禁止拼接字段。

### 5.4 幂等、并发与恢复

- 相同计划发生编号或相同手工幂等键只能创建一个批次。
- 同一股票重复提交相同结果不产生重复条目或事件；不同结果在批次完成前按明确版本规则处理，完成后拒绝改变正式事实。
- finalize 使用数据库行锁和状态条件更新，两个 Worker 竞争时只有一个成功发布事件。
- Worker 重启后从数据库中继续未终态批次；截止已过则立即将剩余项标记超时并 finalize。
- 数据库不可用时不允许只在 Worker 内存或 Redis 中形成批次。

稳定错误至少包括：

```text
QUOTE_CYCLE_NOT_FOUND
QUOTE_CYCLE_STATE_CONFLICT
QUOTE_CYCLE_DEADLINE_EXCEEDED
QUOTE_CYCLE_ALREADY_FINALIZED
QUOTE_SCOPE_EMPTY
QUOTE_ITEM_NOT_IN_SCOPE
QUOTE_ITEM_INVALID
QUOTE_ALL_PROVIDERS_FAILED
```

## 6. 全市场不复权日线模块

### 6.1 对应规格和数据

对应 V3.1 第 8.4、8.6、25.7.2、25.7.6、25.7.7、25.7.9、25.7.10 节。

`daily_data_batch` 保存目标交易日、范围快照、开始/截止/完成时间、抓取与校验计数、有效/缺失/失败计数和状态。状态固定为：

```text
PENDING
FETCHING
VALIDATING
COMMITTING
SUCCEEDED
PARTIAL
FAILED
```

日线暂存条目保存 Provider 标准结果、校验状态、缺失原因和失败码，最多保留 7 天。`daily_bar_unadjusted` 保存永久原始事实，按交易年份原生分区，唯一键为 `(security_id, trade_date)`。`daily_bar_revision` 追加保存旧值、新值、字段差异、来源和修订原因。

缺失清单必须区分已知停牌、尚未上市、已经退市、非预期交易和无法解释缺失。只有证券主数据或交易日历公开服务能为前三类提供依据，不能因为 Provider 没返回就自行推断。

### 6.2 公开服务和接口

公开 Service 提供：创建交易日批次、暂存标准日线、校验批次、提交全部有效行、查询缺失、重试失败范围、查询股票日线和修订。

HTTP 接口为：

```text
GET  /api/v1/daily-data/batches
GET  /api/v1/daily-data/batches/{id}/missing
POST /api/v1/daily-data/batches/{id}/retry
GET  /api/v1/daily-bars/{symbol}
GET  /api/v1/daily-bars/{symbol}/revisions
```

17:00 自动入口只允许 `TradingCalendarService` 返回正式交易日时创建。手工重试需要 Session、Origin、CSRF、确认、幂等键和审计，并且只重试原批次冻结范围内的失败股票。

### 6.3 校验、提交和修订

每条正式日线必须满足价格大于零、最高最低关系合理、成交量额非负、日期和代码匹配、同日无重复。相对前收盘异常跳变、数量级异常、字段缺失和 Schema 改变形成质量问题；新股、ST 和已知公司行为只作为上下文，不能被普通阈值直接误杀。

批次流程固定为冻结全市场 A 股范围、Provider 抓取、暂存、逐项校验、解释合理缺失、提交所有有效行、保存无法解释缺失并一次性确定终态。任何一只失败不能回滚其他股票已经验证的正式日线。

同股同日首次提交使用幂等插入。完全相同的重放不产生修订；正式字段发生变化时，必须在同一事务中先追加 `daily_bar_revision`，再更新正式日线版本并写 `daily_bar.corrected` 事件。

### 6.4 幂等、分区和恢复

- 同一目标交易日和同一范围版本的自动批次只能有一个有效执行。
- 重试批次保留原批次关联和独立幂等键，不扩大冻结范围。
- PostgreSQL 分区按交易年份由迁移预先创建，运行时业务代码不执行 DDL。
- Worker 重启从数据库状态和暂存记录恢复，不重复抓取已经通过校验的股票。
- 当晚有限重试后停止自动循环；第二个交易日先创建历史缺失补偿任务，但不阻止当天任务。
- 磁盘达到 95% 时暂停历史回填和批量历史任务；当天日线批次按运维保护策略告警，不静默丢弃。

稳定错误至少包括：

```text
DAILY_BATCH_NOT_FOUND
DAILY_BATCH_STATE_CONFLICT
DAILY_BATCH_ALREADY_COMMITTED
DAILY_BATCH_DATE_NOT_TRADING
DAILY_SCOPE_EMPTY
DAILY_BAR_INVALID
DAILY_BAR_DATE_MISMATCH
DAILY_BAR_SYMBOL_MISMATCH
DAILY_MISSING_UNEXPLAINED
DAILY_PARTITION_UNAVAILABLE
```

## 7. 跨模块数据流

### 7.1 实时行情

任务执行器调用行情公开 Service 冻结范围，Provider Router 返回主源结果及备用源结果，行情模块负责质量判断和持久化。最终确定后通过事务发件箱发布 `quote_cycle.finalized`。本批没有信号模块，因此事件可以被可靠保存和分发，但没有正式信号消费者。

### 7.2 当日日线

任务执行器先通过交易日历公开 Service 验证目标日，再通过股票公开 Service 冻结全市场范围，Provider Router 返回不复权日线。日线模块完成暂存、质量判断、逐股提交和批次终态。下一批前复权模块订阅 `daily_batch.completed/partial`，只处理当日日线成功的监控股票。

### 7.3 失败隔离

- Provider 模块崩溃只使当前外部调用失败，不终止 API 或其他 Worker。
- 实时队列和日线队列分离；日线大批量任务不能占用实时预留能力。
- 单只股票失败只改变自己的条目和批次聚合结果。
- Redis 故障时数据库事实和发件箱仍可提交，恢复后继续分发。
- 数据库故障时不允许产生仅存在队列、缓存或进程内存的正式结果。

## 8. 施工批次

### 8.1 主流程串行底座

1. 固化公共质量契约、错误响应和事件负载。
2. 准备模块公开端口和测试夹具，不建立跨模块内部依赖。
3. 定义主流程接入点，但暂不让子模块修改路由、迁移主链和生成类型。

### 8.2 第一并行批

- 子任务 A 只修改 `quotes` 及其测试。
- 子任务 B 只修改 `daily_data` 及其测试。

两个子任务分别使用独立分支和工作区。若需要改变 Provider、股票、日历、任务、审计、质量公共契约或事务发件箱，停止施工并提交偏差分析。

### 8.3 主流程集成

主流程逐个接入模块，统一创建迁移、路由和任务处理器，更新 OpenAPI 和前端类型。整批通过后才进入监控股前复权、质量人工处理完整接口和历史缺失补采增强。

## 9. 测试与完成标准

公共底座至少覆盖质量问题去重、非法裁决、状态竞争、事务回滚和 Redis 故障。

实时模块至少覆盖：正常全部成功、空范围、19/20 部分成功、主源缺失备用成功、双源冲突、过时行情、非法 OHLC、整体超时、迟到响应、重复创建、重复提交、并发 finalize、Worker 恢复、Provider 故障和数据库故障。

日线模块至少覆盖：全量成功、空数据、部分缺失、合理停牌、无法解释缺失、重复执行、同值重放、数值修订、非法 OHLC、日期或代码不匹配、暂存恢复、单股失败隔离、重试不扩大范围、非交易日拒绝、分区缺失和迁移升级/回退。

整批完成必须同时满足：

- 后端全量测试通过，Ruff 无错误。
- Alembic 只有一个最新版本，模型与迁移一致，年份分区可实际写入。
- 两个模块没有跨模块内部导入或直接改表。
- 写接口具备认证、CSRF、确认、幂等和审计保护。
- 19/20 实时行情场景中 19 只可进入后续事件，失败股票不使用旧价。
- 日线部分失败时有效股票仍永久提交，失败股票能够按冻结范围重试。
- Redis、Provider 和单个 Worker 故障不会破坏已经提交的数据库事实。
- Compose 全部服务健康，实时与日线代表性任务可在容器中完成并从数据库查询到结果。
