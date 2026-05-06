from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class JsonRpcPart(BaseModel):
    type: str
    text: str | None = None
    data: dict[str, Any] | None = None


class JsonRpcMessage(BaseModel):
    parts: list[JsonRpcPart]


class TaskParams(BaseModel):
    id: str
    sessionId: str
    message: JsonRpcMessage


class TaskRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str = "tasks/send"
    id: str
    params: TaskParams


class TaskResponse(BaseModel):
    jsonrpc: str
    id: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
