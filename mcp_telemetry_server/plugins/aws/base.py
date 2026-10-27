"""Shared plumbing for the AWS source plugins: boto3 clients, threads, regions.

boto3 is imported lazily, the first time an enabled AWS plugin makes a call,
so a telemetry server (or the RCA pod's in-process one) that only runs
simulation/replay never needs it installed. The same pattern applies to any
other cloud's SDK.

Credentials come from the standard AWS chain. Set `profile:` to use a named
profile; for the lab that is a profile assuming the read-only
`netcortex-lab-netcortex` role (see config/telemetry.aws-lab.yaml). Every call
the plugins make is a read.
"""

from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime, timezone
from typing import Any

from contract import TelemetryQuery
from mcp_telemetry_server.plugins.base import SourcePlugin

AWS_REGION = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d+$")


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class AwsPlugin(SourcePlugin):
    """Base for plugins that read AWS APIs. Subclasses implement fetch()."""

    needs_cloud_credentials = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._profile = self.config.get("profile")
        self._fixed_region = self.config.get("region")
        if self._fixed_region and not AWS_REGION.match(self._fixed_region):
            raise ValueError(f"ConfigValidationError: sources.{self.name}.region '{self._fixed_region}' is not an AWS region")
        self._session = None
        self._clients: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def region_for(self, query: TelemetryQuery) -> str:
        region = self._fixed_region or query.region
        if not AWS_REGION.match(region):
            raise ValueError(f"region '{region}' is not an AWS region; set sources.{self.name}.region")
        return region

    def client(self, service: str, region: str):
        key = (service, region)
        with self._lock:
            if key not in self._clients:
                import boto3
                from botocore.config import Config

                if self._session is None:
                    self._session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
                self._clients[key] = self._session.client(
                    service, region_name=region,
                    config=Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=5, read_timeout=10),
                )
            return self._clients[key]

    async def call(self, service: str, region: str, operation: str, **params: Any) -> dict[str, Any]:
        client = self.client(service, region)
        return await asyncio.to_thread(getattr(client, operation), **params)

    async def paginate(self, service: str, region: str, operation: str, *, max_pages: int, **params: Any) -> tuple[list[dict[str, Any]], bool]:
        """All pages of a paginated call, up to max_pages. Returns (pages, truncated)."""
        client = self.client(service, region)

        def run() -> tuple[list[dict[str, Any]], bool]:
            pages: list[dict[str, Any]] = []
            for page in client.get_paginator(operation).paginate(**params):
                pages.append(page)
                if len(pages) >= max_pages:
                    return pages, bool(page.get("NextToken") or page.get("nextToken"))
            return pages, False

        return await asyncio.to_thread(run)

    def health(self) -> dict[str, Any]:
        try:
            import boto3  # noqa: F401
        except ImportError:
            return {"status": "down", "error": "boto3 is not installed"}
        return {"status": "ok", "profile": self._profile or "default chain", "region": self._fixed_region or "per query"}
