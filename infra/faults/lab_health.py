#!/usr/bin/env python3
"""Is the lab healthy again? Exit 0 when it is, 1 when not, and print why.

    lab_health.py --since <epoch seconds> [--samples 2] [--region us-east-1]

Healthy means both of these:
  - no netcortex-lab-* CloudWatch alarm is in ALARM
  - the key signals are clean in each of the last `--samples` one-minute datapoints
    after `--since` (the revert): every flow's SuccessRate >= 95, every BGP peer
    up (BgpPeerState = 1) and every VPN tunnel up (TunnelState = 1)

A series with no datapoint after `--since` is not healthy yet: when a fault takes
the metrics dark (the NAT faults), recovery means they are back. Series silent for
the 45 minutes before the revert belong to an earlier lab build and are skipped. drill.sh uses this
instead of a fixed cooldown before the next fault.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import boto3

# A series counts only if it reported in this long before the revert.
ACTIVE = timedelta(minutes=45)

CHECKS = [
    # namespace, metric, dimension, statistic, healthy(value)
    ("NetCortexLab/Traffic", "SuccessRate", "Flow", "Minimum", lambda v: v >= 95),
    ("NetCortexLab/Router", "BgpPeerState", "Neighbor", "Minimum", lambda v: v >= 1),
    ("AWS/VPN", "TunnelState", "TunnelIpAddress", "Minimum", lambda v: v >= 1),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=float, required=True)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    since = datetime.fromtimestamp(args.since, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    cw = boto3.client("cloudwatch", region_name=args.region)

    problems: list[str] = []
    alarms = cw.describe_alarms(AlarmNamePrefix="netcortex-lab-", StateValue="ALARM")["MetricAlarms"]
    problems += [f"alarm {a['AlarmName']}" for a in alarms]

    queries, labels = [], {}
    for namespace, metric, dim, stat, _ in CHECKS:
        listed = cw.list_metrics(Namespace=namespace, MetricName=metric, RecentlyActive="PT3H")["Metrics"]
        for m in listed:
            dims = [d for d in m["Dimensions"] if d["Name"] == dim]
            if len(dims) != len(m["Dimensions"]) or not dims:
                continue
            qid = f"q{len(queries)}"
            labels[qid] = (metric, dims[0]["Value"])
            queries.append({"Id": qid, "MetricStat": {"Metric": m, "Period": 60, "Stat": stat}, "ReturnData": True})
    healthy_fn = {metric: fn for _, metric, _, _, fn in CHECKS}
    for i in range(0, len(queries), 500):
        result = cw.get_metric_data(MetricDataQueries=queries[i:i + 500], StartTime=since - ACTIVE,
                                    EndTime=now + timedelta(minutes=1), ScanBy="TimestampDescending")
        for r in result["MetricDataResults"]:
            metric, who = labels[r["Id"]]
            if not r["Timestamps"]:
                continue  # a series of an earlier lab build (old tunnel IPs), not this one
            values = [v for t, v in zip(r["Timestamps"], r["Values"]) if t >= since][: args.samples]
            if len(values) < args.samples:
                problems.append(f"{metric} {who}: {len(values)}/{args.samples} datapoints since revert")
            elif not all(healthy_fn[metric](v) for v in values):
                problems.append(f"{metric} {who}: {values}")
    print(json.dumps({"healthy": not problems, "problems": problems[:20]}))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
