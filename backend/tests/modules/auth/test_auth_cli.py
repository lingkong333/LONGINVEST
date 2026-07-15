import pytest

from long_invest.entrypoints.cli import build_parser


def test_admin_cli_exposes_only_the_v31_user_commands() -> None:
    parser = build_parser()

    for command in (
        "create-admin",
        "reset-password",
        "revoke-sessions",
        "disable",
        "enable",
    ):
        parsed = parser.parse_args(["user", command, "--username", "admin"])
        assert parsed.group == "user"
        assert parsed.command == command
        assert parsed.username == "admin"


def test_admin_cli_rejects_password_command_line_arguments() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "user",
                "create-admin",
                "--username",
                "admin",
                "--password",
                "must-not-appear-in-process-list",
            ]
        )
