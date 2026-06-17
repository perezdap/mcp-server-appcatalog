"""Adapter package: per-source package metadata backends."""

from appcatalog_mcp.adapters.base import (
    PackageAdapter,
    PackageNotFoundError,
)
from appcatalog_mcp.adapters.chocolatey_adapter import ChocolateyAdapter
from appcatalog_mcp.adapters.evergreen_adapter import EvergreenAdapter
from appcatalog_mcp.adapters.sihq_adapter import SihqAdapter
from appcatalog_mcp.adapters.winget_adapter import WingetAdapter

__all__ = [
    "PackageAdapter",
    "PackageNotFoundError",
    "WingetAdapter",
    "ChocolateyAdapter",
    "EvergreenAdapter",
    "SihqAdapter",
]
