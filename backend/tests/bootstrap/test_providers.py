import asyncio

from long_invest.bootstrap.providers import build_history_http_client
from long_invest.modules.providers.browser_http_client import (
    BrowserProviderHttpClient,
)
from long_invest.modules.providers.playwright_http_client import (
    PlaywrightProviderHttpClient,
)
from long_invest.platform.config.settings import AppSettings


def test_history_client_defaults_to_lightweight_transport() -> None:
    client = build_history_http_client(AppSettings(_env_file=None))

    assert isinstance(client, BrowserProviderHttpClient)
    asyncio.run(client.close())


def test_history_client_selects_server_browser_transport() -> None:
    settings = AppSettings(
        _env_file=None,
        eastmoney_history_transport="playwright",
        eastmoney_history_min_interval_seconds=5,
    )

    client = build_history_http_client(settings)

    assert isinstance(client, PlaywrightProviderHttpClient)
    asyncio.run(client.close())
