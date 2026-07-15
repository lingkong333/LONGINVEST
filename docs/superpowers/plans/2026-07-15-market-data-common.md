# 行情质量公共底座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立实时行情和日线共同使用、但不依赖两者内部模型的数据质量契约与事务服务。

**Architecture:** `market_data` 模块拥有质量问题和裁决状态，通过关联类型、关联编号和结构化证据连接业务对象。写入、状态变更和事务发件箱使用同一数据库 Session；具体行情模块只调用公开 Service，不直接修改质量表。

**Tech Stack:** Python 3.13、FastAPI、Pydantic、SQLAlchemy 2、PostgreSQL、pytest、Ruff

---

## 文件结构

- Modify: `backend/src/long_invest/modules/providers/router.py`：公开按指定来源抓取的受保护调用。
- Modify: `backend/src/long_invest/modules/providers/service.py`：补齐实时和日线采集公开服务。
- Modify: `backend/tests/modules/providers/test_router.py`
- Modify: `backend/tests/modules/providers/test_repository_service.py`
- Modify: `backend/src/long_invest/modules/securities/contracts.py`：增加显式股票范围冻结命令。
- Modify: `backend/src/long_invest/modules/securities/service.py`：按代码校验并冻结不可变范围。
- Modify: `backend/tests/modules/securities/test_service.py`
- Create: `backend/src/long_invest/modules/market_data/contracts.py`：状态、命令和只读结果。
- Create: `backend/src/long_invest/modules/market_data/models.py`：质量问题持久化模型。
- Create: `backend/src/long_invest/modules/market_data/repository.py`：查询、锁定、幂等创建和分页。
- Create: `backend/src/long_invest/modules/market_data/service.py`：创建、合并和状态转换规则。
- Create: `backend/src/long_invest/modules/market_data/integrations.py`：事务发件箱端口与适配器。
- Create: `backend/src/long_invest/modules/market_data/__init__.py`：公开导出。
- Create: `backend/tests/modules/market_data/test_contracts.py`
- Create: `backend/tests/modules/market_data/test_models.py`
- Create: `backend/tests/modules/market_data/test_service.py`

### Task 0: 补齐 Provider 公开采集端口

- [ ] **Step 1: 写失败测试**

验证上层可以通过 `ProviderService` 获取路由实时行情、指定来源实时行情和不复权日线，并且指定来源调用仍经过现有超时、限流和熔断管线：

```python
async def test_service_fetches_quotes_from_requested_provider() -> None:
    service, router = _service_with_router()
    result = await service.realtime_quotes_from(
        ProviderCode.SINA,
        ("600000.SH",),
        deadline(),
    )
    assert result.items[0].source is ProviderCode.SINA
    assert router.requested_provider is ProviderCode.SINA

async def test_service_fetches_unadjusted_daily_bars() -> None:
    service, _ = _service_with_router()
    result = await service.daily_bars(_daily_request(), deadline())
    assert result.items[0].capability is ProviderCapability.DAILY_BAR_UNADJUSTED
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/providers/test_router.py tests/modules/providers/test_repository_service.py -q`

Expected: FAIL，`ProviderService` 尚未公开这些采集方法。

- [ ] **Step 3: 实现来源级公开调用**

在 `ProviderRouter` 将现有受韧性管线保护的指定来源调用公开为：

```python
async def realtime_quotes_from(
    self,
    provider_code: ProviderCode,
    symbols: tuple[str, ...],
    deadline: datetime,
) -> ProviderBatchResult[RealtimeQuote]: ...
```

保留 `diagnostic_quotes()` 作为兼容委托，避免破坏现有诊断接口。`ProviderService` 增加：

```python
async def realtime_quotes(self, symbols, deadline):
    return await self._router.realtime_quotes(symbols, deadline)

async def realtime_quotes_from(self, provider_code, symbols, deadline):
    return await self._router.realtime_quotes_from(provider_code, symbols, deadline)

async def daily_bars(self, request, deadline):
    return await self._router.daily_bars(request, deadline)
```

