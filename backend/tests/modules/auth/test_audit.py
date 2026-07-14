from long_invest.modules.auth.audit import AuditContext, build_auth_audit_event


def test_audit_idempotency_key_is_stable_and_fits_public_audit_storage() -> None:
    context = AuditContext(
        request_id="req_test",
        idempotency_key="x" * 160,
    )

    first = build_auth_audit_event(
        context,
        action_code="AUTH_PASSWORD_CHANGE",
        object_type="app_user",
        object_id="user-1",
        result="SUCCESS",
        risk_level="CRITICAL",
    )
    replay = build_auth_audit_event(
        context,
        action_code="AUTH_PASSWORD_CHANGE",
        object_type="app_user",
        object_id="user-1",
        result="SUCCESS",
        risk_level="CRITICAL",
    )

    assert first.idempotency_key == replay.idempotency_key
    assert len(first.idempotency_key) <= 160
