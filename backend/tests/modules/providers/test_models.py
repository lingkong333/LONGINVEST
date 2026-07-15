from datetime import datetime, timedelta, timezone

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.models import (
    ProviderCapabilitySetting,
    ProviderCircuitHistory,
    ProviderConfigVersion,
    ProviderFailureSample,
    ProviderHealthState,
)
from long_invest.modules.providers.repository import redact_failure_sample


def test_provider_models_cover_version_settings_health_history_and_samples() -> None:
    assert ProviderConfigVersion.__tablename__ == "provider_config_version"
    assert ProviderCapabilitySetting.__tablename__ == "provider_capability_setting"
    assert ProviderHealthState.__tablename__ == "provider_health_state"
    assert ProviderCircuitHistory.__tablename__ == "provider_circuit_history"
    assert ProviderFailureSample.__tablename__ == "provider_failure_sample"


def test_failure_sample_is_redacted_and_expires_within_seven_days() -> None:
    now = datetime.now(timezone.utc)
    sample = redact_failure_sample(
        provider=ProviderCode.EASTMONEY,
        capability=ProviderCapability.REALTIME_QUOTE_BATCH,
        error_code="PROVIDER_SCHEMA_INCOMPATIBLE",
        payload={"token": "secret", "cookie": "x", "field_names": ["f2", "f12"]},
        now=now,
    )
    assert "secret" not in str(sample.sample)
    assert "x" not in str(sample.sample)
    assert sample.expires_at == now + timedelta(days=7)
