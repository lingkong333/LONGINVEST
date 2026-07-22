from long_invest.bootstrap.app import create_app


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
