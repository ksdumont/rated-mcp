# rated-mcp

A local [MCP](https://modelcontextprotocol.io) server that gives Claude Code access to the
[Rated API](https://docs.rated.network): validator, operator and network data for Ethereum,
Solana, Cosmos, Avalanche, Cardano, Polkadot and EigenLayer (API v0 and v1).

Tools are generated from Rated's OpenAPI spec (`src/rated_mcp/openapi.json`):

- **143 read tools** named `<network>_<version>_<resource>`, e.g. `eth_v1_entity_effectiveness`,
  `solana_v1_validator_rewards`, `cosmos_v1_network_rewards`.
- **`rated_fetch_next_page`**: follows a `next`/`previous` link from a paginated result.
- **7 write tools**, hidden by default (Private Sets create/update/delete/add/remove validators,
  v0 validator self-report). See [Enabling write tools](#enabling-write-tools).
- Retired endpoints (those returning `410 Gone`) are skipped.

Every Rated call costs [compute units](https://docs.rated.network/rated-api/pricing/compute-units),
so the server keeps costs low by design: page size defaults to 5 (max 50), it never auto-paginates,
requests are spaced to the free-tier limit of 2/second, and 429s are retried with backoff
(honoring `Retry-After`). Every response shows its request ID and compute units used.

## Prerequisites

- [Claude Code](https://docs.claude.com/en/docs/claude-code)
- [uv](https://docs.astral.sh/uv/) (it installs Python for you):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  On macOS, `brew install uv` also works.
- A Rated account at [console.rated.network](https://console.rated.network)
- Access to this repository

## 1. Get your own API key

Each teammate uses their **own** key. Don't share keys.

1. Sign in at [console.rated.network](https://console.rated.network).
2. Open **API Keys** ([console.rated.network/apiKeys](https://console.rated.network/apiKeys)) and
   create a key.
3. Copy it to your clipboard. Don't paste it into chats, tickets, or files in this repo.

## 2. Store the key

The server reads `RATED_API_KEY` from its environment. On macOS, if that isn't set, it also reads
the login Keychain item `rated-api-key`. That way the key works even when Claude Code is
launched from the Dock or the desktop app, which don't load your shell profile.

### macOS (recommended: Keychain)

With the key on your clipboard:

```bash
security add-generic-password -U -a "$USER" -s rated-api-key -w "$(pbpaste)"
```
```bash
pbcopy </dev/null
```

The second command clears the clipboard.

> Don't use the interactive prompt (`... -w` with no value). It silently truncates input to 128
> characters, and Rated keys are longer.

To check that it's stored without printing it:

```bash
security find-generic-password -s rated-api-key -w | wc -c
```

Optional: to also expose it as an environment variable in your terminals (needed only for the
project `.mcp.json` below or for scripts), add this line to `~/.zshrc`:

```bash
export RATED_API_KEY="$(security find-generic-password -s rated-api-key -w 2>/dev/null)"
```

### Linux

Store it in your keyring with `secret-tool` (libsecret; it prompts for the value) and export it
from your shell profile:

```bash
secret-tool store --label="Rated API key" service rated-api-key
```
```bash
echo 'export RATED_API_KEY="$(secret-tool lookup service rated-api-key)"' >> ~/.bashrc
```

### Windows (PowerShell)

Set a user-level environment variable, then restart Claude Code:

```powershell
[Environment]::SetEnvironmentVariable("RATED_API_KEY", (Read-Host "Rated API key"), "User")
```

Never put a real key in `.env`, `.mcp.json`, `~/.claude.json`, or any committed file.
`.env.example` contains placeholders only.

## 3. Install

```bash
git clone https://github.com/ksdumont/rated-mcp.git ~/rated-mcp
```
```bash
cd ~/rated-mcp && uv sync
```

## 4. Add it to Claude Code

### Option A: available in every project (recommended)

Run this from the repo folder. It records absolute paths, so it works from any directory:

```bash
claude mcp add rated --scope user -- "$(which uv)" run --directory "$(pwd)" rated-mcp
```

This doesn't put the key in Claude Code's config; the server looks it up at startup.

### Option B: project-scoped `.mcp.json`

This repo includes a `.mcp.json` that passes `${RATED_API_KEY}` through from your environment
(Claude Code expands `${VAR}` in `.mcp.json`; this has been verified). It only applies when you
start Claude Code **from this repo's root**, and Claude Code asks you to approve it the first
time. On macOS it also works without the variable set, by falling back to the Keychain.

If you use Option A as well, you don't need this.

## 5. Verify

Check that the server can find and use your key:

```bash
uv run --directory ~/rated-mcp rated-mcp </dev/null
```

Expected output (the server then exits, because stdin is closed):

```
rated-mcp: using API key from ...; 143 tools registered (7 write tools hidden; ...)
```

Then check Claude Code:

```bash
claude mcp get rated
```

It should show `Status: ✔ Connected`. In a new Claude Code session, try:

> Use the rated tools to get the Ethereum network overview.

## Enabling write tools

Write tools modify your Rated account (Private Sets, self-reports), so they're off by default.
To enable them, re-register the server with the flag:

```bash
claude mcp remove rated -s user
```
```bash
claude mcp add rated --scope user -e RATED_ENABLE_WRITES=true -- "$(which uv)" run --directory "$(pwd)" rated-mcp
```

With the project `.mcp.json`, export `RATED_ENABLE_WRITES=true` before starting Claude Code.
The startup line will then say `(write tools ENABLED)`. Claude Code still asks you to approve
each tool call; read write calls carefully before you approve them.

## Configuration

All optional, set via environment (or `-e NAME=value` on `claude mcp add`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `RATED_API_KEY` | (Keychain on macOS) | Your Rated API key |
| `RATED_ENABLE_WRITES` | `false` | Register non-GET tools |
| `RATED_DEFAULT_PAGE_SIZE` | `5` | `limit`/`size` sent when not specified |
| `RATED_MAX_PAGE_SIZE` | `50` | Hard cap on page size |
| `RATED_MAX_RETRIES` | `3` | Retries on 429 (and 502/503/504 for reads) |
| `RATED_MAX_RETRY_WAIT` | `30` | Max seconds to wait per retry |
| `RATED_MIN_REQUEST_INTERVAL` | `0.5` | Seconds between requests (free tier: 2 req/s) |
| `RATED_TIMEOUT` | `60` | HTTP timeout in seconds |
| `RATED_MAX_RESPONSE_CHARS` | `60000` | Truncate very large responses |
| `RATED_SKIP_KEY_CHECK` | `false` | Skip the key validation call at startup |

For the Ethereum testnet, pass `network: "hoodi"` to any Ethereum tool.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `no API key found` | Store the key (step 2). If you use only `RATED_API_KEY`, make sure the process that launches Claude Code has it. GUI-launched apps don't read `~/.zshrc`. |
| `the Rated API rejected your API key` / HTTP 401 | The key is wrong, expired, or revoked. Create a new one at [console.rated.network/apiKeys](https://console.rated.network/apiKeys) and store it again. On macOS, check that it wasn't truncated: `security find-generic-password -s rated-api-key -w \| wc -c` should match your key's length. |
| HTTP 403 | Your plan doesn't include that endpoint. |
| HTTP 429 after retries | You hit the rate limit or plan quota. Wait, or check usage in the console. |
| `claude mcp get rated` shows `✘ Failed to connect` | Run the verify command in step 5 to see the error. Make sure the `uv` path in the command exists (`which uv`). |
| `spawn uv ENOENT` | Claude Code can't find `uv`. Re-add the server with the absolute path from `which uv`. |
| Tools don't appear | Start a new Claude Code session, or run `/mcp` in the current one. |
| Need help from Rated support | Include the request ID shown in the error message. |

## Development

```bash
uv run pytest
```

To refresh the OpenAPI spec and re-run the tests (new endpoints become tools automatically):

```bash
./scripts/update_spec.sh
```

Code layout: `spec.py` turns the OpenAPI spec into tool definitions, `client.py` handles HTTP
(throttling, retries, errors), `server.py` is the MCP server and startup checks, and
`config.py` holds environment settings and key lookup.
