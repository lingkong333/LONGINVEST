from fastapi import FastAPI

from long_invest.modules.settings.api import router


def test_settings_openapi_exposes_typed_values_metadata_and_actions() -> None:
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()

    expected_responses = {
        ("get", "/api/v1/settings"): "SettingListEnvelope",
        ("get", "/api/v1/settings/{key}"): "SettingEnvelope",
        ("patch", "/api/v1/settings/{key}"): "SettingCommandEnvelope",
        ("get", "/api/v1/settings/{key}/history"): "SettingHistoryEnvelope",
        ("post", "/api/v1/settings/{key}/rollback"): "SettingCommandEnvelope",
        ("get", "/api/v1/secrets/status"): "SecretStatusListEnvelope",
        ("patch", "/api/v1/secrets/{key}"): "SecretCommandEnvelope",
    }
    for (method, path), model_name in expected_responses.items():
        response_schema = schema["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema["$ref"].endswith(model_name)

    definitions = schema["components"]["schemas"]
    assert {
        "definition",
        "allowed_actions",
    } <= set(definitions["SettingResponse"]["required"])
    assert {
        "definition",
        "allowed_actions",
    } <= set(definitions["SecretStatusResponse"]["required"])
