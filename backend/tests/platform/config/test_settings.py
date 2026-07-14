from long_invest.platform.config.settings import AppSettings


def test_settings_use_safe_defaults() -> None:
    settings = AppSettings(_env_file=None)

    assert settings.app_name == "LongInvest"
    assert settings.environment == "development"
    assert settings.api_port == 8000
    assert settings.log_level == "INFO"
    assert settings.database_url.startswith("postgresql+")
    assert settings.redis_url.startswith("redis://")


def test_settings_accept_longinvest_environment_prefix(monkeypatch) -> None:
    monkeypatch.setenv("LONGINVEST_ENVIRONMENT", "test")
    monkeypatch.setenv("LONGINVEST_API_PORT", "9000")
    monkeypatch.setenv("LONGINVEST_LOG_LEVEL", "DEBUG")

    settings = AppSettings(_env_file=None)

    assert settings.environment == "test"
    assert settings.api_port == 9000
    assert settings.log_level == "DEBUG"
