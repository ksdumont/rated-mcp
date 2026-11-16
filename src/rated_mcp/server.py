"""Rated API MCP server (stdio)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from rated_mcp.client import RatedAPIError, RatedClient, RatedResponse, validate_next_url
from rated_mcp.config import KEYCHAIN_SERVICE, Settings, resolve_api_key
from rated_mcp.spec import (
    BODY_ARG,
    NETWORK_ARG,
    NETWORK_HEADER,
    PAGE_SIZE_PARAMS,
    ToolSpec,
    build_tools,
    load_spec,
)

# Logs go to stderr; stdout is reserved for the MCP protocol. Never log the API key.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="rated-mcp: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rated_mcp")

NEXT_PAGE_TOOL = "rated_fetch_next_page"
KEY_CHECK_PATH = "/v1/eth/apiMetadata"

MISSING_KEY_MESSAGE = f"""
Rated MCP server: no API key found.

Set RATED_API_KEY in the environment that launches this server, or on macOS
store it in the login Keychain (the server reads it automatically):

    security add-generic-password -U -a "$USER" -s {KEYCHAIN_SERVICE} -w

Get a key at https://console.rated.network (API Keys).
"""

REJECTED_KEY_MESSAGE = """
Rated MCP server: the Rated API rejected your API key ({detail}).

