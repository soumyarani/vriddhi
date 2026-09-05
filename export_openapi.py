#!/usr/bin/env python
"""Export the OpenAPI (Swagger) schema to static files.

FastAPI serves the live schema at /openapi.json, but that endpoint is disabled in
production and needs a running server besides. This writes the same document to
disk so it can be committed, diffed in review, published to a portal, or fed to a
client generator without booting anything.

The schema is generated from the actual route table, so it cannot drift from the
implementation: if a response model changes, the next export shows it.

Usage:
    .venv/bin/python export_openapi.py                  # -> docs/openapi.json (+ .yaml)
    .venv/bin/python export_openapi.py --out build/api  # custom prefix
    .venv/bin/python export_openapi.py --check          # CI: fail if out of date

YAML output is skipped with a note if PyYAML is not installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The app reads settings at import time and refuses to start without a JWT
# secret. Export is a pure schema dump that never signs anything, so a dummy
# value is enough and avoids requiring a populated .env just to read the routes.
os.environ.setdefault("JWT_SECRET", "export-only-not-a-real-secret-" + "x" * 32)
os.environ.setdefault("ENVIRONMENT", "development")


def build_schema() -> dict:
    """Generate the schema from the live route table."""
    from app.main import app

    schema = app.openapi()

    # `servers` is stamped from API_BASE_URL, which on a developer machine is
    # localhost. A published spec should offer the real deployments instead, so
    # a reader can try a request without hand-editing the host.
    schema["servers"] = [
        {"url": "https://api.example.com", "description": "production"},
        {"url": "https://staging-api.example.com", "description": "staging"},
        {"url": "http://localhost:8000", "description": "local development"},
    ]
    return schema


def write(schema: dict, prefix: Path) -> list[Path]:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    written = []

    json_path = prefix.with_suffix(".json")
    json_path.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n")
    written.append(json_path)

    try:
        import yaml
    except ImportError:
        print("note: PyYAML not installed, skipping .yaml output", file=sys.stderr)
    else:
        yaml_path = prefix.with_suffix(".yaml")
        yaml_path.write_text(
            yaml.safe_dump(schema, sort_keys=False, allow_unicode=True, width=100)
        )
        written.append(yaml_path)

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="docs/openapi",
        help="output path prefix, without extension (default: docs/openapi)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed schema differs from the code",
    )
    args = parser.parse_args()

    schema = build_schema()
    prefix = Path(args.out)

    if args.check:
        current = prefix.with_suffix(".json")
        if not current.exists():
            print(f"{current} does not exist; run export_openapi.py", file=sys.stderr)
            return 1
        if json.loads(current.read_text()) != schema:
            print(
                f"{current} is out of date with the route table.\n"
                f"Run: .venv/bin/python export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"{current} is up to date")
        return 0

    written = write(schema, prefix)
    paths = len(schema["paths"])
    operations = sum(
        1 for ops in schema["paths"].values() for m in ops if m in _HTTP_METHODS
    )
    for path in written:
        print(f"wrote {path}")
    print(f"  {paths} paths, {operations} operations, "
          f"{len(schema.get('components', {}).get('schemas', {}))} schemas")
    return 0


_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


if __name__ == "__main__":
    raise SystemExit(main())
