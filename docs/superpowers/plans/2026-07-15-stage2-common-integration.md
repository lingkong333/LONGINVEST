# Stage 2 Batch 1 Common Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立三个阶段 2 模块共享的认证请求入口和 HTTPX 运行依赖，并在子模块完成后串行接入路由、CLI、迁移、Worker 与 OpenAPI。

**Architecture:** 认证模块公开读写请求依赖，领域 API 不再导入 `auth.api` 私有函数。主流程独占依赖锁、FastAPI 主路由、CLI 主入口、Alembic 主链和生成类型，三个子模块只消费这些稳定入口。

**Tech Stack:** FastAPI、SQLAlchemy 2、Alembic、HTTPX、pytest、Ruff、Docker Compose。

---

### Task 1: 公开认证请求依赖

**Files:**
- Create: `backend/src/long_invest/modules/auth/dependencies.py`
- Modify: `backend/src/long_invest/modules/auth/api.py`
- Test: `backend/tests/modules/auth/test_auth_dependencies.py`

- [ ] **Step 1: 写失败测试**

测试 `require_authenticated_request` 在无 Cookie 时返回 `AUTH_SESSION_INVALID`，`require_verified_write_request` 依次校验 Origin、Cookie、CSRF，并把用户和 Session 写入请求上下文。使用假的 `AuthApplication` 断言只调用公开 `authenticate` 或 `validate_csrf`。

- [ ] **Step 2: 确认测试因模块不存在而失败**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/auth/test_auth_dependencies.py -q`
Expected: FAIL，提示 `long_invest.modules.auth.dependencies` 不存在。

- [ ] **Step 3: 实现最小公开契约**

```python
@dataclass(frozen=True, slots=True)
class AuthenticatedRequest:
    user: AppUser
    session: UserSession
    audit_context: AuditContext

async def require_authenticated_request(...) -> AuthenticatedRequest: ...
async def require_verified_write_request(...) -> AuthenticatedRequest: ...
```

公共模块负责可信客户端 IP、审计上下文、Cookie、Origin 和 CSRF 校验。`auth.api` 改为复用这些函数，行为和错误码保持不变。

- [ ] **Step 4: 验证认证测试**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/auth -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```text
git add backend/src/long_invest/modules/auth backend/tests/modules/auth
git commit -m "refactor: publish authenticated request dependencies"
```

### Task 2: 增加 Provider 运行时 HTTPX

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`

- [ ] **Step 1: 将 `httpx>=0.28,<1` 从仅开发依赖提升为运行依赖**
- [ ] **Step 2: 在 `backend` 目录执行 `uv lock` 更新锁文件**
- [ ] **Step 3: 执行 `uv sync --frozen --extra dev`，确认锁文件可复现**
- [ ] **Step 4: 运行 `python -c "import httpx; print(httpx.__version__)"`**
- [ ] **Step 5: 提交 `feat: add provider http runtime dependency`**

### Task 3: 串行接入三个模块

**Files:**
- Modify: `backend/src/long_invest/bootstrap/app.py`
- Modify: `backend/src/long_invest/entrypoints/cli.py`
- Modify: `backend/alembic/env.py`
- Create: `backend/alembic/versions/20260715_0007_stage2_batch1.py`
- Modify: `backend/openapi.json`
- Modify: `frontend/src/shared/api/generated/schema.d.ts`
- Test: `backend/tests/integration/test_stage2_batch1.py`

- [ ] **Step 1: 逐个合并 securities、calendar、providers 已通过复核的提交**
- [ ] **Step 2: 在 Alembic 环境显式导入三个模块模型并生成单一 `0007` 迁移**
- [ ] **Step 3: 审查迁移，确保只创建本批拥有的表、约束、索引和应用角色权限**
- [ ] **Step 4: 接入三个路由和 `calendar import` CLI，不让模块互相导入内部文件**
- [ ] **Step 5: 注册股票主数据刷新 Worker：ProviderRouter 获取标准 DTO，SecurityMasterService 原子应用快照**
- [ ] **Step 6: 写集成测试，覆盖刷新任务、日历版本切换、Provider 探测、事务回滚和发件箱事实**
- [ ] **Step 7: 运行 `alembic upgrade head`、`alembic check` 和 `alembic heads`**
- [ ] **Step 8: 导出 OpenAPI 并执行 `npm run generate:api`**
- [ ] **Step 9: 运行后端全量 pytest/Ruff 与前端 test/lint/typecheck/build**
- [ ] **Step 10: 提交 `feat: integrate stage 2 market foundations`**

### Task 4: 服务器验收

- [ ] **Step 1: 推送服务器并重建 Compose**
- [ ] **Step 2: 在 test 容器运行后端全量测试和 Ruff**
- [ ] **Step 3: 验证迁移单 head 且无待生成操作**
- [ ] **Step 4: 使用固定小范围股票执行东方财富与新浪受控探测；失败时区分网络环境与契约错误**
- [ ] **Step 5: 验证 Redis 停止时 Provider 保守降级，PostgreSQL 失败时不产生正式任务事实**
- [ ] **Step 6: 检查全部长期容器、就绪接口、最近日志和资源占用**

