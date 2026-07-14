# Notification foundation

## Specification boundary

- Baseline: V3.1 sections 4.3-4.6, 13, 15.3-15.5, 18.4, 21 stage 1B,
  22.5, 23, 25.5, 25.12.5-25.12.7, 25.13, and 25.15.4.
- Owned data: `notification_event`, `notification_delivery`, and
  `notification_delivery_attempt` model definitions. The main integration flow owns
  migration generation and registration.
- Public capabilities: policy resolution, channel protocols and safe channel results,
  immutable Git template resolution, pre-send eligibility review, notification
  publication, leased delivery persistence and recovery, channel-scoped execution,
  and retry/circuit/event status decisions.
- Internal events for later integration: `notification.requested/suppressed`,
  `delivery_created/started/succeeded/failed/unknown/canceled`, and
  `channel_degraded/recovered`.
- Stable errors: `NOTIFICATION_POLICY_UNCONFIGURED`,
  `NOTIFICATION_TEMPLATE_MISSING_FIELD`, `NOTIFICATION_TEMPLATE_UNSAFE`,
  `NOTIFICATION_SECRET_VALUE_REJECTED`, channel-specific temporary/permanent errors,
  and `NOTIFICATION_OUTCOME_UNKNOWN`.

## Explicit exclusions

This foundation does not publish business signals, call external networks, register
HTTP or process entrypoints, write outbox records, manage secrets, or own Alembic
migrations. It exposes the transaction-safe service/repository operations and the
claimed-delivery executor that future independent WeCom and email processes will call.
Real WeCom/SMTP adapters and business integration belong to stage 5.