指定来源不存在、能力关闭或熔断时返回现有稳定 Provider 错误，不允许行情模块访问 `_router` 或 `_providers`。

- [ ] **Step 4: 运行 Provider 全量测试和代码检查**

Run: `cd backend; python -m pytest tests/modules/providers -q; python -m ruff check src/long_invest/modules/providers tests/modules/providers`

Expected: 全部 PASS，Ruff 无错误。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/providers backend/tests/modules/providers
git commit -m "feat: expose provider market data acquisition"
```

### Task 0.5: 补齐显式股票范围冻结端口

- [ ] **Step 1: 写失败测试**

验证显式代码列表会去重排序、拒绝不存在或非 A 股代码，并且主数据后续变化不改变已冻结范围：

```python
async def test_freeze_symbols_is_immutable_after_master_change(session) -> None:
    service = _security_service(session)
    frozen = await service.freeze_symbols(("600000.SH", "000001.SZ"))
    await _rename_security(session, "600000.SH", "新名称")
    loaded = await _repository(session).get_universe_snapshot(frozen.id)
    assert [item.symbol for item in loaded.items] == ["000001.SZ", "600000.SH"]
    assert loaded.items[1].master_version == frozen.master_version
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/securities/test_service.py -q`

Expected: FAIL，`freeze_symbols()` 尚未定义。

- [ ] **Step 3: 实现公开范围命令和服务**

在证券契约中定义 `SymbolUniverseQuery(symbols: tuple[str, ...])`，最多 200 个代码。`SecurityMasterService.freeze_symbols()` 使用 `get_many()` 读取，逐项执行正式监控资格校验，然后复用现有 `SecurityUniverseSnapshot` 和 Item 保存不可变版本。空范围返回 `SECURITY_UNIVERSE_EMPTY`，缺失和不支持项返回带逐项代码的 `SECURITY_UNIVERSE_INVALID`。

- [ ] **Step 4: 运行证券模块测试和代码检查**

Run: `cd backend; python -m pytest tests/modules/securities -q; python -m ruff check src/long_invest/modules/securities tests/modules/securities`

Expected: 全部 PASS，Ruff 无错误。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/securities backend/tests/modules/securities
git commit -m "feat: freeze explicit security universes"
```

### Task 1: 固化质量契约

- [ ] **Step 1: 写失败测试**

在 `backend/tests/modules/market_data/test_contracts.py` 定义测试，验证四种状态、空证据拒绝、关联编号必填和来源选择必须来自已有证据：

```python
def test_quality_issue_command_rejects_empty_evidence() -> None:
    with pytest.raises(ValueError, match="证据"):
        OpenQualityIssue(
            issue_type="QUOTE_CONFLICT",
            subject_type="quote_cycle_item",
            subject_id="item-1",
            symbol="600000.SH",
            severity=QualitySeverity.WARNING,
            evidence={},
            dedupe_key="quote:item-1:conflict",
        )
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/market_data/test_contracts.py -q`

Expected: FAIL，提示 `long_invest.modules.market_data` 尚不存在。

- [ ] **Step 3: 实现最小契约**

在 `contracts.py` 定义：

```python
class QualityIssueStatus(StrEnum):
    OPEN = "OPEN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RESOLVED = "RESOLVED"
    INVALIDATED = "INVALIDATED"

class QualitySeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

@dataclass(frozen=True, slots=True)
class OpenQualityIssue:
    issue_type: str
    subject_type: str
    subject_id: str
    symbol: str | None
    severity: QualitySeverity
    evidence: Mapping[str, object]
    dedupe_key: str
    requires_review: bool = False

@dataclass(frozen=True, slots=True)
class ResolveQualityIssue:
    issue_id: UUID
    action: str
    actor_user_id: str
    reason: str
    selected_source: str | None = None
```

所有字符串执行 `strip()` 后必须非空，证据必须能安全序列化为 JSON。

- [ ] **Step 4: 运行契约测试**

Run: `cd backend; python -m pytest tests/modules/market_data/test_contracts.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/market_data backend/tests/modules/market_data/test_contracts.py
git commit -m "feat: define market data quality contracts"
```