Create or copy a valid key at https://console.rated.network/apiKeys and update
where you stored it (RATED_API_KEY or the '{service}' Keychain item).
"""


def _clamp_page_size(value: Any, settings: Settings) -> Any:
    try:
        return max(1, min(int(value), settings.max_page_size))
    except (TypeError, ValueError):
        return value


def _format_response(result: RatedResponse, settings: Settings) -> str:
    meta = [f"HTTP {result.status}"]
    if result.request_id:
        meta.append(f"request ID {result.request_id}")
    if result.compute_units:
        meta.append(f"{result.compute_units} compute units")
    body = json.dumps(result.data, indent=1, ensure_ascii=False, default=str)
    limit = settings.max_response_chars
    if limit > 0 and len(body) > limit:
        body = (
            body[:limit]
            + f"\n... [truncated: response was {len(body):,} characters; request a smaller page size or narrower date range]"
        )
    return f"[{' · '.join(meta)}]\n{body}"


class RatedMCP:
    def __init__(self, client: RatedClient, settings: Settings, tools: list[ToolSpec]):
        self.client = client
        self.settings = settings
        self.tools = {t.name: t for t in tools}
        self.tool_defs = self._tool_definitions()
        schemas = {t.name: t.input_schema for t in self.tool_defs}
        self.server = Server(
            "rated",
            version="0.1.0",
            instructions=(
                "Tools for the Rated API (staking and validator data). Tool names are "
                "<network>_<api version>_<resource>. Every call costs compute units: keep page "
                "sizes and date ranges small and fetch extra pages only when needed."
            ),
            get_tool_input_schema=schemas.get,
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )

    async def list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self.tool_defs)

    def _tool_definitions(self) -> list[types.Tool]:
        result = [
            types.Tool(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                annotations=types.ToolAnnotations(
                    read_only_hint=not t.is_write,
                    destructive_hint=t.method == "delete" or (t.is_write and "remove" in t.name),
                    idempotent_hint=t.method in ("get", "delete"),
                    open_world_hint=True,
                ),
            )
            for t in self.tools.values()
        ]
        result.append(
            types.Tool(
                name=NEXT_PAGE_TOOL,
                description=(
                    "Fetch the next (or previous) page of a paginated Rated API result. Pass the "
                    "`next` or `previous` URL exactly as returned by another rated tool. Only "
                    "api.rated.network links are accepted; page size is capped at "
                    f"{self.settings.max_page_size}. Fetch one page at a time and stop once you "
                    "have enough data, since every page costs compute units."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The `next`/`previous` URL."},
                        NETWORK_ARG: {
                            "type": "string",
                            "enum": ["mainnet", "hoodi"],
                            "description": "Ethereum network header, if the original call used one.",
                        },
                    },
                    "required": ["url"],
                },
                annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=True),
            )
        )
        return result

    async def call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        name, arguments = params.name, params.arguments or {}
        try:
            if name == NEXT_PAGE_TOOL:
                result = await self._fetch_next_page(arguments)
            else:
                tool = self.tools.get(name)
                if tool is None:
                    raise ValueError(f"Unknown tool: {name}")
                result = await self._call_endpoint(tool, arguments)
            text, is_error = _format_response(result, self.settings), False
        except (RatedAPIError, ValueError) as exc:
            text, is_error = str(exc), True
        except httpx.HTTPError as exc:
            text, is_error = f"Could not reach the Rated API: {type(exc).__name__}: {exc}", True
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)

    async def _call_endpoint(self, tool: ToolSpec, args: dict[str, Any]) -> RatedResponse:
        path = tool.path
        for pname in tool.path_params:
            if args.get(pname) in (None, ""):
                raise ValueError(f"Missing required path parameter: {pname}")
            path = path.replace("{" + pname + "}", quote(str(args[pname]), safe=""))

        params: dict[str, Any] = {}
        for pname in tool.query_params:
            value = args.get(pname)
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            params[pname] = value
        if tool.page_size_param:
            params[tool.page_size_param] = _clamp_page_size(
                params.get(tool.page_size_param, self.settings.default_page_size), self.settings
            )

        headers = {}
        if tool.has_network_header and args.get(NETWORK_ARG):
            headers[NETWORK_HEADER] = str(args[NETWORK_ARG])

        body = args.get(BODY_ARG) if tool.has_body else None
        return await self.client.request(tool.method, path, params=params, headers=headers, json_body=body)

    async def _fetch_next_page(self, args: dict[str, Any]) -> RatedResponse:
        url = validate_next_url(str(args.get("url", "")))
        parts = urlsplit(url)
        query = [
            (k, str(_clamp_page_size(v, self.settings)) if k in PAGE_SIZE_PARAMS else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        url = parts._replace(query=urlencode(query)).geturl()
        headers = {NETWORK_HEADER: str(args[NETWORK_ARG])} if args.get(NETWORK_ARG) else {}
        return await self.client.request("get", url, headers=headers)


def _fail(message: str) -> None:
    print(message.strip(), file=sys.stderr)
    sys.exit(1)


async def _verify_key(client: RatedClient) -> None:
    try:
        await client.request("get", KEY_CHECK_PATH)
    except RatedAPIError as exc:
        if exc.status in (401, 403):
            _fail(REJECTED_KEY_MESSAGE.format(detail=str(exc), service=KEYCHAIN_SERVICE))
        log.warning("API key check returned an unexpected error, continuing: %s", exc)
    except httpx.HTTPError as exc:
        log.warning("Could not reach api.rated.network to verify the key, continuing: %s", exc)


async def _run() -> None:
    settings = Settings.from_env()
    api_key, source = resolve_api_key()
    if not api_key:
        _fail(MISSING_KEY_MESSAGE)

    all_tools = build_tools(load_spec(), settings.default_page_size, settings.max_page_size)
    tools = [t for t in all_tools if settings.enable_writes or not t.is_write]
    gated = len(all_tools) - len(tools)

    client = RatedClient(api_key, settings)
    try:
        if not settings.skip_key_check:
            await _verify_key(client)
        log.info(
            "using API key from %s; %d tools registered%s",
            source,
            len(tools) + 1,
            f" ({gated} write tools hidden; set RATED_ENABLE_WRITES=true to enable)" if gated else
            " (write tools ENABLED)",
        )
        app = RatedMCP(client, settings, tools)
        async with stdio_server() as (read_stream, write_stream):
            await app.server.run(read_stream, write_stream, app.server.create_initialization_options())
    finally:
        await client.aclose()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
