# 行情采集统一接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将质量底座、实时行情和日线模块接入数据库、API、任务执行、容器和生成接口类型。

**Architecture:** 主流程串行维护共享入口和数据库主链。任务先在 PostgreSQL 创建并经事务发件箱投递，Worker 再调用模块公开 Service；实时与日线使用独立队列和独立超时，正式事实始终先落数据库。

**Tech Stack:** Alembic、PostgreSQL、Redis/RQ、FastAPI、Docker Compose、OpenAPI、React TypeScript、pytest、Ruff

---

## 文件结构

- Modify: `backend/src/long_invest/platform/database/base.py` 或现有模型加载入口：登记三个模块模型。
- Create: `backend/alembic/versions/20260715_0008_market_data_collection.py`：质量、实时、日线和年份分区。
- Modify: `backend/src/long_invest/bootstrap/app.py`：接入读取和任务创建路由。
- Modify: `backend/src/long_invest/bootstrap/jobs.py`：新增实时、诊断、日线和重试处理器。
- Modify: `backend/src/long_invest/platform/jobs/contracts.py`：冻结每类任务的软硬超时和执行上下文。
- Modify: `backend/src/long_invest/platform/jobs/models.py`：持久化软硬超时。
- Modify: `backend/src/long_invest/platform/jobs/service.py`：提交时校验超时并支持进度更新。
- Modify: `backend/src/long_invest/platform/jobs/worker.py`：传递任务编号、栅栏令牌和进度上下文并登记明确任务类型。
- Modify: `backend/src/long_invest/platform/outbox/repository.py`：将冻结的任务硬超时交给分发器。
- Modify: `backend/src/long_invest/platform/outbox/dispatcher.py`：按任务使用硬超时，而非全局同一值。
- Modify: `backend/src/long_invest/platform/config/settings.py`：加入受范围约束的批次超时和数据保留设置。
- Modify: `deploy/compose.yaml`：按现有 Worker 模式增加实时、日线队列消费者。
- Modify: `openapi/openapi.json`：重新生成接口基线。
- Modify: `frontend/src/shared/api/generated.ts`：重新生成类型。
- Create: `backend/tests/integration/test_quote_cycle_job.py`
- Create: `backend/tests/integration/test_daily_data_job.py`
- Create: `backend/tests/integration/test_market_data_migration.py`
- Create: `backend/tests/integration/test_market_data_quality_transaction.py`
- Create: `backend/tests/platform/jobs/test_execution_context.py`
- Create: `backend/tests/platform/outbox/test_job_timeout_dispatch.py`

### Task 1: 建立单一迁移主链

- [ ] **Step 1: 写失败迁移测试**

验证升级后所有表、约束、外键、索引和当年及相邻年份分区存在，并能向复合主键日线分区写入：

```python
async def test_daily_bar_routes_to_year_partition(migrated_database) -> None:
    await migrated_database.execute(_insert_daily_bar(date(2026, 7, 15)))
    partition = await migrated_database.scalar(text("SELECT tableoid::regclass::text FROM daily_bar_unadjusted WHERE trade_date = '2026-07-15'"))
    assert partition == "daily_bar_unadjusted_2026"
```

同时在 `backend/tests/integration/test_market_data_quality_transaction.py` 使用真实 PostgreSQL 和真实 `AsyncSession` 验证质量裁决与事务发件箱的原子性：

1. 先创建并提交一个 `OPEN` 质量问题；在新事务中使用故障事件适配器触发发件箱写入失败，确认异常导致事务回滚。再用全新 Session 查询，问题必须仍为 `OPEN`，且该问题没有任何 `event_outbox` 记录。
2. 成功路径在同一事务完成裁决和事件写入；提交后用全新 Session 查询，问题必须为 `RESOLVED`，且恰好存在一条对应的 `data_quality_issue.resolved` 发件箱记录。
3. 使用相同裁决命令重放后再次查询，问题终态不变，发件箱记录仍恰好一条。

