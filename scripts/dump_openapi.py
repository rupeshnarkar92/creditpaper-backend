"""Generate the engine API contract: docs/openapi.json and the web app's types.

The schema is the engine/platform boundary in machine-readable form. The web app
hand-mirrored every response type in `api.ts` with nothing pinning the two
together, so renaming a field on the Python side left the frontend compiling
cleanly and failing at runtime — the worst place to find out.

Both outputs are generated, never hand-edited. CI regenerates and diffs, so a
contract change is either committed deliberately or fails the build.

The TypeScript is emitted here rather than by `openapi-typescript` because that
package does not yet support the TypeScript 6 this app is on (peer-dependency
conflict). Forcing it with --legacy-peer-deps to obtain a type generator is a poor
trade; the subset of OpenAPI a FastAPI app produces is small and generating it
alongside the schema keeps one command as the source of both.

    .venv/Scripts/python scripts/dump_openapi.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Docs routes are intentionally disabled for deployed environments. The shared
# `APP_ENV` logic must therefore be set before importing the composition root.
os.environ["APP_ENV"] = "test"
load_dotenv(ROOT / ".env", override=False)
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/creditpaper_openapi")

from creditpaper.api.main import app  # noqa: E402

SCHEMA_OUT = ROOT / "docs" / "openapi.json"
TS_OUT = ROOT / "apps" / "credit-web" / "src" / "api-types.ts"

_PRIMITIVES = {"string": "string", "integer": "number", "number": "number", "boolean": "boolean"}


def _ts_type(node: dict[str, Any]) -> str:
    """One OpenAPI schema node as a TypeScript type expression."""
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in node:
        # FastAPI renders `X | None` as anyOf[X, null].
        parts = [_ts_type(s) for s in node["anyOf"]]
        return " | ".join(dict.fromkeys(p if p != "null" else "null" for p in parts))
    if "enum" in node:
        return " | ".join(json.dumps(v) for v in node["enum"])
    kind = node.get("type")
    if kind == "array":
        return f"{_ts_type(node.get('items', {}))}[]"
    if kind == "object" or kind is None:
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {_ts_type(extra)}>"
        return "Record<string, unknown>"
    if kind == "null":
        return "null"
    return _PRIMITIVES.get(kind, "unknown")


def _interface(name: str, schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return f"export type {name} = {' | '.join(json.dumps(v) for v in schema['enum'])};"
    required = set(schema.get("required", []))
    lines = [f"export interface {name} {{"]
    for prop, node in (schema.get("properties") or {}).items():
        optional = "" if prop in required else "?"
        if desc := (node.get("description") or "").strip():
            lines.append(f"  /** {' '.join(desc.split())} */")
        lines.append(f"  {prop}{optional}: {_ts_type(node)};")
    lines.append("}")
    return "\n".join(lines)


def _render_ts(schema: dict[str, Any]) -> str:
    components = schema.get("components", {}).get("schemas", {})
    header = (
        "// GENERATED — do not edit by hand.\n"
        "//\n"
        "// Produced from the engine API's OpenAPI schema by\n"
        "// scripts/dump_openapi.py. These types ARE the engine/platform contract:\n"
        "// renaming a field on the Python side changes this file, and CI fails if\n"
        "// the committed copy has drifted. Previously api.ts hand-mirrored them,\n"
        "// with nothing pinning the two together, so a rename compiled cleanly and\n"
        "// broke at runtime.\n"
        "//\n"
        "// Regenerate:  .venv/Scripts/python scripts/dump_openapi.py\n"
    )
    body = [_interface(name, node) for name, node in sorted(components.items())]
    routes = [
        f"  | {json.dumps(verb.upper() + ' ' + path)}"
        for path, ops in sorted(schema.get("paths", {}).items())
        for verb in sorted(ops)
        if verb in {"get", "post", "put", "patch", "delete"}
    ]
    body.append(
        "/** Every route the engine API exposes. */\nexport type ApiRoute =\n" + "\n".join(routes) + ";"
    )
    return header + "\n" + "\n\n".join(body) + "\n"


def main() -> int:
    schema = app.openapi()
    # Sorted and newline-terminated so the diff is reviewable and stable: an
    # unordered dump churns on every regeneration and teaches people to ignore it.
    SCHEMA_OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    TS_OUT.write_text(_render_ts(schema), encoding="utf-8")
    paths = len(schema.get("paths", {}))
    models = len(schema.get("components", {}).get("schemas", {}))
    print(f"wrote {SCHEMA_OUT.relative_to(ROOT)} — {paths} paths, {models} schemas")
    print(f"wrote {TS_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
