"""The committed OpenAPI document must describe the API as it really is."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_committed_spec_matches_app():
    spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export_openapi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    committed = (ROOT / "docs" / "openapi.json").read_text()
    assert committed == mod.render(), "run: .venv/bin/python scripts/export_openapi.py"


def test_post_readings_documents_every_status_it_returns():
    spec = json.loads((ROOT / "docs" / "openapi.json").read_text())
    responses = spec["paths"]["/readings"]["post"]["responses"]
    assert set(responses) == {"200", "201", "409", "422", "429", "500"}
    assert "Retry-After" in responses["429"]["headers"]
    for code in ("409", "422", "429", "500"):
        assert list(responses[code]["content"]) == ["application/problem+json"]
    assert "HTTPValidationError" not in spec["components"]["schemas"]
