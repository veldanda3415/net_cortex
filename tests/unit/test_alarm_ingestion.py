"""CloudWatch alarm events -> incidents, and the SQS -> /incidents bridge."""

from __future__ import annotations

import json

import boto3
import httpx
import pytest
from botocore.stub import Stubber

from ingestion.cloudwatch_alarms import incident_payload
from ingestion.incident_normalizer import normalize_incident
from ingestion.sqs_alarm_bridge import SqsAlarmBridge

QUEUE = "https://sqs.us-east-1.amazonaws.com/111111111111/netcortex-lab-alarms"


def _alarm(name: str, at: str, state: str = "ALARM", event_id: str | None = None, description: str = "") -> dict:
    return {
        "version": "0", "id": event_id or f"ev-{name}-{at}", "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch", "region": "us-east-1", "time": at,
        "detail": {"alarmName": name, "state": {"value": state, "reason": "Threshold Crossed", "timestamp": at.replace("Z", "+0000")},
                   "configuration": {"description": description}},
    }


def test_burst_of_alarms_becomes_one_incident_ending_at_the_last_alarm():
    payload = incident_payload([
        _alarm("netcortex-lab-flow-svc-to-app-degraded", "2026-10-27T10:03:10Z", description="Synthetic flow svc-to-app is below 100% success."),
        _alarm("netcortex-lab-vpn-tunnel1-down", "2026-10-27T10:02:00Z", description="VPN tunnel 1 (3.3.3.1) is down."),
        _alarm("netcortex-lab-vpn-tunnel1-down", "2026-10-27T10:02:30Z"),   # same alarm again: listed once
    ])
    assert payload["alarms"] == ["netcortex-lab-vpn-tunnel1-down", "netcortex-lab-flow-svc-to-app-degraded"]
    assert payload["reported_at"] == "2026-10-27T10:03:10+00:00"
    assert payload["external_incident_id"] == "netcortex-lab-vpn-tunnel1-down@2026-10-27T10:02:00+00:00"
    assert payload["description"].startswith("CloudWatch alarms went into ALARM: netcortex-lab-vpn-tunnel1-down: VPN tunnel 1")

    incident = normalize_incident(payload)
    assert incident.region == "us-east-1" and incident.source_system == "cloudwatch"
    assert incident.reported_at.isoformat() == "2026-10-27T10:03:10+00:00"


def test_normalizer_accepts_a_raw_eventbridge_alarm_event():
    incident = normalize_incident(_alarm("netcortex-lab-nlb-unhealthy-targets", "2026-10-27T11:00:00Z"))
    assert incident.reported_at.isoformat() == "2026-10-27T11:00:00+00:00"
    assert "netcortex-lab-nlb-unhealthy-targets" in incident.description


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _msg(body: dict | str, receipt: str) -> dict:
    return {"MessageId": receipt, "ReceiptHandle": receipt, "Body": body if isinstance(body, str) else json.dumps(body)}


@pytest.mark.asyncio
async def test_bridge_groups_dedupes_posts_once_and_deletes_after_success(tmp_path):
    sqs = boto3.client("sqs", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(sqs)
    tunnel = _alarm("netcortex-lab-vpn-tunnel1-down", "2026-10-27T10:02:00Z", event_id="e1")
    stub.add_response("receive_message", {"Messages": [
        _msg(tunnel, "r1"),
        _msg(_alarm("netcortex-lab-vpn-tunnel1-down", "2026-10-27T10:09:00Z", state="OK"), "r-ok"),  # recovery: ignored
        _msg("not json", "r-junk"),
    ]})
    stub.add_response("delete_message_batch", {"Successful": [], "Failed": []},
                      {"QueueUrl": QUEUE, "Entries": [{"Id": "0", "ReceiptHandle": "r-ok"}, {"Id": "1", "ReceiptHandle": "r-junk"}]})
    stub.add_response("receive_message", {"Messages": [
        _msg(tunnel, "r1-again"),                                              # SQS redelivery: same event id
        _msg(_alarm("netcortex-lab-bgp-169.254.10.1-down", "2026-10-27T10:02:40Z", event_id="e2"), "r2"),
    ]})
    stub.add_response("delete_message_batch", {"Successful": [], "Failed": []},
                      {"QueueUrl": QUEUE, "Entries": [{"Id": "0", "ReceiptHandle": "r1-again"}]})
    stub.add_response("delete_message_batch", {"Successful": [], "Failed": []},
                      {"QueueUrl": QUEUE, "Entries": [{"Id": "0", "ReceiptHandle": "r1"}, {"Id": "1", "ReceiptHandle": "r2"}]})
    stub.activate()

    posted: list[dict] = []
    fail = {"n": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        if fail["n"]:
            fail["n"] -= 1
            return httpx.Response(503, json={"detail": "handler_not_configured"})
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"incident_id": "inc-1", "root_cause": "Metrics domain anomaly: tunnel down"})

    clock = _Clock()
    bridge = SqsAlarmBridge(QUEUE, "http://netcortex/incidents", sqs_client=sqs, out_dir=tmp_path, quiet_seconds=60,
                            max_group_seconds=180, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=clock)

    assert await bridge.poll_once(wait_seconds=0) == 1
    clock.t += 30
    assert await bridge.poll_once(wait_seconds=0) == 1
    assert not bridge.due()                       # 30s since the last alarm, quiet period is 60s
    clock.t += 60
    assert bridge.due()
    assert await bridge.flush() is None           # webhook down: nothing deleted, retry later
    assert not bridge.due()
    clock.t += 31
    record = await bridge.flush()

    stub.assert_no_pending_responses()
    assert len(posted) == 1 and posted[0]["alarms"] == ["netcortex-lab-vpn-tunnel1-down", "netcortex-lab-bgp-169.254.10.1-down"]
    saved = json.loads((tmp_path / "inc-1.json").read_text())
    assert saved == record and saved["report"]["incident_id"] == "inc-1"
    assert not bridge.due()                       # buffer cleared


def test_alarm_dimensions_become_alarm_entities():
    def with_metric(event: dict, namespace: str, dims: dict) -> dict:
        event["detail"]["configuration"]["metrics"] = [
            {"id": "m1", "metricStat": {"metric": {"namespace": namespace, "name": "x", "dimensions": dims}}}]
        return event

    payload = incident_payload([
        with_metric(_alarm("netcortex-lab-flow-svc-to-app-degraded", "2026-10-27T10:03:10Z"), "NetCortexLab/Traffic", {"Flow": "svc-to-app"}),
        with_metric(_alarm("netcortex-lab-nlb-unhealthy-targets", "2026-10-27T10:03:20Z"), "AWS/NetworkELB",
                    {"TargetGroup": "targetgroup/app/abc", "LoadBalancer": "net/app/def"}),
        with_metric(_alarm("netcortex-lab-flow-svc-to-app-degraded", "2026-10-27T10:03:40Z"), "NetCortexLab/Traffic", {"Flow": "svc-to-app"}),
    ])
    assert payload["alarm_entities"] == [{"type": "flow", "id": "svc-to-app"},
                                         {"type": "target_group", "id": "targetgroup/app/abc"}]
    assert normalize_incident(payload).alarm_entities == payload["alarm_entities"]
