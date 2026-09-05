from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig


def test_resolve_model_to_profile_with_fallback_chains():
    # Setup matching exact providers.yaml structure with fallback chains
    config = CodeRouterConfig(
        allow_paid=False,
        default_profile="coding",
        mode_aliases={
            "opus": "reasoning",
            "sonnet": "coding",
            "haiku": "general",
        },
        providers=[
            ProviderConfig(
                name="ollama-opus-model",
                kind="openai_compat",
                base_url="http://localhost:11434/v1",
                model="hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:latest",
            ),
            ProviderConfig(
                name="ollama-sonnet-model",
                kind="openai_compat",
                base_url="http://localhost:11434/v1",
                model="gemma4:e4b-it-qat",
            ),
            ProviderConfig(
                name="ollama-haiku-model",
                kind="openai_compat",
                base_url="http://localhost:11434/v1",
                model="hf.co/bartowski/Ling-3.0-tiny-GGUF:Q4_K_M",
            ),
        ],
        profiles=[
            FallbackChain(name="reasoning", providers=["ollama-opus-model", "ollama-sonnet-model"]),
            FallbackChain(name="coding", providers=["ollama-sonnet-model", "ollama-haiku-model"]),
            FallbackChain(name="general", providers=["ollama-haiku-model", "ollama-sonnet-model"]),
        ],
    )

    # 1. Exact match with primary provider models (must NOT be hijacked by fallback in reasoning)
    assert config.resolve_model_to_profile("gemma4:e4b-it-qat") == "coding"
    assert config.resolve_model_to_profile("hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:latest") == "reasoning"
    assert config.resolve_model_to_profile("hf.co/bartowski/Ling-3.0-tiny-GGUF:Q4_K_M") == "general"

    # 2. Exact match with provider name
    assert config.resolve_model_to_profile("ollama-sonnet-model") == "coding"
    assert config.resolve_model_to_profile("ollama-opus-model") == "reasoning"
    assert config.resolve_model_to_profile("ollama-haiku-model") == "general"

    # 3. Exact match with profile name
    assert config.resolve_model_to_profile("reasoning") == "reasoning"
    assert config.resolve_model_to_profile("coding") == "coding"
    assert config.resolve_model_to_profile("general") == "general"

    # 4. Mode aliases and substring fallback
    assert config.resolve_model_to_profile("opus") == "reasoning"
    assert config.resolve_model_to_profile("sonnet") == "coding"
    assert config.resolve_model_to_profile("haiku") == "general"
    assert config.resolve_model_to_profile("claude-3-opus-20240229") == "reasoning"
    assert config.resolve_model_to_profile("claude-3-5-sonnet-20241022") == "coding"
    assert config.resolve_model_to_profile("claude-3-5-haiku-20241022") == "general"

    # 5. Unknown model returns None
    assert config.resolve_model_to_profile("unknown-model-xyz") is None
