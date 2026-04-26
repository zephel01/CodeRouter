"""Tests for ``coderouter.doctor`` — v0.7-B per-provider probe suite.

Coverage map (mirrors plan.md §9.4 DoD):

    * Auth probe (symptoms 4 + 5):
        401 → AUTH_FAIL → exit 1
        403 → AUTH_FAIL → exit 1
        404 → UNSUPPORTED (model-not-found) → exit 1
        transport error → TRANSPORT_ERROR → exit 1
        200 + non-empty → OK → subsequent probes run

    * Tool-calls probe (symptom 2):
        native tool_calls + registry=True → OK
        native tool_calls + registry=False → NEEDS_TUNING (patch providers.yaml)
        text-JSON only + registry=False → OK (repair catches at runtime)
        text-JSON only + registry=True → NEEDS_TUNING (narrow declaration)
        no tool activity + registry=True → NEEDS_TUNING (flip to False)
        no tool activity + registry=False → OK

    * Thinking probe (symptom 3):
        anthropic + block emitted + declared → OK
        anthropic + no block + declared → NEEDS_TUNING
        anthropic + 400 rejection + declared → NEEDS_TUNING
        openai_compat → SKIP (non-applicable)

    * Reasoning leak probe (v0.5-C observability):
        openai_compat + reasoning field present, passthrough=false → OK (informational)
        openai_compat + no reasoning field → OK
        anthropic → SKIP

    * Report / exit code:
        NEEDS_TUNING alone → exit 2
        AUTH_FAIL dominates NEEDS_TUNING → exit 1
        all OK → exit 0

    * Patch emitters: well-formed YAML, target correct file.

    * Orchestration: auth fail short-circuits remaining probes (SKIP);
      unknown provider name raises KeyError; registry injection works.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from coderouter.config.capability_registry import (
    CapabilityRegistry,
    CapabilityRule,
    RegistryCapabilities,
)
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.doctor import (
    _NUM_CTX_PROBE_CANARY,
    DoctorReport,
    ProbeResult,
    ProbeVerdict,
    _patch_model_capabilities_yaml,
    _patch_providers_yaml_capability,
    _patch_providers_yaml_num_ctx,
    _patch_providers_yaml_num_predict,
    _probes_by_name,
    check_model,
    exit_code_for,
    format_report,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _oa_provider(
    *,
    name: str = "local",
    base_url: str = "http://localhost:8080/v1",
    model: str = "qwen3-coder:7b",
    api_key_env: str | None = None,
    caps: Capabilities | None = None,
    timeout_s: float = 5.0,
    extra_body: dict[str, Any] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="openai_compat",
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        capabilities=caps or Capabilities(),
        timeout_s=timeout_s,
        extra_body=extra_body or {},
    )


def _anthropic_provider(
    *,
    name: str = "anth",
    base_url: str = "https://api.anthropic.com",
    model: str = "claude-opus-4-8",
    api_key_env: str | None = None,
    caps: Capabilities | None = None,
    timeout_s: float = 5.0,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="anthropic",
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        capabilities=caps or Capabilities(),
        timeout_s=timeout_s,
    )


def _config_for(providers: list[ProviderConfig]) -> CodeRouterConfig:
    return CodeRouterConfig(
        providers=providers,
        profiles=[FallbackChain(name="default", providers=[providers[0].name])],
    )


def _empty_registry() -> CapabilityRegistry:
    """Registry that declares nothing — all lookups return None."""
    return CapabilityRegistry([])


def _registry_with_tools_true(match: str = "*") -> CapabilityRegistry:
    return CapabilityRegistry(
        [
            CapabilityRule(
                match=match,
                kind="any",
                capabilities=RegistryCapabilities(tools=True),
            )
        ]
    )


def _registry_with_thinking_true(match: str = "claude-opus-4-*") -> CapabilityRegistry:
    return CapabilityRegistry(
        [
            CapabilityRule(
                match=match,
                kind="anthropic",
                capabilities=RegistryCapabilities(thinking=True),
            )
        ]
    )


def _openai_ok_response(
    *,
    content: str | None = "PONG",
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    if reasoning is not None:
        msg["reasoning"] = reasoning
    return {
        "id": "chatcmpl-probe",
        "object": "chat.completion",
        "created": 0,
        "model": "probe",
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1},
    }


def _sse_stream_count_body(
    *,
    numbers: int = 30,
    finish_reason: str = "stop",
    include_done: bool = True,
) -> bytes:
    """v1.0-C helper: build an SSE body that simulates a successful
    "count from 1 to N" streaming completion.

    Default (numbers=30, finish_reason='stop', include_done=True) produces
    ~80 chars of content — comfortably above the streaming probe's
    40-char floor. Callers shorten ``numbers`` to simulate premature
    termination (combined with ``finish_reason='length'``).
    """
    pieces: list[str] = []
    for i in range(1, numbers + 1):
        pieces.append(
            "data: "
            + json.dumps(
                {
                    "id": "s",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "probe-stream",
                    "choices": [{"index": 0, "delta": {"content": f"{i}\n"}}],
                }
            )
            + "\n\n"
        )
    # Closing chunk with the finish_reason.
    pieces.append(
        "data: "
        + json.dumps(
            {
                "id": "s",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "probe-stream",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
        )
        + "\n\n"
    )
    if include_done:
        pieces.append("data: [DONE]\n\n")
    return "".join(pieces).encode("utf-8")


def _add_sse_ok_mock(httpx_mock: HTTPXMock, url: str, **kwargs: Any) -> None:
    """Register a successful streaming mock in one call.

    Shared by existing Ollama-shape num_ctx tests that now need a 5th
    mock for the v1.0-C streaming probe at the tail of the probe chain.
    """
    httpx_mock.add_response(
        url=url,
        method="POST",
        content=_sse_stream_count_body(**kwargs),
        headers={"content-type": "text/event-stream"},
    )


def _anthropic_ok_response(
    *,
    text_blocks: list[str] | None = None,
    tool_use: dict[str, Any] | None = None,
    thinking_text: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if thinking_text is not None:
        content.append({"type": "thinking", "thinking": thinking_text})
    for t in text_blocks or []:
        content.append({"type": "text", "text": t})
    if tool_use is not None:
        content.append({"type": "tool_use", **tool_use})
    return {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "model": "probe",
        "content": content,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }


# ---------------------------------------------------------------------------
# Patch emitters
# ---------------------------------------------------------------------------


def test_patch_providers_yaml_contains_provider_name_and_key() -> None:
    """Per-provider patch targets the named provider and the right key."""
    out = _patch_providers_yaml_capability("local", "tools", False)
    assert "local" in out
    assert "tools: false" in out
    assert "capabilities:" in out
    # Header comment points the user at the right file.
    assert out.startswith("# providers.yaml")


def test_patch_model_capabilities_yaml_emits_rule_fragment() -> None:
    """Glob-level patch targets the registry file with kind filter."""
    out = _patch_model_capabilities_yaml(
        match="qwen3-coder:*", kind="openai_compat", key="tools", value=True
    )
    assert "rules:" in out
    assert "match: 'qwen3-coder:*'" in out
    assert "kind: openai_compat" in out
    assert "tools: true" in out
    # Header comment points at the right file.
    assert "model-capabilities.yaml" in out


def test_patch_is_loadable_yaml() -> None:
    """The snippet must parse as YAML so users can literally copy-paste."""
    import yaml

    out = _patch_providers_yaml_capability("foo", "thinking", True)
    # Strip leading comment lines before parsing.
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    parsed = yaml.safe_load(body)
    assert parsed == {
        "providers": [
            {
                "name": "foo",
                "capabilities": {"thinking": True},
            }
        ]
    }


def test_patch_num_ctx_contains_nested_options() -> None:
    """v1.0-B num_ctx patch targets extra_body.options.num_ctx."""
    out = _patch_providers_yaml_num_ctx("ollama-qwen", 32768)
    assert "ollama-qwen" in out
    assert "extra_body:" in out
    assert "options:" in out
    assert "num_ctx: 32768" in out
    assert out.startswith("# providers.yaml")


def test_patch_num_ctx_is_loadable_yaml() -> None:
    """The num_ctx patch parses as YAML with the expected nested shape."""
    import yaml

    out = _patch_providers_yaml_num_ctx("ollama-qwen", 16384)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    parsed = yaml.safe_load(body)
    assert parsed == {
        "providers": [
            {
                "name": "ollama-qwen",
                "extra_body": {"options": {"num_ctx": 16384}},
            }
        ]
    }


def test_patch_num_predict_contains_nested_options() -> None:
    """v1.0-C num_predict patch targets extra_body.options.num_predict."""
    out = _patch_providers_yaml_num_predict("ollama-qwen", 4096)
    assert "ollama-qwen" in out
    assert "extra_body:" in out
    assert "options:" in out
    assert "num_predict: 4096" in out
    assert out.startswith("# providers.yaml")
    # Crucially, this patch does NOT name num_ctx — a user merging both
    # patches will end up with both keys under options (expected).
    assert "num_ctx" not in out


def test_patch_num_predict_is_loadable_yaml() -> None:
    """The num_predict patch parses as YAML with the expected nested shape."""
    import yaml

    out = _patch_providers_yaml_num_predict("ollama-qwen", 8192)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    parsed = yaml.safe_load(body)
    assert parsed == {
        "providers": [
            {
                "name": "ollama-qwen",
                "extra_body": {"options": {"num_predict": 8192}},
            }
        ]
    }


# ---------------------------------------------------------------------------
# Auth probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_probe_401_returns_auth_fail_and_short_circuits(
    httpx_mock: HTTPXMock,
) -> None:
    """401 from upstream → AUTH_FAIL; remaining probes become SKIP."""
    provider = _oa_provider(api_key_env="CR_TEST_MISSING_KEY")
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=401,
        json={"error": "unauthorized"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    by_name = _probes_by_name(report.results)
    assert by_name["auth+basic-chat"].verdict == ProbeVerdict.AUTH_FAIL
    # Short-circuit: tool_calls / thinking / reasoning-leak all SKIP.
    assert by_name["tool_calls"].verdict == ProbeVerdict.SKIP
    assert by_name["thinking"].verdict == ProbeVerdict.SKIP
    assert by_name["reasoning-leak"].verdict == ProbeVerdict.SKIP
    assert exit_code_for(report) == 1


@pytest.mark.asyncio
async def test_auth_probe_403_returns_auth_fail(
    httpx_mock: HTTPXMock,
) -> None:
    """403 is the same class of failure as 401 for auth semantics."""
    provider = _oa_provider(api_key_env="CR_TEST_MISSING_KEY")
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=403,
        json={"error": "forbidden"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    auth = _probes_by_name(report.results)["auth+basic-chat"]
    assert auth.verdict == ProbeVerdict.AUTH_FAIL
    # Detail names the env var so the operator knows where to look.
    assert "CR_TEST_MISSING_KEY" in auth.detail


@pytest.mark.asyncio
async def test_auth_probe_404_reports_model_not_installed(
    httpx_mock: HTTPXMock,
) -> None:
    """404 on openai_compat probe means the model string is wrong / not pulled."""
    provider = _oa_provider(model="qwen3-mystery:1b")
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=404,
        json={"error": "model not found"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    auth = _probes_by_name(report.results)["auth+basic-chat"]
    assert auth.verdict == ProbeVerdict.UNSUPPORTED
    # Hint surfaces the model name so the operator can cross-check.
    assert "qwen3-mystery:1b" in auth.detail
    assert exit_code_for(report) == 1


@pytest.mark.asyncio
async def test_auth_probe_transport_error_maps_to_transport_error() -> None:
    """Unreachable host → TRANSPORT_ERROR → exit 1. No mock needed."""
    provider = _oa_provider(
        base_url="http://127.0.0.1:1/v1",  # invalid port forces connect fail
        timeout_s=1.0,
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    auth = _probes_by_name(report.results)["auth+basic-chat"]
    assert auth.verdict == ProbeVerdict.TRANSPORT_ERROR
    assert exit_code_for(report) == 1


@pytest.mark.asyncio
async def test_auth_probe_2xx_unparseable_body(
    httpx_mock: HTTPXMock,
) -> None:
    """2xx but garbage body is a protocol-level transport error."""
    provider = _oa_provider()
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        content=b"not json at all",
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    auth = _probes_by_name(report.results)["auth+basic-chat"]
    assert auth.verdict == ProbeVerdict.TRANSPORT_ERROR


# ---------------------------------------------------------------------------
# v1.0-B: num_ctx probe
#
# Direct detection of Ollama's silent prompt truncation (plan.md §9.4
# symptom #1). The probe fires only for Ollama-shape providers — port
# 11434 in the base URL or an explicit ``extra_body.options.num_ctx``
# declaration. On non-Ollama upstreams it SKIPs without issuing HTTP,
# which lets every existing test (default fixture → :8080) remain
# unchanged.
#
# The probe embeds a canary at the front of a ~5k-token prompt and
# asks the model to echo it back. A model at Ollama's 2048-token
# default loses the canary (the front of the prompt is truncated) and
# cannot echo it. A properly-bumped ``num_ctx`` preserves it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_num_ctx_skips_for_non_ollama_port(
    httpx_mock: HTTPXMock,
) -> None:
    """Default :8080 base_url → num_ctx probe SKIPs, no HTTP call issued."""
    provider = _oa_provider()  # :8080, no extra_body
    # Only auth + tool_calls + reasoning-leak hit the endpoint.
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    num_ctx = _probes_by_name(report.results)["num_ctx"]
    assert num_ctx.verdict == ProbeVerdict.SKIP
    assert "Ollama-shape" in num_ctx.detail


@pytest.mark.asyncio
async def test_num_ctx_ollama_port_canary_missing_suggests_patch(
    httpx_mock: HTTPXMock,
) -> None:
    """Ollama default 2048 → canary dropped → NEEDS_TUNING with patch.

    Simulates the typical fresh-install Ollama symptom: the upstream
    silently drops the front of the prompt so the model responds with
    something that doesn't contain the canary token.
    """
    provider = _oa_provider(
        name="ollama-default",
        base_url="http://localhost:11434/v1",
        model="qwen2.5-coder:7b",
    )
    # auth OK
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # num_ctx: model replies without echoing the canary (truncation).
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="I don't see any canary token."),
    )
    # tool_calls
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="nothing"),
    )
    # thinking SKIP — openai_compat.
    # reasoning-leak
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="Paris"),
    )
    # v1.0-C streaming probe — succeeds; not the subject of this test.
    _add_sse_ok_mock(httpx_mock, "http://localhost:11434/v1/chat/completions")
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    num_ctx = _probes_by_name(report.results)["num_ctx"]
    assert num_ctx.verdict == ProbeVerdict.NEEDS_TUNING
    assert num_ctx.target_file == "providers.yaml"
    assert num_ctx.suggested_patch is not None
    assert "num_ctx: 32768" in num_ctx.suggested_patch
    assert "options:" in num_ctx.suggested_patch
    assert exit_code_for(report) == 2


@pytest.mark.asyncio
async def test_num_ctx_declared_high_canary_echoed_is_ok(
    httpx_mock: HTTPXMock,
) -> None:
    """Operator already set num_ctx=32768 → canary survives → OK."""
    provider = _oa_provider(
        name="ollama-tuned",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    # auth OK
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # num_ctx: canary echoed.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    # tool_calls + reasoning-leak
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # v1.0-C streaming probe — succeeds.
    _add_sse_ok_mock(httpx_mock, "http://localhost:11434/v1/chat/completions")
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    num_ctx = _probes_by_name(report.results)["num_ctx"]
    assert num_ctx.verdict == ProbeVerdict.OK
    assert "32768" in num_ctx.detail
    assert exit_code_for(report) == 0


@pytest.mark.asyncio
async def test_num_ctx_declared_low_canary_missing_bumps(
    httpx_mock: HTTPXMock,
) -> None:
    """Operator set num_ctx=4096 (still too low) → NEEDS_TUNING → bump to 32768."""
    provider = _oa_provider(
        name="ollama-low",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 4096}},
    )
    # auth OK
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # num_ctx: canary missing.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="I'm not sure."),
    )
    # tool_calls + reasoning-leak
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # v1.0-C streaming probe — succeeds.
    _add_sse_ok_mock(httpx_mock, "http://localhost:11434/v1/chat/completions")
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    num_ctx = _probes_by_name(report.results)["num_ctx"]
    assert num_ctx.verdict == ProbeVerdict.NEEDS_TUNING
    assert "4096" in num_ctx.detail
    assert "num_ctx: 32768" in (num_ctx.suggested_patch or "")


@pytest.mark.asyncio
async def test_num_ctx_declared_adequate_canary_missing_informs_intrinsic_limit(
    httpx_mock: HTTPXMock,
) -> None:
    """Operator declared num_ctx=16384 but the canary is still missing →
    NEEDS_TUNING that warns about the model's intrinsic limit."""
    provider = _oa_provider(
        name="ollama-highish",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 16384}},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # num_ctx: canary missing despite adequate-looking declared value.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="Sorry, nothing like that."),
    )
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # v1.0-C streaming probe — succeeds.
    _add_sse_ok_mock(httpx_mock, "http://localhost:11434/v1/chat/completions")
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    num_ctx = _probes_by_name(report.results)["num_ctx"]
    assert num_ctx.verdict == ProbeVerdict.NEEDS_TUNING
    # The detail should flag the model's intrinsic limit as the suspect.
    assert "intrinsic" in num_ctx.detail.lower()


