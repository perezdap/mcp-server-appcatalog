"""Probe live upstream endpoints and print their response shapes.

Ground-truth verification tool: run this before trusting any adapter against an
assumed API shape. It hits each source the server federates (plus a couple of
candidate future sources) with the project's polite User-Agent, prints the HTTP
status, content-type, and a small shape sample, and never writes anything.

Usage:
    uv run python scripts/probe_sources.py
    uv run python scripts/probe_sources.py --only winget,chocolatey

Output is intended to be pasted/summarized into docs/sources.md whenever an
upstream schema changes. This script is standalone (only depends on httpx) so
it can be run even if the package import graph is mid-refactor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import httpx

UA = "mcp-server-appcatalog/0.1.0 (+https://github.com/perezdap/mcp-server-appcatalog)"
TIMEOUT = httpx.Timeout(30.0)


def _head(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n... [+{len(text) - limit} bytes]"


def _keys(obj: object, limit: int = 25) -> str:
    if isinstance(obj, dict):
        return ", ".join(list(obj.keys())[:limit])
    if isinstance(obj, list):
        first = obj[0] if obj else None
        inner = _keys(first) if isinstance(first, dict) else type(first).__name__
        return f"list[{len(obj)}] of {{{inner}}}"
    return type(obj).__name__


async def _get(
    client: httpx.AsyncClient, label: str, url: str, *, accept: str | None = None
) -> None:
    headers = {"Accept": accept} if accept else None
    print(f"\n{'=' * 78}\n{label}\nGET {url}")
    try:
        r = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        print(f"  ERROR: {exc}")
        return
    ctype = r.headers.get("content-type", "?")
    print(f"  status={r.status_code} content-type={ctype}")
    # Surface rate-limit headers when present (GitHub / winget.run).
    for h in ("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"):
        if h in r.headers:
            print(f"  {h}={r.headers[h]}")
    if r.status_code >= 400:
        print(f"  body[head]:\n{_head(r.text, 300)}")
        return
    if "json" in ctype:
        try:
            data = r.json()
            print(f"  json shape: {_keys(data)}")
            print(f"  sample:\n{_head(json.dumps(data, indent=2))}")
            return
        except ValueError:
            pass
    print(f"  text[head]:\n{_head(r.text)}")


async def probe_winget(client: httpx.AsyncClient) -> None:
    # GitHub Contents API directory layout (Microsoft.PowerShell as test case).
    gh_headers = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        gh_headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    print(f"\n{'=' * 78}\nwinget-pkgs GitHub Contents API (Microsoft.PowerShell)")
    url = (
        "https://api.github.com/repos/microsoft/winget-pkgs/contents/"
        "manifests/m/Microsoft/PowerShell"
    )
    print(f"GET {url}")
    try:
        r = await client.get(url, headers=gh_headers)
        remaining = r.headers.get("x-ratelimit-remaining")
        print(f"  status={r.status_code} ratelimit-remaining={remaining}")
        if r.status_code < 400:
            entries = r.json()
            names = [e.get("name") for e in entries if isinstance(e, dict)]
            print(f"  version/dir entries: {names}")
    except httpx.HTTPError as exc:
        print(f"  ERROR: {exc}")

    await _get(
        client,
        "winget.run search API (free-text fallback, stale-2023 data)",
        "https://api.winget.run/v2/packages?query=powershell&take=3",
        accept="application/json",
    )


async def probe_chocolatey(client: httpx.AsyncClient) -> None:
    await _get(
        client,
        "Chocolatey OData v2 Packages() filter (Id eq)",
        "https://community.chocolatey.org/api/v2/Packages()?"
        "$filter=tolower(Id)%20eq%20%277zip%27%20and%20IsLatestVersion%20eq%20true&$top=1",
        accept="application/atom+xml,application/xml",
    )


async def probe_scoop(client: httpx.AsyncClient) -> None:
    # Scoop Main bucket raw JSON manifest (candidate future source).
    # NOTE: package availability is bucket-specific. 7zip/git live in Main;
    # browser apps like googlechrome live in the Extras bucket.
    await _get(
        client,
        "Scoop Main bucket JSON manifest (7zip)",
        "https://raw.githubusercontent.com/ScoopInstaller/Main/master/bucket/7zip.json",
        accept="application/json",
    )


async def probe_evergreen(client: httpx.AsyncClient) -> None:
    await _get(
        client,
        "Evergreen /apps (supported app list)",
        "https://evergreen-api.stealthpuppy.com/apps",
        accept="application/json",
    )
    await _get(
        client,
        "Evergreen /app/{Name} (installer rows)",
        "https://evergreen-api.stealthpuppy.com/app/MicrosoftEdge",
        accept="application/json",
    )


async def probe_repology(client: httpx.AsyncClient) -> None:
    await _get(
        client,
        "Repology project API (candidate future source)",
        "https://repology.org/api/v1/project/firefox",
        accept="application/json",
    )


async def probe_osv(client: httpx.AsyncClient) -> None:
    print(f"\n{'=' * 78}\nOSV.dev query API (candidate future source)\nPOST https://api.osv.dev/v1/query")
    try:
        r = await client.post(
            "https://api.osv.dev/v1/query",
            json={"package": {"name": "7-Zip", "ecosystem": "Windows"}},
        )
        print(f"  status={r.status_code} content-type={r.headers.get('content-type')}")
        print(f"  body[head]:\n{_head(r.text, 400)}")
    except httpx.HTTPError as exc:
        print(f"  ERROR: {exc}")


PROBES = {
    "winget": probe_winget,
    "chocolatey": probe_chocolatey,
    "scoop": probe_scoop,
    "evergreen": probe_evergreen,
    "repology": probe_repology,
    "osv": probe_osv,
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Probe upstream source endpoints.")
    parser.add_argument(
        "--only",
        default=None,
        help=f"Comma list of sources to probe (default: all). Choices: {','.join(PROBES)}",
    )
    args = parser.parse_args()
    selected = args.only.split(",") if args.only else list(PROBES)

    async with httpx.AsyncClient(
        headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True
    ) as client:
        for name in selected:
            name = name.strip().lower()
            probe = PROBES.get(name)
            if probe is None:
                print(f"\n[skip] unknown source {name!r}")
                continue
            await probe(client)


if __name__ == "__main__":
    asyncio.run(main())
