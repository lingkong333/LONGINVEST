# 实时行情批次 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成可恢复、一次性最终确定、支持主备来源和 19/20 部分成功的实时行情批次。

**Architecture:** `quotes` 模块拥有批次和条目，输入仅为冻结范围 DTO 与 Provider 标准 DTO。Worker 负责调用 Provider，领域服务负责质量校验、冲突识别、状态机和事务事件；finalize 前不发布正式判断项目。

**Tech Stack:** Python 3.13、FastAPI、Pydantic、SQLAlchemy 2、PostgreSQL、pytest、Ruff

---

## 文件结构

- Create: `backend/src/long_invest/modules/quotes/contracts.py`
- Create: `backend/src/long_invest/modules/quotes/models.py`
- Create: `backend/src/long_invest/modules/quotes/repository.py`
- Create: `backend/src/long_invest/modules/quotes/quality.py`
- Create: `backend/src/long_invest/modules/quotes/service.py`
- Create: `backend/src/long_invest/modules/quotes/outbox.py`
- Create: `backend/src/long_invest/modules/quotes/api.py`
- Create: `backend/src/long_invest/modules/quotes/__init__.py`
- Create: `backend/tests/modules/quotes/test_contracts.py`
- Create: `backend/tests/modules/quotes/test_models.py`
- Create: `backend/tests/modules/quotes/test_quality.py`
- Create: `backend/tests/modules/quotes/test_service.py`
- Create: `backend/tests/modules/quotes/test_api.py`

### Task 1: 定义批次状态与模型

- [ ] **Step 1: 写失败测试**

验证状态集合、截止时间范围 10～60 秒、预期范围不可为空、同一批次股票唯一以及数量非负：

```python
def test_create_cycle_requires_supported_deadline() -> None:
    with pytest.raises(ValueError, match="10.*60"):
        CreateQuoteCycle(symbols=("600000.SH",), timeout_seconds=9, **_base())
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/quotes/test_contracts.py tests/modules/quotes/test_models.py -q`

Expected: FAIL，`quotes` 模块尚不存在。

- [ ] **Step 3: 实现契约和模型**

`contracts.py` 定义 `QuoteCycleStatus`、`QuoteItemStatus`、`CreateQuoteCycle`、`QuoteSubmission`、`QuoteCycleSummary`。`models.py` 定义 `QuoteCycle` 和 `QuoteCycleItem`，关键约束为：

```python
UniqueConstraint("idempotency_scope", "idempotency_key", name="uq_quote_cycle_idempotency")
UniqueConstraint("cycle_id", "symbol", name="uq_quote_cycle_item_symbol")
CheckConstraint("deadline_at > started_at", name="ck_quote_cycle_deadline")
```

条目保存完整标准行情字段、Provider、质量状态、错误码、冲突证据和 `eligible_for_evaluation`。

- [ ] **Step 4: 运行测试**

Run: `cd backend; python -m pytest tests/modules/quotes/test_contracts.py tests/modules/quotes/test_models.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/quotes backend/tests/modules/quotes
git commit -m "feat: define realtime quote cycles"
```

### Task 2: 实现实时质量校验和冲突判断

- [ ] **Step 1: 写失败测试**

`test_quality.py` 覆盖有效报价、未来时间、超过 3 分钟、价格和 OHLC 非法、负成交量额，以及绝对/相对冲突阈值边界：

```python
def test_quote_conflicts_when_relative_difference_exceeds_threshold() -> None:
    assert compare_quotes(_quote("10.00"), _quote("10.03")).conflict is True

def test_quote_does_not_conflict_at_exact_threshold() -> None:
    assert compare_quotes(_quote("10.00"), _quote("10.02")).conflict is False
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/quotes/test_quality.py -q`

Expected: FAIL，质量函数尚未定义。

- [ ] **Step 3: 实现纯函数校验**

在 `quality.py` 实现：

```python
def validate_quote(quote: RealtimeQuote, *, symbol: str, now: datetime) -> QuoteValidation: ...

def compare_quotes(primary: RealtimeQuote, fallback: RealtimeQuote) -> QuoteComparison:
    difference = abs(primary.price - fallback.price)
    threshold = max(Decimal("0.02"), max(primary.price, fallback.price) * Decimal("0.002"))
    return QuoteComparison(conflict=difference > threshold, difference=difference, threshold=threshold)
```

所有时间比较使用带时区 UTC；正式新鲜度上限为 180 秒。

- [ ] **Step 4: 运行质量测试**

