import json
from pathlib import Path

from long_invest.bootstrap.app import create_app


def main() -> None:
    output = Path("openapi.json")
    output.write_text(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
