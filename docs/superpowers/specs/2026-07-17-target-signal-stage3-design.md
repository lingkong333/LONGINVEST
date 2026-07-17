# 阶段 3 手工目标与信号闭环设计

## 1. 目标与范围

本设计承接当前生效规格 `docs/requirements/A股个股长波段价格提醒系统_需求与技术设计_v3.1_项目实施基线.md` 的第 11、12、25.11、25.12 章，并接续已经完成的监控订阅、调度、持仓与行情基础能力。

本批作为一个阶段 3 大模块交付，内部顺序为：

1. 冻结目标与信号公共契约。
2. 建立目标和信号表的一条迁移主链。
3. 完成手工四档目标。
4. 完成信号状态机和通知资格判定。
5. 串行接入行情批次、持仓变化、路由、任务与接口类型。
6. 最后统一执行全量验收和部署。

本批包含手工目标、历史恢复、目标激活重评、五区间信号、滞回、乱序保护、持仓通知资格、受控重置和查询接口。

本批不实现 Python 策略、策略计算、系统候选复核、企业微信或邮件发送。相关策略接口不伪造成功；调用时返回稳定的能力未开放错误。通知部分只接入现有通知公共能力，创建业务通知事实和待投递记录，真实渠道适配仍属于阶段 5。

## 2. 方案选择

采用两个独立业务模块和一条批次迁移：

- `targets` 只负责目标版本、绑定、激活和恢复。
- `signals` 只负责区间判断、当前状态、判断记录和转换事件。

不把它们合并到 `monitoring`。监控模块只拥有订阅和调度配置，目标与信号通过公开 Service 和集成契约读取订阅快照。两个模块同批迁移，避免部署出“目标已经激活但系统不能重评信号”的长期半成品状态。

## 3. 模块边界

### 3.1 `targets` 模块

对应规格：11、25.11、19.3 的目标接口、22.3 的目标验收。

拥有的数据：

- `target_revision`：不可变四档目标及冻结来源。
- `subscription_target_binding`：订阅当前目标指针、状态和版本。
- `target_calculation_run`：后续策略执行记录，由阶段 4 在策略契约冻结后创建。
- `target_review`：后续系统候选复核，由阶段 4 在复核契约冻结后创建。

本批迁移只创建 `target_revision` 和 `subscription_target_binding`。后两张表仍归 `targets` 模块所有，但不提前建立未经阶段 4 验证的空结构。

公开接口：

- 查询目标列表、详情和历史。
- 创建并激活手工目标。
- 恢复历史目标为新的不可变版本。
- 读取指定订阅的当前目标快照。
- 批量读取信号判断需要的目标快照。

公开集成契约只返回不可变数据对象，不暴露 ORM、Repository 或数据库 Session。

内部事件：

- `target.activated`
- `target.restored`
- `signal.reevaluation_requested`

稳定错误码：

- `TARGET_SUBSCRIPTION_NOT_FOUND`
- `TARGET_SUBSCRIPTION_ARCHIVED`
- `TARGET_VALUES_INVALID`
- `TARGET_CONFIRMATION_REQUIRED`
- `TARGET_MODE_SWITCH_CONFIRMATION_REQUIRED`
- `TARGET_VERSION_CONFLICT`
- `TARGET_REVISION_NOT_FOUND`
- `TARGET_RESTORE_STALE`
- `TARGET_IDEMPOTENCY_CONFLICT`
- `TARGET_CAPABILITY_NOT_READY`

### 3.2 `signals` 模块

对应规格：12、25.12、19.4 的信号接口、22.3 的信号验收。

拥有的数据：

- `signal_state`：每个未归档订阅的当前区间及版本快照。
- `signal_evaluation`：每次正式判断、跳过或淘汰记录。
- `signal_event`：真实区间转换的永久事实。

公开接口：

- 按正式行情、目标激活、持仓变化、修正或恢复原因执行判断。
- 查询当前状态、判断记录和信号事件。
- 受控重置为 `UNKNOWN` 并发起立即重评。
- 读取持仓高位复核所需的当前信号快照。

内部事件：

- `signal.evaluation_requested`
- `signal.evaluation_completed`
- `signal.evaluation_skipped`
- `signal.transitioned`
- `signal.notification_requested`
- `signal.notification_suppressed`
- `signal.state_reset`

稳定错误码：

- `SIGNAL_SUBSCRIPTION_NOT_FOUND`
- `SIGNAL_SUBSCRIPTION_DISABLED`
- `SIGNAL_TARGET_UNAVAILABLE`
- `SIGNAL_PRICE_INVALID`
- `SIGNAL_QUOTE_INELIGIBLE`
- `SIGNAL_INPUT_SUPERSEDED`
- `SIGNAL_VERSION_CONFLICT`
- `SIGNAL_RESET_CONFIRMATION_REQUIRED`
- `SIGNAL_IDEMPOTENCY_CONFLICT`

### 3.3 允许的依赖方向

```text
monitoring ──公开订阅快照──> targets
monitoring ──公开订阅快照──> signals
targets ──公开目标快照──> signals
quotes ──合格行情条目事件──> signals
positions ──公开持仓快照/变化事件──> signals
signals ──公开通知发布端口──> notifications
targets/signals ──公共审计和事务发件箱──> platform
```

