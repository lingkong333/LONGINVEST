from long_invest.platform.errors import AppError


def validate_new_password(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise AppError(
            code="AUTH_PASSWORD_INVALID",
            message="密码长度必须为 12 到 128 个字符",
            status_code=422,
        )