@pytest.mark.asyncio
async def test_num_ctx_extra_body_signal_fires_on_non_11434_port(
    httpx_mock: HTTPXMock,
) -> None:
    """Operator on a non-default Ollama port (say :12345) with a num_ctx
    declaration → probe still fires because the declaration itself signals
    Ollama-shape."""
    provider = _oa_provider(
        name="ollama-custom-port",
        base_url="http://localhost:12345/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:12345/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # num_ctx: canary echoed (adequate).
    httpx_mock.add_response(
        url="http://localhost:12345/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:12345/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # v1.0-C streaming probe — also fires on the non-11434 port because
    # the same Ollama-shape signal (declared options) triggers both.
    _add_sse_ok_mock(httpx_mock, "http://localhost:12345/v1/chat/completions")
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    num_ctx = _probes_by_name(report.results)["num_ctx"]
    # Must NOT be SKIP — the extra_body signal should fire.
    assert num_ctx.verdict == ProbeVerdict.OK


@pytest.mark.asyncio
async def test_num_ctx_request_body_merges_extra_body_options(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must forward ``extra_body`` into the outbound request so
    the upstream actually gets the declared ``options.num_ctx``."""
    provider = _oa_provider(
        name="ollama-merge",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768, "keep_alive": "5m"}},
    )
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY))

    httpx_mock.add_callback(
        _capture,
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    # Second request is the num_ctx probe (after auth). Its body should
    # carry the merged options.
    import json as _json

    num_ctx_body = _json.loads(captured[1].content.decode("utf-8"))
    assert num_ctx_body["options"] == {"num_ctx": 32768, "keep_alive": "5m"}
    # And the probe's own fields must be present and dominate over any
    # extra_body collisions on top-level keys.
    assert num_ctx_body["model"] == provider.model
    # v1.8.2: default probe budget bumped 32 → 256. Thinking-flagged
    # models bump further to 1024 (covered by a dedicated test below).
    # The provider here has no `capabilities.thinking`, so the default
    # baseline applies.
    assert num_ctx_body["max_tokens"] == 256


@pytest.mark.asyncio
async def test_num_ctx_max_tokens_bumped_for_thinking_provider_declaration(
    httpx_mock: HTTPXMock,
) -> None:
    """v1.8.2: provider declared ``capabilities.thinking: true`` →
    num_ctx probe budget is the thinking variant (1024) instead of the
    256 baseline.

    Thinking models (Gemma 4 26B, Qwen3-with-/think, gpt-oss, deepseek-r1)
    burn output tokens on a hidden ``reasoning`` trace before any visible
    ``content`` is emitted. The pre-v1.8.2 default of 32 caused
    ``finish_reason='length'`` with empty content, producing a
    false-positive NEEDS_TUNING. Bumping the budget to 1024 gives the
    reasoning trace + canary echo room to surface.
    """
    thinking_caps = Capabilities(thinking=True, tools=True)
    provider = _oa_provider(
        name="ollama-gemma4-26b",
        base_url="http://localhost:11434/v1",
        model="gemma4:26b",
        caps=thinking_caps,
        extra_body={"options": {"num_ctx": 32768}},
    )
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream") is True:
            return httpx.Response(
                200,
                content=_sse_stream_count_body(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200, json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY)
        )

    httpx_mock.add_callback(
        _capture,
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
    # auth → captured[0], num_ctx → captured[1]
    num_ctx_body = json.loads(captured[1].content.decode("utf-8"))
    assert num_ctx_body["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_num_ctx_max_tokens_bumped_when_registry_says_thinking(
    httpx_mock: HTTPXMock,
) -> None:
    """v1.8.2: provider declares no thinking but registry says thinking=true
    for the (kind, model) → still bump to 1024.

    This mirrors the production path: bundled model-capabilities.yaml
    declares ``thinking: true`` for ``gemma4:*`` / ``qwen3.6:*`` so
    operators don't have to repeat the flag in every providers.yaml.
    """
    provider = _oa_provider(
        name="ollama-gemma4-26b",
        base_url="http://localhost:11434/v1",
        model="gemma4:26b",
        # capabilities.thinking left at the default (False)
        extra_body={"options": {"num_ctx": 32768}},
    )
    registry = CapabilityRegistry(
        [
            CapabilityRule(
                match="gemma4:*",
                kind="openai_compat",
                capabilities=RegistryCapabilities(thinking=True),
            )
        ]
    )
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream") is True:
            return httpx.Response(
                200,
                content=_sse_stream_count_body(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200, json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY)
        )

    httpx_mock.add_callback(
        _capture,
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    await check_model(_config_for([provider]), provider.name, registry=registry)
    num_ctx_body = json.loads(captured[1].content.decode("utf-8"))
    assert num_ctx_body["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_tool_calls_max_tokens_bumped_for_thinking_provider(
    httpx_mock: HTTPXMock,
) -> None:
    """v1.8.3: tool_calls probe uses thinking-aware budget too.

    Pre-v1.8.3 the probe sent ``max_tokens=64``, which thinking models
    (Qwen3.6, Gemma 4, gpt-oss, deepseek-r1) consume entirely on the
    ``reasoning_content`` field before any ``tool_calls`` can surface —
    producing a false-positive NEEDS_TUNING that recommended flipping
    ``tools`` to false despite the model supporting tools perfectly
    (observed 2026-04-26 with Qwen3.6:35b-a3b on llama-server).
    """
    thinking_caps = Capabilities(thinking=True, tools=True)
    provider = _oa_provider(
        name="llamacpp-qwen3-6-35b-a3b",
        base_url="http://localhost:8080/v1",
        model="qwen3.6",
        caps=thinking_caps,
    )
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if "tools" in body:
            # tool_calls probe — return a native tool_calls structure.
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-probe",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "probe",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "echo",
                                            "arguments": '{"message":"probe"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 1},
                },
            )
        return httpx.Response(200, json=_openai_ok_response(content="PONG"))

    httpx_mock.add_callback(
        _capture,
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
    # Identify the tool_calls probe request by the presence of ``tools``.
    tool_calls_bodies = [
        json.loads(req.content.decode("utf-8"))
        for req in captured
        if "tools" in json.loads(req.content.decode("utf-8"))
    ]
    assert len(tool_calls_bodies) == 1
    assert tool_calls_bodies[0]["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_streaming_max_tokens_bumped_for_thinking_provider(
    httpx_mock: HTTPXMock,
) -> None:
    """v1.8.2: streaming probe also uses the thinking-aware budget — same
    rationale as num_ctx (reasoning trace burns through the small default).

    Pre-v1.8.2 streaming used ``max_tokens=128`` and Gemma 4 reported
    ``finish_reason='length'`` after 0 chars of content. Bumping to 1024
    lets the reasoning prefix + the "1..30" answer fit.
    """
    thinking_caps = Capabilities(thinking=True, tools=True)
    provider = _oa_provider(
        name="ollama-gemma4-26b",
        base_url="http://localhost:11434/v1",
        model="gemma4:26b",
        caps=thinking_caps,
        extra_body={"options": {"num_ctx": 32768}},
    )
    captured: list[httpx.Request] = []

    def _route(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream") is True:
            return httpx.Response(
                200,
                content=_sse_stream_count_body(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200, json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY)
        )

    httpx_mock.add_callback(
        _route,
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
    # The streaming probe runs last; identify it by ``stream: true``.
    stream_bodies = [
        json.loads(req.content.decode("utf-8"))
        for req in captured
        if json.loads(req.content.decode("utf-8")).get("stream") is True
    ]
    assert len(stream_bodies) == 1
    assert stream_bodies[0]["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_num_ctx_auth_fail_short_circuits_num_ctx_probe(
    httpx_mock: HTTPXMock,
) -> None:
    """Auth failure → num_ctx is also SKIPped (alongside the others)."""
    provider = _oa_provider(
        name="ollama-bad-auth",
        base_url="http://localhost:11434/v1",
        api_key_env="CR_TEST_MISSING_KEY",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=401,
        json={"error": "unauthorized"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    by_name = _probes_by_name(report.results)
    assert by_name["auth+basic-chat"].verdict == ProbeVerdict.AUTH_FAIL
    assert by_name["num_ctx"].verdict == ProbeVerdict.SKIP
    assert "auth probe did not succeed" in by_name["num_ctx"].detail


# ---------------------------------------------------------------------------
# Tool-calls probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_native_with_tools_registry_declared_true_is_ok(
    httpx_mock: HTTPXMock,
) -> None:
    """Native tool_calls + registry declares tools=true → OK, no patch."""
    provider = _oa_provider()
    # Probe 1: auth OK.
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # Probe 2: tool_calls native.
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(
            content=None,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"message":"probe"}'},
                }
            ],
        ),
    )
    # Probe 3: thinking SKIP for openai_compat → no HTTP call.
    # Probe 4: reasoning-leak — no reasoning field.
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="Paris"),
    )
    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_tools_true(),
    )
    tool = _probes_by_name(report.results)["tool_calls"]
    assert tool.verdict == ProbeVerdict.OK
    assert tool.suggested_patch is None
    assert exit_code_for(report) == 0


@pytest.mark.asyncio
async def test_tool_calls_native_but_registry_silent_suggests_true(
    httpx_mock: HTTPXMock,
) -> None:
    """Native tool_calls but no declaration → NEEDS_TUNING (enable tools=true)."""
    provider = _oa_provider()
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(
            content=None,
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{}"},
                }
            ],
        ),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    tool = _probes_by_name(report.results)["tool_calls"]
    assert tool.verdict == ProbeVerdict.NEEDS_TUNING
    assert tool.target_file == "providers.yaml"
    assert "tools: true" in (tool.suggested_patch or "")
    assert exit_code_for(report) == 2


@pytest.mark.asyncio
async def test_tool_calls_text_json_only_with_declared_false_is_ok(
    httpx_mock: HTTPXMock,
) -> None:
    """Model wrote JSON-in-text; declaration says tools=false → OK (repair rescues)."""
    provider = _oa_provider()
    text_with_tool = (
        "Sure, I'll use the tool.\n"
        '```json\n{"name": "echo", "arguments": {"message": "probe"}}\n```'
    )
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content=text_with_tool),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    tool = _probes_by_name(report.results)["tool_calls"]
    assert tool.verdict == ProbeVerdict.OK
    # Informational message mentions the repair path so operators know.
    assert "repair" in tool.detail.lower()


@pytest.mark.asyncio
async def test_tool_calls_text_json_with_declared_true_needs_tuning(
    httpx_mock: HTTPXMock,
) -> None:
    """Declaration says native, but model only wrote JSON-in-text → NEEDS_TUNING."""
    provider = _oa_provider()
    text_with_tool = (
        'Calling echo:\n```json\n{"name": "echo", "arguments": {"message": "probe"}}\n```'
    )
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content=text_with_tool),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_tools_true(),
    )
    tool = _probes_by_name(report.results)["tool_calls"]
    assert tool.verdict == ProbeVerdict.NEEDS_TUNING
    assert "tools: false" in (tool.suggested_patch or "")


@pytest.mark.asyncio
async def test_tool_calls_none_with_declared_true_suggests_false(
    httpx_mock: HTTPXMock,
) -> None:
    """Nothing tool-shaped + registry says true → NEEDS_TUNING (downgrade)."""
    provider = _oa_provider()
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="I don't know how to do that."),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_tools_true(),
    )
    tool = _probes_by_name(report.results)["tool_calls"]
    assert tool.verdict == ProbeVerdict.NEEDS_TUNING
    assert tool.target_file == "providers.yaml"
    assert "tools: false" in (tool.suggested_patch or "")


@pytest.mark.asyncio
async def test_tool_calls_none_with_declared_false_is_ok(
    httpx_mock: HTTPXMock,
) -> None:
    """Plain text response + declaration false → expected, OK."""
    provider = _oa_provider()
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="I don't support tools."),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    tool = _probes_by_name(report.results)["tool_calls"]
    assert tool.verdict == ProbeVerdict.OK


@pytest.mark.asyncio
async def test_tool_calls_explicit_providers_caps_true_takes_precedence(
    httpx_mock: HTTPXMock,
) -> None:
    """providers.yaml explicit capabilities.tools=True counts as declared, even with empty registry."""
    provider = _oa_provider(caps=Capabilities(tools=True))
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(
            content=None,
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{}"},
                }
            ],
        ),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    tool = _probes_by_name(report.results)["tool_calls"]
    assert tool.verdict == ProbeVerdict.OK


# ---------------------------------------------------------------------------
# Thinking probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_skipped_for_openai_compat_without_opt_in(
    httpx_mock: HTTPXMock,
) -> None:
    """Plain openai_compat provider → thinking probe SKIP, not applicable."""
    provider = _oa_provider()
    # Only 3 HTTP calls: auth, tool_calls, reasoning-leak. Thinking probe
    # returns SKIP without issuing a request.
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="meh"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    thinking = _probes_by_name(report.results)["thinking"]
    assert thinking.verdict == ProbeVerdict.SKIP
    assert "kind=openai_compat" in thinking.detail


@pytest.mark.asyncio
async def test_thinking_skipped_for_openai_compat_with_opt_in_flag_warns(
    httpx_mock: HTTPXMock,
) -> None:
    """An explicit capabilities.thinking=true on openai_compat is a misconfig — SKIP with note."""
    provider = _oa_provider(caps=Capabilities(thinking=True))
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="meh"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    thinking = _probes_by_name(report.results)["thinking"]
    assert thinking.verdict == ProbeVerdict.SKIP
    assert "no effect" in thinking.detail


@pytest.mark.asyncio
async def test_thinking_anthropic_block_emitted_matches_declared(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic + block emitted + registry says thinking=true → OK."""
    provider = _anthropic_provider(model="claude-opus-4-8", api_key_env="CR_TEST_ANTH_KEY")
    # basic chat ok
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(text_blocks=["PONG"]),
    )
    # tool_calls probe: no tool_use, no content — treated as "no tools"
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(text_blocks=["ok"]),
    )
    # thinking probe: block present
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(thinking_text="let me think", text_blocks=["4"]),
    )
    # reasoning-leak probe is skipped for anthropic kind → no HTTP call.
    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_thinking_true(),
    )
    thinking = _probes_by_name(report.results)["thinking"]
    assert thinking.verdict == ProbeVerdict.OK
    assert exit_code_for(report) == 0


