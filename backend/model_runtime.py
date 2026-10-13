"""Bounded, schema-validated analysis calls. No tool execution or prompt logging."""
import json
import os
import time
from typing import Annotated
import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from .config import MODEL, MODEL_URL, credentials


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3500)]
EvidenceId = Annotated[str, StringConstraints(min_length=1, max_length=254)]
Limitation = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    finding: ShortText
    evidence: list[EvidenceId] = Field(max_length=10)
    limitations: list[Limitation] = Field(max_length=8)


# DeepSeek Beta does not support min/maxLength or min/maxItems. Apply those locally.
STRICT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"finding": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}}},
    "required": ["finding", "evidence", "limitations"],
}
MAX_ATTEMPTS = 2
MAX_OUTPUT_TOKENS = 1400


class ModelFailure(Exception):
    def __init__(self, code, telemetry):
        super().__init__(code)
        self.code, self.telemetry = code, telemetry


def output_format():
    return os.getenv("DEEPSEEK_ANALYSIS_FORMAT", "strict_tools").strip().lower()


def validate_finding(value, evidence):
    try:
        finding = Finding.model_validate(value)
    except (ValidationError, TypeError):
        raise ValueError("OUTPUT_SCHEMA") from None
    allowed = set(evidence)
    if not set(finding.evidence).issubset(allowed) or (allowed and not finding.evidence):
        raise ValueError("OUTPUT_EVIDENCE")
    # Remove repeated references without allowing the model to manufacture sources.
    finding.evidence = list(dict.fromkeys(finding.evidence))
    return finding.model_dump()


def analyze_json(system, context, evidence):
    """At most two attempts, only transient errors retry; failures expose safe codes."""
    started = time.monotonic()
    fmt = output_format()
    telemetry = {"attempts": 0, "latency_ms": 0, "input_tokens": None,
        "output_tokens": None, "format": fmt, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "model": MODEL, "reused": False}
    def fail(code):
        telemetry["latency_ms"] = round((time.monotonic() - started) * 1000)
        raise ModelFailure(code, dict(telemetry)) from None
    if fmt not in ("json", "strict_tools"):
        fail("MODEL_FORMAT_CONFIG")
    key = credentials().get("DEEPSEEK_API_KEY")
    if not key:
        fail("MODEL_NOT_CONFIGURED")
    body = {"model": MODEL, "thinking": {"type": "disabled"}, "stream": False,
        "max_tokens": MAX_OUTPUT_TOKENS, "messages": [
            {"role": "system", "content": system +
                '\nReturn JSON: {"finding":"依据数据的结论","evidence":[],"limitations":[]}. '
                "只允许这三个字段。finding 必须是非空字符串，最多3500字符；evidence 最多10条，只能引用输入的 evidence_ids；存在证据时至少引用一条。limitations 是字符串数组，最多8条，每条最多500字符。"},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
    endpoint = MODEL_URL + "/chat/completions"
    if fmt == "strict_tools":
        endpoint = MODEL_URL + "/beta/chat/completions"
        body.update(tools=[{"type": "function", "function": {
            "name": "submit_analysis", "description": "Return a read-only analysis report.",
            "strict": True, "parameters": STRICT_SCHEMA}}],
            tool_choice={"type": "function", "function": {"name": "submit_analysis"}})
    else:
        body["response_format"] = {"type": "json_object"}
    try:
        with httpx.Client(timeout=httpx.Timeout(35, connect=8), trust_env=False) as client:
            for attempt in range(MAX_ATTEMPTS):
                telemetry["attempts"] += 1
                try:
                    response = client.post(endpoint, headers={"Authorization": "Bearer " + key}, json=body)
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt + 1 < MAX_ATTEMPTS:
                        time.sleep(0.25)
                        continue
                    fail("MODEL_TRANSPORT")
                if response.status_code in (429, 502, 503, 504):
                    try:
                        delay = float(response.headers.get("retry-after", "0.5"))
                    except ValueError:
                        delay = 0.5
                    # Never wait unboundedly or hammer a provider requesting a longer delay.
                    if attempt + 1 < MAX_ATTEMPTS and 0 <= delay <= 2:
                        time.sleep(delay)
                        continue
                    fail("MODEL_RATE_LIMIT" if response.status_code == 429 else "MODEL_UNAVAILABLE")
                if response.is_error:
                    fail("MODEL_AUTH" if response.status_code in (401, 403) else "MODEL_REQUEST")
                try:
                    data = response.json()
                    usage = data.get("usage") or {}
                    for dest, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
                        v = usage.get(source)
                        telemetry[dest] = v if type(v) is int and v >= 0 else None
                    choice = data["choices"][0]
                    if choice.get("finish_reason") == "length":
                        fail("OUTPUT_TRUNCATED")
                    message = choice["message"]
                    if fmt == "strict_tools":
                        calls = message["tool_calls"]
                        if len(calls) != 1 or calls[0]["function"]["name"] != "submit_analysis":
                            fail("OUTPUT_TOOL")
                        raw = calls[0]["function"]["arguments"]
                    else:
                        if message.get("tool_calls"):
                            fail("OUTPUT_TOOL")
                        raw = message["content"]
                    result = validate_finding(json.loads(raw), evidence)
                except ValueError as error:
                    fail(str(error) if str(error) in ("OUTPUT_SCHEMA", "OUTPUT_EVIDENCE") else "OUTPUT_JSON")
                except (KeyError, IndexError, TypeError):
                    fail("OUTPUT_ENVELOPE")
                telemetry["latency_ms"] = round((time.monotonic() - started) * 1000)
                return result, dict(telemetry)
    except ModelFailure:
        raise
    except Exception:
        fail("MODEL_INTERNAL")
