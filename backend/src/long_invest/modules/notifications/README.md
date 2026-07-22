# Notification module

## Specification boundary

- Baseline: V3.3 section 25.13, with dynamic configuration and secret handling
  defined by section 25.15.
- Owned data: `notification_event`, `notification_delivery`, and
  `notification_delivery_attempt` model definitions. The main integration flow owns
  migration generation and registration.
- Public capabilities: policy resolution, channel protocols and safe channel results,
  immutable Git template resolution, pre-send eligibility review, notification
  publication, leased delivery persistence and recovery, real WeCom and SMTP
  delivery, query and manual control APIs, channel-scoped worker processes, and
  retry/circuit/event status decisions.
- Internal events for later integration: `notification.requested/suppressed`,
  `delivery_created/started/succeeded/failed/unknown/canceled`, and
  `channel_degraded/recovered`.
- Stable errors: `NOTIFICATION_POLICY_UNCONFIGURED`,
  `NOTIFICATION_TEMPLATE_MISSING_FIELD`, `NOTIFICATION_TEMPLATE_UNSAFE`,
  `NOTIFICATION_SECRET_VALUE_REJECTED`, channel-specific temporary/permanent errors,
  and `NOTIFICATION_OUTCOME_UNKNOWN`.

## Runtime boundary

Signal transactions freeze eligible channels and target fingerprints when the event is
created. Separate WeCom and email processes re-check channel availability immediately
before sending. Configuration and encrypted secrets are owned by the settings module;
notification code only uses its public transaction-bound service. External delivery
failure never rolls back the business signal transaction.
