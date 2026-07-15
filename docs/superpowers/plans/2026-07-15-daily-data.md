# 全市场不复权日线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成按冻结股票范围抓取、校验、部分提交、修订和缺失重试的全市场不复权日线链路。

**Architecture:** `daily_data` 模块拥有批次、暂存、正式日线、缺失和修订事实。交易日历与股票模块通过公开服务提供日期判断和范围快照，Provider 只返回标准 DTO；每只股票独立提交，批次最终汇总为成功、部分成功或失败。

**Tech Stack:** Python 3.13、FastAPI、Pydantic、SQLAlchemy 2、PostgreSQL 原生分区、pytest、Ruff

---

## 文件结构

- Create: `backend/src/long_invest/modules/daily_data/contracts.py`
- Create: `backend/src/long_invest/modules/daily_data/models.py`
- Create: `backend/src/long_invest/modules/daily_data/repository.py`
- Create: `backend/src/long_invest/modules/daily_data/quality.py`
- Create: `backend/src/long_invest/modules/daily_data/service.py`
- Create: `backend/src/long_invest/modules/daily_data/outbox.py`
- Create: `backend/src/long_invest/modules/daily_data/api.py`
- Create: `backend/src/long_invest/modules/daily_data/__init__.py`
- Create: `backend/tests/modules/daily_data/test_contracts.py`
- Create: `backend/tests/modules/daily_data/test_models.py`
- Create: `backend/tests/modules/daily_data/test_quality.py`
- Create: `backend/tests/modules/daily_data/test_service.py`
- Create: `backend/tests/modules/daily_data/test_api.py`

### Task 1: 定义日线批次与数据模型

- [ ] **Step 1: 写失败测试**

验证七种批次状态、目标日期必填、范围编号不可为空、暂存股票唯一和正式日线复合唯一：

```python
def test_daily_batch_requires_snapshot() -> None:
    with pytest.raises(ValueError, match="范围"):
        CreateDailyBatch(
            trading_date=date(2026, 7, 15),
            universe_snapshot_id=None,
            idempotency_key="daily:2026-07-15",
        )
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/daily_data/test_contracts.py tests/modules/daily_data/test_models.py -q`

Expected: FAIL，`daily_data` 模块尚不存在。

- [ ] **Step 3: 实现契约和模型**

定义 `DailyBatchStatus`、`DailyStageStatus`、`DailyMissingReason`、`CreateDailyBatch`、`StageDailyBar` 和 `DailyBatchSummary`。模型包括 `DailyDataBatch`、`DailyBarStage`、`DailyBarUnadjusted`、`DailyBarRevision`、`DailyBatchMissingItem`。

关键约束：

```python
UniqueConstraint("trading_date", "universe_snapshot_id", name="uq_daily_batch_scope")
UniqueConstraint("batch_id", "symbol", name="uq_daily_stage_symbol")
PrimaryKeyConstraint("security_id", "trade_date", name="pk_daily_bar_unadjusted")
UniqueConstraint("daily_bar_security_id", "daily_bar_trade_date", "revision_no", name="uq_daily_bar_revision_no")
```

`DailyBarUnadjusted` 表声明 PostgreSQL `partition_by: RANGE (trade_date)`，业务代码不创建分区。

- [ ] **Step 4: 运行测试**

Run: `cd backend; python -m pytest tests/modules/daily_data/test_contracts.py tests/modules/daily_data/test_models.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/daily_data backend/tests/modules/daily_data
git commit -m "feat: define unadjusted daily data models"
```

### Task 2: 实现日线质量判断

- [ ] **Step 1: 写失败测试**

覆盖 OHLC、数量、代码、日期、重复、前收盘异常和新股/ST/公司行为上下文：

```python
def test_bar_rejects_wrong_trading_date() -> None:
    result = validate_daily_bar(
        _bar(trading_date=date(2026, 7, 14)),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
    )
    assert result.code == "DAILY_BAR_DATE_MISMATCH"
    assert result.valid is False
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/daily_data/test_quality.py -q`

Expected: FAIL，质量函数尚未定义。

- [ ] **Step 3: 实现纯函数校验**

