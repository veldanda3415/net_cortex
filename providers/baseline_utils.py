from __future__ import annotations

from models.schemas import EntityBaseline


def compute_z_score(value: float, baseline: EntityBaseline) -> float:
    if baseline.std_dev <= 0:
        return 0.0
    return abs(value - baseline.mean) / baseline.std_dev


def is_anomalous(value: float, baseline: EntityBaseline, z_threshold: float = 3.0) -> bool:
    if baseline.std_dev <= 0:
        return False
    return compute_z_score(value, baseline) > z_threshold
