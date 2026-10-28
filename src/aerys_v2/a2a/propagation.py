"""W3C trace context only; never persist baggage or authentication headers."""

import re

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

propagator = TraceContextTextMapPropagator()
tracer = trace.get_tracer("aerys_v2.a2a")


def carrier(headers):
    value = headers.get("traceparent", "")
    if not re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", value):
        return {}
    result = {"traceparent": value}
    # tracestate/baggage are intentionally not forwarded: arbitrary vendor/user data.
    return result


def scrub(text, secrets):
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)(bearer\s+|(?:api[_-]?key|token|password)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
