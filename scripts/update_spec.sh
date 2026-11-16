#!/bin/sh
# Refresh the vendored OpenAPI spec, then run the tests to catch naming changes.
set -eu
cd "$(dirname "$0")/.."
curl -fsSL https://docs.rated.network/openapi.json -o src/rated_mcp/openapi.json
uv run pytest -q
