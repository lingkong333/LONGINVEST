from fastapi import FastAPI

from long_invest.modules.system_status.api import router


def test_router_exposes_all_system_operations_endpoints() -> None:
    app = FastAPI()
    app.include_router(router)
    methods = {
        (route.path, method) for route in router.routes for method in route.methods
    }

    assert methods == {
        ("/api/v1/system/health", "GET"),
        ("/api/v1/system/components", "GET"),
        ("/api/v1/workers", "GET"),
        ("/api/v1/queues", "GET"),
        ("/api/v1/scheduler/status", "GET"),
        ("/api/v1/schedule-occurrences", "GET"),
        ("/api/v1/system-clock/status", "GET"),
    }

    schema = app.openapi()
    query = schema["paths"]["/api/v1/schedule-occurrences"]["get"]["parameters"]
    assert {item["name"] for item in query} >= {
        "page",
        "page_size",
        "occurrence_type",
        "status",
        "from_date",
        "through_date",
    }
    for path, method in methods:
        operation = schema["paths"][path][method.lower()]
        response = operation["responses"]["200"]["content"]["application/json"]
        assert response["schema"].get("$ref")
