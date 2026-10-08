import json
from pathlib import Path

from carbon.api.openapi import build_openapi

OPENAPI_JSON = Path(__file__).resolve().parents[1] / "openapi.json"


def test_openapi_lists_contract_models():
    schemas = build_openapi()["components"]["schemas"]
    assert {"CheckRequest", "Job", "CheckResult"} <= schemas.keys()


def test_response_fields_are_all_required():
    # Serialization schemas: the client can rely on every field being present.
    schemas = build_openapi()["components"]["schemas"]
    for name in ("Job", "CheckResult", "SideResult", "MatchedPassage"):
        assert set(schemas[name]["required"]) == set(schemas[name]["properties"]), name


def test_openapi_json_is_up_to_date():
    committed = json.loads(OPENAPI_JSON.read_text())
    assert committed == build_openapi(), "run: uv run python scripts/export_openapi.py"
