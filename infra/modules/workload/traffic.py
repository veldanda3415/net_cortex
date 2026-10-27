#!/usr/bin/env python3
"""NetCortex lab traffic agent.

Runs each configured flow in its own thread and publishes one-minute aggregates
to CloudWatch (dimension Flow=<name>):

  http  : Requests, Failures, SuccessRate (%), LatencyMs (statistic set, successes only)
  iperf : Requests, Failures, SuccessRate (%), ThroughputMbps, LossPercent, JitterMs

Every failure is also logged to the journal with its reason, which is what an
operator sees first when a fault is injected.
"""

import json
import subprocess
import sys
import threading
import time
import urllib.request
from collections import defaultdict

import boto3

PUBLISH_SECONDS = 60


class Window:
    """Per-flow samples for the current publish window."""

    def __init__(self):
        self.lock = threading.Lock()
        self.counts = defaultdict(lambda: {"requests": 0, "failures": 0})
        self.samples = defaultdict(lambda: defaultdict(list))

    def record(self, flow, ok, **values):
        with self.lock:
            self.counts[flow]["requests"] += 1
            if not ok:
                self.counts[flow]["failures"] += 1
            for metric, value in values.items():
                self.samples[flow][metric].append(value)

    def drain(self):
        with self.lock:
            counts, samples = self.counts, self.samples
            self.counts = defaultdict(lambda: {"requests": 0, "failures": 0})
            self.samples = defaultdict(lambda: defaultdict(list))
        return counts, samples


UNITS = {
    "LatencyMs": "Milliseconds",
    "ThroughputMbps": "None",
    "LossPercent": "Percent",
    "JitterMs": "Milliseconds",
}


def log(msg):
    print(msg, flush=True)


def http_flow(flow, window):
    timeout = flow["timeout"]
    while True:
        start = time.monotonic()
        try:
            with urllib.request.urlopen(flow["target"], timeout=timeout) as resp:
                resp.read()
                ok = resp.status == 200
                reason = f"HTTP {resp.status}"
        except Exception as exc:  # timeouts, resets, DNS failures: all count as failures
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - start
        if ok:
            window.record(flow["name"], True, LatencyMs=elapsed * 1000)
        else:
            window.record(flow["name"], False)
            log(f"FAIL flow={flow['name']} target={flow['target']} after={elapsed:.1f}s reason={reason}")
        time.sleep(max(0, flow["interval"] - elapsed))


def iperf_flow(flow, window):
    # UDP at a fixed rate gives loss and jitter directly. 1200-byte datagrams stay
    # under the 1399-byte tunnel MTU, so this flow measures loss, not fragmentation.
    cmd = [
        "iperf3", "-c", flow["target"], "-u",
        "-b", f"{flow['bitrate_mbps']}M", "-l", "1200",
        "-t", str(flow["duration"]), "--json",
    ]
    while True:
        start = time.monotonic()
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=flow["duration"] + 20)
            result = json.loads(out.stdout or "{}")
            if "error" in result or out.returncode != 0:
                raise RuntimeError(result.get("error") or out.stderr.strip() or f"exit {out.returncode}")
            s = result["end"]["sum"]
            window.record(
                flow["name"], True,
                ThroughputMbps=s["bits_per_second"] / 1e6,
                LossPercent=s["lost_percent"],
                JitterMs=s["jitter_ms"],
            )
        except Exception as exc:
            window.record(flow["name"], False)
            log(f"FAIL flow={flow['name']} target={flow['target']} reason={type(exc).__name__}: {exc}")
        time.sleep(max(0, flow["interval"] - (time.monotonic() - start)))


def stat_set(values):
    return {"SampleCount": len(values), "Sum": sum(values), "Minimum": min(values), "Maximum": max(values)}


def publish(cfg, window):
    cw = boto3.client("cloudwatch", region_name=cfg["region"])
    while True:
        time.sleep(PUBLISH_SECONDS)
        counts, samples = window.drain()
        data = []
        for flow, c in counts.items():
            dims = [{"Name": "Flow", "Value": flow}]
            ok = c["requests"] - c["failures"]
            data += [
                {"MetricName": "Requests", "Dimensions": dims, "Value": c["requests"], "Unit": "Count"},
                {"MetricName": "Failures", "Dimensions": dims, "Value": c["failures"], "Unit": "Count"},
                {"MetricName": "SuccessRate", "Dimensions": dims, "Value": 100.0 * ok / c["requests"], "Unit": "Percent"},
            ]
            for metric, values in samples[flow].items():
                data.append({"MetricName": metric, "Dimensions": dims, "StatisticValues": stat_set(values), "Unit": UNITS[metric]})
        # PutMetricData takes up to 1000 metrics per call; a lab host sends a few dozen.
        if data:
            try:
                cw.put_metric_data(Namespace=cfg["namespace"], MetricData=data)
            except Exception as exc:  # e.g. egress down: drop this window, keep going
                log(f"PUBLISH FAILED: {type(exc).__name__}: {exc}")


def main():
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    window = Window()
    runners = {"http": http_flow, "iperf": iperf_flow}
    for flow in cfg["flows"]:
        threading.Thread(target=runners[flow["kind"]], args=(flow, window), daemon=True).start()
        log(f"started flow={flow['name']} kind={flow['kind']} target={flow['target']} every={flow['interval']}s")
    publish(cfg, window)


if __name__ == "__main__":
    main()
