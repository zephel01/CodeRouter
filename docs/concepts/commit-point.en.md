# The commit point — which failures before the body ships can be saved

> Applies to v2.16.0+ (`ttft_timeout_s` and `x-should-retry` are v2.17.0)
> 日本語: [commit-point.md](./commit-point.md)

A streaming response has a point of no return. Once even one byte of the body has reached the client, an upstream failure cannot be handed to another provider — the text already delivered and the new provider's text would not join up. CodeRouter calls that point the **commit point** and tries to send every failure that happens before it to the next provider.

## What can happen before the commit point, and what CodeRouter does

| Situation | Handling | Needs |
|---|---|---|
| Upstream refuses, times out, or answers 4xx/5xx (before the first event) | Next provider. If the whole chain fails: **an Anthropic-shaped error with a real HTTP status** (v2.16.0) | nothing |
| Upstream never produces its first token (model loading, long prompt) | Next provider once `ttft_timeout_s` expires (v2.17.0) | `ttft_timeout_s` on the provider |
| Opening events (`message_start` …) arrived but the stream ends or breaks before any content | Withhold the opening events from the client and move to the next provider | `empty_response_action: fallback` on the profile |
| The stream breaks **after** content shipped | **No switch.** Detect and report it (`stream_truncation_action`, v2.15.0), or close with the content received (`partial_stitch_action: surface`) | those settings |

CodeRouter cannot save the last row either. As far as we checked in 2026-10, LiteLLM and Bifrost do not switch after the body has shipped either.

## `empty_response_action: fallback` is the effective commit point

With this setting the engine holds back `message_start` and the other opening events and releases them together with the **first real output** (a text / tool_use / thinking delta). That moment is the commit point; anything that fails before it is moved to the next provider without the client seeing it.

Turn it on first when serving Claude Code:

```yaml
profiles:
  - name: claude-code
    providers: [local-llamacpp, openrouter-free]
    empty_response_action: fallback
```

## `ttft_timeout_s` — a deadline for the first token (v2.17.0)

```yaml
providers:
  - name: local-llamacpp
    kind: anthropic                  # pass straight through to llama-server's /v1/messages
    base_url: http://127.0.0.1:8080
    model: ""
    ttft_timeout_s: 20     # no first output within 20 s → next provider
```

- **Starts** when the attempt on that provider starts.
- **Stops** at the first real output; opening events alone do not stop it. Watching past the opening events needs `empty_response_action: fallback` (without it those events go to the client at once and the stream is committed).
- **Never applied to the last provider of a chain** — there is nothing to fall back to, so waiting is the better answer.
- Not applied to non-streaming requests (`timeout_s` bounds those).
- **Only the `/v1/messages` (Anthropic) streaming path** is covered; `/v1/chat/completions` is not.
- **Not applied to `kind: openai_compat` requests that carry tools** (Claude Code sends tools on almost every request). That combination calls the upstream non-streaming so tool calls can be repaired (the tool-turn downgrade), which makes the "first output" the whole answer; a deadline there would throw away slow but healthy local replies. To use the TTFT cut-off with a local llama.cpp / Ollama, connect with `kind: anthropic` (native `/v1/messages`) as in the example above.
- An abandoned attempt is an ordinary failure: `X-CodeRouter-Fallback-Reason: ttft-timeout`, and the self-healing guards learn from it.

Typical use: a local llama-server is still loading a model or chewing through a long prompt while a cloud provider could answer now. Start at two to three times your usual local TTFT; too short and every request escapes to the cloud.

## What a failed chain returns (v2.16.0 / v2.17.0)

| Upstream situation | HTTP | `error.type` | `x-should-retry` |
|---|---|---|---|
| Context overflow (any provider) | 400 | `invalid_request_error` (`prompt is too long: …`) | `false` |
| A 400 / 422 that retrying cannot fix | 400 | `invalid_request_error` (upstream wording) | `false` |
| 413 | 413 | `request_too_large` | `false` |
| 429 | 429 | `rate_limit_error` (upstream `retry-after` passed through) | `true` |
| Anything else (5xx, timeouts, 401/403, …) | 502 | `api_error` | `true` if at least one failure was retryable |

A streaming request whose chain fails before the first event gets this HTTP status too, not `200` with an in-stream `error` event. Claude Code compacts on `prompt is too long` and uses `x-should-retry` / `retry-after` to decide whether to retry.

## Known limits

- **Nothing reaches the client while opening events are withheld.** If a long local prompt keeps that window open, the client's idle timeout (Claude Code's `CLAUDE_STREAM_IDLE_TIMEOUT_MS`) can fire. Keep `ttft_timeout_s` below it.
- There is no keep-alive `ping` during that window yet.
