"""SQS -> POST /incidents bridge for CloudWatch alarms.

EventBridge sends every lab alarm's change to ALARM into an SQS queue (see
infra/live/lab/alarm_ingest.tf). This bridge long-polls the queue, groups
the alarms one fault trips into a single incident (a burst ends after
`quiet_seconds` with no new alarm, or `max_group_seconds` after it began),
POSTs it to NetCortex's webhook, and saves the response as
<out_dir>/<incident_id>.json for infra/faults/drill.sh to score.

Polling a queue means NetCortex needs no public endpoint, which suits a
laptop or an in-cluster deployment. Messages are deleted only after a
successful POST, so a NetCortex restart loses nothing; duplicates (SQS is
at-least-once) are dropped by EventBridge event id.

    python app/main.py alarm-bridge --queue-url "$(terraform -chdir=infra/live/lab output -raw alarm_queue_url)"

Needs: sqs:ReceiveMessage, sqs:DeleteMessage on the queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ingestion.cloudwatch_alarms import incident_payload, is_alarm_event, is_alarming

logger = logging.getLogger("net_cortex.ingestion.sqs_bridge")

RETRY_SECONDS = 30.0


class SqsAlarmBridge:
    def __init__(
        self,
        queue_url: str,
        webhook_url: str,
        *,
        sqs_client: Any,
        out_dir: Path = Path("output/alarm_bridge"),
        quiet_seconds: float = 60.0,
        max_group_seconds: float = 180.0,
        token: str | None = None,
        http: httpx.AsyncClient | None = None,
        clock=time.monotonic,
    ) -> None:
        self.queue_url = queue_url
        self.webhook_url = webhook_url
        self.sqs = sqs_client
        self.out_dir = out_dir
        self.quiet_seconds = quiet_seconds
        self.max_group_seconds = max_group_seconds
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        # An RCA with LLM synthesis can take minutes; the webhook answers when it is done.
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
        self.clock = clock
        self._events: list[dict[str, Any]] = []
        self._receipts: list[str] = []
        self._seen_ids: set[str] = set()
        self._first = self._last = 0.0
        self._retry_at = 0.0

    async def _delete(self, receipts: list[str]) -> None:
        for i in range(0, len(receipts), 10):
            entries = [{"Id": str(n), "ReceiptHandle": r} for n, r in enumerate(receipts[i: i + 10])]
            await asyncio.to_thread(self.sqs.delete_message_batch, QueueUrl=self.queue_url, Entries=entries)

    async def poll_once(self, wait_seconds: int = 20) -> int:
        """Receive one batch; buffer ALARM events, delete everything else. Returns events buffered."""
        resp = await asyncio.to_thread(
            self.sqs.receive_message, QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=wait_seconds,
        )
        ignored: list[str] = []
        added = 0
        for msg in resp.get("Messages", []):
            try:
                event = json.loads(msg["Body"])
            except ValueError:
                event = None
            if not is_alarm_event(event) or not is_alarming(event) or event.get("id") in self._seen_ids:
                ignored.append(msg["ReceiptHandle"])
                continue
            self._seen_ids.add(event.get("id"))
            if not self._events:
                self._first = self.clock()
            self._last = self.clock()
            self._events.append(event)
            self._receipts.append(msg["ReceiptHandle"])
            added += 1
            logger.info("alarm %s buffered (%s in this burst)", event["detail"].get("alarmName"), len(self._events))
        if ignored:
            await self._delete(ignored)
        return added

    def due(self) -> bool:
        if not self._events or self.clock() < self._retry_at:
            return False
        now = self.clock()
        return now - self._last >= self.quiet_seconds or now - self._first >= self.max_group_seconds

    async def flush(self) -> dict[str, Any] | None:
        """POST the buffered burst as one incident. Returns the saved record, or None on failure."""
        payload = incident_payload(self._events)
        try:
            resp = await self.http.post(self.webhook_url, json=payload, headers=self.headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            self._retry_at = self.clock() + RETRY_SECONDS
            logger.warning("POST %s failed (%s); retrying in %.0fs", self.webhook_url, exc, RETRY_SECONDS)
            return None
        report = resp.json()
        record = {"incident": payload, "posted_at": datetime.now(timezone.utc).isoformat(), "report": report}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / f"{report['incident_id']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        logger.info("incident %s from %s alarm(s): %s", report["incident_id"], len(payload["alarms"]), report.get("root_cause"))
        await self._delete(self._receipts)
        self._events, self._receipts = [], []
        self._retry_at = 0.0
        return record

    async def run(self) -> None:
        logger.info("bridging %s -> %s", self.queue_url, self.webhook_url)
        while True:
            # Short polls while a burst is open so it is flushed on time.
            await self.poll_once(wait_seconds=5 if self._events else 20)
            if self.due():
                await self.flush()