`validate_daily_bar()` 对硬错误直接拒绝；相对前收盘异常返回 `review_required=True`。`DailyQualityContext` 明确包含 `is_new_listing/is_st/has_known_corporate_action/previous_close`，上下文只改变异常解释，不修改原始价格。

- [ ] **Step 4: 运行质量测试**

Run: `cd backend; python -m pytest tests/modules/daily_data/test_quality.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/daily_data/quality.py backend/tests/modules/daily_data/test_quality.py
git commit -m "feat: validate unadjusted daily bars"
```

### Task 3: 实现暂存、部分提交和修订

- [ ] **Step 1: 写失败测试**

覆盖全部成功、有效股票部分提交、合理缺失、无法解释缺失、同值重放、数值修订和单股失败隔离：

```python
async def test_partial_batch_commits_valid_bars(session) -> None:
    batch = await _batch(session, symbols=("600000.SH", "000001.SZ"))
    await _stage_valid(session, batch, "600000.SH")
    await _stage_missing(session, batch, "000001.SZ", explained=False)
    result = await _commit(session, batch)
    assert result.status == DailyBatchStatus.PARTIAL
    assert result.committed_count == 1
    assert await _stored_bar(session, "600000.SH") is not None
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/daily_data/test_service.py -q`

Expected: FAIL，服务和仓储尚未完成。

- [ ] **Step 3: 实现仓储**

仓储提供批次幂等认领、范围内暂存 upsert、批次锁、正式日线读取、同值比较、修订号分配、缺失清单和分页查询。提交每只股票使用独立保存点，数据库约束错误只标记该股票失败。

- [ ] **Step 4: 实现领域服务**

`DailyDataService` 暴露：

```python
async def create(self, command: CreateDailyBatch) -> DailyBatchSummary: ...
async def stage(self, batch_id: UUID, item: StageDailyBar) -> None: ...
async def validate(self, batch_id: UUID) -> DailyBatchSummary: ...
async def commit(self, batch_id: UUID) -> DailyBatchSummary: ...
async def retry_scope(self, batch_id: UUID) -> tuple[str, ...]: ...
```

同值重放不产生修订；字段变化时同一保存点内先写 `DailyBarRevision`，再更新正式行并写 `daily_bar.corrected`。没有无法解释的失败或缺失时为 `SUCCEEDED`；至少提交一条有效日线且仍有无法解释项时为 `PARTIAL`；没有有效提交且存在无法解释项时为 `FAILED`。已知停牌、尚未上市、已退市和明确非预期交易属于已解释终态，不单独使批次降级。

批次结束发布 `daily_batch.completed` 或 `daily_batch.partial`；失败批次保存事实但不伪装成完成事件。无法解释缺失同时创建质量问题，事件负载带批次编号、冻结范围版本、有效股票编号和缺失清单，供下一批前复权只选择满足门槛的监控股票。

- [ ] **Step 5: 运行服务测试**

Run: `cd backend; python -m pytest tests/modules/daily_data/test_service.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```text
git add backend/src/long_invest/modules/daily_data backend/tests/modules/daily_data/test_service.py
git commit -m "feat: commit partial daily data batches"
```

### Task 4: 提供模块内 HTTP 接口

- [ ] **Step 1: 写失败 API 测试**

覆盖认证读取、批次分页、缺失查询、重试保护、股票日线和修订分页：

```python
async def test_retry_requires_idempotency_key(client, write_headers) -> None:
    response = await client.post(
        f"/api/v1/daily-data/batches/{uuid4()}/retry",
        json={"confirm": True},
        headers=write_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/daily_data/test_api.py -q`

Expected: FAIL，路由尚未定义。

- [ ] **Step 3: 实现模块路由**

实现设计中的五个接口。重试接口只提交 `DAILY_DATA_RETRY` 任务，并在冻结配置中保存原批次编号和失败股票列表；日线查询要求日期范围且限制最大返回行数。

- [ ] **Step 4: 运行模块测试和代码检查**

Run: `cd backend; python -m pytest tests/modules/daily_data -q; python -m ruff check src/long_invest/modules/daily_data tests/modules/daily_data`

Expected: 全部 PASS，Ruff 无错误。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/daily_data backend/tests/modules/daily_data
git commit -m "feat: expose daily data api"
```