该测试是 `0008` 迁移升级的不可跳过验收，必须连接真实 PostgreSQL。禁止因数据库不可达、迁移未执行或表不存在而调用 `skip`；这些情况都必须让验收失败。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/integration/test_market_data_migration.py tests/integration/test_market_data_quality_transaction.py -q`

Expected: FAIL，迁移 `0008` 尚不存在，质量问题与发件箱事务场景无法在真实表上完成。

- [ ] **Step 3: 实现迁移**

迁移 `down_revision = "20260715_0007"`，为 `job` 增加非空的软硬超时字段并创建公共质量、实时和日线表。为当前年份前后各一年创建 `daily_bar_unadjusted_2025/2026/2027` 分区；降级按外键依赖逆序删除。所有唯一和检查约束使用显式稳定名称。

- [ ] **Step 4: 验证升级、降级、再升级**

Run: `cd backend; python -m alembic upgrade head; python -m alembic downgrade 20260715_0007; python -m alembic upgrade head; python -m pytest tests/integration/test_market_data_migration.py tests/integration/test_market_data_quality_transaction.py -q`

Expected: 三次迁移命令成功，两组真实 PostgreSQL 集成测试 PASS，`alembic heads` 只有一个版本。数据库不可用时本步骤必须失败，不得跳过质量事务测试。

- [ ] **Step 5: 提交**

```text
git add backend/alembic backend/src/long_invest/platform/database backend/tests/integration/test_market_data_migration.py backend/tests/integration/test_market_data_quality_transaction.py
git commit -m "feat: migrate market data collection"
```

### Task 2: 接入任务处理器和队列隔离

- [ ] **Step 1: 写失败集成测试**

先在平台任务测试中验证提交时冻结软硬超时、分发器将任务硬超时传给 RQ、Worker 将执行上下文传给处理器。实时测试使用假 Provider 返回 19 个有效和 1 个失败，日线测试返回有效与缺失混合结果；验证任务状态、进度、批次终态、发件箱和正式数据：

```python
async def test_quote_job_finishes_partial_without_old_price_fallback(app_db) -> None:
    result = await quote_cycle_job(_quote_job_config(20), provider=_provider_19_of_20())
    assert result.success is True
    assert result.code == "PARTIAL"
    assert result.data["valid_count"] == 19
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/integration/test_quote_cycle_job.py tests/integration/test_daily_data_job.py -q`

Expected: FAIL，任务处理器尚未登记。

- [ ] **Step 3: 实现任务处理器**

`SubmitJob` 增加 `soft_timeout_seconds` 和 `hard_timeout_seconds`，要求 `0 < soft <= hard <= 3600`。`Job` 持久化这两个冻结值；`ClaimedOutbox` 从关联任务读取 `hard_timeout_seconds` 并传给 RQ；Worker 使用相同冻结值认领运行，禁止分发超时与监督超时漂移。

定义并传给处理器的执行上下文：

```python
@dataclass(frozen=True, slots=True)
class JobExecutionContext:
    job_id: UUID
    fence_token: UUID
    config: Mapping[str, Any]

