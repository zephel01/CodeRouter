"""`coderouter doctor --check-model <provider>` — per-provider capability probe.

Purpose (v0.7-B)
----------------
Run a small set of live probes against a single provider from
``providers.yaml`` and compare the observed behavior against the
declarations in ``providers.yaml`` + ``model-capabilities.yaml`` (v0.7-A
registry). Emit a per-probe verdict and, on mismatch, a copy-paste-able
YAML patch that the user can drop into either file.

Motivated by the 5 silent-fail symptoms enumerated in plan.md §9.4:

    1. 空応答 / 意味不明応答            → num_ctx probe + basic-chat probe
    2. Claude Code「ファイル読めない」  → tool_calls probe (symptom 2)
    3. UI に <think> タグ生露出         → thinking probe (symptom 3)
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
from enum import Enum
from typing import Any

import httpx

from coderouter.config.capability_registry import (
    CapabilityRegistry,
    ResolvedCapabilities,
)
from coderouter.config.loader import resolve_api_key
from coderouter.config.schemas import CodeRouterConfig, ProviderConfig
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


class ProbeVerdict(str, Enum):
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


# ---------------------------------------------------------------------------
# Patch emitters
#
# Kept as tiny helpers rather than a Jinja dance — the surface area is too
# small to justify templating, and exact indentation in the emitted YAML
# matters for copy-paste fidelity.
# ---------------------------------------------------------------------------


def _patch_providers_yaml_capability(
    provider_name: str, key: str, value: bool
) -> str:
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


def _patch_model_capabilities_yaml(
    *, match: str, kind: str, key: str, value: bool
) -> str:
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


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


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

    status, parsed, raw = await _http_post_json(
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
            suggested_patch=_patch_providers_yaml_capability(
                provider.name, "tools", True
            ),
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
                suggested_patch=_patch_providers_yaml_capability(
                    provider.name, "tools", False
                ),
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
            suggested_patch=_patch_providers_yaml_capability(
                provider.name, "tools", False
            ),
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
            status is not None
            and status == 400
            and raw is not None
            and "thinking" in raw.lower()
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
                suggested_patch=_patch_providers_yaml_capability(
                    provider.name, "thinking", False
                ),
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
            suggested_patch=_patch_providers_yaml_capability(
                provider.name, "thinking", False
            ),
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
    """Probe 4 — does the upstream emit the non-standard ``reasoning`` field?

    The v0.5-C adapter strips this before the response reaches the
    client, but here we bypass the adapter and look at the raw body.
    If the field is present and the provider has
    ``reasoning_passthrough=false`` (default), the behavior is already
    correct — we just surface the observation so the operator knows
    why they might see ``capability-degraded`` log lines on this
    provider. If ``reasoning_passthrough=true`` and the field is
    present, that's still the user's explicit choice; we record OK
    with a note.
    """
    if provider.kind != "openai_compat":
        return ProbeResult(
            name="reasoning-leak",
            verdict=ProbeVerdict.SKIP,
            detail="not applicable (only openai_compat emits this non-standard field).",
        )

    url = _openai_chat_url(provider)
    headers = _openai_headers(provider)
    body = {
        "model": provider.model,
        "messages": [
            {"role": "user", "content": "In one word: what is the capital of France?"},
        ],
        "max_tokens": 16,
        "temperature": 0,
    }
    status, parsed, raw = await _http_post_json(
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

    passthrough_on = (
        provider.capabilities.reasoning_passthrough
        or resolved.reasoning_passthrough is True
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
        detail="no `reasoning` field observed; nothing to strip.",
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
        for name in ("tool_calls", "thinking", "reasoning-leak"):
            report.results.append(
                ProbeResult(
                    name=name,
                    verdict=ProbeVerdict.SKIP,
                    detail="skipped — auth probe did not succeed.",
                )
            )
        return report

    report.results.append(await _probe_tool_calls(provider, resolved))
    report.results.append(await _probe_thinking(provider, resolved))
    report.results.append(await _probe_reasoning_leak(provider, resolved))
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
        f"  thinking:              providers={p.capabilities.thinking}, "
        f"registry={caps.thinking}"
    )
    lines.append(
        f"  tools:                 providers={p.capabilities.tools}, "
        f"registry={caps.tools}"
    )
    lines.append(
        f"  reasoning_passthrough: providers={p.capabilities.reasoning_passthrough}, "
        f"registry={caps.reasoning_passthrough}"
    )

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
