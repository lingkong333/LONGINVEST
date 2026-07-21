# Stage 4 Strategy and Fixed-Target Holdout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付策略编辑、验证、隔离预测、发布、正式目标计算以及策略发布所需的单股固定目标样本外回测。

**Architecture:** `strategies` 管理用户策略和不可变版本，并通过受控预测端口调用一次性 Docker 沙箱；`backtests` 保存独立预测快照并在可信引擎中使用隐藏测试数据模拟交易；`targets` 复用同一预测端口生成正式候选目标。公共契约、依赖、迁移、路由、任务注册、Compose 和生成类型由主流程串行维护。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、PostgreSQL 16、Redis/RQ、pandas、NumPy、jsonschema、GitPython、Docker SDK for Python、React 19、TypeScript、TanStack Query、React Hook Form、Zod、CodeMirror 6、Docker Compose。

---

## 1. 风险、范围和完成条件

风险等级为 L3：用户 Python、强沙箱、公开契约、策略版本、目标价格、回测收益、数据库迁移和进程编排都属于高风险边界。

本阶段完成：

- 策略草稿、修订、验证、发布、归档和恢复；
- 固定入口 `calculate_targets(history, params, context)`；
- 静态检查、JSON Schema 参数验证和一次性 Docker 沙箱；
- 共享目标预测端口；
- 单股固定目标样本外回测；
- 策略目标计算、大幅变化复核、应用和回滚；
- 策略编辑器、冲突处理、版本差异和单股回测页面。

本阶段不完成企业微信/邮件真实投递、监控列表回测、全市场回测、参数搜索、滚动重算目标、组合回测和策略自定义依赖。

阶段完成条件：模块测试和真实 PostgreSQL 事务测试通过；沙箱证明无网络、数据库、密钥、宿主挂载和测试期数据；单股回测可确定性重放；OpenAPI 和前端类型一致；最后只在服务器隔离 Docker 环境执行一次全量验收。

## 2. 文件和所有权

### 主流程串行维护

- `backend/pyproject.toml`、`backend/uv.lock`：增加并锁定成熟依赖。
- `backend/alembic/versions/20260721_0012_strategy_backtest.py`：阶段 4 唯一迁移主链。
- `backend/src/long_invest/bootstrap/app.py`、`backend/src/long_invest/bootstrap/jobs.py`：路由和任务接入。
- `backend/src/long_invest/entrypoints/job_worker.py`：策略、目标和单股回测任务注册。
- `backend/src/long_invest/platform/config/settings.py`：策略 Git、沙箱镜像和资源限制。
- `deploy/compose.yaml`、`deploy/docker/strategy-runner.Dockerfile`：独立策略 Worker 和一次性 Runner。
- `backend/openapi.json`、`frontend/src/shared/api/generated/schema.d.ts`：生成契约。
- `frontend/src/app/router.tsx`、`frontend/package.json`、`frontend/package-lock.json`：主路由和 CodeMirror 6 依赖。

### `strategies` 拥有

- `strategy`、`strategy_draft`、`strategy_draft_revision`、`strategy_version`、`strategy_validation_run`、`strategy_run`。
- `backend/src/long_invest/modules/strategies/` 下的契约、模型、仓储、服务、应用、API、静态检查、Git 存储和事件。
- 公开策略查询、草稿、验证、测试、发布、归档、版本应用和回滚接口。

### `backtests` 拥有

- `backtest_task`、`backtest_universe_snapshot`、`backtest_item`、`backtest_forecast_snapshot`、`backtest_target_adjustment`、`backtest_order`、`backtest_trade`、`backtest_metric`、`backtest_daily_result`。
- `backend/src/long_invest/modules/backtests/` 下的契约、模型、仓储、可信交易引擎、服务、应用、API 和事件。
- 单股回测创建、查询、摘要、交易和每日结果接口。

### `targets` 扩展但仍独占

- 新增 `target_calculation_run` 和 `target_review`。
- 实现现有占位的计算、重试、批量、复核、通过、拒绝和重新计算接口。
- 只通过公开端口读取策略、订阅、行情和前复权数据，不引用其他模块内部模型或仓储。

## 3. 施工任务

### Task 1：串行冻结公共契约、依赖和迁移

**Files:** 公共依赖、阶段 4 三个模块的 `contracts.py/models.py`、市场数据只读调整契约、Alembic `0012`、对应契约/模型/迁移测试。

