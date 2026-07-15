# Trading Calendar Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成 PostgreSQL 为唯一运行依据的不可变 A 股交易日历模块。

**Architecture:** 每次导入、覆盖和恢复都创建不可变版本并原子切换当前指针；其他模块只通过 `TradingCalendarService` 查询，禁止自行按星期判断。

**Tech Stack:** Python 3.12、SQLAlchemy 2、FastAPI、Pydantic、pytest。

---

### Task 1: 日历契约和完整校验

**Files:**
- Create: `backend/src/long_invest/modules/calendar/contracts.py`
- Create: `backend/tests/modules/calendar/test_contracts.py`

- [ ] 测试四种状态、Asia/Shanghai 日期解释、默认时段、特殊时段和自动执行资格。
- [ ] 测试日期重复、时段重叠、结束早于开始、交易日无时段、非交易日含时段均返回逐项错误。
- [ ] 实现 `CalendarDayInput`、`TradingSessionInput`、`CalendarImport`、`CalendarValidationIssue`、`CalendarVersionResult`、`OverrideCalendarDay`、`RestoreCalendarVersion`、`CalendarCoverage` 和完整版本校验器。

```python
@dataclass(frozen=True, slots=True)
class TradingSessionInput:
    starts_at: time
    ends_at: time

@dataclass(frozen=True, slots=True)
class CalendarDayInput:
    trade_date: date
    is_trading_day: bool
    status: CalendarDayStatus
    sessions: tuple[TradingSessionInput, ...]
    note: str | None = None

@dataclass(frozen=True, slots=True)
class CalendarImport:
    market: str
    source: str
    source_version: str
    expected_current_version: int | None
    days: tuple[CalendarDayInput, ...]
```
- [ ] 运行测试并提交 `feat: define trading calendar contracts`。

### Task 2: 不可变版本模型

**Files:**
- Create: `backend/src/long_invest/modules/calendar/models.py`
- Create: `backend/src/long_invest/modules/calendar/repository.py`
- Create: `backend/tests/modules/calendar/test_models.py`
- Create: `backend/tests/modules/calendar/test_repository.py`

- [ ] 测试版本、当前指针、交易日和交易时段的唯一约束与父子关系。
- [ ] 测试旧版本日期和时段不能被更新或删除。
- [ ] 实现 `TradingCalendarVersion`、`TradingCalendarCurrent`、`TradingCalendarDay`、`TradingSession`。
- [ ] 实现按日期、范围、前后交易日、版本和覆盖范围读取的仓储。
- [ ] 运行测试并提交 `feat: persist immutable trading calendars`。

### Task 3: 导入、覆盖、恢复和覆盖检查

**Files:**
- Create: `backend/src/long_invest/modules/calendar/service.py`
- Create: `backend/tests/modules/calendar/test_service.py`

- [ ] 测试一项错误使整个导入不落库，全部错误一次返回。
- [ ] 测试重复相同导入幂等，不同内容复用键返回 409。
- [ ] 测试未来确认覆盖低于 60 天产生 WARNING、低于 30 天产生 ERROR、当天 MISSING 阻止自动执行。
- [ ] 测试单日覆盖创建新版本，旧版本保持不变；并发旧版本修改返回乐观锁冲突。
- [ ] 测试恢复历史版本形成新切换事实，不修改历史版本。
- [ ] 实现 `TradingCalendarService`，在同一事务写审计和日历事件发件箱。

```python
async def import_version(self, command: CalendarImport) -> CalendarVersionResult: ...
async def override_day(self, command: OverrideCalendarDay) -> CalendarVersionResult: ...
async def restore_version(self, command: RestoreCalendarVersion) -> CalendarVersionResult: ...
async def is_automatic_trading_day(self, trade_date: date) -> bool: ...
async def coverage(self, from_date: date) -> CalendarCoverage: ...
```
- [ ] 运行测试并提交 `feat: manage versioned trading calendars`。

### Task 4: API 与 CLI 子命令

**Files:**
- Create: `backend/src/long_invest/modules/calendar/api.py`
- Create: `backend/src/long_invest/modules/calendar/cli.py`
- Create: `backend/tests/modules/calendar/test_api.py`
- Create: `backend/tests/modules/calendar/test_cli.py`

- [ ] 测试查询接口需要有效 Session，写接口需要 Origin、CSRF、确认、原因和幂等键。
- [ ] 测试导入返回逐项字段错误，版本冲突返回 409。
- [ ] 测试 CLI 从 UTF-8 JSON 文件或标准输入读取，拒绝未知字段、无效编码和任意脚本。
- [ ] 实现 V3.1 的九个日历 HTTP 接口与 `calendar import` 子命令处理函数。
- [ ] 运行模块测试和 Ruff，提交 `feat: expose trading calendar operations`。
