from types import SimpleNamespace

from long_invest.bootstrap.app import create_app
from long_invest.modules.alerts.api import _alert


def test_alert_routes_are_registered() -> None:
    paths = create_app().openapi()["paths"]
    expected = {
        "/api/v1/alerts",
        "/api/v1/alerts/{alert_id}",
        "/api/v1/alerts/{alert_id}/occurrences",
        "/api/v1/alerts/{alert_id}/actions",
        "/api/v1/alerts/{alert_id}/acknowledge",
        "/api/v1/alerts/{alert_id}/resolve",
        "/api/v1/alerts/{alert_id}/retry",
    }
    assert expected <= paths.keys()


def test_alert_response_exposes_state_based_allowed_actions() -> None:
    base = {
        "id": "alert-1",
        "aggregation_key": "worker:one",
        "alert_type": "WORKER_DOWN",
        "object_type": "worker",
        "object_id": "one",
        "severity": "ERROR",
        "title": "Worker unavailable",
        "summary": "Worker heartbeat expired",
        "details": {},
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "acknowledged_at": None,
        "acknowledged_by_user_id": None,
        "resolved_at": None,
        "resolved_by_user_id": None,
        "resolution_reason": None,
        "version": 1,
        "created_at": None,
        "updated_at": None,
        "retry_job_type": "worker.recover",
        "retry_queue": "maintenance",
        "retry_config": {},
    }

    opened = _alert(SimpleNamespace(**base, status="OPEN"))
    acknowledged = _alert(SimpleNamespace(**base, status="ACKNOWLEDGED"))
    resolved = _alert(SimpleNamespace(**base, status="RESOLVED"))

    assert opened["allowed_actions"] == ["ACKNOWLEDGE", "RESOLVE", "RETRY"]
    assert acknowledged["allowed_actions"] == ["RESOLVE", "RETRY"]
    assert resolved["allowed_actions"] == []