- [ ] 定义策略生命周期、预测输入输出、四个回测日期、预测快照、目标调整、订单/交易/指标、目标计算和复核的严格类型。
- [ ] 定义 `StrategyForecastPort`、`TrainingDataPort`、`AdjustmentTimelinePort`、`BacktestSignalRulePort` 和策略就绪查询端口；载荷只使用冻结值对象。
- [ ] 稳定错误码：日期非法、历史不足、训练/测试数据非法、预测超时、目标非法、测试数据泄漏、禁止重预测、调整数据不可用、价格口径不一致、保存失败及策略生命周期冲突。
- [ ] 增加 pandas、NumPy、jsonschema、GitPython 和 Docker SDK；前端增加 CodeMirror 6 React 封装、Python 扩展和差异视图并锁定版本。
- [ ] 建立一条 `20260721_0012` 迁移，包含唯一约束、版本约束、精确价格、不可变快照和稳定索引；验证升级、降级、重升级以及单一 head。
- [ ] 最小验证：`pytest -q tests/modules/strategies/test_contracts.py tests/modules/backtests/test_contracts.py tests/modules/targets/test_contracts.py tests/integration/test_strategy_backtest_migration.py`；`ruff check` 仅覆盖本任务文件。
- [ ] 提交：`feat: freeze stage4 strategy backtest contracts`。

### Task 2：策略生命周期后端（第一批并行 A）

**Files:** `backend/src/long_invest/modules/strategies/{repository,service,application,api,outbox,git_store}.py` 及对应模块测试。

- [ ] 先覆盖创建、空草稿、30 秒自动保存、手工修订、预期版本冲突、恢复、列表分页、归档和不可删除已绑定版本。
- [ ] 用 SQLAlchemy 仓储保存可变草稿和不可变修订；唯一约束与 `draft_version` 防止重复或覆盖。
- [ ] 发布冻结源码、元数据、参数 Schema、环境、源码哈希和验证依据；GitPython 只操作受控策略仓库，不接受网页路径、命令或远程地址。
- [ ] Git 或数据库失败时保持 `PUBLISH_FAILED`，不得产生可绑定的半发布版本；重试复用冻结快照和幂等键。
- [ ] 写操作需要身份、CSRF、确认、原因、预期版本和幂等键；记录审计与事务发件箱。
- [ ] 最小验证：策略模块全部测试和策略事务测试；不运行全仓测试。
- [ ] 提交：`feat: manage strategy lifecycle`。

### Task 3：静态检查和隔离预测执行（第一批并行 B）

**Files:** `backend/src/long_invest/modules/strategies/{static_analysis,forecast,runner_client}.py`、`backend/src/long_invest/entrypoints/strategy_runner.py`、Runner Dockerfile 相关模块测试。

- [ ] 使用 Python AST 检查固定入口、元数据、导入白名单、危险内置和明显文件/网络/进程访问；jsonschema 校验参数 Schema 和参数。
- [ ] 使用 pandas/NumPy 构造只含训练期的 DataFrame；输出必须是四个有限正数、严格递增、量化到 0.01 元，诊断仅允许 JSON 基本类型且不超过 64 KB。
- [ ] Docker SDK 启动一次性非 root Runner：无网络、只读、cap-drop、no-new-privileges、受控 seccomp、CPU 1、内存 512 MB、pids 32、tmpfs 64 MB、10 秒硬超时和受限输出。
- [ ] Runner 输入中不包含测试期字段或数据访问句柄；父 Worker 在超时、OOM、异常退出后继续服务并清理容器。
- [ ] 测试语法错误、禁用导入、参数不合规、非法目标、超时、内存/输出超限、无网络、无密钥、无宿主路径和容器清理。
- [ ] 最小验证：静态检查、预测契约、Runner 客户端和隔离测试；真实 Docker 隔离测试在本模块门槛运行一次。
- [ ] 提交：`feat: execute strategy forecasts in sandbox`。

### Task 4：策略前端工作台（第一批并行 C）

**Files:** `frontend/src/features/strategies/`、`frontend/src/pages/strategy-*.tsx`、对应组件和 MSW 测试；不得修改主路由和生成类型。

- [ ] 使用 CodeMirror 6 提供 Python 高亮、行号、搜索替换、括号和版本差异能力；源码只保存在组件内存和服务端，不写浏览器持久存储。
- [ ] React Hook Form 与 Zod 管理元数据、参数 Schema、确认和原因；TanStack Query 管理服务端状态。
- [ ] 自动保存携带预期版本；409 时停止自动保存并显示本地与服务器差异，允许复制、放弃或合并后重试。
- [ ] 提供草稿历史、恢复、验证、测试、发布、归档和版本页面；所有异步操作覆盖加载、空数据、成功、失败和重复提交。
- [ ] 单股回测表单明确要求四个手工日期并展示训练/测试隔离；结果页展示预测目标、调整历史、交易和指标。
- [ ] 最小验证：策略功能目录测试、ESLint 和 TypeScript 目标检查；不运行全前端构建。
- [ ] 提交：`feat: build strategy workspace`。

### Task 5：单股固定目标样本外回测

**Files:** `backend/src/long_invest/modules/backtests/{repository,engine,service,application,api,outbox}.py` 及模块/事务测试。

