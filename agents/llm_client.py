"""One entry point for every LLM call NetCortex makes: incident classification
(supervisor), RCA synthesis (rca_synthesizer) and hypothesis triage (triage).

`llm.provider` in config selects the backend:

  google   Gemini through the Gemini API (GEMINI_API_KEY) or Vertex AI ADC
           (GOOGLE_CLOUD_PROJECT). The default, and the behaviour before this module.
  bedrock  Amazon Bedrock Converse. `llm.model` is any model Converse accepts: a
           foundation model or inference profile id (us.amazon.nova-micro-v1:0), or the
           ARN of an imported (Custom Model Import) or custom model deployment.
           Credentials: the standard AWS chain (AWS_PROFILE, instance role, ...);
           region: llm.region, else AWS_REGION / AWS_DEFAULT_REGION, else us-east-1.

Call sites pass a model *reference*: `model_ref(cfg["llm"])` returns the plain model
name for google and "bedrock:<model>" for bedrock, so a reference alone says where to go.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import time
from functools import lru_cache
from typing import Any

logger = logging.getLogger("net_cortex.llm")

BEDROCK_PREFIX = "bedrock:"
_RETRYABLE_STATUS_CODES = {429, 503}
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2.0
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def model_ref(llm_cfg: dict[str, Any] | None, default: str = "gemini-2.5-flash") -> str:
    """The reference call sites pass around: provider folded into the model string."""
    llm_cfg = llm_cfg or {}
    model = str(llm_cfg.get("model", default))
    if str(llm_cfg.get("provider", "google")).lower() == "bedrock" and not model.startswith(BEDROCK_PREFIX):
        region = llm_cfg.get("region")
        return f"{BEDROCK_PREFIX}{region}/{model}" if region else f"{BEDROCK_PREFIX}{model}"
    return model


def _split_bedrock(ref: str) -> tuple[str, str]:
    """'bedrock:[region/]model' -> (region, model). Model ARNs carry their own region."""
    rest = ref[len(BEDROCK_PREFIX):]
    if "/" in rest and not rest.startswith("arn:"):
        region, model = rest.split("/", 1)
        if re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
            return region, model
    if rest.startswith("arn:aws:bedrock:"):
        return rest.split(":")[3], rest
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1", rest


def available(ref: str) -> bool:
    """Could a call to this model be attempted at all (package + credentials configured)?"""
    if ref.startswith(BEDROCK_PREFIX):
        return importlib.util.find_spec("boto3") is not None
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT")):
        return False
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


def _to_object(text: str) -> dict | None:
    """Parse a JSON-object reply. A model sometimes wraps it in a one-item list."""
    parsed = json.loads(text)
    if isinstance(parsed, list) and len(parsed) == 1:
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        logger.warning("LLM returned %s, not a JSON object; using deterministic fallback", type(parsed).__name__)
        return None
    return parsed


def generate_json(prompt: str, ref: str) -> dict | None:
    """Ask for a JSON object. None (and a warning) on any failure, so callers fall back."""
    if ref.startswith(BEDROCK_PREFIX):
        return _bedrock_json(prompt, ref)
    return _google_json(prompt, ref)


# ---- google ---------------------------------------------------------------

def _google_json(prompt: str, model_name: str) -> dict | None:
    try:
        from google import genai  # type: ignore
        from google.genai import errors  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError:
        logger.info("LLM disabled: google-genai package not installed")
        return None
    api_key = os.environ.get("GEMINI_API_KEY", "")
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    gcp_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if api_key:
        logger.info("LLM auth mode=api_key model=%s", model_name)
        client = genai.Client(api_key=api_key)
    elif gcp_project:
        logger.info("LLM auth mode=adc project=%s location=%s model=%s", gcp_project, gcp_location, model_name)
        client = genai.Client(vertexai=True, project=gcp_project, location=gcp_location)
    else:
        logger.info("LLM disabled: no GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT configured")
        return None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return _to_object(response.text)
        except errors.APIError as exc:
            if exc.code in _RETRYABLE_STATUS_CODES and attempt < _MAX_ATTEMPTS:
                logger.warning("LLM call attempt %d/%d failed with transient error, retrying: %s",
                               attempt, _MAX_ATTEMPTS, exc)
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue
            logger.warning("LLM call failed, using deterministic fallback: %s", exc)
            return None
        except Exception as exc:
            logger.warning("LLM call failed, using deterministic fallback: %s", exc)
            return None
    return None


# ---- bedrock --------------------------------------------------------------

@lru_cache(maxsize=8)
def _bedrock_client(region: str):
    import boto3
    from botocore.config import Config
    # standard retry mode retries throttling and, for imported models, ModelNotReadyException
    # (a cold start after the model scaled to zero can take up to about a minute)
    return boto3.client("bedrock-runtime", region_name=region,
                        config=Config(retries={"mode": "standard", "total_max_attempts": 8}, read_timeout=120))


def _bedrock_json(prompt: str, ref: str) -> dict | None:
    region, model = _split_bedrock(ref)
    logger.info("LLM auth mode=bedrock region=%s model=%s", region, model)
    try:
        response = _bedrock_client(region).converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 2048},
        )
        parts = response["output"]["message"]["content"]
        text = "".join(p.get("text", "") for p in parts)
        # Converse has no JSON mode: accept a reply fenced as ```json ... ```
        return _to_object(_FENCE.sub("", text))
    except Exception as exc:
        logger.warning("LLM call failed, using deterministic fallback: %s", exc)
        return None
