from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


@dataclass(frozen=True)
class PasswordVerification:
    valid: bool
    upgraded_hash: str | None = None


class PasswordService:
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher(type=Type.ID)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, encoded: str) -> PasswordVerification:
        try:
            self._hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return PasswordVerification(valid=False)

        upgraded_hash = None
        if self._hasher.check_needs_rehash(encoded):
            upgraded_hash = self.hash(password)
        return PasswordVerification(valid=True, upgraded_hash=upgraded_hash)