禁止目标或信号模块引用其他模块的 ORM、Repository、私有 Service，禁止直接更新其他模块的表。跨模块同事务协作由主流程提供事务绑定的公开端口。

## 4. 数据设计

### 4.1 `target_revision`

保存：订阅、四档目标、来源、目标日期、策略版本、参数快照、数据版本、源码哈希、原因、创建人、请求 ID、原恢复版本、内容哈希和创建时间。

四档统一为 `Numeric(20, 2)`，数据库约束和领域校验同时保证：

```text
0 < low_strong < low_watch < high_watch < high_strong
```

内容创建后禁止更新和删除。相同订阅、相同幂等键和相同内容返回原结果；相同幂等键不同内容返回冲突。

### 4.2 `subscription_target_binding`

每个订阅最多一条，保存当前目标版本、绑定版本、目标状态、激活时间、过期原因和更新时间。本批正式状态使用 `READY` 或 `MISSING`；表结构保留 `STALE/CALCULATING/REVIEW_REQUIRED/ACTIVATING/FAILED`，供阶段 4 使用。

### 4.3 `signal_state`

每个订阅最多一条，保存当前区间、状态版本、最近价格和时间、行情条目、目标版本、订阅版本、持仓版本、最近判断和事件。初始记录为 `UNKNOWN`，版本从 1 开始。

### 4.4 `signal_evaluation`

保存输入快照、判断原因、前后状态、结果状态、价格、目标、行情、订阅、持仓版本、是否使用滞回、是否使用过期目标、跳过码、任务和幂等内容哈希。

结果状态明确为：

- `APPLIED`：输入有效，已完成判断。
- `UNCHANGED`：输入有效但区间未变化。
- `SKIPPED`：订阅暂停、目标或行情不可用。
- `SUPERSEDED`：行情、目标或订阅版本已经落后。

### 4.5 `signal_event`

只在真实转换时创建，保存前后状态、价格和行情时间、目标快照、持仓快照、状态版本、判断原因、通知资格和抑制原因。事件不可更新和删除。

## 5. 手工目标流程

请求必须包含四档目标、原因、确认、预期绑定版本和 `Idempotency-Key`。服务在一个事务中：

1. 锁定订阅和目标绑定。
2. 校验订阅未归档。
3. 规范化 Decimal 到两位小数并校验顺序。
4. 计算相对当前目标的变化比例。
5. 变化超过 30% 时要求 `large_change_confirmed=true`。
6. 当前订阅为策略模式时要求 `switch_to_manual_confirmed=true`，通过监控模块公开端口创建 MANUAL 订阅修订。
7. 创建不可变目标版本并原子更新当前指针。
8. 写审计、`target.activated` 和 `signal.reevaluation_requested` 发件箱。
9. 提交后返回目标版本、变化预览、绑定版本和是否切换模式。

目标激活不等待信号重评。重评失败不得回滚已经激活的目标，由任务和告警机制重试。

恢复历史目标必须复制四档内容创建来源 `RESTORED` 的新版本，不允许把当前指针直接指回旧行。恢复默认要求切换为 MANUAL，且必须重新检查当前绑定版本，防止旧页面覆盖新目标。

## 6. 信号算法

基础区间：

```text
price <= LS       STRONG_LOW
LS < price <= LW  LOW
LW < price < HW   NORMAL
HW <= price < HS  HIGH
price >= HS       STRONG_HIGH
```

所有输入使用 Decimal。普通行情使用目标边界的 `max(target * hysteresis_ratio, hysteresis_min)` 作为退出缓冲；进入区间仍使用正式目标线。目标激活重评、状态重置重评不沿用旧目标退出缓冲，直接按当前目标做基础归类。

一次跨越多个区间只产生一个最终转换事件。首次 `UNKNOWN → NORMAL` 只写判断和状态；首次进入其他区间创建事件。

通知分类：

- 到达 `LOW/STRONG_LOW`：低位类，不受持仓限制。
- 低位返回 `NORMAL`：低位解除类，不受持仓限制。
- 到达 `HIGH/STRONG_HIGH`：高位类，仅持仓时有外部通知资格。
- 高位返回 `NORMAL`：高位解除类，仅持仓时有外部通知资格。
- 高低直接跨越：按到达区间分类，只产生一条完整转换事件。

未持仓高位事件仍保存并更新状态，通知资格为受抑制，原因 `NOT_HOLDING`。

## 7. 正式判断事务

每次判断在一个 PostgreSQL 事务中：

1. 锁定或初始化 `signal_state`。
2. 读取并校验当前订阅公开快照。
3. 读取当前目标公开快照。
4. 校验行情条目可正式判断且未过期、未冲突。
5. 比较订阅、目标、行情和状态中的版本及时间顺序。
6. 读取当前持仓公开快照。
7. 写 `signal_evaluation`。
8. 状态变化时写 `signal_event` 并更新 `signal_state`。
9. 有通知资格时通过事务绑定通知发布端口创建通知事实；无资格时记录抑制事件。
10. 写审计或内部发件箱并提交。

