"""Build the OpenAPI document for the API contract in :mod:`carbon.api.schemas`."""

from typing import Any

from pydantic.json_schema import models_json_schema

import carbon
from carbon.api.schemas import REQUEST_MODELS, RESPONSE_MODELS


def build_openapi() -> dict[str, Any]:
    """OpenAPI 3.1 document holding every request and response schema under ``components``.

    Request models use their validation schema and response models their serialization schema,
    so defaulted response fields are marked required, as the client will always receive them.
    """
    models = [(m, "validation") for m in REQUEST_MODELS] + [
        (m, "serialization") for m in RESPONSE_MODELS
    ]
    _, schema = models_json_schema(models, ref_template="#/components/schemas/{model}")
    return {
        "openapi": "3.1.0",
        "info": {"title": "Carbon API", "version": carbon.__version__},
        "paths": {},
        "components": {"schemas": dict(sorted(schema["$defs"].items()))},
    }
