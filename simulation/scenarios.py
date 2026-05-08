from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models.schemas import (
    ConfigChange,
    IncidentRequest,
    LogEvent,
    MetricSnapshot,
    RoutingEvent,
    ScenarioDataBundle,
)


def _t(minutes_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def _bundle(
    scenario_id: int,
    name: str,
    metrics: list[MetricSnapshot],
    logs: list[LogEvent],
    routing: list[RoutingEvent],
    config: list[ConfigChange],
    description: str,
    keywords: list[str],
) -> ScenarioDataBundle:
    return ScenarioDataBundle(
        scenario_id=scenario_id,
        scenario_name=name,
        metrics_data=metrics,
        log_events=logs,
        routing_events=routing,
        config_changes=config,
        incident_request=IncidentRequest(
            scenario_id=scenario_id,
            description=description,
            region="us-east",
            severity="high",
            source_system="simulation",
        ),
        expected_rca_keywords=keywords,
    )


def build_scenario_1() -> ScenarioDataBundle:
    metrics = [
        MetricSnapshot(timestamp=_t(3), region="us-east", error_rate=0.7, packet_loss=0.3, throughput_gbps=1.0, latency_ms=50, tags={"switch": "A"}),
        MetricSnapshot(timestamp=_t(3), region="us-east", error_rate=0.7, packet_loss=0.3, throughput_gbps=1.0, latency_ms=49, tags={"switch": "B"}),
        MetricSnapshot(timestamp=_t(3), region="us-east", error_rate=2.5, packet_loss=4.8, throughput_gbps=0.5, latency_ms=98, tags={"switch": "C"}),
        MetricSnapshot(timestamp=_t(3), region="us-east", error_rate=0.6, packet_loss=0.2, throughput_gbps=1.0, latency_ms=50, tags={"switch": "D"}),
    ]
    logs = [LogEvent(timestamp=_t(5), level="WARN", service="policy-engine", message="Policy QOS-GW-V3 applied")]
    config = [
        ConfigChange(
            timestamp=_t(5),
            component="Switch-C eth0/1",
            change_type="bandwidth_limit",
            before={"capacity_gbps": 10},
            after={"capacity_gbps": 5},
        )
    ]
    return _bundle(1, "Port Capacity Reduction", metrics, logs, [], config, "Gateway PLR baseline shift", ["port c", "10g", "5g", "policy"])


def build_scenario_2() -> ScenarioDataBundle:
    metrics = [
        MetricSnapshot(timestamp=_t(2), region="us-east", error_rate=0.6, packet_loss=2.8, throughput_gbps=0.9, latency_ms=75, tags={"dst_prefix": "192.0.2.0/24"}),
        MetricSnapshot(timestamp=_t(2), region="us-east", error_rate=0.5, packet_loss=0.2, throughput_gbps=1.0, latency_ms=51, tags={"dst_prefix": "198.51.100.0/24"}),
    ]
    logs = [LogEvent(timestamp=_t(3), level="WARN", service="bgp", message="BGP session flap - peer 203.0.113.1")]
    routing = [RoutingEvent(timestamp=_t(3), region="us-east", path_id="192.0.2.0/24", change_type="bgp_update", details="Peer withdrew prefix, rerouted via backup +3 hops")]
    return _bundle(2, "BGP Route Withdrawal", metrics, logs, routing, [], "Selective prefix PLR spike", ["bgp", "withdraw", "192.0.2.0/24"])


def build_scenario_3() -> ScenarioDataBundle:
    metrics = [
        MetricSnapshot(timestamp=_t(4), region="us-east", error_rate=0.5, packet_loss=0.2, throughput_gbps=1.0, latency_ms=250, tags={"dscp": "EF"}),
        MetricSnapshot(timestamp=_t(4), region="us-east", error_rate=0.5, packet_loss=0.2, throughput_gbps=1.0, latency_ms=48, tags={"dscp": "BE"}),
    ]
    logs = [LogEvent(timestamp=_t(8), level="WARN", service="qos", message="Policy QOS-CORE-V2 applied")]
    config = [ConfigChange(timestamp=_t(8), component="QOS-CORE-V2", change_type="policy_update", before={"ef_queue_pct": 30}, after={"ef_queue_pct": 5})]
    return _bundle(3, "QoS Starvation", metrics, logs, [], config, "VoIP latency spike", ["qos", "ef", "queue"])


def build_scenario_4() -> ScenarioDataBundle:
    metrics = [MetricSnapshot(timestamp=_t(2), region="us-east", error_rate=1.2, packet_loss=0.6, throughput_gbps=10.0, latency_ms=55, tags={"lag": "LAG0"})]
    logs = [LogEvent(timestamp=_t(2), level="ERROR", service="lacp", message="LACP timeout on LAG0 member eth1/2")]
    return _bundle(4, "LAG Member Failure", metrics, logs, [], [], "Throughput halved on LAG0", ["lag0", "eth1/2", "lacp"])


def build_scenario_5() -> ScenarioDataBundle:
    metrics = [MetricSnapshot(timestamp=_t(6), region="us-east", error_rate=18.0, packet_loss=0.3, throughput_gbps=0.4, latency_ms=52, tags={"src": "10.20.0.0/16"})]
    logs = [LogEvent(timestamp=_t(6), level="ERROR", service="edge-fw", message="ACL EDGE-INBOUND-V4 deny src=10.20.0.0/16")]
    config = [ConfigChange(timestamp=_t(10), component="ACL EDGE-INBOUND-V4", change_type="policy_update", before={"rules": ["allow any"]}, after={"rules": ["allow any", "deny 10.20.0.0/16"]})]
    return _bundle(5, "ACL Blocking", metrics, logs, [], config, "Source subnet failures", ["acl", "deny", "10.20.0.0/16"])


def build_scenario_6() -> ScenarioDataBundle:
    metrics = [MetricSnapshot(timestamp=_t(5), region="us-east", error_rate=2.0, packet_loss=3.1, throughput_gbps=0.8, latency_ms=50, tags={"packet_size": ">1500"})]
    logs = [LogEvent(timestamp=_t(15), level="ERROR", service="icmp", message="Fragmentation needed, DF bit set - eth2/0")]
    config = [ConfigChange(timestamp=_t(15), component="eth2/0", change_type="policy_update", before={"mtu": 9000}, after={"mtu": 1500})]
    return _bundle(6, "MTU Mismatch", metrics, logs, [], config, "Large packet PLR", ["mtu", "1500", "fragmentation"])


def build_scenario_7() -> ScenarioDataBundle:
    metrics = [
        MetricSnapshot(timestamp=_t(20), region="us-east", error_rate=6.0, packet_loss=0.2, throughput_gbps=1.0, latency_ms=50, tags={"service": "A"}),
        MetricSnapshot(timestamp=_t(20), region="us-east", error_rate=6.2, packet_loss=0.2, throughput_gbps=1.0, latency_ms=50, tags={"service": "B"}),
    ]
    logs = [LogEvent(timestamp=_t(30), level="ERROR", service="auth", message="Kerberos validation failed - clock skew exceeds 5 minutes")]
    config = [ConfigChange(timestamp=_t(30), component="NTP", change_type="policy_update", before={"server": "10.0.0.1"}, after={"server": "pool.ntp.org"})]
    return _bundle(7, "NTP Drift", metrics, logs, [], config, "Distributed auth failures", ["ntp", "clock skew", "kerberos"])


def build_scenario_8() -> ScenarioDataBundle:
    metrics = [MetricSnapshot(timestamp=_t(10), region="us-east", error_rate=9.0, packet_loss=6.5, throughput_gbps=0.1, latency_ms=400, tags={"cpu_core": "95"})]
    logs = [LogEvent(timestamp=_t(20), level="ERROR", service="stp", message="STP TCN storm detected from SW-ACC-07")]
    config = [ConfigChange(timestamp=_t(20), component="SW-ACC-07", change_type="deployment", before={}, after={"stp_priority": 32768})]
    routing = [RoutingEvent(timestamp=_t(10), region="us-east", path_id="L2", change_type="congestion", details="L2 forwarding instability")]
    return _bundle(8, "STP Storm", metrics, logs, routing, config, "Broadcast storm symptoms", ["stp", "root", "broadcast storm"])


def build_scenario_9() -> ScenarioDataBundle:
    metrics = [MetricSnapshot(timestamp=_t(1), region="us-east", error_rate=8.1, packet_loss=12.0, throughput_gbps=0.3, latency_ms=280, tags={"tunnel": "TE-CORE-01"})]
    logs = [LogEvent(timestamp=_t(2), level="WARN", service="rsvp", message="TE-CORE-01 path teardown, no make-before-break")]
    routing = [RoutingEvent(timestamp=_t(2), region="us-east", path_id="TE-CORE-01", change_type="reroute", details="Path A-B-D to A-C-D, convergence 118s")]
    config = [ConfigChange(timestamp=_t(2), component="TE", change_type="policy_update", before={"make_before_break": True}, after={"make_before_break": False})]
    return _bundle(9, "TE Reoptimization", metrics, logs, routing, config, "Transient blackhole", ["te-core-01", "make-before-break", "blackhole"])


def build_scenario_10() -> ScenarioDataBundle:
    metrics = [
        MetricSnapshot(timestamp=_t(2), region="us-east", error_rate=4.2, packet_loss=5.1, throughput_gbps=0.45, latency_ms=120, tags={"switch": "Switch-C"}),
        MetricSnapshot(timestamp=_t(2), region="us-east", error_rate=0.4, packet_loss=0.2, throughput_gbps=1.1, latency_ms=49, tags={"switch": "Switch-A"}),
    ]
    return _bundle(
        10,
        "Conflict: Metrics Spike Without Config Change",
        metrics,
        [],
        [],
        [],
        "High error rate on Switch-C with throughput drop; verify if any config changes occurred",
        ["switch-c", "error_rate", "throughput", "no config changes", "conflict"],
    )


def build_scenario_11() -> ScenarioDataBundle:
    metrics = [
        MetricSnapshot(timestamp=_t(3), region="us-east", error_rate=3.4, packet_loss=6.8, throughput_gbps=0.42, latency_ms=140, tags={"uplink": "xe-0/0/7", "switch": "EDGE-22"}),
        MetricSnapshot(timestamp=_t(3), region="us-east", error_rate=0.5, packet_loss=0.2, throughput_gbps=1.0, latency_ms=50, tags={"uplink": "xe-0/0/8", "switch": "EDGE-23"}),
    ]
    logs = [
        LogEvent(timestamp=_t(4), level="ERROR", service="optic-monitor", message="CRC burst detected on EDGE-22 xe-0/0/7"),
        LogEvent(timestamp=_t(4), level="WARN", service="optic-monitor", message="Laser bias current drifting above baseline on EDGE-22"),
    ]
    return _bundle(
        11,
        "Optical CRC Burst",
        metrics,
        logs,
        [],
        [],
        "Intermittent packet loss and throughput collapse on EDGE-22 uplink xe-0/0/7",
        ["crc", "xe-0/0/7", "edge-22", "packet loss", "throughput"],
    )


def build_scenario_12() -> ScenarioDataBundle:
    metrics = [
        MetricSnapshot(timestamp=_t(2), region="us-east", error_rate=0.4, packet_loss=0.15, throughput_gbps=1.05, latency_ms=52, tags={"core": "R1"}),
        MetricSnapshot(timestamp=_t(2), region="us-east", error_rate=0.5, packet_loss=0.12, throughput_gbps=1.02, latency_ms=50, tags={"core": "R2"}),
    ]
    logs = [
        LogEvent(timestamp=_t(3), level="ERROR", service="control-plane", message="BGP update queue saturation on RR-1"),
        LogEvent(timestamp=_t(3), level="ERROR", service="control-plane", message="Route reflector CPU soft lockup detected"),
    ]
    routing = [
        RoutingEvent(timestamp=_t(3), region="us-east", path_id="RR-1", change_type="flap", details="Control-plane churn caused repeated path flap without data-plane loss"),
    ]
    return _bundle(
        12,
        "Control-Plane Churn",
        metrics,
        logs,
        routing,
        [],
        "Route reflector instability with path flaps but no major data-plane metric impact",
        ["control-plane", "rr-1", "flap", "bgp", "conflict"],
    )


def build_scenario_13() -> ScenarioDataBundle:
    """Full 4-domain corroboration: fiber-cut triggers reroute + emergency failover config."""
    metrics = [
        MetricSnapshot(timestamp=_t(5), region="us-east", error_rate=11.2, packet_loss=18.5, throughput_gbps=0.18, latency_ms=310, tags={"interface": "GigE0/0/1", "switch": "CORE-01"}),
        MetricSnapshot(timestamp=_t(5), region="us-east", error_rate=0.4, packet_loss=0.2, throughput_gbps=1.1, latency_ms=51, tags={"interface": "GigE0/0/2", "switch": "CORE-01"}),
    ]
    logs = [
        LogEvent(timestamp=_t(6), level="ERROR", service="ifmon", message="Interface GigE0/0/1 on CORE-01 transitioned to DOWN (physical loss of signal)"),
        LogEvent(timestamp=_t(6), level="FATAL", service="bfd", message="BFD session CORE-01 <-> CORE-02 via GigE0/0/1 declared DOWN after 3 missed hellos"),
    ]
    routing = [
        RoutingEvent(timestamp=_t(6), region="us-east", path_id="CORE-01-to-CORE-02", change_type="reroute", details="Primary GigE0/0/1 down; failover to backup path via CORE-03, convergence 4s"),
    ]
    config = [
        ConfigChange(
            timestamp=_t(5),
            component="CORE-01",
            change_type="rollback",
            before={"failover_policy": "manual"},
            after={"failover_policy": "automatic"},
        )
    ]
    return _bundle(
        13,
        "Fiber Cut Full Corroboration",
        metrics,
        logs,
        routing,
        config,
        "Packet loss spike and throughput collapse on CORE-01 GigE0/0/1; BFD down and reroute in progress",
        ["gige0/0/1", "core-01", "bfd", "reroute", "fiber"],
    )


def build_scenario_14() -> ScenarioDataBundle:
    """Full 4-domain corroboration: bad canary deployment saturates uplink and triggers congestion."""
    metrics = [
        MetricSnapshot(timestamp=_t(8), region="us-east", error_rate=5.8, packet_loss=7.4, throughput_gbps=0.55, latency_ms=190, tags={"service": "api-gw", "datacenter": "DC1"}),
        MetricSnapshot(timestamp=_t(8), region="us-east", error_rate=5.6, packet_loss=7.1, throughput_gbps=0.57, latency_ms=188, tags={"service": "api-gw", "datacenter": "DC2"}),
    ]
    logs = [
        LogEvent(timestamp=_t(9), level="ERROR", service="api-gw", message="Rate limiter triggered: upstream bandwidth exceeded 90% on DC1 and DC2"),
        LogEvent(timestamp=_t(9), level="ERROR", service="health-check", message="api-gw /healthz returning 503 - connection queue full"),
    ]
    routing = [
        RoutingEvent(timestamp=_t(9), region="us-east", path_id="api-gw-uplink", change_type="congestion", details="Uplink saturated due to canary traffic burst; load balancer rerouting 40% to secondary AZ"),
    ]
    config = [
        ConfigChange(
            timestamp=_t(15),
            component="api-gw",
            change_type="deployment",
            before={"version": "2.4.1", "max_conn": 5000},
            after={"version": "2.5.0-canary", "max_conn": 50000},
        )
    ]
    return _bundle(
        14,
        "Canary Deployment Saturation",
        metrics,
        logs,
        routing,
        config,
        "Packet loss and throughput drop on api-gw network path across DC1 and DC2; routing congestion following canary rollout",
        ["api-gw", "canary", "deployment", "congestion", "rate limiter"],
    )


SCENARIOS: dict[int, ScenarioDataBundle] = {
    1: build_scenario_1(),
    2: build_scenario_2(),
    3: build_scenario_3(),
    4: build_scenario_4(),
    5: build_scenario_5(),
    6: build_scenario_6(),
    7: build_scenario_7(),
    8: build_scenario_8(),
    9: build_scenario_9(),
    10: build_scenario_10(),
    11: build_scenario_11(),
    12: build_scenario_12(),
    13: build_scenario_13(),
    14: build_scenario_14(),
}
