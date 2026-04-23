"""Regression guard: every ``examples/providers*.yaml`` must load cleanly.

The sample YAMLs under ``examples/`` are what users copy to
``~/.coderouter/providers.yaml``. If any of them stop validating after a
pydantic-schema tweak, users hit the break at the worst possible moment
(their very first ``coderouter start``). Parametrizing over the
directory and calling the real loader makes the break a CI failure
instead.

Each YAML is required to satisfy three properties:

* it loads through :func:`coderouter.config.loader.load_config`;
* its ``default_profile`` resolves to a declared profile (or is
  ``"auto"``, the reserved v1.6 sentinel);
* every provider referenced by every profile is declared in
  ``providers:``.

The last two are already enforced by pydantic validators, but keeping
the assertions explicit here preserves a human-readable failure message
when one of them does trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coderouter.config.loader import load_config

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
_YAML_PATHS = sorted(_EXAMPLES_DIR.glob("providers*.yaml"))


@pytest.mark.parametrize(
    "yaml_path",
    _YAML_PATHS,
    ids=[p.name for p in _YAML_PATHS],
)
def test_example_yaml_loads(yaml_path: Path) -> None:
    cfg = load_config(yaml_path)

    profile_names = {p.name for p in cfg.profiles}
    # "auto" is the v1.6 sentinel that activates auto_router — exempt.
    if cfg.default_profile != "auto":
        assert cfg.default_profile in profile_names, (
            f"{yaml_path.name}: default_profile={cfg.default_profile!r} "
            f"not in profiles={sorted(profile_names)}"
        )

    provider_names = {p.name for p in cfg.providers}
    for prof in cfg.profiles:
        missing = [p for p in prof.providers if p not in provider_names]
        assert not missing, (
            f"{yaml_path.name}: profile {prof.name!r} references "
            f"undeclared providers: {missing}"
        )


_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"


def test_nvidia_nim_example_has_nim_providers() -> None:
    """Narrow guard for the NIM starter.

    Pins every property the Note article (docs/note-nvidia-nim.md) and
    the article's recommended quickstart depend on. A future edit that
    drifts from any of these is CI-visible before the sample ships.
    """
    cfg = load_config(_EXAMPLES_DIR / "providers.nvidia-nim.yaml")

    # The article's 3-line quickstart points users at the claude-code-nim
    # profile specifically; if someone renames it here the article breaks
    # silently. Pin it.
    assert cfg.default_profile == "claude-code-nim", (
        f"providers.nvidia-nim.yaml: default_profile should be "
        f"'claude-code-nim' (the profile the Note article quickstart "
        f"recommends), got {cfg.default_profile!r}"
    )

    names = {p.name for p in cfg.providers}
    # Tool-capable NIM entries, verified live 2026-04-23 — these three
    # are the minimum set the ``claude-code-nim`` profile relies on.
    required_tool_capable = {
        "nim-llama-3.3-70b",
        "nim-qwen3-coder-480b",
        "nim-kimi-k2",
    }
    assert required_tool_capable <= names, (
        f"providers.nvidia-nim.yaml is missing expected tool-capable "
        f"NIM entries: {sorted(required_tool_capable - names)}"
    )

    # The qwen2.5-coder-32b chat-only entry must exist AND declare
    # tools: false. NIM returns HTTP 400 on tool-laden requests to
    # this slug; the tools: false declaration is what steers
    # Claude Code's (tool-every-turn) traffic past this entry rather
    # than tripping a 400. The article documents this behavior
    # explicitly — if the entry vanishes, the article lies.
    assert "nim-qwen-coder-32b-chat" in names, (
        "providers.nvidia-nim.yaml must include nim-qwen-coder-32b-chat "
        "as a chat-only (tools: false) provider — the Note article "
        "documents it as the demonstration of capability-gated routing."
    )
    p32 = cfg.provider_by_name("nim-qwen-coder-32b-chat")
    assert p32.capabilities.tools is False, (
        "nim-qwen-coder-32b-chat must declare tools: false — "
        "NIM rejects tool-laden requests to qwen2.5-coder-32b-instruct "
        "with HTTP 400."
    )

    # Every NIM stanza (tool-capable or not) must use the shared env
    # var name so users only have to set one secret, must point at the
    # NIM gateway exactly (not a v2 / trailing-slash typo), and must
    # declare paid=False so the chain works under the default
    # ALLOW_PAID=false.
    nim_names = {n for n in names if n.startswith("nim-")}
    assert nim_names, "expected at least one provider prefixed 'nim-'"
    for name in nim_names:
        p = cfg.provider_by_name(name)
        assert p.api_key_env == "NVIDIA_NIM_API_KEY", (
            f"{name}: expected api_key_env=NVIDIA_NIM_API_KEY, "
            f"got {p.api_key_env!r}"
        )
        assert str(p.base_url).rstrip("/") == _NIM_BASE_URL, (
            f"{name}: expected base_url={_NIM_BASE_URL!r}, "
            f"got {p.base_url!r}"
        )
        assert p.paid is False, (
            f"{name}: NIM free-tier stanza must declare paid=False "
            f"so the chain is usable with ALLOW_PAID=false (the default)."
        )

    # Tool-capable ones must declare tools: true.
    for name in required_tool_capable:
        p = cfg.provider_by_name(name)
        assert p.capabilities.tools is True, (
            f"{name}: declared tool-capable but capabilities.tools is False"
        )

    # nim-kimi-k2-thinking is reasoning-only and the article explicitly
    # calls out that it is NOT in the primary Claude Code chain — it's
    # only referenced from the nim-reasoning profile. Guard both
    # invariants so a profile-shuffling edit can't silently push it in.
    if "nim-kimi-k2-thinking" in names:
        claude_code_profile = cfg.profile_by_name("claude-code-nim")
        assert "nim-kimi-k2-thinking" not in claude_code_profile.providers, (
            "nim-kimi-k2-thinking must NOT appear in the claude-code-nim "
            "profile — its high first-byte latency and reasoning_content "
            "output shape make it a poor Claude Code primary. Move to "
            "the nim-reasoning profile instead."
        )
        reasoning_profile = cfg.profile_by_name("nim-reasoning")
        assert "nim-kimi-k2-thinking" in reasoning_profile.providers, (
            "nim-kimi-k2-thinking is declared but unreferenced — "
            "add it to the nim-reasoning profile or remove the stanza."
        )
