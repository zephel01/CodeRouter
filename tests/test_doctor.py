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
    DoctorReport,
    ProbeResult,
    ProbeVerdict,
    _patch_model_capabilities_yaml,
    _patch_providers_yaml_capability,
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
    base_url: str = "http://localhost:11434/v1",
    model: str = "qwen3-coder:7b",
    api_key_env: str | None = None,
    caps: Capabilities | None = None,
    timeout_s: float = 5.0,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="openai_compat",
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        capabilities=caps or Capabilities(),
        timeout_s=timeout_s,
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
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=401,
        json={"error": "unauthorized"},
    )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=403,
        json={"error": "forbidden"},
    )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=404,
        json={"error": "model not found"},
    )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        content=b"not json at all",
    )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
    auth = _probes_by_name(report.results)["auth+basic-chat"]
    assert auth.verdict == ProbeVerdict.TRANSPORT_ERROR


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
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # Probe 2: tool_calls native.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
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
        url="http://localhost:11434/v1/chat/completions",
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
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
        "Calling echo:\n"
        '```json\n{"name": "echo", "arguments": {"message": "probe"}}\n```'
    )
    for body in (
        _openai_ok_response(content="PONG"),
        _openai_ok_response(content=text_with_tool),
        _openai_ok_response(content="Paris"),
    ):
        httpx_mock.add_response(
            url="http://localhost:11434/v1/chat/completions",
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
            url="http://localhost:11434/v1/chat/completions",
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
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
    thinking = _probes_by_name(report.results)["thinking"]
    assert thinking.verdict == ProbeVerdict.SKIP
    assert "no effect" in thinking.detail


@pytest.mark.asyncio
async def test_thinking_anthropic_block_emitted_matches_declared(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic + block emitted + registry says thinking=true → OK."""
    provider = _anthropic_provider(
        model="claude-opus-4-8", api_key_env="CR_TEST_ANTH_KEY"
    )
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
        json=_anthropic_ok_response(
            thinking_text="let me think", text_blocks=["4"]
        ),
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
    provider = _anthropic_provider(
        model="claude-opus-4-8", api_key_env="CR_TEST_ANTH_KEY"
    )
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
    provider = _anthropic_provider(
        model="claude-sonnet-3-5", api_key_env="CR_TEST_ANTH_KEY"
    )
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
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="PONG"),
    )
    # tool_calls
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(content="nothing"),
    )
    # thinking SKIP — no HTTP call.
    # reasoning-leak: response carries a reasoning field.
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json=_openai_ok_response(
            content="Paris", reasoning="I considered major European capitals"
        ),
    )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
            url="http://localhost:11434/v1/chat/completions",
            method="POST",
            status_code=200,
            json=body,
        )
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.OK
    assert "nothing to strip" in leak.detail


@pytest.mark.asyncio
async def test_reasoning_leak_skipped_for_anthropic_kind(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic responses don't carry the non-standard field; probe SKIP."""
    provider = _anthropic_provider(
        model="claude-opus-4-8", api_key_env="CR_TEST_ANTH_KEY"
    )
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
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
    leak = _probes_by_name(report.results)["reasoning-leak"]
    assert leak.verdict == ProbeVerdict.SKIP


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
        ProbeResult(
            name="tool_calls", verdict=ProbeVerdict.NEEDS_TUNING, detail=""
        ),
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
        ProbeResult(
            name="tool_calls", verdict=ProbeVerdict.NEEDS_TUNING, detail=""
        ),
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
            url="http://localhost:11434/v1/chat/completions",
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
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    # All three openai_compat probes (auth, tool_calls, reasoning-leak)
    # hit the same endpoint — thinking probe is SKIP and doesn't call.
    report = await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
        return httpx.Response(
            200, json=_anthropic_ok_response(text_blocks=["PONG"])
        )

    httpx_mock.add_callback(
        _capture,
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        is_reusable=True,
    )
    await check_model(
        _config_for([provider]), provider.name, registry=_empty_registry()
    )
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
            suggested_patch=_patch_providers_yaml_capability(
                "myprov", "tools", False
            ),
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
