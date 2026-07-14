from enum import StrEnum


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED_IDLE = "EXPIRED_IDLE"
    EXPIRED_ABSOLUTE = "EXPIRED_ABSOLUTE"
    REVOKED = "REVOKED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    USER_DISABLED = "USER_DISABLED"


class RequestActivity(StrEnum):
    BACKGROUND = "BACKGROUND"
    USER = "USER"
    WRITE = "WRITE"
