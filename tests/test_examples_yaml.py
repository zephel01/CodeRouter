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


# ======================================================================
# v1.7-B: examples/providers.yaml — 用途別 4 プロファイル invariants
# ======================================================================


def test_curated_providers_yaml_has_four_use_case_profiles() -> None:
    """examples/providers.yaml must declare the 4 v1.7-B use-case
    profiles (multi / coding / general / reasoning) plus the legacy
    backward-compat profiles.

    The setup.sh wizard, README quickstart, and `coderouter serve
    --mode` examples all reference these names — pin them so a rename
    breaks CI before docs go out.
    """
    cfg = load_config(_EXAMPLES_DIR / "providers.yaml")

    expected_v17b_profiles = {"multi", "coding", "general", "reasoning"}
    profile_names = {p.name for p in cfg.profiles}
    missing = expected_v17b_profiles - profile_names
    assert not missing, (
        f"examples/providers.yaml is missing v1.7-B use-case profile(s): "
        f"{sorted(missing)}"
    )


def test_curated_providers_yaml_default_is_multi() -> None:
    """``default_profile: multi`` is the v1.7-B contract (vision-capable
    chain that also handles text-only requests cleanly). Pin it so a
    well-meaning edit doesn't quietly revert to the v1.6 default."""
    cfg = load_config(_EXAMPLES_DIR / "providers.yaml")
    assert cfg.default_profile == "multi", (
        f"examples/providers.yaml: default_profile should be 'multi' "
        f"(v1.7-B), got {cfg.default_profile!r}"
    )


def test_curated_use_case_profiles_have_append_system_prompt() -> None:
    """All 4 use-case profiles MUST set append_system_prompt to nudge
    non-Claude models toward Claude-like response style. This is the
    primary lever against the "意味合いが違う" UX problem.

    Without this assertion, a future edit could silently strip the
    nudges and users would see noticeably different responses across
    providers without knowing why."""
    cfg = load_config(_EXAMPLES_DIR / "providers.yaml")
    for name in ("multi", "coding", "general", "reasoning"):
        prof = cfg.profile_by_name(name)
        assert prof.append_system_prompt, (
            f"profile {name!r} must set append_system_prompt to nudge "
            f"non-Claude models toward Claude-like style "
            f"(see profile comment in examples/providers.yaml)"
        )


def test_curated_mode_aliases_match_use_case_profiles() -> None:
    """``mode_aliases`` must point at declared profiles and cover the
    documented short names (default / fast / vision / think / cheap).
    The startup validator already enforces "alias targets exist", but
    pinning the names here documents the contract for users wiring up
    ``coderouter serve --mode <alias>``."""
    cfg = load_config(_EXAMPLES_DIR / "providers.yaml")
    expected_aliases = {"default", "fast", "vision", "think", "cheap"}
    actual_aliases = set(cfg.mode_aliases.keys())
    missing = expected_aliases - actual_aliases
    assert not missing, (
        f"examples/providers.yaml: mode_aliases is missing entries: "
        f"{sorted(missing)} (got {sorted(actual_aliases)})"
    )


def test_curated_coding_profile_head_is_claude_compatible_qwen() -> None:
    """The ``coding`` profile is the agentic-coding chain; the head
    must be a Qwen family local provider (Qwen3.6 / Qwen3-Coder /
    Qwen2.5-Coder). All three are flagged ``claude_code_suitability:
    ok`` in the bundled registry and are the closest Claude Sonnet
    behavioral match per docs/articles/note-* / the r/LocalLLaMA
    Megathread quoted in note-2026-04. Lock the head so a re-ordering
    edit doesn't silently degrade the Claude Code experience.

    Accepted head patterns (any of these):
      - ollama-qwen3-6-*  (Qwen3.6 series, e.g. 35b / 27b — note 推奨)
      - ollama-qwen3-coder-*  (Qwen3-Coder series, agentic-coding 専用)
      - ollama-qwen-coder-*  (Qwen2.5-Coder series, legacy)
    """
    cfg = load_config(_EXAMPLES_DIR / "providers.yaml")
    coding = cfg.profile_by_name("coding")
    assert coding.providers, "coding profile has no providers"
    head = coding.providers[0]
    accepted = ("qwen3-6", "qwen3-coder", "qwen-coder")
    assert any(token in head for token in accepted), (
        f"coding profile head must be a Qwen-family local provider "
        f"(one of {accepted}), got {head!r}. See examples/providers.yaml "
        f"comment block '用途別プロファイル' for rationale."
    )


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
