"""Write the API contract to openapi.json (or the path given as the first argument).

uv run python scripts/export_openapi.py [path]
"""

import json
import sys
from pathlib import Path

from carbon.api.openapi import build_openapi

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "openapi.json"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    path.write_text(json.dumps(build_openapi(), indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