@pytest.mark.asyncio
async def test_thinking_anthropic_no_block_but_declared_flags_tuning(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic + declaration promises thinking + no block emitted → NEEDS_TUNING."""
    provider = _anthropic_provider(model="claude-opus-4-8", api_key_env="CR_TEST_ANTH_KEY")
    for body in (
        _anthropic_ok_response(text_blocks=["PONG"]),
        _anthropic_ok_response(text_blocks=["no tool"]),
        _anthropic_ok_response(text_blocks=["4"]),  # NO thinking block
    ):
        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/messages",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_thinking_true(),
    )
    thinking = _probes_by_name(report.results)["thinking"]
    assert thinking.verdict == ProbeVerdict.NEEDS_TUNING
    assert thinking.target_file == "providers.yaml"
    assert "thinking: false" in (thinking.suggested_patch or "")


@pytest.mark.asyncio
async def test_thinking_anthropic_400_rejection_with_declared_is_tuning(
    httpx_mock: HTTPXMock,
) -> None:
    """400 mentioning `thinking` + declaration promises support → NEEDS_TUNING."""
    provider = _anthropic_provider(model="claude-sonnet-3-5", api_key_env="CR_TEST_ANTH_KEY")
    # basic chat OK
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(text_blocks=["PONG"]),
    )
    # tool_calls probe: plain text
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(text_blocks=["no tool"]),
    )
    # thinking probe: 400 with error mentioning thinking
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=400,
        json={"error": {"message": "thinking is not supported on this model"}},
    )
    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=CapabilityRegistry(
            [
                CapabilityRule(
                    match="claude-sonnet-3-*",
                    kind="anthropic",
                    capabilities=RegistryCapabilities(thinking=True),
                )
            ]
        ),
    )
    thinking = _probes_by_name(report.results)["thinking"]
    assert thinking.verdict == ProbeVerdict.NEEDS_TUNING
    assert "thinking: false" in (thinking.suggested_patch or "")


# ---------------------------------------------------------------------------
# Reasoning leak probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_leak_detected_is_informational_ok(
    httpx_mock: HTTPXMock,
) -> None:
    """`reasoning` field present + passthrough=false → OK (v0.5-C strip covers it)."""
    provider = _oa_provider()
    # auth
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # tool_calls
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="nothing"),
    )
    # thinking SKIP — no HTTP call.
    # reasoning-leak: response carries a reasoning field.
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="Paris", reasoning="I considered major European capitals"),
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.OK
    assert "strip" in leak.detail.lower()
    # Informational only — no tuning required.
    assert exit_code_for(report) == 0


@pytest.mark.asyncio
async def test_reasoning_leak_not_present_reports_clean(
    httpx_mock: HTTPXMock,
) -> None:
    """No `reasoning` field → OK with 'nothing to strip'."""
    provider = _oa_provider()
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.OK
    assert "nothing to strip" in leak.detail


@pytest.mark.asyncio
async def test_reasoning_leak_skipped_for_anthropic_kind(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic responses don't carry the non-standard field; probe SKIP."""
    provider = _anthropic_provider(model="claude-opus-4-8", api_key_env="CR_TEST_ANTH_KEY")
    for body in (
        _anthropic_ok_response(text_blocks=["PONG"]),
        _anthropic_ok_response(text_blocks=["no tool"]),
        _anthropic_ok_response(text_blocks=["4"]),
    ):
        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/messages",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.SKIP


# ---------------------------------------------------------------------------
# v1.0-A: reasoning-leak probe — content-embedded marker detection
#
# The probe's ``_probe_reasoning_leak`` now ALSO inspects
# ``message.content`` for the markers that the new ``output_filters``
# chain would scrub, and issues NEEDS_TUNING with a copy-paste patch
# when the configured chain doesn't cover what was observed. Pairs with
# the transformation layer — "transformation には probe が伴う" from the
# v0.7 retrospective.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_leak_detects_content_embedded_think(
    httpx_mock: HTTPXMock,
) -> None:
    """``<think>...</think>`` in content when no strip_thinking configured
    → NEEDS_TUNING with a providers.yaml output_filters patch."""
    provider = _oa_provider()  # default: output_filters=[]
    # auth probe
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # tool_calls probe
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="nothing"),
    )
    # thinking probe SKIP for openai_compat.
    # reasoning-leak probe: leaky content.
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="Answer: <think>reasoning happens here</think> Paris"),
    )

    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.NEEDS_TUNING
    assert "<think>" in leak.detail
    assert leak.target_file == "providers.yaml"
    assert leak.suggested_patch is not None
    assert "output_filters" in leak.suggested_patch
    assert "strip_thinking" in leak.suggested_patch
    # NEEDS_TUNING → exit code 2.
    assert exit_code_for(report) == 2


