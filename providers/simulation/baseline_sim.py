from __future__ import annotations

from datetime import datetime, timezone

from models.schemas import EntityBaseline
from providers.base import BaselineProvider


class SimulationBaselineProvider(BaselineProvider):
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self._baselines: dict[tuple[str, str], EntityBaseline] = {
            ("region:us-east", "error_rate"): EntityBaseline(
                entity_key="region:us-east",
                metric="error_rate",
                mean=0.7,
                std_dev=0.25,
                sample_count=500,
                last_updated=now,
            ),
            ("region:us-east", "packet_loss"): EntityBaseline(
                entity_key="region:us-east",
                metric="packet_loss",
                mean=0.35,
                std_dev=0.2,
                sample_count=500,
                last_updated=now,
            ),
            ("region:us-east", "throughput_gbps"): EntityBaseline(
                entity_key="region:us-east",
                metric="throughput_gbps",
                mean=1.0,
                std_dev=0.18,
                sample_count=500,
                last_updated=now,
            ),
            ("switch:A", "throughput_gbps"): EntityBaseline(
                entity_key="switch:A",
                metric="throughput_gbps",
                mean=1.0,
                std_dev=0.08,
                sample_count=420,
                last_updated=now,
            ),
            ("switch:B", "throughput_gbps"): EntityBaseline(
                entity_key="switch:B",
                metric="throughput_gbps",
                mean=1.0,
                std_dev=0.08,
                sample_count=420,
                last_updated=now,
            ),
            ("switch:C", "error_rate"): EntityBaseline(
                entity_key="switch:C",
                metric="error_rate",
                mean=0.7,
                std_dev=0.25,
                sample_count=420,
                last_updated=now,
            ),
            ("switch:C", "packet_loss"): EntityBaseline(
                entity_key="switch:C",
                metric="packet_loss",
                mean=0.4,
                std_dev=0.2,
                sample_count=420,
                last_updated=now,
            ),
            ("switch:C", "throughput_gbps"): EntityBaseline(
                entity_key="switch:C",
                metric="throughput_gbps",
                mean=1.0,
                std_dev=0.1,
                sample_count=420,
                last_updated=now,
            ),
            ("switch:D", "throughput_gbps"): EntityBaseline(
                entity_key="switch:D",
                metric="throughput_gbps",
                mean=1.0,
                std_dev=0.08,
                sample_count=420,
                last_updated=now,
            ),
            ("component:Switch-C eth0/1", "change_count"): EntityBaseline(
                entity_key="component:Switch-C eth0/1",
                metric="change_count",
                mean=0.2,
                std_dev=0.25,
                sample_count=180,
                last_updated=now,
            ),
            ("component:api-gw", "change_count"): EntityBaseline(
                entity_key="component:api-gw",
                metric="change_count",
                mean=0.4,
                std_dev=0.3,
                sample_count=180,
                last_updated=now,
            ),
            ("component:CORE-01", "change_count"): EntityBaseline(
                entity_key="component:CORE-01",
                metric="change_count",
                mean=0.25,
                std_dev=0.25,
                sample_count=180,
                last_updated=now,
            ),
            ("region:us-east", "change_count"): EntityBaseline(
                entity_key="region:us-east",
                metric="change_count",
                mean=0.3,
                std_dev=0.3,
                sample_count=240,
                last_updated=now,
            ),
        }

    def get_baseline(self, entity_key: str, metric: str) -> EntityBaseline | None:
        return self._baselines.get((entity_key, metric))
