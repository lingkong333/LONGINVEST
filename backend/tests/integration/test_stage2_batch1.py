from long_invest.bootstrap.app import create_app
from long_invest.entrypoints.cli import build_parser


def test_reviewed_stage2_routes_are_registered() -> None:
    paths = create_app().openapi()["paths"]

    assert "get" in paths["/api/v1/securities"]
    assert "post" in paths["/api/v1/securities/refresh"]
    assert "get" in paths["/api/v1/trading-calendar"]
    assert "post" in paths["/api/v1/trading-calendar/import"]
    assert "get" in paths["/api/v1/providers"]
    assert "post" in paths["/api/v1/providers/quote-diagnostics"]


def test_calendar_import_is_available_from_the_main_cli() -> None:
    args = build_parser().parse_args(
        ["calendar", "import", "--file", "calendar.json"]
    )

    assert args.group == "calendar"
    assert args.command == "import"
    assert args.file == "calendar.json"
