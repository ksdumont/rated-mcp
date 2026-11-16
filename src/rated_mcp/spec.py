"""Turn Rated's OpenAPI spec into MCP tool definitions.

Every operation in the spec becomes one tool, except operations documented as
retired (they declare a 410 Gone response). Non-GET operations are marked as
writes so the server can hide them unless RATED_ENABLE_WRITES=true.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
PAGE_SIZE_PARAMS = ("limit", "size")
NETWORK_HEADER = "X-Rated-Network"
NETWORK_ARG = "network"
BODY_ARG = "body"

NETWORK_LABELS = {
    "eth": "Ethereum",
    "solana": "Solana",
    "cosmos": "Cosmos",
    "avalanche": "Avalanche",
    "cardano": "Cardano",
    "polkadot": "Polkadot",
    "eigenlayer": "EigenLayer",
    "private_sets": "Private Sets",
    "account": "Account",
}

# Collection segments that read better singular when followed by an ID,
# e.g. /entities/{entity_id}/rewards -> entity_rewards.
SINGULAR = {
    "entities": "entity",
    "validators": "validator",
    "operators": "operator",
    "stake_accounts": "stake_account",
    "stake_authorities": "stake_authority",
    "eigenpods": "eigenpod",
    "blocks": "block",
    "sets": "set",
}

PARAM_LABELS = {
    "validator_index_or_pubkey": "validator",
    "consensus_slot": "slot",
}

# /v1/tags is the Private Sets API; its operations get explicit, verb-first names.
PRIVATE_SET_NAMES = {
    ("get", "/v1/tags"): "list_sets",
    ("post", "/v1/tags"): "create_set",
    ("get", "/v1/tags/{id}"): "get_set",
    ("patch", "/v1/tags/{id}"): "update_set",
    ("delete", "/v1/tags/{id}"): "delete_set",
    ("get", "/v1/tags/{id}/validators"): "list_set_validators",
    ("post", "/v1/tags/{id}/validators"): "add_set_validators",
    ("post", "/v1/tags/{id}/validators/remove"): "remove_set_validators",
    ("delete", "/v1/tags/{id}/validators/{pubkey}"): "remove_set_validator",
}


@dataclass
class ToolSpec:
    name: str
    method: str
    path: str
    network: str
    version: str
    area: str
    description: str
    input_schema: dict[str, Any]
    path_params: list[str] = field(default_factory=list)
    query_params: list[str] = field(default_factory=list)
    page_size_param: str | None = None
    has_network_header: bool = False
    has_body: bool = False

    @property
    def is_write(self) -> bool:
        return self.method != "get"


def load_spec() -> dict[str, Any]:
    text = resources.files("rated_mcp").joinpath("openapi.json").read_text(encoding="utf-8")
    return json.loads(text)


def is_retired(operation: dict[str, Any]) -> bool:
    return "410" in (operation.get("responses") or {})


def _snake(segment: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", segment).lower()


def tool_name(method: str, path: str) -> tuple[str, str, str]:
    """Return (tool name, network key, version) for an operation."""
    segments = path.strip("/").split("/")
    version, area, rest = segments[0], segments[1], segments[2:]

    if area == "tags":
        return f"private_sets_{version}_{PRIVATE_SET_NAMES[(method, path)]}", "private_sets", version
    if area == "selfReports":
        return f"eth_{version}_self_report_validators", "eth", version
    if area == "pricingPlans":
        return f"account_{version}_pricing_plans", "account", version

    parts: list[str] = []
    for i, segment in enumerate(rest):
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        if segment == "delegators" and nxt in ("stakeAccounts", "stakeAuthorities"):
            continue
        if segment.startswith("{"):
            param = segment.strip("{}")
            if parts and parts[-1] in SINGULAR:
                parts[-1] = SINGULAR[parts[-1]]
            else:
                parts.append("by_" + PARAM_LABELS.get(param, param.removesuffix("_id")))
            continue
        parts.append(_snake(segment))
    if not parts:
        parts = ["network_overview"]
    return f"{area}_{version}_{'_'.join(parts)}", area, version


class _Resolver:
    def __init__(self, spec: dict[str, Any]):
        self.schemas = spec.get("components", {}).get("schemas", {})

    def resolve(self, schema: Any, seen: frozenset[str] = frozenset()) -> Any:
        """Inline $refs and flatten Optional[...] (anyOf with null) for simpler schemas."""
        if isinstance(schema, list):
            return [self.resolve(s, seen) for s in schema]
        if not isinstance(schema, dict):
            return schema
        if "$ref" in schema:
            ref = schema["$ref"].rsplit("/", 1)[-1]
            if ref in seen:
                return {"type": "object"}
            target = copy.deepcopy(self.schemas.get(ref, {}))
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            target.update(siblings)
            return self.resolve(target, seen | {ref})
        out = {k: self.resolve(v, seen) for k, v in schema.items()}
        variants = out.get("anyOf")
        if isinstance(variants, list):
            non_null = [v for v in variants if v.get("type") != "null"]
            if len(non_null) == 1 and len(non_null) < len(variants):
                del out["anyOf"]
                merged = dict(non_null[0])
                merged.update(out)
                out = merged
        return out


def _clean_text(text: str, limit: int = 900) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " ..."
    return text


def _param_schema(param: dict[str, Any], resolver: _Resolver) -> dict[str, Any]:
    schema = resolver.resolve(param.get("schema", {}))
    schema.pop("title", None)
    description = param.get("description") or schema.get("description")
    if description:
        schema["description"] = description
    return schema


def build_tools(spec: dict[str, Any], default_page_size: int, max_page_size: int) -> list[ToolSpec]:
    resolver = _Resolver(spec)
    tools: list[ToolSpec] = []

    for path, item in spec["paths"].items():
        shared_params = item.get("parameters", [])
        for method in HTTP_METHODS:
            operation = item.get(method)
            if operation is None or is_retired(operation):
                continue

            name, network, version = tool_name(method, path)
            area = ", ".join(operation.get("tags") or []) or "General"
            properties: dict[str, Any] = {}
            required: list[str] = []
            tool = ToolSpec(
                name=name,
                method=method,
                path=path,
                network=network,
                version=version,
                area=area,
                description="",
                input_schema={},
            )

            for param in shared_params + operation.get("parameters", []):
                pname, location = param["name"], param["in"]
                schema = _param_schema(param, resolver)
                if location == "header":
                    if pname != NETWORK_HEADER:
                        continue
                    tool.has_network_header = True
                    schema["description"] = (
                        "Network to query (sent as the X-Rated-Network header). Defaults to mainnet."
                    )
                    properties[NETWORK_ARG] = schema
                    continue
                if location == "path":
                    tool.path_params.append(pname)
                elif location == "query":
                    tool.query_params.append(pname)
                    if pname in PAGE_SIZE_PARAMS:
                        tool.page_size_param = pname
                        api_default = schema.get("default")
                        schema["default"] = default_page_size
                        schema["maximum"] = min(schema.get("maximum", max_page_size), max_page_size)
                        schema["description"] = (
                            f"Page size. This server defaults to {default_page_size} to keep compute-unit "
                            f"cost low (API default: {api_default}); capped at {schema['maximum']}. "
                            "Use the response's `next` link with rated_fetch_next_page for more."
                        )
                else:
                    continue
                properties[pname] = schema
                if param.get("required"):
                    required.append(pname)

            body = operation.get("requestBody")
            if body:
                body_schema = (body.get("content", {}).get("application/json", {}) or {}).get("schema")
                if body_schema:
                    tool.has_body = True
                    resolved = resolver.resolve(body_schema)
                    resolved.pop("title", None)
                    resolved.setdefault("description", "JSON request body.")
                    properties[BODY_ARG] = resolved
                    if body.get("required", True):
                        required.append(BODY_ARG)

            tool.input_schema = {"type": "object", "properties": properties}
            if required:
                tool.input_schema["required"] = required
            tool.description = _describe(operation, tool)
            tools.append(tool)

    names = [t.name for t in tools]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"Duplicate tool names generated from spec: {sorted(duplicates)}")
    tools.sort(key=lambda t: (t.network, t.version, t.name))
    return tools


def _describe(operation: dict[str, Any], tool: ToolSpec) -> str:
    label = NETWORK_LABELS.get(tool.network, tool.network)
    header = f"[{label} · {tool.version} · {tool.area}] {operation.get('summary', '').strip()}"
    lines = [header]
    body = _clean_text(operation.get("description", ""))
    if body and body != operation.get("summary", "").strip():
        lines.append(body)
    lines.append(f"Endpoint: {tool.method.upper()} {tool.path}")
    if tool.is_write:
        lines.append("WRITE OPERATION: modifies data in your Rated account.")
    return "\n\n".join(lines)