JobHandler = Callable[[JobExecutionContext], Awaitable[JobResult]]
```

`bootstrap/jobs.py` 增加 `realtime_quote_cycle()`、`quote_diagnostic()`、`daily_data_coordinate()`、`daily_data_item()`、`daily_data_finalize()` 和 `daily_data_retry()`。处理器验证冻结配置，使用数据库事务调用公开服务，外部调用有明确 deadline；Provider 异常转成稳定且可重试的 `JobResult`。

日线父协调任务从股票范围快照恢复待处理股票，创建业务批次和持久化 `job_item`，再为每只股票提交独立的 `DAILY_DATA_ITEM` 子任务。子任务使用独立数据库会话和单股硬超时调用 `ProviderService.daily_bars()`，完成后提交暂存事实并通过任务编号和栅栏令牌更新持久化进度；不能让一个 3600 秒硬超时覆盖全市场。所有项目进入终态后由幂等的 `DAILY_DATA_FINALIZE` 任务统一校验和提交。进程重启时从 `job_item` 与暂存区恢复，跳过已经通过校验的股票。实时处理器先显式请求东方财富，对缺失或领域质量无效的股票再显式请求新浪，随后一次性 finalize。

`worker.py` 显式登记：

```python
HANDLERS.update({
    "SECURITY_MASTER_REFRESH": security_master_refresh,
    "REALTIME_QUOTE_CYCLE": realtime_quote_cycle,
    "QUOTE_DIAGNOSTIC": quote_diagnostic,
    "DAILY_DATA_COORDINATE": daily_data_coordinate,
    "DAILY_DATA_ITEM": daily_data_item,
    "DAILY_DATA_FINALIZE": daily_data_finalize,
    "DAILY_DATA_RETRY": daily_data_retry,
})
```

确保 `SECURITY_MASTER_REFRESH` 同时补入真实 Worker 注册，防止已有刷新任务只能创建不能执行。

- [ ] **Step 4: 配置队列和超时**

设置 `quote_cycle_timeout_seconds` 默认 30、限制 10～60；实时任务软/硬超时为 45/60 秒。日线父协调和最终提交任务只负责有界编排，单股项目使用独立软硬超时，不给全市场父任务设置覆盖全部股票的短硬超时。实时任务进入 `realtime-quotes` 队列，日线进入 `daily-market-data` 队列。`deploy/compose.yaml` 中消费者只监听自己的队列，不让日线占用实时 Worker。

- [ ] **Step 5: 运行集成测试**

Run: `cd backend; python -m pytest tests/platform/jobs tests/platform/outbox tests/integration/test_quote_cycle_job.py tests/integration/test_daily_data_job.py tests/integration/test_jobs_outbox_flow.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```text
git add backend/src/long_invest/bootstrap backend/src/long_invest/platform backend/tests deploy/compose.yaml
git commit -m "feat: execute isolated market data jobs"
```

### Task 3: 接入 API 并发布接口类型

- [ ] **Step 1: 写失败路由注册测试**

验证应用包含设计中的行情和日线路径，且所有接口有统一认证响应：

```python
def test_market_data_routes_are_registered(app) -> None:
    paths = {route.path for route in app.routes}
    assert "/api/v1/quote-cycles" in paths
    assert "/api/v1/daily-data/batches" in paths
    assert "/api/v1/daily-bars/{symbol}" in paths
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; python -m pytest tests/modules/quotes/test_api.py tests/modules/daily_data/test_api.py -q`

Expected: FAIL，主应用尚未包含新路由。

- [ ] **Step 3: 串行接入主应用**

在 `bootstrap/app.py` 只增加模块公开 router；依赖构建器按现有 Application 模式管理数据库事务，不把 Repository 暴露到路由层。

- [ ] **Step 4: 重新生成并校验接口类型**

使用项目现有 OpenAPI 生成命令更新 `openapi/openapi.json` 和 `frontend/src/shared/api/generated.ts`，随后运行：

Run: `cd frontend; npm run typecheck; npm run lint; npm test -- --run`

Expected: 类型检查、代码检查和测试全部 PASS。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/bootstrap/app.py openapi frontend/src/shared/api/generated.ts
git commit -m "chore: publish market data api schema"
```

### Task 4: 全量和容器验收

- [ ] **Step 1: 后端全量验证**

Run: `cd backend; python -m pytest -q; python -m ruff check src tests; python -m alembic heads; python -m alembic check`

Expected: 全部测试通过，Ruff 无错误，单一 head，模型与迁移无差异。

- [ ] **Step 2: 前端全量验证**

Run: `cd frontend; npm test -- --run; npm run lint; npm run typecheck; npm run build`

Expected: 全部命令成功。

- [ ] **Step 3: 容器验收**

Run: `docker compose -f deploy/compose.yaml up -d --build; docker compose -f deploy/compose.yaml ps`

Expected: API、PostgreSQL、Redis、分发器、看门狗及 Worker 全部健康；实时和日线 Worker 分别监听自己的队列。

- [ ] **Step 4: 代表性数据链验收**

通过受控离线 Provider 或本地固定样本执行 19/20 实时任务与部分日线任务，查询数据库确认：实时批次为 `PARTIAL` 且只有 19 个有效条目进入事件；日线批次为 `PARTIAL` 且有效日线已经写入，缺失项可以重试。

- [ ] **Step 5: 最终提交**

```text
git add -A
git commit -m "test: verify market data collection batch"
```
