# Source ground truth

Verified live endpoint shapes for every source this server federates, plus
candidate future sources. **Regenerate with `uv run python scripts/probe_sources.py`
whenever an upstream schema changes** and update the relevant adapter + this file
together.

Last verified: **2026-06-17**.

> The probe script is standalone (httpx only) and writes nothing. It prints HTTP
> status, content-type, rate-limit headers, and a shape sample for each source.

---

## Active sources (wired into the server)

### winget — GitHub Contents API (primary) + winget.run (search fallback)

- **GitHub Contents API**: `GET https://api.github.com/repos/microsoft/winget-pkgs/contents/manifests/{first}/{Publisher}/{Package}`
  returns a directory listing of **version folders** (plus channel/scope subdirs
  like `Preview`). Confirmed live for `Microsoft.PowerShell` → 90+ version dirs +
  `Preview`. Manifest triplet per version:
  `manifests/m/Microsoft/PowerShell/{ver}/Microsoft.PowerShell.{installer,locale.*,}.yaml`.
- **Rate limit**: anonymous `x-ratelimit-remaining` observed at 57/60. Set
  `GITHUB_TOKEN` for 5000/hr. The probe prints the remaining count.
- **winget.run** (`https://api.winget.run/v2/packages?query=...&take=N`): JSON,
  shape `{Packages: [...], Total}`. Data is **frozen at early-2023** — used
  ONLY for free-text `search()`, never as a version source of truth. Confirmed
  live, returns `Microsoft.PowerShell.Preview` etc.

### Chocolatey — community.chocolatey.org OData v2 (Atom XML)

- `GET /api/v2/Packages()?$filter=tolower(Id) eq '7zip' and IsLatestVersion eq true&$top=1`
  → `content-type: application/atom+xml`. Confirmed `7zip` `26.1.0` live.
- Atom XML only (no JSON). Parsed with stdlib `xml.etree.ElementTree`; the feed
  is sometimes truncated mid-stream so `_parse_feed` regex-recovers `<entry>` blocks.
- The download URL is the `.nupkg` zip, not the raw installer — real per-arch
  URLs + SHA256 come from parsing `tools/chocolateyInstall.ps1` inside the zip.

### Evergreen — evergreen-api.stealthpuppy.com

- `GET /apps` → JSON `list[553] of {Name, Application, Link}`. Confirmed live.
- `GET /app/{Name}` → JSON `list of {Version, Date, Channel, Release, Expiry,
  SHA256, Size, Architecture, Type, URI}`. Confirmed for `MicrosoftEdge` (10 rows).
- **Caveats proven by probe**: `SHA256` is upper-case in Edge rows (adapter does
  case-insensitive lookup); `Date` is ISO `2026-06-16T17:58:00` for Edge but
  DD/MM/YYYY for other apps (adapter handles both); endpoint names are
  **case-sensitive**. No free-text search server-side — fuzzy-match `/apps` client-side.
- The historic `api.evergreen.stealthpuppy.com` base from the original spec is
  **dead**; the live base is `evergreen-api.stealthpuppy.com`.

### Silent Install HQ — MCP delegation (not scraped here)

- This server delegates over MCP (`APPCATALOG_SIHQ_URL`) to a separately deployed
  `mcp-server-silentinstallhq`, rather than lifting its scraper in. Keeps
  robots.txt/scraper concerns in that server. Used only as a `get_silent_switches`
  fallback. No live endpoint shape to probe here.

---

## Candidate future sources (probed, not yet wired)

### Scoop — raw GitHub JSON manifests ✅ easiest next adapter

- `GET https://raw.githubusercontent.com/ScoopInstaller/Main/master/bucket/{name}.json`.
  Confirmed `7zip.json` (200) and `git.json` (200).
- Shape: `{version, description, homepage, license, architecture: {64bit, 32bit,
  arm64}, ...}`. Each arch carries `url` + **`hash` (already SHA256)** — e.g.
  `7zip` 64bit → `7z2601-x64.msi` + sha256. No download needed for the hash.
- **Bucket caveat proven by probe**: availability is bucket-specific. `7zip`/`git`
  are in `Main`; `googlechrome.json` 404s in Main (lives in the `Extras` bucket).
  A Scoop adapter must search across buckets (Main, Extras, Versions, etc.).

### Repology — repology.org/api/v1/project/{name}

- `GET /api/v1/project/firefox` → JSON `list of {repo, subrepo, srcname, binname,
  visiblename, version, origversion, status, summary, licenses}`. Confirmed (3643
  rows for firefox — huge, cross-ecosystem). Useful only for "latest version
  anywhere" cross-referencing in `compare`. **No installers or hashes.** Response
  is very large; would need tight filtering before use.

### OSV.dev — api.osv.dev/v1/query ⚠️ assumption does NOT hold

- `POST /v1/query` with `{"package": {"name": "7-Zip", "ecosystem": "Windows"}}`
  → **400 `Invalid ecosystem.`** Dropping the ecosystem → 400 `Invalid query.`
- **Finding**: OSV has no `Windows` desktop-app ecosystem. The original spec's
  assumption that OSV can map "a Windows software identifier + version" to CVEs
  is **not directly supported**. A CVE-annotation feature would need a different
  strategy (CPE matching against NVD, or vendor advisories) — not a thin OSV
  package query. Do not build the OSV adapter against the assumed shape.
