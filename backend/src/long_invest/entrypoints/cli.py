import argparse
import asyncio
import getpass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.auth.account_service import AccountAdminService
from long_invest.modules.auth.application import AuthAuditAdapter
from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.auth.passwords import PasswordService
from long_invest.modules.auth.repository import SqlAlchemyAuthRepository
from long_invest.platform.database.engine import get_database
from long_invest.platform.errors import AppError

PASSWORD_COMMANDS = frozenset({"create-admin", "reset-password"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="long-invest")
    groups = parser.add_subparsers(dest="group", required=True)
    user = groups.add_parser("user", help="管理员账号维护")
    commands = user.add_subparsers(dest="command", required=True)
    for command in (
        "create-admin",
        "reset-password",
        "revoke-sessions",
        "disable",
        "enable",
    ):
        item = commands.add_parser(command)
        item.add_argument("--username", required=True)
    return parser


def _read_new_password() -> str:
    password = getpass.getpass("新密码: ")
    confirmation = getpass.getpass("再次输入新密码: ")
    if password != confirmation:
        raise AppError(
            code="AUTH_PASSWORD_MISMATCH",
            message="两次输入的密码不一致",
            status_code=422,
        )
    return password


async def run_user_command(args: argparse.Namespace) -> str:
    request_id = f"cli_{uuid4().hex}"
    audit_context = AuditContext(
        request_id=request_id,
        idempotency_key=request_id,
        trusted_ip="local-cli",
    )
    password = _read_new_password() if args.command in PASSWORD_COMMANDS else None
    database = get_database()
    try:
        async with database.transaction() as session:
            service = AccountAdminService(
                SqlAlchemyAuthRepository(session),
                PasswordService(),
                AuthAuditAdapter(session),
                audit_context,
            )
            now = datetime.now(UTC)
            if args.command == "create-admin":
                user = await service.create_admin(args.username, password, now=now)
                return f"管理员已创建: {user.username}"
            if args.command == "reset-password":
                user = await service.reset_password(args.username, password, now=now)
                return f"密码已重置: {user.username}"
            if args.command == "revoke-sessions":
                count = await service.revoke_sessions(args.username, now=now)
                return f"已撤销 Session: {count}"
            if args.command == "disable":
                changed = await service.disable(args.username, now=now)
                return "账号已禁用" if changed else "账号已经是禁用状态"
            if args.command == "enable":
                changed = await service.enable(args.username, now=now)
                return "账号已启用" if changed else "账号已经是启用状态"
            raise RuntimeError(f"unsupported user command: {args.command}")
    except SQLAlchemyError as exc:
        raise AppError(
            code="AUTH_BACKEND_UNAVAILABLE",
            message="认证服务暂时不可用",
            status_code=503,
        ) from exc


def main() -> int:
    args = build_parser().parse_args()
    try:
        message = asyncio.run(run_user_command(args))
    except AppError as exc:
        print(f"失败 [{exc.code}]: {exc.message}")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
