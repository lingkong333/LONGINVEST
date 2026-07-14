import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionCredentials:
    session_token: str
    csrf_token: str
    token_digest: str
    csrf_digest: str


@dataclass(frozen=True)
class CsrfCredentials:
    csrf_token: str
    csrf_digest: str


class TokenService:
    def issue(self) -> SessionCredentials:
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        return SessionCredentials(
            session_token=session_token,
            csrf_token=csrf_token,
            token_digest=self.digest(session_token),
            csrf_digest=self.digest(csrf_token),
        )

    def issue_csrf(self) -> CsrfCredentials:
        csrf_token = secrets.token_urlsafe(32)
        return CsrfCredentials(
            csrf_token=csrf_token,
            csrf_digest=self.digest(csrf_token),
        )

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def verify_digest(cls, token: str, expected_digest: str) -> bool:
        return secrets.compare_digest(cls.digest(token), expected_digest)
