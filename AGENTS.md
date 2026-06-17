# AI Agent Guide

Instructions for Codex, Claude, Cursor, Grok, and similar tools working on this codebase.

## Project summary

Python MCP server ([FastMCP](https://github.com/modelcontextprotocol/python-sdk))
that aggregates end-user/desktop application metadata from multiple public
package repositories and exposes it as a single, normalized tool surface for AI
agents.

Sources today:
- **winget** — `microsoft/winget-pkgs` GitHub repo (primary, authoritative) with
  the `winget.run` REST API as a stale-but-unlimited fallback for free-text
  search only.
- **Chocolatey** — Community Repository OData v2 API (Atom XML).
- **silentinstallhq** — optional delegation to a separately-deployed
  `mcp-server-silentinstallhq` MCP server for silent switches when winget
  manifests omit them.

Stack: Python 3.11+, `uv`, `httpx`, `PyYAML`, `Pydantic v2`, SQLite cache.

## Non-negotiable rules

- Application code stays **Python-only**. No PowerShell/Node helpers without an
  explicit user request.
- Be polite to upstream APIs: keep the configured User-Agent, rate limiting,
  and SQLite cache. The winget.run and Chocolatey endpoints are shared public
  services.
- Do not commit `.env`, `data/`, `data_integration/`, `.venv/`, cache DBs, or
  local MCP harness folders (`mcps/`).
- Match the existing layout under `src/appcatalog_mcp/`. New sources go in
  `adapters/`. New MCP tools go in `tools/`. Pydantic shapes go in `models.py`.
- Write or update `pytest` tests for behavior changes. Run `uv run pytest` and
  `uv run ruff check src tests` before declaring done.
- Surgical edits only. Do not refactor unrelated modules.

## Quick commands

```powershell
uv sync --extra dev
uv run appcatalog-mcp --transport stdio
uv run pytest                              # unit tests, no live network
uv run pytest -m integration               # live winget/chocolatey API tests
uv run ruff check src tests
docker compose up -d --build
```

## Architecture

```text
src/appcatalog_mcp/
├── server.py                # FastMCP app + lifespan wiring
├── __main__.py              # CLI entry + transport flag
├── tools/
│   └── catalog.py           # All 7 MCP tool registrations
├── adapters/
│   ├── base.py              # PackageAdapter ABC + PackageNotFoundError
│   ├── winget_adapter.py    # GitHub Contents API primary, winget.run fallback
│   ├── chocolatey_adapter.py # OData v2 Atom XML adapter
│   └── sihq_adapter.py      # MCP client delegation to silentinstallhq
├── models.py                # Normalized Pydantic models (PackageMetadata etc.)
├── cache.py                 # SQLite TTL cache
├── http_client.py           # httpx + cache + rate limiter
├── rate_limiter.py          # asyncio pacing
└── config.py                # env-driven settings (APPCATALOG_*)
```

## MCP tools

| Tool | Purpose |
|------|---------|
| `search_packages` | Search winget and/or chocolatey by keyword |
| `get_package` | Full metadata for a specific package (latest or version) |
| `get_installer_metadata` | All installers, hashes, archs, switches, product codes |
| `compare_sources` | Side-by-side: winget vs Chocolatey for the same app |
| `list_recent` | Recently updated packages |
| `get_silent_switches` | Silent install switches (winget manifest → SIHQ fallback) |
| `get_changelog_or_releasenotes` | Release notes URL/text from manifest or OData |

## Adapter contract

Every source implements `PackageAdapter` (see `adapters/base.py`):
- `search(query, limit)` — keyword search, latest-version summaries
- `get_package(package_id, version=None)` — full normalized record
- `get_installer_detail(package_id, version=None)` — installer-focused record
  (defaults to `get_package`; winget/choco override for richer fields)
- `list_recent(limit)` — recently updated packages
- `get_manifest(package_id, version=None)` — best-effort raw manifest
- `normalize(raw)` — source payload → `PackageMetadata` (static method)

To add a source (e.g. Scoop, Npackd):
1. Create `adapters/<source>_adapter.py` implementing `PackageAdapter`.
2. In `adapters/__init__.py` export it.
3. Construct it in `server.app_lifespan` and add to the yielded context.
4. Add the source name to `VALID_SOURCES` in `tools/catalog.py`.
5. Add `_<source>(ctx)` helper if you need it elsewhere.
6. Add tests + fixtures under `tests/`.

## Source-specific quirks

### winget (`adapters/winget_adapter.py`)
- Primary backend is the **GitHub Contents API** on `microsoft/winget-pkgs`.
  The manifest path on disk is: `manifests/{first_letter}/{Publisher}/{Package}/{Version}/{PackageIdentifier}.{type}.yaml`.
  Files fetched per version: `.yaml` (version), `.installer.yaml`,
  `.locale.default.yaml`, `.locale.en-US.yaml`.
- GitHub anonymous quota is **60 req/hr**. Setting `GITHUB_TOKEN` raises it to
  5000/hr. Cache TTL (default 6h) absorbs the bulk of repeat traffic.
- "Latest version" detection lists the version directory, filters out
  channel/scope subdirs (e.g. `Beta`, `EXE`, `MSI`), and sorts the remaining
  entries by a semver tuple. Multi-channel packages (e.g. `Google.Chrome.Beta`)
  live under `manifests/g/Google/Chrome/Beta/{Version}/` and are NOT returned by
  this adapter for the root id — query the channel id explicitly if needed.
- **winget.run** is used *only* for `search()` because the GitHub Contents API
  has no free-text package name search. winget.run data is from early 2023 —
  treat search results as suggestions, then call `get_package` for the real
  manifest from GitHub.

### Chocolatey (`adapters/chocolatey_adapter.py`)
- The OData v2 API serves **Atom XML only** (no JSON). We parse with stdlib
  `xml.etree.ElementTree`.
- The feed is sometimes truncated mid-stream (server emits a stray
  `<m:error>Object reference not set...</m:error>` after the last `<entry>`).
  `_parse_feed` falls back to regex-extracting `<entry>...</entry>` blocks and
  re-wrapping them in a synthetic feed.
- `Search()` requires parameters in this exact set and order:
  `searchTerm='vlc' & $filter=IsLatestVersion eq true & $skip=0 & $top=N & includePrerelease=false`.
  Strict ordering matters — the server otherwise returns "Bad Request - Error in query syntax."
- `substringof(...)` filters are unreliable on the server; we use exact
  `tolower(Id) eq '...'` lookups and `IsLatestVersion eq true` for the latest
  version, with lexicographic `$orderby=Version desc` re-sorted client-side by
  semver tuple (because OData's `Version desc` mis-sorts `9.0.0` > `26.1.0`).
- The download URL is the `.nupkg` zip (chocolateyInstall.ps1 + tools), **not
  the raw installer**. `installer_type` is labeled `"nupkg"`. The SHA512
  (`PackageHash`, base64) is preserved in `raw_data.package_hash`; `sha256` is
  `None` since Chocolatey uses SHA512.
- `Dependencies` field format: `id1:[ver1]:|id2:[ver2]:|...`

### silentinstallhq (`adapters/sihq_adapter.py`)
- Delegates to a separately-deployed `mcp-server-silentinstallhq` MCP server via
  the MCP Python SDK `streamablehttp_client`. Used only as a fallback by
  `get_silent_switches` when a winget manifest carries no Silent switch.
- All failures are caught and surfaced as `None` (graceful degradation). If the
  SIHQ endpoint is unset or unreachable, callers only get manifest switches.

## Caching

- `CacheStore` (SQLite, WAL mode) keeps both raw HTTP responses (text + JSON)
  and normalized `PackageMetadata` JSON, keyed by source + endpoint shape.
- TTL is server-wide via `APPCATALOG_CACHE_TTL_HOURS` (default 6h).
- Cache keys are stable URL-derived strings. `HttpClient._json_cache_key`
  hashes query params so different `take=`/`partialMatch=` values don't
  collide.
- `purge_expired()` is available but not scheduled; the cache grows lazily.
  Operators can `DELETE FROM cache` or `rm data/cache.sqlite` for full reset.

## Settings

`Settings` (env prefix `APPCATALOG_`) lives in `config.py`. Notable overrides:
- `APPCATALOG_WINGET_API`: `auto` (default, GitHub primary + winget.run
  fallback for search), `github` only, or `winget.run` only.
- `GITHUB_TOKEN`: strongly recommended. Read via `os.getenv` (no prefix).
- `APPCATALOG_SIHQ_URL`: endpoint of the SIHQ MCP server. Empty = disabled.
- `APPCATALOG_REQUEST_DELAY_SECONDS`: shared global pacing (default 0.5s).

## Testing guidance

- Unit tests must pass **offline**. They use real fixture files under
  `tests/fixtures/` patched into the HTTP client via `unittest.mock.AsyncMock`.
  Refresh fixtures with the snippet below when the upstream schemas change.
- Integration tests (`@pytest.mark.integration`) hit live APIs and are skipped by
  default; run with `uv run pytest -m integration`.
- Cache tests use `tmp_path` SQLite files.
- When normalizers need a new field, add an assertion to
  `test_winget_adapter.py::test_normalize_full_manifest_to_package_metadata`
  (or the chocolatey equivalent) BEFORE editing the adapter, then run
  `uv run pytest -m "not integration" -x` to drive the fix.

### Refreshing test fixtures

```python
import urllib.request
def fetch(u, accept='application/atom+xml,application/xml', ua='mcp-server-appcatalog/0.1.0'):
    r = urllib.request.Request(u, headers={'Accept':accept,'User-Agent':ua})
    return urllib.request.urlopen(r, timeout=30).read().decode('utf-8')

open('tests/fixtures/winget_github_dir_Google.Chrome.json','w',encoding='utf-8') \
  .write(fetch('https://api.github.com/repos/microsoft/winget-pkgs/contents/manifests/g/Google/Chrome','application/vnd.github+json'))
open('tests/fixtures/choco_search_vlc.xml','w',encoding='utf-8') \
  .write(fetch('https://community.chocolatey.org/api/v2/Search()?searchTerm=%27vlc%27&$filter=IsLatestVersion%20eq%20true&$skip=0&$top=5&includePrerelease=false'))
```

## Packaging-agent integration

This server is designed to be paired with an Intune/PSADT/winget packaging
agent. Typical flow:

1. `search_packages("Chrome")` → find the winget id `Google.Chrome`.
2. `get_installer_metadata("Google.Chrome")` → direct MSI URLs, SHA256,
   `ProductCode`, `UpgradeCode`, arch per installer.
3. `get_silent_switches("Google.Chrome")` → confirm `/quiet /norestart`
   (MSI defaults flow as `None` from manifests — fall back to SIHQ if needed).
4. `compare_sources("7zip.7zip")` → decide winget vs Chocolatey for packaging.

Prompt for a packaging agent:

```text
Query appcatalog-mcp for "7-Zip". Use get_installer_metadata for the winget
manifest to retrieve SHA256 + ProductCode, then get_silent_switches to confirm
silent install. Use those values to build an Intune-ready PSADT package.
```
