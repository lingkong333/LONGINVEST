from long_invest.modules.auth.tokens import TokenService


def test_session_and_csrf_tokens_are_random_and_only_digests_are_persistable() -> None:
    service = TokenService()

    first = service.issue()
    second = service.issue()

    assert first.session_token != second.session_token
    assert first.csrf_token != second.csrf_token
    assert len(first.token_digest) == 64
    assert len(first.csrf_digest) == 64
    assert first.session_token not in {first.token_digest, first.csrf_digest}
    assert first.csrf_token not in {first.token_digest, first.csrf_digest}
    assert service.digest(first.session_token) == first.token_digest
    assert service.digest(first.csrf_token) == first.csrf_digest


def test_issued_tokens_contain_at_least_256_bits_of_random_input() -> None:
    credentials = TokenService().issue()

    # token_urlsafe(32) encodes 32 random bytes as at least 43 URL-safe characters.
    assert len(credentials.session_token) >= 43
    assert len(credentials.csrf_token) >= 43