### Task 2: 建立质量问题模型和仓储

- [ ] **Step 1: 写失败测试**

在 `test_models.py` 和 `test_service.py` 验证去重键唯一、状态约束、证据不可为空、同一问题重复创建只更新最近发现时间：

```python
async def test_open_replays_existing_issue(session) -> None:
    service = DataQualityService(session)
    first = await service.open(_command("issue-key"))
    replay = await service.open(_command("issue-key"))
    assert replay.id == first.id
    assert replay.occurrence_count == 2
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/market_data/test_models.py tests/modules/market_data/test_service.py -q`

Expected: FAIL，模型和服务尚未定义。

- [ ] **Step 3: 实现模型和仓储**

`models.py` 的 `DataQualityIssue` 必须包含 `id/issue_type/subject_type/subject_id/symbol/status/severity/evidence/dedupe_key/occurrence_count/first_seen_at/last_seen_at/resolved_at/resolved_by_user_id/resolution_action/resolution_reason/selected_source`，并设置：

```python
__table_args__ = (
    UniqueConstraint("dedupe_key", name="uq_data_quality_issue_dedupe"),
    CheckConstraint("occurrence_count > 0", name="ck_quality_occurrence_positive"),
    CheckConstraint(
        "status IN ('OPEN','REVIEW_REQUIRED','RESOLVED','INVALIDATED')",
        name="ck_quality_status_valid",
    ),
)
```

`repository.py` 提供 `find_by_dedupe_key()`、`get_for_update()`、`add()`、`flush()` 和带状态、类型、股票筛选的 `list()/count()`。

- [ ] **Step 4: 实现服务状态转换**

`DataQualityService.open()` 使用嵌套事务处理并发唯一冲突；重复未解决问题递增次数并合并最新证据。`resolve()` 只允许：

```text
OPEN/REVIEW_REQUIRED -> RESOLVED
OPEN/REVIEW_REQUIRED -> INVALIDATED
```

选择来源时校验 `selected_source` 存在于 `evidence["sources"]`；终态重复提交相同裁决返回原结果，不同裁决返回 `QUALITY_ISSUE_STATE_CONFLICT`。

- [ ] **Step 5: 运行模型和服务测试**

Run: `cd backend; python -m pytest tests/modules/market_data -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```text
git add backend/src/long_invest/modules/market_data backend/tests/modules/market_data
git commit -m "feat: persist market data quality issues"
```

### Task 3: 接入事务发件箱端口

- [ ] **Step 1: 写失败测试**

验证解决问题时事件和状态在同一 Session，发件箱失败会回滚状态：

```python
async def test_resolution_rolls_back_when_event_append_fails(session) -> None:
    issue = await _open_issue(session)
    service = DataQualityService(session, events=FailingQualityEventPort(session))
    with pytest.raises(RuntimeError):
        await service.resolve(_resolution(issue.id))
    await session.rollback()
    assert (await session.get(DataQualityIssue, issue.id)).status == "OPEN"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/market_data/test_service.py -q`

Expected: FAIL，事件端口尚未参与状态变更。

- [ ] **Step 3: 实现事件端口和适配器**

在 `integrations.py` 定义绑定 Session 的 `QualityEventPort`，用 `TransactionalOutboxWriter.append()` 发布：

```python
await writer.append(
    session=session,
    topic="data_quality_issue.resolved",
    aggregate_type="data_quality_issue",
    aggregate_id=str(issue.id),
    queue="domain-events",
    payload={"event_type": event_type, **payload},
    dedupe_key=f"quality:{issue.id}:{issue.status}",
)
```

服务构造时若事件端口来自不同 Session，返回 `QUALITY_TRANSACTION_MISMATCH`。

- [ ] **Step 4: 运行模块测试和代码检查**

Run: `cd backend; python -m pytest tests/modules/market_data -q; python -m ruff check src/long_invest/modules/market_data tests/modules/market_data`

Expected: 全部 PASS，Ruff 无错误。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/market_data backend/tests/modules/market_data
git commit -m "feat: publish market data quality events"
```
