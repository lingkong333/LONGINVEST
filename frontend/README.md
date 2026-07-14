# LongInvest 前端基础工程

本目录对应 V3.1 的第 17 章、第 21 章阶段 1B、第 22.7 节和第 25.16 节。

## 当前边界

- `src/app` 只负责路由、全局 Provider 和错误边界。
- `src/shared` 只负责通用 UI、请求、Query、表单、错误诊断与基础工具，不得引用 `features` 或 `pages`。
- `src/features` 由后续业务模块拥有，各模块只能通过自己的公开入口被页面组合。
- `src/pages` 只组合功能，不实现业务规则。
- 本阶段不拥有业务数据、不声明业务事件、不接入真实 API，也不实现业务页面。

## 公共接口

- UI：`Button`、`Input`、`FormField`、`Dialog`、`DataTable`、`PageState`。
- 应用：`AppErrorBoundary`、`RouteErrorPage`、`AppProviders` 和 Data Mode 路由。
- Query：`shared/query` 中的统一 Query Client；查询只对网络、超时和 5xx 最多重试一次。
- 表单：`useZodForm` 和受控 `FormField`，字段错误统一关联到输入控件。
- 请求：`createApiClient`、`ApiError`。调用方使用 `api.request(api.client.GET(...))`，不得直接消费 openapi-fetch 的原始 `data/error` 结果。

同一认证代际的 401 只触发一次退出流程。成功建立新会话后，认证层必须调用 `api.resetUnauthorized()`，再开始新会话的数据请求。

后端提供 OpenAPI 文件后，在 `frontend` 目录运行 `npm run generate:api`，再由主流程提交生成类型。页面和业务组件不得直接调用 `fetch`。

## 验证

```text
npm test
npm run lint
npm run typecheck
npm run build
```

Playwright 已配置桌面和手机项目，但本阶段不下载浏览器，也不编写业务端到端流程。
