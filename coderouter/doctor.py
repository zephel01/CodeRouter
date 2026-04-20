"""`coderouter doctor --check-model <provider>` — per-provider capability probe.

Purpose (v0.7-B)
----------------
Run a small set of live probes against a single provider from
``providers.yaml`` and compare the observed behavior against the
declarations in ``providers.yaml`` + ``model-capabilities.yaml`` (v0.7-A
registry). Emit a per-probe verdict and, on mismatch, a copy-paste-able
YAML patch that the user can drop into either file.

Motivated by the 5 silent-fail symptoms enumerated in plan.md §9.4:

    1. 空応答 / 意味不明応答            → num_ctx probe (v1.0-B direct detection
                                          via canary echo-back) + streaming probe
                                          (v1.0-C — output-side num_predict cap)
                                          + basic-chat probe
    2. Claude Code「ファイル読めない」  → tool_calls probe (symptom 2)
    3. UI に <think> タグ生露出         → thinking probe + reasoning-leak
                                          content-marker detection (v1.0-A)
    4. 起動後 1 発目で必ず失敗          → auth + model-not-found probe (symptom 4)
    5. 全部 fallback 失敗               → auth probe (symptom 5)

Exit-code contract (CI-friendly)
--------------------------------
    0 = all probes match the registry / providers.yaml declarations.
    2 = at least one probe returned NEEDS_TUNING (structural mismatch;
        the user should apply the emitted YAML patch).
    1 = at least one probe could not run (AUTH_FAIL / UNSUPPORTED /
        TRANSPORT_ERROR). When the auth probe fails, subsequent probes
        are marked SKIP and do not influence the exit code — the auth
        failure dominates.

Non-destructive contract
------------------------
Probes must not induce tool-side-effects. The tool-calls probe declares
a fake ``echo`` tool with no real-world meaning; even if the caller
later re-used the response (they won't), ``echo`` cannot trigger
anything on the caller's side. Each probe is minimized to ≤ ~100
tokens in / ≤ ~20 tokens out.

Layering
--------
Probes issue raw httpx calls rather than going through
``OpenAICompatAdapter`` / ``AnthropicAdapter`` because:

  * The reasoning-leak probe needs to see the raw upstream body BEFORE
    the adapter's v0.5-C passive strip runs.
  * The thinking probe for ``kind: anthropic`` needs to send an
    Anthropic wire-format body directly rather than the reverse-
    translated ChatRequest shape.
  * The tool-calls probe wants to observe the raw ``tool_calls`` field
    vs the raw text content before any repair pass.

Keeping the HTTP plumbing inline in this module (~one helper, no
adapter dependency) makes the probe behavior stable against adapter-
layer changes and keeps the test surface narrow (``httpx_mock`` +
assertions on the probe output).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from coderouter.config.capability_registry import (
    CapabilityRegistry,
    ResolvedCapabilities,
)
from coderouter.config.loader import resolve_api_key
from coderouter.config.schemas import CodeRouterConfig, ProviderConfig
from coderouter.output_filters import DEFAULT_STOP_MARKERS
from coderouter.routing.capability import get_default_registry
from coderouter.translation.tool_repair import repair_tool_calls_in_text

__all__ = [
    "DoctorReport",
    "ProbeResult",
    "ProbeVerdict",
    "check_model",
    "exit_code_for",
    "format_report",
    "run_check_model_sync",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class ProbeVerdict(StrEnum):
    """Per-probe verdict.

    Mapping to exit code (see :func:`exit_code_for`):
        OK                 → contributes 0
        SKIP               → contributes 0 (not applicable or blocked by auth)
        NEEDS_TUNING       → contributes 2 (structural mismatch)
        UNSUPPORTED        → contributes 1 (model not found / feature absent)
        AUTH_FAIL          → contributes 1 (401/403 from upstream)
        TRANSPORT_ERROR    → contributes 1 (timeout / 5xx / network)
    """

    OK = "ok"
    SKIP = "skip"
    NEEDS_TUNING = "needs_tuning"
    UNSUPPORTED = "unsupported"
    AUTH_FAIL = "auth_fail"
    TRANSPORT_ERROR = "transport_error"


@dataclass
class ProbeResult:
    """Outcome of a single probe.

    ``suggested_patch`` is a YAML snippet the user can copy-paste into
    the named file. ``target_file`` is either ``"providers.yaml"`` or
    ``"model-capabilities.yaml"`` — the probe picks whichever is the
    more specific fix (per-provider opt-in wins over per-glob registry
    rule when only one provider is affected; glob-level patches are
    preferred when the mismatch appears to be a whole-family pattern,
    but since doctor probes only one provider at a time, providers.yaml
    is always the safe suggestion for a single-provider fix).
    """

    name: str
    verdict: ProbeVerdict
    detail: str
    suggested_patch: str | None = None
    target_file: str | None = None  # "providers.yaml" or "model-capabilities.yaml"


@dataclass
class DoctorReport:
    """Aggregate report for a single ``--check-model`` invocation."""

    provider_name: str
    provider: ProviderConfig
    resolved_caps: ResolvedCapabilities
    results: list[ProbeResult] = field(default_factory=list)


def exit_code_for(report: DoctorReport) -> int:
    """Derive the CLI exit code from a report (see :class:`ProbeVerdict`)."""
    has_blocker = False
    has_tuning = False
    for r in report.results:
        if r.verdict in (
            ProbeVerdict.AUTH_FAIL,
            ProbeVerdict.UNSUPPORTED,
            ProbeVerdict.TRANSPORT_ERROR,
        ):
            has_blocker = True
        elif r.verdict == ProbeVerdict.NEEDS_TUNING:
            has_tuning = True
    if has_blocker:
        return 1
    if has_tuning:
        return 2
    return 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _openai_chat_url(provider: ProviderConfig) -> str:
    base = str(provider.base_url).rstrip("/")
    return f"{base}/chat/completions"


def _anthropic_messages_url(provider: ProviderConfig) -> str:
    base = str(provider.base_url).rstrip("/")
    return f"{base}/v1/messages"


def _openai_headers(provider: ProviderConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "CodeRouter-doctor/0.7"}
    api_key = resolve_api_key(provider.api_key_env)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _anthropic_headers(provider: ProviderConfig) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "CodeRouter-doctor/0.7",
        "anthropic-version": "2023-06-01",
    }
    api_key = resolve_api_key(provider.api_key_env)
    if api_key:
        headers["x-api-key"] = api_key
    return headers


async def _http_post_json(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[int | None, dict[str, Any] | None, str]:
    """POST JSON. Returns (status_or_None, parsed_or_None, raw_text_or_error).

    ``status=None`` signals a transport-level failure (connection refused,
    DNS, timeout). ``parsed=None`` with non-None status means the body
    was not parseable JSON (still treated as an upstream protocol issue
    at the caller's discretion).
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return None, None, f"transport error: {exc}"
    try:
        parsed = resp.json()
    except (json.JSONDecodeError, ValueError):
        return resp.status_code, None, resp.text
    return resp.status_code, parsed, resp.text


async def _http_stream_sse(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[int | None, list[dict[str, Any]], bool, str]:
    """POST a streaming request and consume the SSE stream.

    Returns ``(status, chunks, saw_done, error_text)``.

    * ``status=None`` signals a transport-level failure; ``error_text``
      carries the reason.
    * ``chunks`` are the parsed JSON objects from ``data: <json>`` lines,
      in observed order. ``[DONE]`` is not included.
    * ``saw_done`` is True iff the terminator line ``data: [DONE]`` was
      observed. Strict SSE clients require it; many upstreams omit it
      and rely on connection close instead.
    * On HTTP error (status >= 400) the body is read once and returned
      in ``error_text``; ``chunks`` is empty.

    Mirrors :func:`_http_post_json`'s error handling shape so the caller
    can branch on ``status`` the same way.
    """
    try:
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream("POST", url, json=body, headers=headers) as resp,
        ):
            status = resp.status_code
            if status >= 400:
                raw = await resp.aread()
                return (
                    status,
                    [],
                    False,
                    raw.decode("utf-8", errors="replace")[:400],
                )
            chunks: list[dict[str, Any]] = []
            saw_done = False
            async for line in resp.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    saw_done = True
                    continue
                try:
                    chunks.append(json.loads(data_str))
                except json.JSONDecodeError:
                    continue  # skip malformed chunks, keep consuming
            return status, chunks, saw_done, ""
    except httpx.HTTPError as exc:
        return None, [], False, f"transport error: {exc}"


# ---------------------------------------------------------------------------
# Patch emitters
#
# Kept as tiny helpers rather than a Jinja dance — the surface area is too
# small to justify templating, and exact indentation in the emitted YAML
# matters for copy-paste fidelity.
# ---------------------------------------------------------------------------


def _patch_providers_yaml_capability(provider_name: str, key: str, value: bool) -> str:
    """Emit a providers.yaml patch that flips ``capabilities.<key>``."""
    val = "true" if value else "false"
    return (
        "# providers.yaml — update the entry for "
        f"{provider_name!r}:\n"
        "providers:\n"
        f"  - name: {provider_name}\n"
        "    # ... existing fields ...\n"
        "    capabilities:\n"
        f"      {key}: {val}\n"
    )


def _patch_model_capabilities_yaml(*, match: str, kind: str, key: str, value: bool) -> str:
    """Emit a model-capabilities.yaml rule that declares ``<key>=<value>``."""
    val = "true" if value else "false"
    return (
        "# ~/.coderouter/model-capabilities.yaml — append under `rules:`:\n"
        "rules:\n"
        f"  - match: {match!r}\n"
        f"    kind: {kind}\n"
        "    capabilities:\n"
        f"      {key}: {val}\n"
    )


def _patch_providers_yaml_output_filters(provider_name: str, filters: list[str]) -> str:
    """v1.0-A: Emit a providers.yaml patch adding/extending ``output_filters``.

    Lists the filters verbatim so copy-paste yields a valid YAML list.
    The comment block above the stanza hints that this is additive with
    any existing filter chain — users with a bespoke chain should merge
    rather than replace.
    """
    items = "\n".join(f"      - {f}" for f in filters)
    return (
        "# providers.yaml — update the entry for "
        f"{provider_name!r} (merge if a chain already exists):\n"
        "providers:\n"
        f"  - name: {provider_name}\n"
        "    # ... existing fields ...\n"
        "    output_filters:\n"
        f"{items}\n"
    )


def _patch_providers_yaml_num_ctx(provider_name: str, desired_ctx: int = 32768) -> str:
    """v1.0-B: Emit a providers.yaml patch setting ``extra_body.options.num_ctx``.

    The path is Ollama-specific: ``extra_body`` is shallow-merged into the
    outbound body by the openai_compat adapter, and Ollama exposes context
    length via a nested ``options`` object. 32768 is a practical default
    for Claude Code's tool-heavy system prompts (see plan.md §9.4 symptom
    #1) — operators can dial it down for memory-bound hosts.
    """
    return (
        "# providers.yaml — update the entry for "
        f"{provider_name!r} (merge into any existing extra_body):\n"
        "providers:\n"
        f"  - name: {provider_name}\n"
        "    # ... existing fields ...\n"
        "    extra_body:\n"
        "      options:\n"
        f"        num_ctx: {desired_ctx}\n"
    )


def _patch_providers_yaml_num_predict(provider_name: str, desired_predict: int = 4096) -> str:
    """v1.0-C: Emit a providers.yaml patch setting ``extra_body.options.num_predict``.

    Sibling of :func:`_patch_providers_yaml_num_ctx` — same ``extra_body.options``
    path, but controls the **output-side** token cap rather than the input-side
    window. Ollama's default for ``num_predict`` is -1 (unlimited) in recent
    builds, but older builds and some Ollama-compat servers cap at 128 or 256
    which silently truncates Claude Code's longer completions mid-response.
    4096 is a practical cap that covers ~95 % of Claude Code completions
    without risking runaway generations; operators can set to -1 for uncapped.
    """
    return (
        "# providers.yaml — update the entry for "
        f"{provider_name!r} (merge into any existing extra_body):\n"
        "providers:\n"
        f"  - name: {provider_name}\n"
        "    # ... existing fields ...\n"
        "    extra_body:\n"
        "      options:\n"
        f"        num_predict: {desired_predict}\n"
    )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


# v1.0-B: num_ctx probe constants.
#
# We embed a short, unusual canary token at the very beginning of the user
# prompt, follow it with enough filler sentences to exceed Ollama's default
# 2048-token context window, and ask the model to echo the canary back.
# Because Ollama silently drops the BEGINNING of the prompt when it
# overflows `num_ctx` (not the end), a model running at the default cannot
# know what the canary was and fails to echo it. When the operator has
# correctly bumped `num_ctx` via ``extra_body.options.num_ctx``, the canary
# survives and the model replies with it.
#
# The padding sentence is ~16 tokens; 300 repeats ≈ 4800 tokens — well
# beyond 2048 yet still cheap enough to issue once per doctor invocation.
# ZEBRA-MOON-847 is chosen to be hyphenated and all-caps so it does not
# appear in natural text; the model cannot produce it without having seen
# it in the prompt.
_NUM_CTX_PROBE_CANARY = "ZEBRA-MOON-847"
_NUM_CTX_PROBE_PADDING_SENTENCE = (
    "The quick brown fox jumps over the lazy dog near the river bank today. "
)
_NUM_CTX_PROBE_PADDING_REPEATS = 300
# Threshold below which a declared ``num_ctx`` is still considered "too
# tight for Claude Code's tool-heavy prompts" — the Claude Code system
# prompt + tool roster alone is routinely north of 15k tokens. 8192 leaves
# headroom for small user messages without enabling a corner case where
# the probe happens to fit (our padding is only ~5k tokens) but a real
# Claude Code session still truncates.
_NUM_CTX_ADEQUATE_THRESHOLD = 8192

# v1.0-C: streaming probe constants.
#
# A short, deterministic task that forces the model to emit ~60-80 output
# chars in a predictable shape. Counting 1..30 one-per-line yields "1\n2\n
# ...30\n" = ~80 chars; any cap below the prompt's intent shows up as a
# ``finish_reason: length`` with heavily-truncated content. The prompt is
# kept well under ``num_ctx`` so a stray ``num_ctx`` issue does not
# masquerade as a ``num_predict`` issue (num_ctx probe runs first anyway).
_STREAMING_PROBE_USER_PROMPT = (
    "Count from 1 to 30, one number per line. Output only the numbers, nothing else."
)
# Minimum content length we require to call the stream "not prematurely
# truncated". "1\n2\n...\n30" is ~80 chars; 40 chars covers the halfway
# mark (1..20) which is already obviously-truncated territory.
_STREAMING_PROBE_MIN_EXPECTED_CHARS = 40
# Default ``num_predict`` suggested in the emitted patch. -1 would be
# optimal (uncapped) but "4096" communicates intent more clearly to
# operators unfamiliar with Ollama's sentinel value, and covers Claude
# Code completions comfortably while still protecting against runaway
# generations on broken models.
_STREAMING_PROBE_NUM_PREDICT_DEFAULT = 4096


def _is_ollama_like(provider: ProviderConfig) -> bool:
    """Return True iff num_ctx truncation is plausible for this provider.

    Two signals fire:
      * base_url uses the canonical Ollama port ``11434``. This is the
        off-the-shelf install; operators who moved it still trigger the
        second signal.
      * ``extra_body.options.num_ctx`` is declared. Only Ollama honors
        this path, so an operator who wrote the field is declaring — by
        construction — that the upstream is Ollama-shape.

    Deliberately does NOT fire on llama.cpp (port 8080), OpenRouter,
    Together, Groq, or Anthropic native — those upstreams either don't
    truncate silently (they hard-error on over-long prompts) or use a
    different context-length knob (``max_tokens``, ``n_ctx`` at server
    start, etc.) that isn't reachable from providers.yaml.
    """
    if provider.kind != "openai_compat":
        return False
    if ":11434" in str(provider.base_url):
        return True
    options = provider.extra_body.get("options")
    return isinstance(options, dict) and "num_ctx" in options


def _declared_num_ctx(provider: ProviderConfig) -> int | None:
    """Return the provider's declared ``extra_body.options.num_ctx`` if any."""
    options = provider.extra_body.get("options")
    if not isinstance(options, dict):
        return None
    val = options.get("num_ctx")
    return val if isinstance(val, int) else None


_PROBE_BASIC_USER_PROMPT = "Reply with exactly the single word: PONG"
_PROBE_TOOLS_USER_PROMPT = (
    "You have one tool named `echo`. Call it with the argument "
    '`{"message": "probe"}`. Do not reply with any text — only the tool call.'
)
_PROBE_TOOL_SPEC_OPENAI = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": (
            "Test tool used by CodeRouter's doctor probe. Echo back the "
            "provided message. NEVER interpret as a real command — this "
            "is diagnostic-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
}
_PROBE_TOOL_SPEC_ANTHROPIC = {
    "name": "echo",
    "description": (
        "Test tool used by CodeRouter's doctor probe. Echo back the "
        "provided message. NEVER interpret as a real command — this "
        "is diagnostic-only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
}


async def _probe_auth_and_basic_chat(
    provider: ProviderConfig,
) -> ProbeResult:
    """Probe 1 — auth + model-reachable + basic chat completion.

    Dominates subsequent probes: if this fails with AUTH_FAIL,
    UNSUPPORTED, or TRANSPORT_ERROR, the caller short-circuits and
    marks other probes SKIP. A 401/403 almost always means the
    provider's ``api_key_env`` points at an empty / wrong env var. A
    404 on an openai_compat upstream typically means the ``model``
    string is a typo or (for Ollama) ``ollama pull X`` was skipped.
    """
    if provider.kind == "anthropic":
        url = _anthropic_messages_url(provider)
        headers = _anthropic_headers(provider)
        body: dict[str, Any] = {
            "model": provider.model,
            "messages": [{"role": "user", "content": _PROBE_BASIC_USER_PROMPT}],
            "max_tokens": 16,
        }
    else:
        url = _openai_chat_url(provider)
        headers = _openai_headers(provider)
        body = {
            "model": provider.model,
            "messages": [{"role": "user", "content": _PROBE_BASIC_USER_PROMPT}],
            "max_tokens": 16,
            "temperature": 0,
        }

    status, parsed, raw = await _http_post_json(
        url, headers=headers, body=body, timeout=provider.timeout_s
    )

    if status is None:
        return ProbeResult(
            name="auth+basic-chat",
            verdict=ProbeVerdict.TRANSPORT_ERROR,
            detail=f"could not reach {url}: {raw}",
        )

    if status in (401, 403):
        return ProbeResult(
            name="auth+basic-chat",
            verdict=ProbeVerdict.AUTH_FAIL,
            detail=(
                f"upstream returned {status}. Check that env var "
                f"{provider.api_key_env!r} is set "
                "and holds a valid key (plan.md §9.4 symptom #5)."
            ),
        )

    if status == 404:
        return ProbeResult(
            name="auth+basic-chat",
            verdict=ProbeVerdict.UNSUPPORTED,
            detail=(
                f"upstream returned 404 for model {provider.model!r}. "
                "For Ollama: run `ollama pull "
                f"{provider.model}`. For OpenRouter: verify the model slug "
                "at https://openrouter.ai/models (plan.md §9.4 symptom #4)."
            ),
        )

    if status >= 400:
        snippet = (raw or "")[:160]
        return ProbeResult(
            name="auth+basic-chat",
            verdict=ProbeVerdict.TRANSPORT_ERROR,
            detail=f"upstream returned {status}: {snippet!r}",
        )

    if parsed is None:
        return ProbeResult(
            name="auth+basic-chat",
            verdict=ProbeVerdict.TRANSPORT_ERROR,
            detail="upstream returned 2xx but body was not JSON",
        )

    # Success — give a short confirmation with observed usage (if any).
    usage = parsed.get("usage") or {}
    tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens")
    tokens_out = usage.get("completion_tokens") or usage.get("output_tokens")
    return ProbeResult(
        name="auth+basic-chat",
        verdict=ProbeVerdict.OK,
        detail=(
            f"{status} OK"
            + (f" (in={tokens_in}, out={tokens_out})" if tokens_in is not None else "")
        ),
    )


def _extract_openai_assistant_choice(
    body: dict[str, Any],
) -> dict[str, Any] | None:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    msg = first.get("message")
    return msg if isinstance(msg, dict) else None


async def _probe_num_ctx(provider: ProviderConfig) -> ProbeResult:
    """v1.0-B Probe — direct detection of Ollama ``num_ctx`` truncation.

    Addresses plan.md §9.4 symptom #1 (空応答 / 意味不明応答). Prior to
    v1.0-B the symptom was inferred only indirectly — a silently-truncated
    system prompt often produced a tool-unaware assistant reply, which the
    v0.7-B tool_calls probe then flagged as NEEDS_TUNING for
    ``capabilities.tools=false``. That patch did not fix the root cause;
    the remediation was always the same ``extra_body.options.num_ctx: N``
    bump. The direct probe here uses a canary echo-back to observe the
    truncation first-hand and emit the correct patch.

    Mechanism:
      * Apply the canary (``ZEBRA-MOON-847``) at the very beginning.
      * Follow with ~5k tokens of filler sentences to overflow Ollama's
        default 2048-token context window.
      * Close with an explicit ask to echo the canary token back.
      * Merge ``provider.extra_body`` into the request body (so any
        declared ``options.num_ctx`` is exercised).

    Verdict branches:

        canary echoed + num_ctx declared ≥ threshold  → OK
        canary echoed + num_ctx not declared         → OK (informational —
                                                        upstream isn't
                                                        actually truncating
                                                        at its advertised
                                                        default, which is
                                                        unusual but benign)
        canary missing + num_ctx not declared        → NEEDS_TUNING, patch
                                                        adds 32768
        canary missing + num_ctx declared < threshold → NEEDS_TUNING, patch
                                                        bumps to 32768
        canary missing + num_ctx declared ≥ threshold → NEEDS_TUNING with a
                                                        note about model
                                                        intrinsic limits

    Non-Ollama-shape providers SKIP (see ``_is_ollama_like``).
    """
    if not _is_ollama_like(provider):
        return ProbeResult(
            name="num_ctx",
            verdict=ProbeVerdict.SKIP,
            detail=(
                "not applicable — provider does not look Ollama-shape "
                "(base_url is not on port 11434 and no "
                "`extra_body.options.num_ctx` is declared)."
            ),
        )

    padding = _NUM_CTX_PROBE_PADDING_SENTENCE * _NUM_CTX_PROBE_PADDING_REPEATS
    user_prompt = (
        f"CANARY: {_NUM_CTX_PROBE_CANARY}\n\n"
        + padding
        + "\n\nQuestion: What exact canary token appeared at the very "
        "beginning of this message? Reply with only the canary token "
        "itself, nothing else."
    )

    url = _openai_chat_url(provider)
    headers = _openai_headers(provider)
    # Start from the provider's extra_body — this is the only probe that
    # merges it in, because the whole point of this probe is to exercise
    # whatever ``options.num_ctx`` the operator has declared. Request
    # fields win over extra_body, matching the adapter's merge order.
    body: dict[str, Any] = dict(provider.extra_body)
    body.update(
        {
            "model": provider.model,
            "messages": [{"role": "user", "content": user_prompt}],
            "max_tokens": 32,
            "temperature": 0,
        }
    )

    status, parsed, _raw = await _http_post_json(
        url, headers=headers, body=body, timeout=provider.timeout_s
    )

    if status is None or status >= 400 or parsed is None:
        return ProbeResult(
            name="num_ctx",
            verdict=ProbeVerdict.SKIP,
            detail=f"skipped (upstream status={status!r}).",
        )

    msg = _extract_openai_assistant_choice(parsed)
    content = msg.get("content") if isinstance(msg, dict) else None
    content_text = content if isinstance(content, str) else ""
    canary_echoed = _NUM_CTX_PROBE_CANARY in content_text

    declared = _declared_num_ctx(provider)

    if canary_echoed:
        if declared is not None and declared >= _NUM_CTX_ADEQUATE_THRESHOLD:
            return ProbeResult(
                name="num_ctx",
                verdict=ProbeVerdict.OK,
                detail=(
                    f"canary echoed at ~{len(user_prompt)} chars of prompt; "
                    f"declared num_ctx={declared} is adequate "
                    f"(≥ {_NUM_CTX_ADEQUATE_THRESHOLD})."
                ),
            )
        if declared is None:
            return ProbeResult(
                name="num_ctx",
                verdict=ProbeVerdict.OK,
                detail=(
                    f"canary echoed at ~{len(user_prompt)} chars; upstream "
                    "accepted the full prompt without truncation "
                    "(no `options.num_ctx` declared — the Ollama default is "
                    "2048 so this is unusual; treat as informational)."
                ),
            )
        # declared is not None but below threshold, yet canary still echoed.
        # Either Ollama silently overrode the low declaration (some 0.20+
        # builds clamp `options.num_ctx` to the model's loaded context size)
        # or the prompt simply fit. Surface the declared value so operators
        # running the v1.0-verify script can tell this case apart from a
        # config-loading failure.
        return ProbeResult(
            name="num_ctx",
            verdict=ProbeVerdict.OK,
            detail=(
                f"canary echoed at ~{len(user_prompt)} chars; upstream "
                f"accepted the full prompt despite declared num_ctx="
                f"{declared} (below the {_NUM_CTX_ADEQUATE_THRESHOLD}-token "
                "threshold). Either the prompt fit anyway or Ollama "
                "ignored the declared value — check `ollama ps` for the "
                "session's loaded context and consider `ollama stop "
                f"{provider.model}` before probing to force a cold reload."
            ),
        )

    # Canary missing → truncation occurred.
    if declared is None:
        return ProbeResult(
            name="num_ctx",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                f"canary {_NUM_CTX_PROBE_CANARY!r} missing from reply — "
                "upstream truncated the prompt. No `extra_body.options.num_ctx` "
                "is declared, so Ollama is running at its 2048-token default, "
                "which cannot hold Claude Code's system + tool prompts "
                "(plan.md §9.4 symptom #1)."
            ),
            target_file="providers.yaml",
            suggested_patch=_patch_providers_yaml_num_ctx(provider.name, 32768),
        )
    if declared < _NUM_CTX_ADEQUATE_THRESHOLD:
        return ProbeResult(
            name="num_ctx",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                f"canary missing — declared num_ctx={declared} is below "
                f"the {_NUM_CTX_ADEQUATE_THRESHOLD}-token threshold needed "
                "for Claude Code prompts. Bump it (plan.md §9.4 symptom #1)."
            ),
            target_file="providers.yaml",
            suggested_patch=_patch_providers_yaml_num_ctx(provider.name, 32768),
        )
    # Declared high but still truncated — the upstream model's intrinsic
    # limit is probably lower than the declared num_ctx, or the server is
    # silently capping it. Still NEEDS_TUNING because the observed behavior
    # doesn't match the declaration; operator should verify.
    return ProbeResult(
        name="num_ctx",
        verdict=ProbeVerdict.NEEDS_TUNING,
        detail=(
            f"canary missing even with num_ctx={declared} declared. The "
            "model's intrinsic context limit may be shorter than the "
            "declared value, or the upstream is silently capping it — "
            "verify with the model card / server logs. The suggested "
            "patch still emits 32768 as a starting point; dial down if "
            "the host is memory-constrained."
        ),
        target_file="providers.yaml",
        suggested_patch=_patch_providers_yaml_num_ctx(provider.name, 32768),
    )


