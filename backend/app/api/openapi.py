"""Write the published OpenAPI document (M5 S1): `python -m app.api.openapi [PATH]`.

The committed file (`docs/api/openapi.json`) is what the frontend generates its types
from. A unit test fails when it differs from the running app, so the contract and the
code cannot drift silently. Building the document opens no connection: settings are
placeholders and the app lifespan never runs.
"""

import json
import sys
from pathlib import Path
from typing import Any

from app.config import Settings

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"


def spec() -> dict[str, Any]:
    from app.main import create_app

    settings = Settings(_env_file=None, DATABASE_URL="unused", REDIS_URL="unused")
    return create_app(settings).openapi()


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(spec()), encoding="utf-8", newline="\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