- [ ] 创建任务时验证并冻结四个日期、股票、策略或草稿快照、参数、初始资金、规则版本和两个数据哈希。
- [ ] 加载训练数据后调用预测端口一次，原子保存不可变预测快照并进入 `FROZEN`；之后销毁预测执行上下文再加载测试数据。
- [ ] 可信引擎复用信号模块公开纯规则：测试日收盘判断，下一有效开盘成交，同一组目标允许多轮交易，期末不强平。
- [ ] 公司行动按当时可知时间线调整有效目标并保存不可变调整记录；缺少可信因子时该股票失败。
- [ ] 原子保存订单、交易、每日权益和指标；失败不留下部分正式结果，重跑不覆盖旧快照。
- [ ] 覆盖测试数据不进入策略、只调用预测一次、D+1 成交、多轮交易、滞回、停牌顺延、期末持仓、调整连续、确定性重放和生产链隔离。
- [ ] 最小验证：backtests 模块全部测试及真实 PostgreSQL 事务测试。
- [ ] 提交：`feat: run fixed-target holdout backtests`。

### Task 6：策略目标计算、复核、应用和回滚

**Files:** `backend/src/long_invest/modules/targets/{models,contracts,repository,service,application,api,outbox}.py` 及目标模块/事务测试。

- [ ] 计算任务冻结订阅、策略版本、参数、前复权数据版本、训练窗口、原因和当前目标版本；调用共享预测端口。
- [ ] 无旧目标时合法结果直接激活；已有目标任一档变化超过配置阈值时保存候选并进入 `REVIEW_REQUIRED`，旧目标继续服务。
- [ ] 复核通过前重新检查策略、数据、订阅和目标版本；过期候选不能激活。拒绝和重新计算保存不可变历史。
- [ ] 激活在一个事务内更新当前指针、审计和发件箱，提交后触发信号立即重评；单股失败不影响批量其他股票。
- [ ] 发布新策略不自动改变既有订阅；应用和回滚逐只创建订阅修订与目标计算任务。
- [ ] 覆盖重复提交、旧计算晚到、并发复核、计算失败保留旧目标、无旧目标失败、批量失败隔离和信号重评事件。
- [ ] 最小验证：targets、monitoring、signals 相关模块和真实 PostgreSQL 事务测试。
- [ ] 提交：`feat: calculate and review strategy targets`。

### Task 7：主流程串行集成

**Files:** 公共入口、任务注册、配置、Compose、OpenAPI、生成类型和前端主路由。

- [ ] 注册策略、单股回测和目标计算路由；接入策略、目标计算、单股回测队列与 Worker。
- [ ] 配置独立策略 Git 路径、Runner 镜像摘要、Docker 连接、资源限制和静态安全开关；网页不得修改这些启动配置。
- [ ] Compose 增加策略 Worker、单股回测 Worker 和 Runner 镜像构建，不新增公网端口；数据库、Redis 和 Docker 控制接口不得进入 Runner。
- [ ] 导出 OpenAPI、生成前端类型、接入策略和单股回测页面路由；禁止手改生成文件。
- [ ] 验证任务处理器映射、队列隔离、Compose 配置、OpenAPI 一致和前端路由。
- [ ] 提交：`feat: integrate stage4 strategy workflows`。

### Task 8：阶段 4 集成验收和提交

- [ ] 在服务器以合并提交创建独立 Git worktree、独立 Compose 项目和独立数据卷。
- [ ] 后端一次全量：`docker compose -p longinvest-stage4-acceptance -f deploy/compose.yaml --profile test run --rm -e LONGINVEST_STRATEGY_TRANSACTION_TESTS=1 test`。
- [ ] 后端质量：`ruff check .`、只读容器下使用 `/tmp` 字节码目录执行编译、`alembic heads`，并验证空库升级/降级/重升级。
- [ ] 前端一次全量：在一次性 Node 22 容器复制只读源码后执行 `npm ci`、`npm test -- --run`、`npm run lint`、`npm run typecheck`、`npm run build`。
- [ ] 沙箱验收：真实恶意样本不能联网、读取密钥或宿主文件、创建额外进程；超时和 OOM 后 Worker 健康。
- [ ] 关键流程验收：创建草稿、冲突保存、验证、单股样本外回测、发布、应用、目标复核、激活和信号重评。
- [ ] 清理隔离容器、网络、数据卷和测试 worktree；确认正式环境未被修改。
- [ ] 合并主干并推送远程；正式部署另按用户指令执行。

## 4. 依赖与并行规则

Task 1 必须串行先完成。Task 2、3、4 在公共契约冻结后最多三个并行，分别只修改自己的模块或前端功能目录。Task 5 依赖 Task 3；Task 6 依赖 Task 2 和 3；Task 7 必须由主流程串行执行。任何子任务需要修改迁移、依赖、Compose、公共契约、主路由或生成类型时停止该部分，由主流程接管。