async def _probe_streaming(provider: ProviderConfig) -> ProbeResult:
    """v1.0-C Probe — streaming completion path integrity.

    Addresses plan.md §9.4 symptom #1 from the **output** side. The v1.0-B
    ``num_ctx`` probe catches silent **prompt** truncation; this one
    catches silent **completion** truncation — specifically Ollama's
    ``options.num_predict`` cap closing the stream early with
    ``finish_reason: length``. Secondary failure mode covered: upstream
    silently ignoring ``stream: true`` (2xx response but zero SSE chunks),
    which Claude Code experiences as a "no output until timeout" stall.

    Ollama-shape gating
    -------------------
    Fires only when :func:`_is_ollama_like` returns True — same signal set
    as the num_ctx probe (``:11434`` port or declared
    ``extra_body.options.num_ctx``). Rationale:

      * Non-Ollama openai_compat upstreams (OpenRouter, Together, Groq,
        vLLM, llama.cpp) either cap via non-``extra_body`` knobs (server
        start flags, plan-level limits) that ``providers.yaml`` cannot
        reach, or they don't silently cap at all. Emitting a patch would
        be actionless.
      * Anthropic native streaming uses a different event wire format
        (``content_block_delta`` etc.); deferred to a hypothetical v1.0-D
        if symptoms ever surface there.

    Gating also keeps the existing :8080 fixture-based tests
    SKIP-without-HTTP, so the mock FIFO in 30+ tests stays intact.

    Verdicts
    --------
      * non-Ollama-shape                    → SKIP
      * transport/auth/HTTP error           → SKIP (auth probe dominates)
      * 2xx + 0 chunks (stream ignored)     → NEEDS_TUNING (no patch —
                                              advisory; the upstream
                                              framing is broken or the
                                              model does not support
                                              streaming)
      * 2xx + chunks + finish_reason=length
        + content < threshold               → NEEDS_TUNING + num_predict
                                              patch
      * 2xx + chunks + finish_reason=stop
        + content ≥ threshold               → OK
      * 2xx + chunks + no ``[DONE]``        → OK with informational note
                                              (most clients tolerate; the
                                              signal is surfaced for
                                              operators running strict
                                              SSE parsers)
    """
    if not _is_ollama_like(provider):
        return ProbeResult(
            name="streaming",
            verdict=ProbeVerdict.SKIP,
            detail=(
                "not applicable — streaming-path truncation detection is "
                "Ollama-shape-gated (same signal as num_ctx probe: port "
                "11434 or declared `extra_body.options.num_ctx`). Cloud "
                "openai_compat upstreams do not expose an actionable "
                "`num_predict` knob from providers.yaml."
            ),
        )

    url = _openai_chat_url(provider)
    headers = _openai_headers(provider)
    # Merge extra_body same as num_ctx probe — we want declared
    # ``options.num_predict`` (if any) to actually take effect during
    # probing. Top-level probe fields win on collision, matching adapter
    # merge order.
    body: dict[str, Any] = dict(provider.extra_body)
    body.update(
        {
            "model": provider.model,
            "messages": [{"role": "user", "content": _STREAMING_PROBE_USER_PROMPT}],
            "max_tokens": 128,
            "temperature": 0,
            "stream": True,
        }
    )

    status, chunks, saw_done, err = await _http_stream_sse(
        url, headers=headers, body=body, timeout=provider.timeout_s
    )

    if status is None:
        return ProbeResult(
            name="streaming",
            verdict=ProbeVerdict.SKIP,
            detail=f"skipped (transport error during streaming: {err}).",
        )
    if status in (401, 403):
        return ProbeResult(
            name="streaming",
            verdict=ProbeVerdict.SKIP,
            detail=(
                f"skipped (upstream status={status} during streaming); "
                "auth probe already reported this."
            ),
        )
    if status >= 400:
        return ProbeResult(
            name="streaming",
            verdict=ProbeVerdict.SKIP,
            detail=f"skipped (upstream status={status}): {err[:160]!r}",
        )

    # 2xx — aggregate content + finish_reason across chunks.
    content_parts: list[str] = []
    finish_reason: str | None = None
    for chunk in chunks:
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for c in choices:
            if not isinstance(c, dict):
                continue
            delta = c.get("delta")
            if isinstance(delta, dict):
                piece = delta.get("content")
                if isinstance(piece, str):
                    content_parts.append(piece)
            fr = c.get("finish_reason")
            if isinstance(fr, str) and fr:
                finish_reason = fr
    content = "".join(content_parts)

    if not chunks:
        # Non-blocking upstream: 2xx arrived but no SSE chunks did. The
        # `stream: true` flag was likely dropped (some Ollama-compat
        # forks) or the upstream returned a single-shot JSON with a
        # non-SSE content-type. No actionable ``extra_body`` patch —
        # surface the observation and let the operator investigate.
        return ProbeResult(
            name="streaming",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                "upstream returned 2xx but emitted no streaming chunks. "
                "`stream: true` was likely ignored, or the SSE framing is "
                "non-standard (no `data:` prefix / content-type != "
                "`text/event-stream`). Verify with "
                "`curl -N -H 'Accept: text/event-stream'` before relying "
                "on streaming from Claude Code."
            ),
        )

    if finish_reason == "length" and len(content) < _STREAMING_PROBE_MIN_EXPECTED_CHARS:
        # Premature cap — the hallmark of a low ``num_predict`` on
        # Ollama. Claude Code users see this as "assistant cut off
        # mid-word". Since we're already Ollama-shape-gated, the
        # remediation is always the ``extra_body.options.num_predict``
        # bump.
        return ProbeResult(
            name="streaming",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                f"stream closed with `finish_reason='length'` after only "
                f"{len(content)} chars (expected ≥ "
                f"{_STREAMING_PROBE_MIN_EXPECTED_CHARS}). Upstream is "
                "capping output — most likely `options.num_predict`. "
                "Bump it via `extra_body` (plan.md §9.4 symptom #1 "
                "streaming variant)."
            ),
            target_file="providers.yaml",
            suggested_patch=_patch_providers_yaml_num_predict(
                provider.name, _STREAMING_PROBE_NUM_PREDICT_DEFAULT
            ),
        )

    # Stream completed; surface the `[DONE]` observation as an
    # informational suffix so strict-SSE operators know to check their
    # parser tolerance.
    done_note = (
        ""
        if saw_done
        else (
            " (no explicit `[DONE]` terminator observed — most clients "
            "tolerate this but strict SSE parsers may stall)"
        )
    )
    return ProbeResult(
        name="streaming",
        verdict=ProbeVerdict.OK,
        detail=(
            f"stream completed: {len(chunks)} chunks, {len(content)} "
            f"chars, finish_reason={finish_reason!r}{done_note}."
        ),
    )


