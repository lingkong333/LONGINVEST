# 发布 V4 简化架构基线与迁移清单

## Description

基于 `tasks/prd-longinvest-simplified-architecture.md` 发布新的生效规格，明确旧任务体系、Redis/RQ、多后台角色和现有数据的迁移边界。施工前必须先完成该事项。

## Acceptance Criteria

- [x] 新规格完整继承仍然有效的产品行为，并明确替代 V3.15 的架构章节。
- [x] `docs/requirements/README.md` 只指向新的唯一生效规格。
- [x] 决策记录比较现有方案与简化方案的收益、风险和回退方式。
- [x] 清单列出保留的数据表、迁移的数据、废弃的运行组件和旧任务处理规则。
- [x] 明确每个阶段的停止条件、回退点和验收范围。
- [x] 文档链接和格式检查通过。

## Dependencies

None

## Type

docs

## Priority

high