@pytest.mark.asyncio
async def test_reasoning_leak_detects_stop_markers_in_content(
    httpx_mock: HTTPXMock,
) -> None:
    """Stop marker in content, chain does not include strip_stop_markers
    → NEEDS_TUNING with the marker-specific filter in the patch."""
    provider = _oa_provider()
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="nothing"),
    )
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="Paris<|eot_id|>"),
    )

    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.NEEDS_TUNING
    assert "<|eot_id|>" in leak.detail
    assert "strip_stop_markers" in (leak.suggested_patch or "")


@pytest.mark.asyncio
async def test_reasoning_leak_silent_when_filter_already_configured(
    httpx_mock: HTTPXMock,
) -> None:
    """Chain covers what was observed → no NEEDS_TUNING, fall through to
    the v0.5-C reasoning-field observation (here: clean → OK)."""
    provider = _oa_provider()
    provider.output_filters = ["strip_thinking"]
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="nothing"),
    )
    # Content has <think> but strip_thinking is configured → no tuning.
    httpx_mock.add_response(
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="<think>x</think>Paris"),
    )

    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.OK


# ---------------------------------------------------------------------------
# v1.0-C: streaming probe
#
# Direct detection of Ollama output-side truncation (``options.num_predict``
# cap) and of "stream: true silently ignored" framing. Same Ollama-shape
# gating as the v1.0-B num_ctx probe: ``:11434`` port OR declared
# ``extra_body.options.num_ctx``. Fires last in the probe chain so the
# num_ctx / tool_calls / thinking / reasoning-leak verdicts dominate the
# report — streaming is an output-side sibling of num_ctx and its verdict
# stands independently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_skips_for_non_ollama_port(
    httpx_mock: HTTPXMock,
) -> None:
    """Default :8080 base_url → streaming probe SKIPs (same gating as num_ctx)."""
    provider = _oa_provider()  # :8080, no extra_body
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    assert streaming.verdict == ProbeVerdict.SKIP
    assert "Ollama-shape" in streaming.detail
    # Critically — no HTTP request was issued for the streaming probe.