async def _probe_tool_calls(
    provider: ProviderConfig,
    resolved: ResolvedCapabilities,
) -> ProbeResult:
    """Probe 2 — does the model emit native ``tool_calls`` structure?

    Three observed paths, mapped to a verdict vs the declaration chain
    (``provider.capabilities.tools`` → registry → None):

        * Native ``tool_calls`` populated → *supports tools natively*.
          If declaration says False → NEEDS_TUNING (flip to True).
          If declaration says True → OK.

        * No ``tool_calls`` but text contains tool-shaped JSON that
          v0.3-A ``repair_tool_calls_in_text`` can extract → *supports
          tools via text-JSON only*. If declaration says True →
          NEEDS_TUNING (model works but relies on repair; a narrower
          declaration avoids surprises downstream). If False → OK
          (repair path still rescues at runtime, no tuning needed).

        * Nothing tool-shaped at all → *tools likely unsupported*.
          If declaration says True → NEEDS_TUNING (flip to False). If
          False → OK.
    """
    if provider.kind == "anthropic":
        # Anthropic native tools use a different wire shape; we probe
        # via the messages API. A capable model returns content blocks
        # of type "tool_use".
        url = _anthropic_messages_url(provider)
        headers = _anthropic_headers(provider)
        body: dict[str, Any] = {
            "model": provider.model,
            "messages": [
                {"role": "user", "content": _PROBE_TOOLS_USER_PROMPT},
            ],
            "max_tokens": 64,
            "tools": [_PROBE_TOOL_SPEC_ANTHROPIC],
        }
    else:
        url = _openai_chat_url(provider)
        headers = _openai_headers(provider)
        body = {
            "model": provider.model,
            "messages": [
                {"role": "user", "content": _PROBE_TOOLS_USER_PROMPT},
            ],
            "max_tokens": 64,
            "temperature": 0,
            "tools": [_PROBE_TOOL_SPEC_OPENAI],
        }

    status, parsed, _raw = await _http_post_json(
        url, headers=headers, body=body, timeout=provider.timeout_s
    )

    if status is None or status >= 400 or parsed is None:
        return ProbeResult(
            name="tool_calls",
            verdict=ProbeVerdict.SKIP,
            detail=(
                f"skipped (upstream status={status!r}); run auth probe "
                "first. Probe re-inspects this on the next invocation."
            ),
        )

    native_tool_call = False
    text_json_tool_call = False
    content_sample = ""
    if provider.kind == "anthropic":
        blocks = parsed.get("content")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    native_tool_call = True
                    break
            content_sample = " ".join(
                str(b.get("text", ""))
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )[:200]
    else:
        msg = _extract_openai_assistant_choice(parsed)
        if msg is not None:
            if msg.get("tool_calls"):
                native_tool_call = True
            content = msg.get("content")
            if isinstance(content, str):
                content_sample = content[:200]

    if not native_tool_call and content_sample:
        _, repaired = repair_tool_calls_in_text(content_sample, ["echo"])
        text_json_tool_call = bool(repaired)

    # Resolve the declared support:
    # - explicit providers.yaml `capabilities.tools` wins (schema default is
    #   False, so "declared" here means the user opted in). We treat the
    #   registry as our fallback source of truth.
    declared_explicit = provider.capabilities.tools
    declared_registry = resolved.tools
    # "declared true" = either explicit opt-in OR registry True.
    # "declared false" = explicit False AND registry False/None.
    declared = declared_explicit or (declared_registry is True)

    if native_tool_call:
        if declared:
            return ProbeResult(
                name="tool_calls",
                verdict=ProbeVerdict.OK,
                detail="native `tool_calls` observed; matches declaration.",
            )
        return ProbeResult(
            name="tool_calls",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                "model emitted native `tool_calls` but neither "
                "providers.yaml nor the registry declares tools=true. "
                "Opt in to unlock tool-bearing prompts."
            ),
            target_file="providers.yaml",
            suggested_patch=_patch_providers_yaml_capability(provider.name, "tools", True),
        )

    if text_json_tool_call:
        # Model wrote tool JSON in text. v0.3-A repair will rescue it,
        # but advertise it as a partial support so operators know.
        if declared:
            return ProbeResult(
                name="tool_calls",
                verdict=ProbeVerdict.NEEDS_TUNING,
                detail=(
                    "model wrote tool JSON in assistant text (not native "
                    "`tool_calls`). v0.3-A repair will rescue it at runtime, "
                    "but the declaration implies native support. Either "
                    "update the model to a tool-native build, or downgrade "
                    "the declaration to rely on repair."
                ),
                target_file="providers.yaml",
                suggested_patch=_patch_providers_yaml_capability(provider.name, "tools", False),
            )
        return ProbeResult(
            name="tool_calls",
            verdict=ProbeVerdict.OK,
            detail=(
                "no native `tool_calls`, but v0.3-A repair extracted tool "
                "JSON from the text — matches declaration tools=false."
            ),
        )

    # Nothing tool-shaped at all.
    if declared:
        return ProbeResult(
            name="tool_calls",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                "declaration says tools=true but model produced neither "
                "native `tool_calls` nor repairable tool JSON. Common for "
                "quantized small models (plan.md §9.4 symptom #2)."
            ),
            target_file="providers.yaml",
            suggested_patch=_patch_providers_yaml_capability(provider.name, "tools", False),
        )
    return ProbeResult(
        name="tool_calls",
        verdict=ProbeVerdict.OK,
        detail="no tool calls, declaration tools=false — consistent.",
    )


