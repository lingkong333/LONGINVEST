# Securities Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成可查询、搜索、校验、版本化刷新并冻结范围的 A 股主数据模块。

**Architecture:** `securities` 独占主数据和修订表，通过公开服务接收标准股票 DTO；不导入 Provider 内部代码。刷新 API 只创建幂等任务，快照应用在调用方事务中完成。

**Tech Stack:** Python 3.12、SQLAlchemy 2、FastAPI、Pydantic、pytest。

---

### Task 1: 标准契约和范围规则

**Files:**
- Create: `backend/src/long_invest/modules/securities/contracts.py`
- Create: `backend/tests/modules/securities/test_contracts.py`

- [ ] 写失败测试覆盖 `600000.SH/000001.SZ/430047.BJ`、非法后缀、ETF、可转债、B 股、指数、停牌和退市。
- [ ] 运行测试确认因契约不存在失败。
- [ ] 实现 `Market`、`SecurityType`、`ListingStatus`、`SecurityMasterItem`、`SecurityMasterSnapshot`、`SecurityEligibility`、`UniverseQuery`、`SnapshotResult`，金额和日期使用强类型。

```python
@dataclass(frozen=True, slots=True)
class SecurityMasterItem:
    symbol: str
    exchange_code: str
    name: str
    market: Market
    security_type: SecurityType
    listing_status: ListingStatus
    listed_on: date | None
    delisted_on: date | None
    is_st: bool
    is_suspended: bool
    provider_codes: Mapping[str, str]

@dataclass(frozen=True, slots=True)
class SecurityMasterSnapshot:
    source: str
    source_version: str
    idempotency_key: str
    items: tuple[SecurityMasterItem, ...]
```
- [ ] 实现统一代码解析与正式监控资格判断；停牌允许存在，退市和非 A 股返回稳定拒绝码。
- [ ] 运行测试并提交 `feat: define securities contracts`。

### Task 2: 数据模型和仓储

**Files:**
- Create: `backend/src/long_invest/modules/securities/models.py`
- Create: `backend/src/long_invest/modules/securities/repository.py`
- Create: `backend/tests/modules/securities/test_models.py`
- Create: `backend/tests/modules/securities/test_repository.py`

- [ ] 先测试 `security` 统一代码唯一、Provider 映射字段、主数据版本和状态约束。
- [ ] 先测试 `security_revision` 只追加并保存变化前后安全摘要。
- [ ] 先测试范围快照与条目冻结股票代码、状态、筛选条件和主数据版本。
- [ ] 实现模型与仓储的分页、搜索、按代码查询、批量加载和快照读取。
- [ ] 运行模块测试并提交 `feat: persist securities master data`。

### Task 3: 主数据版本服务

**Files:**
- Create: `backend/src/long_invest/modules/securities/service.py`
- Create: `backend/tests/modules/securities/test_service.py`

- [ ] 测试首次快照创建股票、相同快照重放不创建修订、字段变化追加一条修订。
- [ ] 测试快照含重复代码、非法映射或不完整数据时整个应用失败。
- [ ] 测试两个并发相同版本只产生一个正式版本，不同内容复用幂等键返回 409。
- [ ] 实现 `SecurityMasterService.apply_snapshot()`、`validate_monitoring_eligibility()` 和 `freeze_universe()`。

```python
async def apply_snapshot(self, snapshot: SecurityMasterSnapshot) -> SnapshotResult: ...
async def validate_monitoring_eligibility(self, symbol: str) -> SecurityEligibility: ...
async def freeze_universe(self, query: UniverseQuery) -> SecurityUniverseSnapshot: ...
```
- [ ] 在同一事务写 `security_master.updated` 发件箱事件，不自行提交事务。
- [ ] 运行测试并提交 `feat: apply versioned security snapshots`。

### Task 4: 查询和刷新 API

**Files:**
- Create: `backend/src/long_invest/modules/securities/api.py`
- Create: `backend/src/long_invest/modules/securities/application.py`
- Create: `backend/tests/modules/securities/test_api.py`

- [ ] 测试列表和搜索需要有效 Session，支持服务端分页且不返回全部大表。
- [ ] 测试详情不存在返回 `SECURITY_NOT_FOUND`。
- [ ] 测试刷新要求 Origin、CSRF、确认和幂等键，并通过 `JobService` 创建刷新任务与发件箱。
- [ ] 实现四个 V3.1 接口，使用标准响应，不导入 `providers`、审计模型或任务内部仓储。
- [ ] 运行模块测试和 Ruff，提交 `feat: expose securities api`。
