# mcp-server-appcatalog

Production-ready [Model Context Protocol](https://modelcontextprotocol.io/) server that aggregates application metadata — latest versions, download URLs, SHA256 hashes, installer types/architectures/scopes, product & upgrade codes, silent install switches, dependencies, and release notes — from **winget**, **Chocolatey**, and **Silent Install HQ** into a single, source-agnostic tool surface.

Unlike Microsoft's built-in `winget mcp`, this server:

- Does **not** require winget to be installed locally.
- Works on **Linux, macOS, and Windows** (all queries are HTTP API calls).
- Aggregates **multiple sources**, not just winget.
- Returns a **normalized** data model so agents can compare sources apples-to-apples.

## Quickstart

```powershell
git clone https://github.com/perezdap/mcp-server-appcatalog.git
cd mcp-server-appcatalog
uv sync --extra dev
Copy-Item .env.example .env
uv run appcatalog-mcp --transport stdio
```

## Features

- **FastMCP** server using the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
- **9 tools**: `search_packages`, `get_package`, `get_installer_metadata`, `compare_sources`, `find_best_source`, `list_recent`, `get_silent_switches`, `get_changelog_or_releasenotes`, `verify_hash`.
- **Source adapters** with a uniform `PackageAdapter` ABC: add a new source by implementing `search` / `get_package` / `list_recent` / `normalize`.
- **Chocolatey ``.nupkg`` parsing** — downloads the package zip in memory and parses ``tools/chocolateyInstall.ps1`` to surface the real per-arch installer URLs + SHA256 hashes + silent args that the OData feed hides. Cross-source SHA256 agreement (e.g., Chocolatey's googlechrome x86 hash == winget's Google.Chrome x86 hash) is verifiable.
- **SQLite TTL cache** (default 6h) for both raw API responses and normalized records.
- **Rate limiting** + identifiable User-Agent + configurable httpx connection limits.
- **Graceful degradation**: winget.run down → search returns empty (GitHub has no free-text search); Chocolatey down → serve from cache; SIHQ unreachable → `get_silent_switches` returns manifest switches only.
- **Transports**: `stdio` (default, for Cursor/Claude Desktop), `sse`, `streamable-http` (for MCPJungle / remote).
- **Docker** + `docker-compose` for containerized deployment.

## Data sources

| Source | Backend | Notes |
|---|---|---|
| winget | `microsoft/winget-pkgs` GitHub Contents API (primary), `winget.run` REST API (search-only fallback) | Authoritative current manifests. Anonymous GitHub quota is 60 req/hr — set `GITHUB_TOKEN` for 5000/hr. |
| Chocolatey | Community Repository OData v2 API + `.nupkg` install-script parse | `https://community.chocolatey.org/api/v2/`. The OData feed returns the .nupkg URL; ``get_installer_metadata`` downloads the zip in memory and parses ``tools/chocolateyInstall.ps1`` to extract the real per-arch URLs / SHA256 / silent args. |
| Evergreen | `evergreen-api.stealthpuppy.com` REST API | Vendor-direct latest-version + download URL (+ SHA256 when the vendor publishes one). Covers 200+ enterprise apps. Refreshes every 8h. Used for vendor-fresh cross-references. |
| Silent Install HQ | Delegates to a separately-deployed `mcp-server-silentinstallhq` MCP server | Optional. Used as a silent-switch fallback when winget manifests omit `InstallerSwitches`. |

### Why GitHub-backed winget (not winget.run as primary)?

Live verification (2026-06) shows the public `winget.run` REST API's package index was last updated in early 2023 — it returns `Google.Chrome v111` as "latest" while the real current version is `149.0.7827.156`. winget.run is therefore only a fallback used for **free-text search** (the GitHub Contents API has no package-name search); actual versions, installers, and hashes always come from the GitHub manifests.

### Why Evergreen alongside winget?

Evergreen queries the **vendor source directly** (Microsoft Edge update API, 7-Zip GitHub releases, …) rather than a manifest repo. For some apps (e.g. Adobe Acrobat, FSLogix) it can have newer versions than winget. It also publishes a SHA256 hash when the vendor exposes one. Use ``find_best_source`` to auto-compare winget / Chocolatey / Evergreen for a given app and pick the best source for packaging.

## Normalized data model

```python
class PackageMetadata(BaseModel):
    source: Literal["winget", "chocolatey", "silentinstallhq"]
    id: str                      # PackageIdentifier (winget) or Id (chocolatey)
    name: str                    # PackageName / Title
    publisher: str
    version: str
    description: str
    homepage: str | None
    license: str | None
    license_url: str | None
    release_notes_url: str | None
    tags: list[str]
    dependencies: list[DependencyInfo]
    installers: list[InstallerInfo]
    versions: list[str]
    # ...plus fetched_at, cache_hit, raw_data

class InstallerInfo(BaseModel):
    url: str                     # Direct download URL
    sha256: str | None           # Hash for verification (None for Chocolatey .nupkg)
    installer_type: str          # exe, msi, msix, inno, wix, nupkg, portable, zip...
    architecture: str            # x64, x86, arm64
    scope: str | None            # machine, user
    product_code: str | None     # MSI/MSIX product code for Intune detection
    upgrade_code: str | None     # MSI UpgradeCode for supersedence
    silent_switch: str | None
    silent_with_progress_switch: str | None
    file_size: int | None
```

## MCP tools

| Tool | Signature | Returns |
|---|---|---|
| `search_packages` | `query, sources=["winget","chocolatey"], limit=10` | `SearchResults` (normalized list, latest version per match) |
| `get_package` | `package_id, source=None, version=None` | `PackageMetadata` (full). `source=None` tries winget then chocolatey. `source="evergreen"` fetches vendor-direct |
| `get_installer_metadata` | `package_id, source="winget", version=None` | All installer URLs + SHA256 + arch + scope + product/upgrade codes + switches. For Chocolatey, downloads + parses the `.nupkg` in memory to extract real per-arch MSI URLs/hashes
| `compare_sources` | `package_id` | Side-by-side winget vs chocolatey |
| `find_best_source` | `package_id` | Tries winget + Chocolatey + Evergreen in parallel, scores each on SHA/product-code/silent-switch presence, returns the highest-scoring source + per-source ranking. For Chocolatey/Evergreen it also tries id-translation fallbacks (``Google.Chrome`` → ``googlechrome``; ``7zip.7zip`` → ``7zip``) |
| `list_recent` | `limit=10, source=None` | Recently updated packages across sources |
| `get_silent_switches` | `package_id, source="winget"` | Silent install/uninstall switches; falls back to SIHQ if absent |
| `get_changelog_or_releasenotes` | `package_id, source=None` | Release notes URL/text |
| `verify_hash` | `url, expected_sha256, max_bytes=None` | Streams the URL and computes its SHA256 to prove a download matches an expected hash. http/https only, best-effort SSRF guard against private/loopback hosts (redirect-aware; see note below); never written to disk, never cached, capped at `APPCATALOG_VERIFY_MAX_BYTES` (default 500 MB) |

## Example tool calls

### Cursor / Claude (natural language)

- "What is the latest version of Google Chrome, and give me the download URL and SHA256?"
- "Find the Chocolatey package for 7-Zip — what version, what dependencies?"
- "Compare what winget vs Chocolatey have for VLC player."
- "Give me all installer metadata (architectures, installer types, silent switches, product codes) for Microsoft Visual Studio Code."
- "List the top 10 recently updated packages across both winget and Chocolatey."
- "What's the best source for packaging 7-Zip across winget, Chocolatey, and Evergreen?"
- "Pull Microsoft Edge from Evergreen — what's the latest version + per-arch URLs?"

### MCP Python client (stdio)

```python
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command="uv", args=["--directory", ".", "run", "appcatalog-mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool("get_package", {"package_id": "Google.Chrome", "source": "winget"})
            print(json.loads(r.content[0].text))

asyncio.run(main())
```

### CrewAI (HTTP transport)

```python
from crewai_tools import MCPServerAdapter

with MCPServerAdapter({
    "url": "http://127.0.0.1:8010/mcp",
    "transport": "streamable-http",
}) as tools:
    search = tools["search_packages"]
    print(search.run(query="Chrome"))
    installs = tools["get_installer_metadata"]
    print(installs.run(package_id="Microsoft.VisualStudioCode", source="winget"))
```

## Configuration

All settings use the `APPCATALOG_` prefix (`GITHUB_TOKEN` is the one exception). See `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `APPCATALOG_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http` |
| `APPCATALOG_HOST` | `0.0.0.0` | HTTP bind host |
| `APPCATALOG_PORT` | `8010` | HTTP bind port |
| `APPCATALOG_CACHE_DIR` | `./data` | SQLite cache directory |
| `APPCATALOG_CACHE_TTL_HOURS` | `6` | Cache TTL |
| `APPCATALOG_WINGET_API` | `auto` | `auto`, `github`, or `winget.run` |
| `GITHUB_TOKEN` | _unset_ | Optional. Raises GitHub quota to 5000/hr. |
| `APPCATALOG_CHOCO_API` | `https://community.chocolatey.org/api/v2/` | Chocolatey OData v2 base URL |
| `APPCATALOG_EVERGREEN_API` | `https://evergreen-api.stealthpuppy.com` | Evergreen REST API base URL |
| `APPCATALOG_SIHQ_URL` | `http://127.0.0.1:8000/mcp` | Silent Install HQ MCP endpoint; empty = disabled |
| `APPCATALOG_REQUEST_DELAY_SECONDS` | `0.5` | Minimum delay between outbound requests |
| `APPCATALOG_VERIFY_MAX_BYTES` | `524288000` | `verify_hash` streaming download cap (500 MB) |
| `APPCATALOG_VERIFY_BLOCK_PRIVATE_HOSTS` | `true` | Best-effort `verify_hash` SSRF guard: rejects URLs (and redirect hops) whose hostname resolves to loopback/private/link-local/reserved IPs. Validation and connection use separate DNS lookups, so it is not rebind-proof — keep this server behind a trusted agent; set `false` only for internal-only deployments |
| `APPCATALOG_USER_AGENT` | project default | Outbound User-Agent |
| `APPCATALOG_LOG_LEVEL` | `INFO` | Logging level |

## Transport configuration

### Cursor

Add to `.cursor/mcp.json` in the cloned repo (or user MCP settings):

```json
{
  "mcpServers": {
    "appcatalog": {
      "command": "uv",
      "args": [
        "--directory",
        "${workspaceFolder}",
        "run",
        "appcatalog-mcp",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS).
Replace `<path-to-clone>` with the directory where you cloned this repo.

```json
{
  "mcpServers": {
    "appcatalog": {
      "command": "uv",
      "args": ["--directory", "<path-to-clone>", "run", "appcatalog-mcp"]
    }
  }
}
```

### Streamable HTTP (MCPJungle / remote)

```powershell
uv run appcatalog-mcp --transport streamable-http --host 127.0.0.1 --port 8010
```

### SSE (legacy HTTP clients)

```powershell
uv run appcatalog-mcp --transport sse --host 127.0.0.1 --port 8010
```

## Docker

```powershell
docker compose up -d --build
```

The service binds `127.0.0.1:8010` by default. Cache persists in the `appcatalog_cache` volume.

MCPJungle registration:

1. Run the container (or local process) on port `8010`.
2. Put nginx in front with TLS (Cloudflare Zero Trust / Authentik as needed).
3. In MCPJungle, register: **Name** `appcatalog`, **Transport** `streamable-http`, **URL** `https://mcp.yourdomain.example/mcp`.
4. Confirm all 9 tools appear: `search_packages`, `get_package`, `get_installer_metadata`, `compare_sources`, `find_best_source`, `list_recent`, `get_silent_switches`, `get_changelog_or_releasenotes`, `verify_hash`.

Example nginx location:

```nginx
location /mcp/ {
    proxy_pass http://appcatalog-mcp:8010/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
}
```

## Architecture

```text
┌─────────────────────────────────────────────────────────────────────┐
│                          FastMCP server.py                          │
│              (lifespan: http + cache + 3 adapters)                  │
└──────────────┬──────────────────────────────────────┬───────────────┘
               │                                      │
        ┌──────▼──────┐                       ┌────────▼────────┐
        │  tools/     │  register_tools()    │  adapters/      │
        │  catalog.py │ ───────────────────▶ │  base.py (ABC)  │
        └─────────────┘                     └────────┬────────┘
                                                      │
        ┌─────────────────────┬──────────────────────┼──────────────┐
        ▼                     ▼                       ▼              ▼
 ┌───────────────┐    ┌───────────────┐   ┌─────────────────┐  ┌──────────────┐
 │ winget        │    │ chocolatey    │   │ sihq           │  │ cache (SQLite)│
 │ adapter       │    │ adapter       │   │ adapter        │  │ TTL 6h        │
 │ GitHub → run  │    │ OData v2 Atom │   │ MCP client     │  │               │
 └───────┬───────┘    └───────┬───────┘   └────────┬────────┘  └───────▲──────┘
         │                    │                    │                   │
         ▼                    ▼                    ▼                   │
 api.github.com       community.chocolatey.org  127.0.0.1:8000/mcp    │
 /repos/microsoft/    /api/v2/                                          │
 winget-pkgs/                                                            │
 raw.githubusercontent.com ────────────────── httpx ──────────────────┘
```

## Development

```powershell
uv sync --extra dev
# Unit tests — no network required
uv run pytest
# Live integration tests (hit winget GitHub + Chocolatey OData)
uv run pytest -m integration
uv run ruff check src tests
```

Fixtures under `tests/fixtures/` are real API capture snapshots. Refresh them
when upstream schemas change — see `AGENTS.md`'s "Refreshing test fixtures".

## License

MIT
