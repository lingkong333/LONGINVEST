from argon2 import PasswordHasher

from long_invest.modules.auth.passwords import PasswordService


def test_correct_password_is_accepted_without_replacement_hash() -> None:
    service = PasswordService()
    encoded = service.hash("a sufficiently long password")

    result = service.verify("a sufficiently long password", encoded)

    assert result.valid is True
    assert result.upgraded_hash is None


def test_wrong_password_is_rejected_without_raising() -> None:
    service = PasswordService()
    encoded = service.hash("a sufficiently long password")

    result = service.verify("the wrong password", encoded)

    assert result.valid is False
    assert result.upgraded_hash is None


def test_valid_password_with_old_parameters_returns_upgraded_hash() -> None:
    old_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    service = PasswordService(
        PasswordHasher(time_cost=2, memory_cost=8192, parallelism=1)
    )
    old_hash = old_hasher.hash("a sufficiently long password")

    result = service.verify("a sufficiently long password", old_hash)

    assert result.valid is True
    assert result.upgraded_hash is not None
    assert result.upgraded_hash != old_hash
    assert service.verify("a sufficiently long password", result.upgraded_hash).valid
