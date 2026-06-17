"""CLI entry point for the app catalog MCP server."""

from __future__ import annotations

import argparse
import sys

from appcatalog_mcp.config import get_settings
from appcatalog_mcp.server import create_mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="appcatalog-mcp",
        description=(
            "MCP server aggregating winget, Chocolatey, and Silent Install HQ "
            "application metadata"
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=None,
        help="MCP transport (default: stdio, or APPCATALOG_TRANSPORT)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host for HTTP transports (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port for HTTP transports (default: 8010)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = get_settings()

    transport = args.transport or settings.transport
    if args.host is not None:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port

    mcp = create_mcp(settings)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    if transport == "sse":
        mcp.run(transport="sse")
        return

    if transport == "streamable-http":
        mcp.run(transport="streamable-http")
        return

    print(f"Unsupported transport: {transport}", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
