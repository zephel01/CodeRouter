"""Anthropic-shaped error responses for the ``/v1/messages`` ingress (v2.16.0).

Before v2.16.0 a request whose whole chain failed came back as FastAPI's
``{"detail": "profile=...: all providers failed: ..."}`` with HTTP 502,
whatever the upstreams had said. Claude Code reads Anthropic's error shape
(``{"type": "error", "error": {"type": ..., "message": ...}}``) and keys
several recoveries off the upstream's *wording*:

- a 400 that names a rejected beta header, ``context_management`` or tool
  search makes it resend without that feature (2.1.288 to 2.1.305);
- a 400 whose message says ``prompt is too long`` triggers reactive
  compaction instead of ending the turn.

None of that can fire when the message is buried in a 502 ``detail`` string.
This module turns a :class:`NoProvidersAvailableError` into the status code,
Anthropic error type and message the client should see, keeping the
upstream's own words (secret-redacted).

Mapping (first match wins, judged on the chain's errors):

====================================  ======  ========================
condition                             status  ``error.type``
====================================  ======  ========================
any error reads as context overflow   400     ``invalid_request_error``
                                              (message starts with
                                              ``prompt is too long:``)
terminal error 400 / 422, not         400     ``invalid_request_error``
retryable
terminal error 413                    413     ``request_too_large``
terminal error 429                    429     ``rate_limit_error``
                                              (+ ``retry-after`` passed
                                              through when sent)
anything else                         502     ``api_error``
====================================  ======  ========================

Upstream 401 / 403 deliberately fall into the last row: they describe
CodeRouter's *own* upstream credentials, not the client's, and handing a
401 to Claude Code would send the user to ``/login`` for nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coderouter.secret_redaction import redact

if TYPE_CHECKING:
    from coderouter.adapters.base import AdapterError
    from coderouter.routing.fallback import NoProvidersAvailableError


#: Prefix Claude Code matches to start reactive compaction. Anthropic's own
#: wording is ``prompt is too long: 208000 tokens > 200000 maximum``.
PROMPT_TOO_LONG_PREFIX = "prompt is too long"

# Upstream wordings for "the prompt does not fit the context window".
# Sources (2026-10): llama.cpp server ``exceed_context_size_error`` /
# "the request exceeds the available context size"; vLLM / OpenAI
# "This model's maximum context length is N tokens"; Ollama "prompt too
# long"; OpenAI ``context_length_exceeded``; Anthropic "prompt is too long".
_CONTEXT_OVERFLOW_RE = re.compile(
    r"exceed_context_size"
    r"|exceeds? the available context"
    r"|context_length_exceeded"
    r"|maximum context length"
    r"|prompt is too long"
    r"|prompt too long"
    r"|context window (?:is )?(?:exceeded|full)"
    r"|exceeds? (?:the )?(?:model'?s )?context (?:length|window|size)",
    re.IGNORECASE,
)

_MAX_MESSAGE_CHARS = 2000


@dataclass(frozen=True)
class AnthropicErrorResponse:
    """What the ingress should send for a failed chain."""

    status_code: int
    error_type: str
    message: str
    headers: dict[str, str] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        """Anthropic error envelope."""
        return {
            "type": "error",
            "error": {"type": self.error_type, "message": self.message},
        }


def upstream_message(err: AdapterError) -> str:
    """Best human-readable message for one upstream failure.

    Pulls ``error.message`` out of Anthropic / OpenAI / llama.cpp JSON
    bodies, falls back to the raw body text, then to the adapter's own
    message. Always secret-redacted and length-capped.
    """
    body = getattr(err, "upstream_body", None)
    text: str | None = None
    if body:
        text = _message_from_body(body)
    if not text:
        text = Exception.__str__(err)
    text = redact(text.strip())
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[:_MAX_MESSAGE_CHARS] + "…"
    return text


def _message_from_body(body: str) -> str | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        stripped = body.strip()
        return stripped or None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg:
                return msg
        if isinstance(err, str) and err:
            return err
        for key in ("message", "detail"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                return val
    return body.strip() or None


def is_context_overflow(err: AdapterError) -> bool:
    """True when the upstream said the prompt does not fit its context."""
    if err.status_code is not None and err.status_code not in (400, 413, 422):
        return False
    haystack = " ".join(
        part for part in (getattr(err, "upstream_body", None), Exception.__str__(err)) if part
    )
    return bool(_CONTEXT_OVERFLOW_RE.search(haystack))


def map_no_providers_error(exc: NoProvidersAvailableError) -> AnthropicErrorResponse:
    """Decide status / type / message for a chain where every provider failed."""
    errors = list(exc.errors)

    for err in errors:
        if is_context_overflow(err):
            msg = upstream_message(err)
            if not msg.lower().startswith(PROMPT_TOO_LONG_PREFIX):
                msg = f"{PROMPT_TOO_LONG_PREFIX}: {msg}"
            return AnthropicErrorResponse(400, "invalid_request_error", msg)

    terminal = errors[-1] if errors else None
    if terminal is not None and terminal.status_code is not None:
        status = terminal.status_code
        if status in (400, 422) and not terminal.retryable:
            return AnthropicErrorResponse(400, "invalid_request_error", upstream_message(terminal))
        if status == 413:
            return AnthropicErrorResponse(413, "request_too_large", upstream_message(terminal))
        if status == 429:
            headers: dict[str, str] = {}
            retry_after = getattr(terminal, "retry_after", None)
            if retry_after:
                headers["retry-after"] = retry_after
            return AnthropicErrorResponse(
                429, "rate_limit_error", upstream_message(terminal), headers
            )

    return AnthropicErrorResponse(502, "api_error", redact(str(exc)))
