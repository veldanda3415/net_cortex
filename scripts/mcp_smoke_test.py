from __future__ import annotations

import argparse
import json
import time
from typing import Any
from urllib import error, request


def _post_jsonrpc(
    url: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, str], str]:
    method = payload.get("method") if isinstance(payload, dict) else None
    params = payload.get("params") if isinstance(payload, dict) else None
    tool_name = params.get("name") if isinstance(params, dict) else None

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": "2026-07-28",
    }
    if isinstance(method, str):
        headers["Mcp-Method"] = method
    if isinstance(tool_name, str):
        headers["Mcp-Name"] = tool_name

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read().decode("utf-8")
    except error.URLError as exc:
        raise RuntimeError(f"Connection failed for {url}: {exc}") from exc


def _parse_tool_payload(rpc_result: dict[str, Any]) -> Any:
    content = rpc_result.get("content", [])
    if not content:
        return rpc_result

    first = content[0] if isinstance(content, list) else {}
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        return rpc_result

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _jsonrpc_or_exit(status_code: int, body_text: str, context: str) -> dict[str, Any]:
    if status_code >= 400:
        print(f"[{context}] HTTP {status_code}: {body_text}")
        raise SystemExit(1)

    try:
        data = json.loads(body_text)
    except json.JSONDecodeError:
        print(f"[{context}] Non-JSON response: {body_text}")
        raise SystemExit(1)

    if "error" in data:
        print(f"[{context}] JSON-RPC error: {json.dumps(data['error'], indent=2)}")
        raise SystemExit(1)

    return data


def _attach_meta(payload: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
        payload["params"] = params
    params["_meta"] = meta
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke test NetCortex MCP RCA endpoint (MCP 2026-07-28)"
    )
    parser.add_argument("--url", default="http://127.0.0.1:9000/mcp", help="MCP RCA endpoint")
    parser.add_argument(
        "--description",
        default="High packet loss and throughput drop in us-east",
    )
    parser.add_argument("--region", default="us-east")
    parser.add_argument("--severity", default="high")
    parser.add_argument("--scenario-id", type=int, default=1)
    parser.add_argument("--source-system", default="mcp-smoke-test")
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--max-polls", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--skip-run-rca", action="store_true")
    args = parser.parse_args()

    print(f"Connecting to {args.url}")

    # This server expects protocol and client capabilities in per-request _meta.
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    list_req = _attach_meta({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
    }, meta)
    try:
        list_status, _, list_body = _post_jsonrpc(
            args.url, list_req, timeout=args.timeout_seconds
        )
    except RuntimeError as exc:
        print(str(exc))
        print("Tip: keep 'kubectl -n netcortex port-forward svc/netcortex-rca 9000:9000' running in another terminal.")
        raise SystemExit(1)
    list_data = _jsonrpc_or_exit(list_status, list_body, "tools/list")
    tools_payload = list_data.get("result", {})
    tools = tools_payload.get("tools", []) if isinstance(tools_payload, dict) else []
    tool_names = [tool.get("name", "<unknown>") for tool in tools if isinstance(tool, dict)]
    print(f"Tools discovered: {', '.join(tool_names) if tool_names else '<none>'}")

    if args.skip_run_rca:
        print("Skipping run_rca as requested")
        raise SystemExit(0)

    run_req = _attach_meta({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "run_rca",
            "arguments": {
                "description": args.description,
                "region": args.region,
                "severity": args.severity,
                "scenario_id": args.scenario_id,
                "source_system": args.source_system,
            },
        },
    }, meta)
    run_status, _, run_body = _post_jsonrpc(args.url, run_req, timeout=args.timeout_seconds)
    run_data = _jsonrpc_or_exit(run_status, run_body, "run_rca")
    run_payload = _parse_tool_payload(run_data.get("result", {}))

    if not isinstance(run_payload, dict):
        print("[run_rca] Unexpected payload:")
        print(json.dumps(run_data, indent=2))
        raise SystemExit(1)

    task_id = run_payload.get("task_id")
    if not task_id:
        print("[run_rca] No task_id found")
        print(json.dumps(run_payload, indent=2))
        raise SystemExit(1)

    print(f"run_rca accepted: task_id={task_id}")

    for attempt in range(1, args.max_polls + 1):
        poll_req = _attach_meta({
            "jsonrpc": "2.0",
            "id": 2 + attempt,
            "method": "tools/call",
            "params": {
                "name": "get_report",
                "arguments": {"incident_id": task_id},
            },
        }, meta)
        poll_status, _, poll_body = _post_jsonrpc(args.url, poll_req, timeout=args.timeout_seconds)
        poll_data = _jsonrpc_or_exit(poll_status, poll_body, "get_report")
        poll_payload = _parse_tool_payload(poll_data.get("result", {}))

        if not isinstance(poll_payload, dict):
            print(f"Poll {attempt}: unexpected payload type")
            time.sleep(args.poll_interval_seconds)
            continue

        status = str(poll_payload.get("status", "unknown"))
        print(f"Poll {attempt}: status={status}")

        if status == "completed":
            result = poll_payload.get("result", {})
            if isinstance(result, dict):
                print("RCA completed")
                print(f"root_cause: {result.get('root_cause')}")
                print(f"confidence_score: {result.get('confidence_score')}")
                print(f"conflict_detected: {result.get('conflict_detected')}")
            else:
                print("RCA completed (non-dict result payload)")
            raise SystemExit(0)

        if status in {"failed", "cancelled"}:
            print("RCA ended unsuccessfully")
            print(json.dumps(poll_payload, indent=2))
            raise SystemExit(1)

        time.sleep(args.poll_interval_seconds)

    print(f"Timed out waiting for completion after {args.max_polls} polls")
    raise SystemExit(1)


if __name__ == "__main__":
    main()