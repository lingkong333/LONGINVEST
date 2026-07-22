from datetime import UTC, datetime
from unittest.mock import Mock

from long_invest.modules.client_errors.contracts import ClientErrorInput
from long_invest.modules.client_errors.service import ClientErrorCollector


def error(message: str = "failed token=super-secret-value") -> ClientErrorInput:
    return ClientErrorInput(
        route="/dashboard",
        frontend_version="1.2.3",
        error_type="TypeError",
        message=message,
        browser_summary="Chrome 140 cookie=session-value",
        request_id="req_12345678",
        occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


def test_collector_redacts_credentials_and_samples_duplicate_fingerprints(
    monkeypatch,
) -> None:
    log = Mock()
    monkeypatch.setattr(
        "long_invest.modules.client_errors.service.logger.error",
        log,
    )
    collector = ClientErrorCollector()

    receipts = [collector.collect(error()) for _ in range(3)]

    assert [item.sampled for item in receipts] == [True, True, False]
    assert len({item.fingerprint for item in receipts}) == 1
    assert log.call_count == 2
    fields = log.call_args.kwargs
    assert "super-secret-value" not in fields["error_summary"]
    assert "session-value" not in fields["browser_summary"]
    assert fields["occurrence_count"] == 2


def test_fingerprint_ignores_varying_numbers_in_same_error() -> None:
    collector = ClientErrorCollector()

    first = collector.collect(error("request 123 failed"))
    second = collector.collect(error("request 456 failed"))

    assert first.fingerprint == second.fingerprint


def test_fingerprint_cache_is_bounded() -> None:
    collector = ClientErrorCollector(max_fingerprints=2)

    first = collector.collect(error("alpha"))
    collector.collect(error("beta"))
    collector.collect(error("gamma"))
    repeated = collector.collect(error("alpha"))

    assert first.fingerprint == repeated.fingerprint
    assert repeated.sampled is True


def test_global_sample_limit_blocks_unique_error_log_flood(monkeypatch) -> None:
    log = Mock()
    monkeypatch.setattr(
        "long_invest.modules.client_errors.service.logger.error",
        log,
    )
    collector = ClientErrorCollector(max_samples_per_minute=2)

    receipts = [collector.collect(error(value)) for value in ("a", "b", "c")]

    assert [item.sampled for item in receipts] == [True, True, False]
    assert log.call_count == 2
