from long_invest.modules.history_backfills.api import router


def test_history_backfill_routes_match_v33_specification() -> None:
    routes = {
        (route.path, method) for route in router.routes for method in route.methods
    }
    expected = {
        ("/api/v1/market-history/backfills", "POST"),
        ("/api/v1/market-history/backfills", "GET"),
        ("/api/v1/market-history/backfills/{job_id}", "GET"),
        ("/api/v1/market-history/backfills/{job_id}/pause", "POST"),
        ("/api/v1/market-history/backfills/{job_id}/resume", "POST"),
        ("/api/v1/market-history/backfills/{job_id}/cancel", "POST"),
        ("/api/v1/market-history/backfills/{job_id}/retry-failed", "POST"),
    }
    assert expected <= routes
