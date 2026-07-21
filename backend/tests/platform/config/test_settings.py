from long_invest.platform.config.settings import AppSettings


def test_settings_use_safe_defaults(monkeypatch) -> None:
    for name in (
        "LONGINVEST_APP_NAME",
        "LONGINVEST_ENVIRONMENT",
        "LONGINVEST_API_HOST",
        "LONGINVEST_API_PORT",
        "LONGINVEST_LOG_LEVEL",
        "LONGINVEST_DATABASE_URL",
        "LONGINVEST_DATABASE_OWNER_URL",
        "LONGINVEST_DATABASE_APP_ROLE",
        "LONGINVEST_DATABASE_APP_PASSWORD",
        "LONGINVEST_REDIS_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings(_env_file=None)

    assert settings.app_name == "LongInvest"
    assert settings.environment == "development"
    assert settings.api_port == 8000
    assert settings.log_level == "INFO"
    assert settings.database_url.startswith("postgresql+")
    assert settings.database_owner_url.startswith("postgresql+")
    assert settings.database_app_role == "longinvest_app"
    assert settings.redis_url.startswith("redis://")
    assert settings.dispatcher_scan_interval_seconds == 1.0
    assert settings.dispatcher_batch_size == 50
    assert settings.watchdog_scan_interval_seconds == 10.0
    assert settings.outbox_lease_timeout_seconds == 60
    assert settings.run_stale_timeout_seconds == 60
    assert settings.strategy_git_path == "/var/lib/long-invest/strategies"
    assert settings.strategy_environment_version == "python-3.12"
    assert settings.strategy_runner_image_digest == ""


def test_settings_accept_longinvest_environment_prefix(monkeypatch) -> None:
    monkeypatch.setenv("LONGINVEST_ENVIRONMENT", "test")
    monkeypatch.setenv("LONGINVEST_API_PORT", "9000")
    monkeypatch.setenv("LONGINVEST_LOG_LEVEL", "DEBUG")

    settings = AppSettings(_env_file=None)

    assert settings.environment == "test"
    assert settings.api_port == 9000
    assert settings.log_level == "DEBUG"
