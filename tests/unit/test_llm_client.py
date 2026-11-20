"""agents/llm_client.py: provider selection, Bedrock Converse parsing, fallbacks."""
from __future__ import annotations

import types

import pytest

from agents import llm_client, rca_synthesizer, supervisor, triage


def test_model_ref_google_is_the_plain_model():
    assert llm_client.model_ref({"provider": "google", "model": "gemini-flash-lite-latest"}) == "gemini-flash-lite-latest"
    assert llm_client.model_ref({}) == "gemini-2.5-flash"
    assert llm_client.model_ref(None) == "gemini-2.5-flash"


def test_model_ref_bedrock_carries_provider_and_region():
    assert llm_client.model_ref({"provider": "bedrock", "model": "us.amazon.nova-micro-v1:0"}) == \
        "bedrock:us.amazon.nova-micro-v1:0"
    assert llm_client.model_ref({"provider": "bedrock", "model": "us.amazon.nova-micro-v1:0", "region": "us-west-2"}) == \
        "bedrock:us-west-2/us.amazon.nova-micro-v1:0"


def test_split_bedrock_region_sources(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert llm_client._split_bedrock("bedrock:us.amazon.nova-micro-v1:0") == ("us-east-1", "us.amazon.nova-micro-v1:0")
    assert llm_client._split_bedrock("bedrock:eu-central-1/m") == ("eu-central-1", "m")
    arn = "arn:aws:bedrock:us-west-2:123456789012:imported-model/abc123"
    assert llm_client._split_bedrock(f"bedrock:{arn}") == ("us-west-2", arn)
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert llm_client._split_bedrock("bedrock:m")[0] == "ap-south-1"


def test_available(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    assert not llm_client.available("gemini-flash-lite-latest")
    assert llm_client.available("bedrock:us.amazon.nova-micro-v1:0")  # boto3 is a dependency


class _Converse:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.calls = text, exc, []

    def converse(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {"output": {"message": {"content": [{"text": self.text}]}}}


@pytest.mark.parametrize("text", ['{"root_cause": "x [E1]"}', '```json\n{"root_cause": "x [E1]"}\n```',
                                  '[{"root_cause": "x [E1]"}]'])
def test_bedrock_json_parses_plain_fenced_and_wrapped(monkeypatch, text):
    stub = _Converse(text)
    monkeypatch.setattr(llm_client, "_bedrock_client", lambda region: stub)
    assert llm_client.generate_json("p", "bedrock:us.amazon.nova-micro-v1:0") == {"root_cause": "x [E1]"}
    assert stub.calls[0]["modelId"] == "us.amazon.nova-micro-v1:0"
    assert stub.calls[0]["messages"][0]["content"][0]["text"] == "p"


def test_bedrock_json_failure_falls_back_to_none(monkeypatch):
    monkeypatch.setattr(llm_client, "_bedrock_client", lambda region: _Converse(exc=RuntimeError("AccessDenied")))
    assert llm_client.generate_json("p", "bedrock:m") is None
    monkeypatch.setattr(llm_client, "_bedrock_client", lambda region: _Converse("not json"))
    assert llm_client.generate_json("p", "bedrock:m") is None
    monkeypatch.setattr(llm_client, "_bedrock_client", lambda region: _Converse('"a string"'))
    assert llm_client.generate_json("p", "bedrock:m") is None


def test_google_path_unchanged_for_plain_models(monkeypatch):
    called = {}
    monkeypatch.setattr(llm_client, "_google_json", lambda prompt, model: called.setdefault("google", model) and {"ok": 1})
    monkeypatch.setattr(llm_client, "_bedrock_json", lambda prompt, ref: pytest.fail("bedrock must not be called"))
    llm_client.generate_json("p", "gemini-flash-lite-latest")
    assert called["google"] == "gemini-flash-lite-latest"


def test_synthesizer_and_supervisor_dispatch_to_bedrock(monkeypatch):
    stub = _Converse('{"degradation_type": "network"}')
    monkeypatch.setattr(llm_client, "_bedrock_client", lambda region: stub)
    assert rca_synthesizer._call_gemini("p", "bedrock:m") == {"degradation_type": "network"}
    assert supervisor._classify_with_llm("Users report packet loss", "bedrock:m") == "network"


def test_triage_enabled_with_bedrock_without_gemini_env(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    assert not triage.llm_enabled({"llm": {"provider": "google"}})
    assert triage.llm_enabled({"llm": {"provider": "bedrock", "model": "us.amazon.nova-micro-v1:0"}})
    assert not triage.llm_enabled({"llm": {"provider": "bedrock", "model": "m", "hypotheses": False}})
