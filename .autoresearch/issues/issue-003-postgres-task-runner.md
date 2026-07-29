# 建立 PostgreSQL 简化任务执行器

## Description

使用 PostgreSQL 直接保存和领取耗时任务，替代 Redis/RQ、任务分发器和独立看门狗，同时保留进度、控制和恢复能力。

## Acceptance Criteria

- [x] 任务支持 PENDING、RUNNING、SUCCEEDED、PARTIAL、FAILED、PAUSED 和 CANCELED。
- [x] 多个领取方不能同时执行同一个任务。
- [x] 任务保存进度、尝试次数、下次执行时间、执行权到期时间和安全错误摘要。
- [x] 普通后台启动及定期扫描时能够恢复执行权已过期的任务。
- [x] 暂停、继续、取消和有限重试行为可验证。
- [x] 重复提交由数据库约束保持幂等。
- [x] 正常、失败、并发、重启恢复和取消测试通过。

## Dependencies

Issue #2

## Type

backend

## Priority

high
