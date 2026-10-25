"""Write the API's OpenAPI document to docs/openapi.json (run after changing routes).

    .venv/bin/python scripts/export_openapi.py

tests/test_openapi.py fails if the committed file no longer matches the app.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

OUT = ROOT / "docs" / "openapi.json"


def render() -> str:
    return json.dumps(create_app(load_settings()).openapi(), indent=2) + "\n"


if __name__ == "__main__":
    OUT.write_text(render())
    sys.stderr.write(f"wrote {OUT.relative_to(ROOT)}\n")