暂停订阅、目标缺失、行情缺失、冲突和过期输入只写判断记录，不修改当前状态。迟到旧行情、旧目标和旧订阅任务写 `SUPERSEDED`，Worker 重试必须重新检查全部版本。

## 8. 接口

目标接口：

```text
GET  /api/v1/targets
GET  /api/v1/targets/{subscription_id}
GET  /api/v1/targets/{subscription_id}/history
POST /api/v1/targets/{subscription_id}/manual
POST /api/v1/targets/{subscription_id}/restore
POST /api/v1/targets/{subscription_id}/calculate
POST /api/v1/targets/{subscription_id}/retry
POST /api/v1/targets/calculate-batch
GET  /api/v1/target-calculation-runs
GET  /api/v1/target-reviews
POST /api/v1/target-reviews/{review_id}/approve
POST /api/v1/target-reviews/{review_id}/reject
POST /api/v1/target-reviews/{review_id}/recalculate
```

本批 `manual`、`restore` 和查询接口可用；策略计算和复核操作返回 `TARGET_CAPABILITY_NOT_READY`。

信号接口：

```text
GET  /api/v1/signals/states
GET  /api/v1/signals/states/{subscription_id}
GET  /api/v1/signal-events
GET  /api/v1/signal-events/{event_id}
GET  /api/v1/signal-evaluations
GET  /api/v1/signal-evaluations/{evaluation_id}
POST /api/v1/signals/states/{subscription_id}/reset
POST /api/v1/signals/states/{subscription_id}/reevaluate
```

所有写接口要求登录、可信 Origin、CSRF、确认、原因、预期版本和必填幂等键。所有成功响应使用明确响应模型，查询接口分页并按时间倒序稳定排序。

## 9. 异步接入

- `signal.reevaluation_requested` 创建独立信号任务，不在 API 请求内同步执行。
- 实时行情批次完全结束后，仅为合格条目创建信号判断任务，不能边收行情边判断。
- 每个信号任务冻结条目 ID、订阅版本、目标版本、原因和请求 ID；执行时仍重新检查当前版本。
- 目标激活重评使用 3 分钟内合格实时价；没有时由既有行情能力获取。非交易时段使用最近有效收盘价并标明价格日期。
- 本批新增独立 `signals` 队列和 Worker，不能占用实时报价 Worker。

## 10. 并发与恢复

- 手工目标用绑定行锁、预期版本和幂等内容哈希防止重复或覆盖。
- 信号判断用状态行锁串行化同一订阅，股票之间互不阻塞。
- 同一幂等键相同内容返回原结果，不重复事件和通知；不同内容返回冲突。
- 任务崩溃后由数据库中的 Job 和发件箱恢复；Redis 不是正式状态来源。
- 信号提交失败只回滚该订阅的一次判断，不影响同批其他股票。
- 通知创建失败必须回滚对应信号事务，防止状态变化存在但通知事实丢失。

## 11. 测试与验收

开发过程只执行最小验证：当前契约、领域服务、Repository、API 和必要的真实 PostgreSQL 事务测试。目标和信号全部完成后才执行全量验收。

目标最小验收覆盖：

- 正常创建、空历史、重复提交和不同内容复用幂等键。
- 四档等值、逆序、零、负数、过多小数、NaN 和 Infinity。
- 超 30% 未二次确认、策略转手工未确认。
- 乐观锁冲突、审计失败、发件箱失败和订阅修订失败全部回滚。
- 历史恢复创建新版本，不修改旧版本。

信号最小验收覆盖：

- 五区间全部边界等值。
- `UNKNOWN → NORMAL` 静默及 `UNKNOWN → 非 NORMAL` 事件。
- 四条退出滞回、跨多档直达和目标激活绕过旧滞回。
- 同区间重复判断不重复事件和通知。
- 高位未持仓抑制、低位未持仓允许、持仓版本变化复核。
- 暂停、目标缺失、过期、冲突、旧行情、旧目标和旧订阅版本。
- 两个并发判断只产生一个状态版本和一条转换事件。
- 通知、审计或发件箱失败时整次判断回滚。
- Worker 失败隔离、重试和恢复。

阶段 3 大模块完成后统一执行：后端全量测试、前端测试和构建、迁移升级/降级/重升级、真实 PostgreSQL 并发测试、隔离 Compose 验收、正式部署与公网检查。

## 12. 施工顺序

1. 主流程冻结公共契约、错误码、ORM 和迁移。
2. 串行完成手工目标，因为信号依赖目标快照。
3. 完成信号纯算法和状态事务。
4. 串行接入通知事务端口、行情完成事件、目标重评任务和持仓复核。
5. 串行注册路由、任务、Compose、OpenAPI 和前端类型。
6. 完成阶段 3 全量验收、提交、推送和部署。

本批不存在三个真正互不依赖的业务子模块，不为并行而拆分共享事务或迁移主链。纯算法测试、目标 API 和信号查询 API 可在边界冻结后并行准备，但公共入口仍由主流程串行维护。