async def _probe_thinking(
    provider: ProviderConfig,
    resolved: ResolvedCapabilities,
) -> ProbeResult:
    """Probe 3 — does the model actually emit a ``thinking`` block?

    Only applicable to ``kind: anthropic`` providers (the body field is
    Anthropic-specific; openai_compat providers silently lose it during
    OpenAI-shape translation). If the provider is openai_compat, we
    return SKIP unless they explicitly opted in via
    ``capabilities.thinking: true`` — in which case we still SKIP but
    with a one-line note that the flag currently has no effect for
    that adapter (the v0.5-A gate would still strip it on the way out).
    """
    if provider.kind != "anthropic":
        if provider.capabilities.thinking:
            return ProbeResult(
                name="thinking",
                verdict=ProbeVerdict.SKIP,
                detail=(
                    "capabilities.thinking=true on an openai_compat "
                    "provider has no effect — the thinking block is lost "
                    "during OpenAI-shape translation. Remove the flag or "
                    "switch kind to `anthropic` if the upstream speaks "
                    "Anthropic wire."
                ),
            )
        return ProbeResult(
            name="thinking",
            verdict=ProbeVerdict.SKIP,
            detail="not applicable (kind=openai_compat).",
        )

    url = _anthropic_messages_url(provider)
    headers = _anthropic_headers(provider)
    body: dict[str, Any] = {
        "model": provider.model,
        "messages": [
            {
                "role": "user",
                "content": "Briefly: what is 2+2? Think step by step first.",
            },
        ],
        "max_tokens": 128,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }
    status, parsed, raw = await _http_post_json(
        url, headers=headers, body=body, timeout=provider.timeout_s
    )

    if status is None or status >= 400 or parsed is None:
        # A 400 on the thinking-enabled payload is diagnostic: the
        # model rejected the field. Map to NEEDS_TUNING when the
        # registry / explicit flag promised support, otherwise OK.
        rejected = (
            status is not None and status == 400 and raw is not None and "thinking" in raw.lower()
        )
        declared = provider.capabilities.thinking or (resolved.thinking is True)
        if rejected and declared:
            return ProbeResult(
                name="thinking",
                verdict=ProbeVerdict.NEEDS_TUNING,
                detail=(
                    "upstream rejected `thinking: {type: enabled}` with "
                    "400. Declaration says supported — disable it for "
                    "this provider or refine the registry rule."
                ),
                target_file="providers.yaml",
                suggested_patch=_patch_providers_yaml_capability(provider.name, "thinking", False),
            )
        if rejected and not declared:
            return ProbeResult(
                name="thinking",
                verdict=ProbeVerdict.OK,
                detail="upstream rejects thinking; matches declaration.",
            )
        return ProbeResult(
            name="thinking",
            verdict=ProbeVerdict.SKIP,
            detail=f"skipped (upstream status={status!r}).",
        )

    # Look for a `thinking` block in the response content array.
    emitted = False
    blocks = parsed.get("content")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "thinking":
                emitted = True
                break

    declared = provider.capabilities.thinking or (resolved.thinking is True)

    if emitted and declared:
        return ProbeResult(
            name="thinking",
            verdict=ProbeVerdict.OK,
            detail="thinking block emitted; matches declaration.",
        )
    if emitted and not declared:
        return ProbeResult(
            name="thinking",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                "thinking block emitted but declaration is silent. "
                "Declare support to let the capability gate route to "
                "this provider for thinking-bearing requests."
            ),
            target_file="model-capabilities.yaml",
            suggested_patch=_patch_model_capabilities_yaml(
                match=provider.model, kind="anthropic", key="thinking", value=True
            ),
        )
    if not emitted and declared:
        return ProbeResult(
            name="thinking",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                "declaration says thinking supported but response had no "
                "`thinking` block. The upstream may silently drop it; "
                "disable the flag or narrow the registry rule."
            ),
            target_file="providers.yaml",
            suggested_patch=_patch_providers_yaml_capability(provider.name, "thinking", False),
        )
    return ProbeResult(
        name="thinking",
        verdict=ProbeVerdict.OK,
        detail="no thinking block emitted; matches declaration.",
    )