Run: `cd backend; python -m pytest tests/modules/quotes/test_quality.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/quotes/quality.py backend/tests/modules/quotes/test_quality.py
git commit -m "feat: validate realtime quote quality"
```

### Task 3: 实现批次屏障和恢复

- [ ] **Step 1: 写失败测试**

`test_service.py` 覆盖全部成功、19/20、全失败、超时、迟到结果、重复提交和并发 finalize：

```python
async def test_partial_cycle_emits_only_valid_items(session) -> None:
    cycle = await _create_cycle(session, symbols=_twenty_symbols())
    await _submit_valid(session, cycle, symbols=_twenty_symbols()[:19])
    summary = await _finalize_after_deadline(session, cycle)
    assert summary.status == QuoteCycleStatus.PARTIAL
    assert summary.valid_count == 19
    assert summary.missing_count + summary.failed_count == 1
    assert summary.eligible_symbols == _twenty_symbols()[:19]
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/quotes/test_service.py -q`

Expected: FAIL，服务和仓储尚未完成。

- [ ] **Step 3: 实现仓储原子操作**

`repository.py` 提供批次幂等认领、批次带条目读取、条目条件更新和 `SELECT ... FOR UPDATE` finalize。finalize 只接受 `FETCHING/FINALIZING`，终态再次调用返回既有摘要，不重复发事件。

- [ ] **Step 4: 实现领域服务**

`QuoteCycleService` 暴露：

```python
async def create(self, command: CreateQuoteCycle) -> QuoteCycleSummary: ...
async def start(self, cycle_id: UUID, now: datetime) -> QuoteCycleSummary: ...
async def submit(self, cycle_id: UUID, submission: QuoteSubmission, now: datetime) -> None: ...
async def finalize(self, cycle_id: UUID, now: datetime) -> QuoteCycleSummary: ...
async def mark_missed(self, cycle_id: UUID, now: datetime) -> QuoteCycleSummary: ...
async def cancel(self, cycle_id: UUID, now: datetime, reason: str) -> QuoteCycleSummary: ...
async def recover_expired(self, now: datetime, limit: int = 100) -> tuple[UUID, ...]: ...
```

创建成功发布 `quote_cycle.created`。提交冲突报价时通过 `DataQualityService.open()` 在同一 Session 创建问题并发布 `quote_conflict.detected`。finalize 将未终态项目标记 `TIMEOUT`：不存在错误、缺失、冲突和超时时为 `READY`；至少一个 `VALID` 且存在异常项时为 `PARTIAL`；没有 `VALID` 且存在异常项时为 `FAILED`。`NOT_EXPECTED_TO_TRADE` 是合法终态，不单独使批次降级。

finalize 发布一次 `quote_cycle.finalized`，负载只含有效条目编号；存在缺失、超时或失败时再发布一个批次聚合的 `quote_item.missing` 事件，负载包含全部异常股票和错误码，禁止逐股制造重复系统告警。

- [ ] **Step 5: 运行服务测试**

Run: `cd backend; python -m pytest tests/modules/quotes/test_service.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```text
git add backend/src/long_invest/modules/quotes backend/tests/modules/quotes/test_service.py
git commit -m "feat: enforce realtime quote batch barrier"
```

### Task 4: 提供模块内 HTTP 接口

- [ ] **Step 1: 写失败 API 测试**

覆盖未登录读取、缺少 CSRF/Origin/确认/幂等键、手工创建返回 202、分页查询和诊断不写正式批次：

```python
async def test_manual_cycle_requires_confirmation(client, auth_headers) -> None:
    response = await client.post(
        "/api/v1/quote-cycles/manual",
        json={"symbols": ["600000.SH"], "confirm": False},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AUTH_CONFIRMATION_REQUIRED"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/quotes/test_api.py -q`

Expected: FAIL，路由尚未定义。

- [ ] **Step 3: 实现模块路由**

实现设计中的四个接口。写接口通过 `require_verified_write_request`，要求 `Idempotency-Key`，只提交 `REALTIME_QUOTE_CYCLE` 或 `QUOTE_DIAGNOSTIC` 任务并返回任务编号；读取接口只返回批次和标准条目，不返回 Provider 原始响应。

- [ ] **Step 4: 运行模块测试和代码检查**

Run: `cd backend; python -m pytest tests/modules/quotes -q; python -m ruff check src/long_invest/modules/quotes tests/modules/quotes`

Expected: 全部 PASS，Ruff 无错误。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/quotes backend/tests/modules/quotes
git commit -m "feat: expose realtime quote cycle api"
```