@pytest.mark.asyncio
async def test_streaming_skips_for_anthropic_kind(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic native uses a different stream event shape → SKIP without HTTP."""
    provider = _anthropic_provider()
    # For kind=anthropic the probe chain is: auth → num_ctx SKIP →
    # tool_calls (HTTP) → thinking (HTTP, unconditional for anthropic) →
    # reasoning-leak SKIP → streaming SKIP. So 3 HTTP calls total.
    for body in (
        _anthropic_ok_response(text_blocks=["hi"]),
        _anthropic_ok_response(text_blocks=["no tool"]),
        _anthropic_ok_response(text_blocks=["Paris"]),
    ):
        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/messages",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    assert streaming.verdict == ProbeVerdict.SKIP
    # The SKIP reason should NOT be the auth fallback — auth succeeded.
    assert "auth" not in streaming.detail.lower()


@pytest.mark.asyncio
async def test_streaming_ollama_port_successful_stream_is_ok(
    httpx_mock: HTTPXMock,
) -> None:
    """Full stream with finish_reason=stop + [DONE] + adequate content → OK."""
    provider = _oa_provider(
        name="ollama-stream-ok",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    # auth OK
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # num_ctx: echo canary so the probe reports OK.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    # tool_calls + reasoning-leak
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # streaming — 30 chunks, finish_reason=stop, [DONE] terminator.
    _add_sse_ok_mock(httpx_mock, "http://localhost:11434/v1/chat/completions")
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    assert streaming.verdict == ProbeVerdict.OK
    assert streaming.suggested_patch is None
    # The OK detail reports chunk / char counts and finish_reason.
    assert "chunks" in streaming.detail
    assert "'stop'" in streaming.detail
    # No note about missing [DONE] when terminator is present.
    assert "[DONE]" not in streaming.detail or "terminator" not in streaming.detail


@pytest.mark.asyncio
async def test_streaming_finish_length_short_content_needs_tuning_with_num_predict_patch(
    httpx_mock: HTTPXMock,
) -> None:
    """finish_reason=length + truncated content → NEEDS_TUNING with num_predict patch.

    Simulates the num_predict=128 Ollama default biting Claude Code — the
    stream closes after only a handful of tokens.
    """
    provider = _oa_provider(
        name="ollama-low-num-predict",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # streaming — truncated at 3 numbers ("1\n2\n3\n" = 6 chars), length finish.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        content=_sse_stream_count_body(numbers=3, finish_reason="length", include_done=True),
        headers={"content-type": "text/event-stream"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    assert streaming.verdict == ProbeVerdict.NEEDS_TUNING
    assert streaming.target_file == "providers.yaml"
    assert streaming.suggested_patch is not None
    assert "num_predict: 4096" in streaming.suggested_patch
    assert "options:" in streaming.suggested_patch
    # The detail should say "length" and mention num_predict as the
    # likely culprit so operators know what to look for in their config.
    assert "length" in streaming.detail
    assert "num_predict" in streaming.detail
    assert exit_code_for(report) == 2


@pytest.mark.asyncio
async def test_streaming_zero_chunks_is_needs_tuning_no_patch(
    httpx_mock: HTTPXMock,
) -> None:
    """2xx with no SSE chunks at all → NEEDS_TUNING (advisory, no patch).

    Simulates an upstream that silently returned JSON / non-SSE to a
    ``stream: true`` request.
    """
    provider = _oa_provider(
        name="ollama-broken-stream",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # streaming — upstream returns a plain JSON object as if stream=true
    # were ignored. No SSE framing at all.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        content=b'{"id":"x","object":"chat.completion","choices":[]}',
        headers={"content-type": "application/json"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    assert streaming.verdict == ProbeVerdict.NEEDS_TUNING
    # Advisory — no patch because the remediation is server-side /
    # framing, not a providers.yaml knob we can set.
    assert streaming.suggested_patch is None
    assert "no streaming chunks" in streaming.detail
    # Exit code still escalates to 2 since this is a NEEDS_TUNING.
    assert exit_code_for(report) == 2


@pytest.mark.asyncio
async def test_streaming_no_done_terminator_is_ok_with_note(
    httpx_mock: HTTPXMock,
) -> None:
    """Stream completes but upstream omits ``data: [DONE]`` → OK + note.

    Most clients (and CodeRouter's own adapter) tolerate a missing
    terminator; strict SSE parsers may stall. The probe reports OK
    but surfaces the observation so operators can check their toolchain.
    """
    provider = _oa_provider(
        name="ollama-no-done",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # streaming — complete SSE but no [DONE] line.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        content=_sse_stream_count_body(include_done=False),
        headers={"content-type": "text/event-stream"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    assert streaming.verdict == ProbeVerdict.OK
    # The informational note should mention DONE / terminator so
    # operators can check their parser tolerance.
    assert "DONE" in streaming.detail


@pytest.mark.asyncio
async def test_streaming_extra_body_signal_fires_on_non_11434_port(
    httpx_mock: HTTPXMock,
) -> None:
    """Streaming probe fires on custom ports when extra_body declares num_ctx.

    Same disjunctive gating as the num_ctx probe — the declaration of
    Ollama-specific ``options.num_ctx`` is itself evidence of Ollama-shape.
    """
    provider = _oa_provider(
        name="ollama-custom-port-stream",
        base_url="http://localhost:12345/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:12345/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    httpx_mock.add_response(
        url="http://localhost:12345/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:12345/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    _add_sse_ok_mock(httpx_mock, "http://localhost:12345/v1/chat/completions")
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    # Must NOT be SKIP — extra_body signal fires even on non-11434.
    assert streaming.verdict == ProbeVerdict.OK


@pytest.mark.asyncio
async def test_streaming_request_body_carries_stream_true_and_merges_extra_body(
    httpx_mock: HTTPXMock,
) -> None:
    """Streaming probe must set ``stream: true`` and forward declared extra_body.

    Mirrors :func:`test_num_ctx_request_body_merges_extra_body_options`
    but targets the streaming probe's outbound body. The merge matters
    because a declared ``options.num_predict`` must actually travel
    with the request for the probe to observe its effect.
    """
    provider = _oa_provider(
        name="ollama-stream-merge",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768, "num_predict": 4096, "keep_alive": "5m"}},
    )
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # Return a valid streaming body for the streaming probe, valid
        # JSON for the earlier probes. Branch on the URL/headers would
        # over-fit; using content-type alone is enough since the non-
        # streaming probes don't parse SSE from `resp.json()` paths —
        # but they DO call resp.json(), so we return JSON for all and
        # let the streaming probe land on NEEDS_TUNING. That still
        # captures the outbound body, which is what this test verifies.
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream") is True:
            return httpx.Response(
                200,
                content=_sse_stream_count_body(),
                headers={"content-type": "text/event-stream"},
            )
        # canary echoed for the num_ctx probe; generic OK otherwise.
        return httpx.Response(200, json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY))

    httpx_mock.add_callback(
        _capture,
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    # The last captured request is the streaming probe.
    streaming_body = json.loads(captured[-1].content.decode("utf-8"))
    assert streaming_body.get("stream") is True
    assert streaming_body["options"] == {
        "num_ctx": 32768,
        "num_predict": 4096,
        "keep_alive": "5m",
    }
    # Top-level probe fields must win over any extra_body collision.
    assert streaming_body["model"] == provider.model
    # v1.8.2: streaming probe baseline budget bumped 128 → 512 to absorb
    # short stylistic preambles. Thinking models bump further to 1024
    # (covered by ``test_streaming_max_tokens_bumped_for_thinking_provider``).
    # Provider here has no thinking declaration, so baseline applies.
    assert streaming_body["max_tokens"] == 512


@pytest.mark.asyncio
async def test_streaming_http_500_skips(
    httpx_mock: HTTPXMock,
) -> None:
    """Upstream 500 on the streaming probe → SKIP (transport-level noise)."""
    provider = _oa_provider(
        name="ollama-flaky-stream",
        base_url="http://localhost:11434/v1",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content=_NUM_CTX_PROBE_CANARY),
    )
    for body in (
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # streaming probe itself 500s.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=500,
        content=b"internal server error",
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    streaming = _probes_by_name(report.results)["streaming"]
    assert streaming.verdict == ProbeVerdict.SKIP
    assert "500" in streaming.detail


@pytest.mark.asyncio
async def test_streaming_auth_fail_short_circuits_streaming_probe(
    httpx_mock: HTTPXMock,
) -> None:
    """Auth failure → streaming SKIP (alongside num_ctx / tool_calls / etc.)."""
    provider = _oa_provider(
        name="ollama-bad-auth-stream",
        base_url="http://localhost:11434/v1",
        api_key_env="CR_TEST_MISSING_KEY",
        extra_body={"options": {"num_ctx": 32768}},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=401,
        json={"error": "unauthorized"},
    )
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    by_name = _probes_by_name(report.results)
    assert by_name["auth+basic-chat"].verdict == ProbeVerdict.AUTH_FAIL
    assert by_name["streaming"].verdict == ProbeVerdict.SKIP
    assert "auth probe did not succeed" in by_name["streaming"].detail


# ---------------------------------------------------------------------------
# Exit-code semantics / orchestration
# ---------------------------------------------------------------------------


def test_exit_code_all_ok_is_zero() -> None:
    report = DoctorReport(
        provider_name="p",
        provider=_oa_provider(),
        resolved_caps=None,  # type: ignore[arg-type]
    )
    report.results = [
        ProbeResult(name="auth+basic-chat", verdict=ProbeVerdict.OK, detail=""),
        ProbeResult(name="tool_calls", verdict=ProbeVerdict.OK, detail=""),
        ProbeResult(name="thinking", verdict=ProbeVerdict.SKIP, detail=""),
        ProbeResult(name="reasoning-leak", verdict=ProbeVerdict.OK, detail=""),
    ]
    assert exit_code_for(report) == 0


def test_exit_code_needs_tuning_alone_is_two() -> None:
    report = DoctorReport(
        provider_name="p",
        provider=_oa_provider(),
        resolved_caps=None,  # type: ignore[arg-type]
    )
    report.results = [
        ProbeResult(name="auth+basic-chat", verdict=ProbeVerdict.OK, detail=""),
        ProbeResult(name="tool_calls", verdict=ProbeVerdict.NEEDS_TUNING, detail=""),
    ]
    assert exit_code_for(report) == 2


def test_exit_code_auth_fail_dominates_needs_tuning() -> None:
    """If auth fails AND another probe reports tuning, the blocker wins."""
    report = DoctorReport(
        provider_name="p",
        provider=_oa_provider(),
        resolved_caps=None,  # type: ignore[arg-type]
    )
    report.results = [
        ProbeResult(name="auth+basic-chat", verdict=ProbeVerdict.AUTH_FAIL, detail=""),
        ProbeResult(name="tool_calls", verdict=ProbeVerdict.NEEDS_TUNING, detail=""),
    ]
    assert exit_code_for(report) == 1


@pytest.mark.asyncio
async def test_check_model_unknown_provider_raises_keyerror() -> None:
    """Unknown provider name → caller gets a KeyError with known names."""
    config = _config_for([_oa_provider(name="foo")])
    with pytest.raises(KeyError) as exc:
        await check_model(config, "does-not-exist", registry=_empty_registry())
    assert "does-not-exist" in str(exc.value)
    assert "foo" in str(exc.value)


@pytest.mark.asyncio
async def test_check_model_uses_default_registry_when_not_passed(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Omitting ``registry=`` should pull the module-level default."""
    # Stub the default registry to a known rule set so we can observe its use.
    from coderouter.routing import capability as cap_mod

    sentinel_registry = CapabilityRegistry([])
    monkeypatch.setattr(cap_mod, "_DEFAULT_REGISTRY", sentinel_registry)

    provider = _oa_provider()
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content="nothing"),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:8080/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    # No registry kwarg — should pick up sentinel via get_default_registry.
    report = await check_model(_config_for([provider]), provider.name)
    assert exit_code_for(report) == 0


@pytest.mark.asyncio
async def test_api_key_env_set_adds_authorization_header(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor must pass the resolved API key as Bearer auth (openai_compat)."""
    monkeypatch.setenv("CR_DOCTOR_KEY", "sk-probe")
    provider = _oa_provider(api_key_env="CR_DOCTOR_KEY")
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_openai_ok_response(content="PONG"))

    httpx_mock.add_callback(
        _capture,
        url="http://localhost:8080/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    # All three openai_compat probes (auth, tool_calls, reasoning-leak)
    # hit the same endpoint — thinking probe is SKIP and doesn't call.
    report = await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    assert captured[0].headers["Authorization"] == "Bearer sk-probe"
    assert len(captured) == 3
    assert exit_code_for(report) == 0


@pytest.mark.asyncio
async def test_anthropic_probe_uses_x_api_key_not_bearer(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anthropic auth is `x-api-key`, not Authorization header."""
    monkeypatch.setenv("ANTH_KEY", "sk-ant-abc")
    provider = _anthropic_provider(api_key_env="ANTH_KEY")
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_anthropic_ok_response(text_blocks=["PONG"]))

    httpx_mock.add_callback(
        _capture,
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        is_reusable=True,
    )
    await check_model(_config_for([provider]), provider.name, registry=_empty_registry())
    assert captured[0].headers["x-api-key"] == "sk-ant-abc"
    assert "Authorization" not in captured[0].headers


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def test_format_report_includes_provider_details_and_exit_line() -> None:
    """The report ends with 'Exit: N' so CI scrapers can grep for it."""
    provider = _oa_provider(name="myprov", model="qwen3-coder:7b")
    report = DoctorReport(
        provider_name="myprov",
        provider=provider,
        resolved_caps=None,  # type: ignore[arg-type]
    )
    from coderouter.config.capability_registry import ResolvedCapabilities

    report.resolved_caps = ResolvedCapabilities()
    report.results = [
        ProbeResult(name="auth+basic-chat", verdict=ProbeVerdict.OK, detail="200 OK"),
        ProbeResult(
            name="tool_calls",
            verdict=ProbeVerdict.NEEDS_TUNING,
            detail="model needs tools=false",
            suggested_patch=_patch_providers_yaml_capability("myprov", "tools", False),
            target_file="providers.yaml",
        ),
    ]
    text = format_report(report)
    assert "myprov" in text
    assert "qwen3-coder:7b" in text
    assert "[OK]" in text
    assert "[NEEDS TUNING]" in text
    assert text.rstrip().endswith("Exit: 2")
    # The suggested patch must appear verbatim so users can copy-paste.
    assert "tools: false" in text