async def _probe_reasoning_leak(
    provider: ProviderConfig,
    resolved: ResolvedCapabilities,
) -> ProbeResult:
    """Probe 4 — does the upstream leak non-standard reasoning / harness markers?

    Two orthogonal leaks inspected here:

    A. The non-standard ``message.reasoning`` field (v0.5-C).
       The adapter strips it before the response reaches the client, but
       this probe bypasses the adapter and reads the raw body so the
       operator knows whether any ``capability-degraded`` log lines come
       from this provider.

    B. (v1.0-A) Content-embedded harness markers — a ``<think>...</think>``
       block or stop markers (``<|python_tag|>`` / ``<|eot_id|>`` /
       ``<|im_end|>`` / ``<|turn|>`` / ``<|end|>`` / ``<|channel>thought``)
       inside ``message.content``. These slip past the v0.5-C strip (which
       only inspects the ``reasoning`` field), so the v1.0-A
       ``output_filters`` chain is the remediation. When the probe observes
       such markers AND the configured ``output_filters`` list does not
       cover them, a NEEDS_TUNING verdict emits a copy-paste YAML patch.

    Verdict priority: content-embedded leak dominates the reasoning-field
    observation (a NEEDS_TUNING from B overrides an informational OK from
    A) because the user-visible symptom — ``<think>`` rendered in the
    Claude Code UI — is the one operators actually feel.
    """
    if provider.kind != "openai_compat":
        return ProbeResult(
            name="reasoning-leak",
            verdict=ProbeVerdict.SKIP,
            detail=(
                "not applicable (only openai_compat emits the non-standard "
                "reasoning field; Anthropic content blocks would need a "
                "different probe)."
            ),
        )

    url = _openai_chat_url(provider)
    headers = _openai_headers(provider)
    # Nudge models that default to thinking into emitting the block, so
    # the content-embedded check has something to look at when the model
    # is genuinely leaky. A model that ignores the nudge will still be
    # tested against the reasoning-field observation from its plain reply.
    body = {
        "model": provider.model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Think step by step about the capital of France, then answer in one word."
                ),
            },
        ],
        "max_tokens": 128,
        "temperature": 0,
    }
    status, parsed, _raw = await _http_post_json(
        url, headers=headers, body=body, timeout=provider.timeout_s
    )

    if status is None or status >= 400 or parsed is None:
        return ProbeResult(
            name="reasoning-leak",
            verdict=ProbeVerdict.SKIP,
            detail=f"skipped (upstream status={status!r}).",
        )

    msg = _extract_openai_assistant_choice(parsed)
    has_reasoning = bool(msg and "reasoning" in msg)

    # v1.0-A: content-embedded marker detection.
    content = (msg.get("content") if isinstance(msg, dict) else None) or ""
    content_text = content if isinstance(content, str) else ""
    has_think = "<think>" in content_text
    leaked_markers: list[str] = [m for m in DEFAULT_STOP_MARKERS if m in content_text]
    configured_filters = set(provider.output_filters)
    needs_strip_thinking = has_think and "strip_thinking" not in configured_filters
    needs_strip_markers = bool(leaked_markers) and "strip_stop_markers" not in configured_filters

    if needs_strip_thinking or needs_strip_markers:
        # Dominant signal — emit NEEDS_TUNING with a copy-paste patch
        # that adds exactly the filters that would have caught this
        # observation. A provider already running one filter and newly
        # tripping on the other is rare; we still emit the full needed
        # set so operators see the complete remediation.
        recommended: list[str] = []
        if needs_strip_thinking:
            recommended.append("strip_thinking")
        if needs_strip_markers:
            recommended.append("strip_stop_markers")

        found_desc: list[str] = []
        if has_think:
            found_desc.append("<think>...</think>")
        if leaked_markers:
            found_desc.append("stop markers " + ", ".join(repr(m) for m in leaked_markers))

        return ProbeResult(
            name="reasoning-leak",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail=(
                "content-embedded leak detected ("
                + " + ".join(found_desc)
                + "). v1.0-A `output_filters` would scrub this; current "
                f"provider chain = {sorted(configured_filters)}. Recommended: "
                f"add {recommended}."
            ),
            target_file="providers.yaml",
            suggested_patch=_patch_providers_yaml_output_filters(provider.name, recommended),
        )

    passthrough_on = (
        provider.capabilities.reasoning_passthrough or resolved.reasoning_passthrough is True
    )

    if has_reasoning and passthrough_on:
        return ProbeResult(
            name="reasoning-leak",
            verdict=ProbeVerdict.OK,
            detail=(
                "upstream emits `reasoning`; passthrough is on, so the "
                "field reaches clients as intended."
            ),
        )
    if has_reasoning and not passthrough_on:
        # Default behavior — v0.5-C strip removes it. No tuning needed;
        # this is expected. Emit OK with an informational note so the
        # operator understands where any `capability-degraded` logs
        # originate.
        return ProbeResult(
            name="reasoning-leak",
            verdict=ProbeVerdict.OK,
            detail=(
                "upstream emits non-standard `reasoning`; v0.5-C adapter "
                "strips it before it reaches the client (expected — "
                "expect `capability-degraded` log lines for this provider)."
            ),
        )
    return ProbeResult(
        name="reasoning-leak",
        verdict=ProbeVerdict.OK,
        detail=(
            "no `reasoning` field observed and no content-embedded markers — nothing to strip."
        ),
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def check_model(
    config: CodeRouterConfig,
    provider_name: str,
    *,
    registry: CapabilityRegistry | None = None,
) -> DoctorReport:
    """Run the full probe suite against ``provider_name`` in ``config``.

    The auth probe runs first; if it fails, remaining probes are
    returned as SKIP (the suite does not waste tokens against a
    provider that can't respond).

    ``registry`` is optional for testing — production callers pass
    nothing and the function uses the process-wide default (same
    registry the capability gate consults).
    """
    try:
        provider = config.provider_by_name(provider_name)
    except KeyError as exc:
        raise KeyError(
            f"provider {provider_name!r} not found in providers.yaml. "
            f"Known: {sorted(p.name for p in config.providers)}"
        ) from exc

    reg = registry if registry is not None else get_default_registry()
    resolved = reg.lookup(kind=provider.kind, model=provider.model or "")

    report = DoctorReport(
        provider_name=provider_name,
        provider=provider,
        resolved_caps=resolved,
    )

    auth_result = await _probe_auth_and_basic_chat(provider)
    report.results.append(auth_result)

    if auth_result.verdict != ProbeVerdict.OK:
        # Auth dominates; mark the other probes SKIP so the report
        # still lists them (operators can see at a glance what wasn't
        # checked) without spending tokens / API quota.
        for name in (
            "num_ctx",
            "tool_calls",
            "thinking",
            "reasoning-leak",
            "streaming",
        ):
            report.results.append(
                ProbeResult(
                    name=name,
                    verdict=ProbeVerdict.SKIP,
                    detail="skipped — auth probe did not succeed.",
                )
            )
        return report

    # v1.0-B: num_ctx probe runs before tool_calls. When Ollama silently
    # truncates the prompt the assistant often replies without tool calls,
    # which used to flag as a tools=false NEEDS_TUNING in v0.7-B. Putting
    # num_ctx first ensures the truncation verdict dominates the report so
    # operators apply the right remediation (bump num_ctx, not disable tools).
    # v1.0-C: streaming probe runs last. The input-side (num_ctx) and
    # declaration probes (tool_calls / thinking / reasoning-leak) should
    # dominate the report — streaming is the output-side sibling of
    # num_ctx and its NEEDS_TUNING verdict is orthogonal to the others.
    report.results.append(await _probe_num_ctx(provider))
    report.results.append(await _probe_tool_calls(provider, resolved))
    report.results.append(await _probe_thinking(provider, resolved))
    report.results.append(await _probe_reasoning_leak(provider, resolved))
    report.results.append(await _probe_streaming(provider))
    return report


def run_check_model_sync(
    config: CodeRouterConfig,
    provider_name: str,
    *,
    registry: CapabilityRegistry | None = None,
) -> DoctorReport:
    """Sync wrapper — called from the CLI which is not otherwise async."""
    return asyncio.run(check_model(config, provider_name, registry=registry))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


_VERDICT_BADGE = {
    ProbeVerdict.OK: "[OK]",
    ProbeVerdict.SKIP: "[SKIP]",
    ProbeVerdict.NEEDS_TUNING: "[NEEDS TUNING]",
    ProbeVerdict.UNSUPPORTED: "[UNSUPPORTED]",
    ProbeVerdict.AUTH_FAIL: "[AUTH FAIL]",
    ProbeVerdict.TRANSPORT_ERROR: "[TRANSPORT ERROR]",
}


def format_report(report: DoctorReport) -> str:
    """Human-readable, line-oriented report. Goes to stdout."""
    p = report.provider
    caps = report.resolved_caps
    lines: list[str] = []
    lines.append(f"coderouter doctor --check-model {report.provider_name}")
    lines.append("─" * 60)
    lines.append(f"provider:   {p.name}")
    lines.append(f"  kind:     {p.kind}")
    lines.append(f"  base_url: {p.base_url}")
    lines.append(f"  model:    {p.model}")

    lines.append("")
    lines.append("Registry + providers.yaml declarations:")
    lines.append(
        f"  thinking:              providers={p.capabilities.thinking}, registry={caps.thinking}"
    )
    lines.append(
        f"  tools:                 providers={p.capabilities.tools}, registry={caps.tools}"
    )
    lines.append(
        f"  reasoning_passthrough: providers={p.capabilities.reasoning_passthrough}, "
        f"registry={caps.reasoning_passthrough}"
    )
    # v1.0-A: surface the output_filters chain so operators can see at a
    # glance which filters are active before running the probes.
    lines.append(f"  output_filters:        providers={list(p.output_filters)}")

    lines.append("")
    lines.append("Probes:")
    for i, r in enumerate(report.results, start=1):
        badge = _VERDICT_BADGE[r.verdict]
        lines.append(f"  [{i}/{len(report.results)}] {r.name} …… {badge}")
        for dline in r.detail.splitlines():
            lines.append(f"      {dline}")
        if r.suggested_patch:
            lines.append(f"      Suggested patch → {r.target_file}:")
            for pl in r.suggested_patch.splitlines():
                lines.append(f"        {pl}")

    lines.append("")
    code = exit_code_for(report)
    summary = {
        0: "all probes match declarations.",
        1: "at least one probe could not run (auth/transport/model).",
        2: "at least one probe needs tuning (see suggested patches).",
    }[code]
    lines.append(f"Summary: {summary}")
    lines.append(f"Exit: {code}")
    return "\n".join(lines)


def _probes_by_name(results: Sequence[ProbeResult]) -> dict[str, ProbeResult]:
    """Small convenience for tests that want to assert on one probe."""
    return {r.name: r for r in results}
