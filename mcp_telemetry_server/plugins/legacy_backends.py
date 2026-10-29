"""ELK / Splunk source plugin skeletons (Prometheus is implemented in prometheus.py).

These replace the old RCA-side stubs in providers/adapters/. The RCA pod
now only speaks MCP, so backend integrations (and their credentials) live in
the telemetry server as source plugins.
"""

from __future__ import annotations

from mcp_telemetry_server.plugins.base import SkeletonPlugin, register_plugin


@register_plugin
class ElkPlugin(SkeletonPlugin):
    """Elasticsearch/OpenSearch log search. Emits signal_type=event,
    evidence_ref = "<index>/<_id>"."""

    name = "elk"
    domains = frozenset({"logs"})
    needs_cloud_credentials = False


@register_plugin
class SplunkPlugin(SkeletonPlugin):
    """Splunk search jobs. Emits signal_type=event, evidence_ref =
    "<sid>/<_cd>"."""

    name = "splunk"
    domains = frozenset({"logs"})
    needs_cloud_credentials = False
