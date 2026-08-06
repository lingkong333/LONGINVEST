# V4.7 通知对象与信号绑定

本版本继承 V4.6 的全部内容，仅以下差异覆盖旧规则。

## 1. 通知对象

通知中心统一维护可复用的通知对象。第一版支持邮箱收件人、企业微信机器人和企业微信企业用户。每个对象包含名称、类型、启停状态和类型所需配置；Webhook、应用 Secret 等敏感值必须加密保存、只写不读，页面只显示是否已配置和脱敏指纹。

邮箱对象保存收件地址并复用全局 SMTP 发件配置。企业微信机器人对象保存独立 Webhook。企业微信企业用户对象保存企业 ID、应用 AgentId、应用 Secret 和接收成员账号。对象名称在同一类型内唯一，删除改为停用；已经产生的历史投递不得删除。

## 2. 信号绑定

信号中心可按监控订阅选择一个或多个已启用通知对象，也可选择继承信号默认策略。信号模块只保存通知对象标识，不保存或读取通知凭据。绑定变更生成新的监控订阅不可变版本并记录审计。

正式信号创建通知事件时，通知模块校验对象仍存在且启用，并冻结对象标识、名称、类型、渠道、配置版本和目标指纹。后续修改或停用对象只影响新事件；已创建的待投递在发送前发现对象停用时标记为 `SKIPPED_DISABLED`。

同一事件可以向多个相同渠道的对象分别投递。投递、尝试、失败、重试和取消均按具体对象记录，单个对象失败不影响其他对象。

## 3. 页面职责

通知中心默认进入“通知对象”，支持新增、编辑、启停和发送测试，并保留“发送记录”用于查看事件、逐对象投递和尝试。渠道级 SMTP 发件配置保留为高级配置，不再把邮箱收件人放入全局渠道配置。模板和底层策略不与常用操作挤在同一行。

信号中心增加“通知设置”，显示监控股票、当前选择模式和通知对象，可搜索并选择多个对象。保存前必须说明改动只影响之后产生的信号。

## 4. 接口

```text
GET    /api/v1/notifications/recipients
POST   /api/v1/notifications/recipients
PATCH  /api/v1/notifications/recipients/{recipient_id}
POST   /api/v1/notifications/recipients/{recipient_id}/enable
POST   /api/v1/notifications/recipients/{recipient_id}/disable
POST   /api/v1/notifications/recipients/{recipient_id}/test

GET   /api/v1/notifications/signal-bindings/{subscription_id}
PATCH /api/v1/notifications/signal-bindings/{subscription_id}
```

通知模块拥有信号绑定记录，信号模块只传递监控订阅标识。自定义绑定至少选择一个已启用对象；没有绑定的旧订阅继续使用原渠道继承规则。稳定错误码包括 `NOTIFICATION_RECIPIENT_NOT_FOUND`、`NOTIFICATION_RECIPIENT_DISABLED`、`NOTIFICATION_RECIPIENT_INVALID` 和 `MONITOR_NOTIFICATION_RECIPIENT_INVALID`。

## 5. 兼容与验收

1. 空事件列表可正常显示，不再出现 `INVALID_NOTIFICATION_EVENTS`。
2. 可分别创建邮箱、企业微信机器人和企业微信企业用户；敏感值在任何查询、日志和错误中均不回显。
3. 信号中心可为监控订阅选择多个通知对象，重复提交保持幂等，非法或停用对象不能保存。
4. 同一信号可创建多个同渠道投递，投递记录显示具体对象；一个失败不回滚信号或其他投递。
5. 旧订阅、旧事件和旧投递可继续读取；迁移后默认保持原有继承行为。
