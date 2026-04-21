# Do you need CodeRouter?

Short answer: CodeRouter is a **wire-translation + band-aid layer**.
If your agent already talks the same wire as your model, and your
model behaves well out of the box, you do not need it. If either
of those fails, CodeRouter earns its keep. This page gives you two
small matrices — agent × model — so you can decide in about a
minute.

---

## 1. Agent compatibility

Not every agent can be pointed at a local `/v1` endpoint. The
constraint is whether the agent exposes a base-URL / endpoint
knob, and what wire format it speaks.

| Agent | Wire format | Can point at Ollama directly? | Need CodeRouter? |
|---|---|---|---|
| **Claude Code** | Anthropic `/v1/messages` | No — Ollama speaks OpenAI | **Yes** — wire translation is the whole point |
| **Codex CLI** (`@openai/codex`) | OpenAI | Yes, via `OPENAI_BASE_URL` | Optional — only if you need filters / fallback |
| **Plain OpenAI SDK / `curl`** | OpenAI | Yes, via `base_url` | Optional — same as above |
| **gemini-cli** | Gemini | No — different wire | Yes, once a Gemini adapter is added |
| **GitHub Copilot CLI** (`gh copilot`) | GitHub-proprietary | **No — backend is locked** | **N/A** — Copilot cannot be redirected at all |

Takeaway: **Claude Code users need a bridge** (CodeRouter or
equivalent) to ever reach a local model. **OpenAI-compatible
CLIs can hit Ollama directly**, so for them the decision shifts
to the model.

---

## 2. Model behavior

CodeRouter's output filters and repair logic are band-aids for
specific model misbehaviors. If your model is well-behaved,
those filters sit idle.

| Model family | Typical issue | Filter that fixes it |
|---|---|---|
| `llama3.1` / `llama3.2` instruct (Q5+) | None usual | — |
| `mistral-nemo` / `mistral-small` | None usual | — |
| `phi-3` / `phi-4` | None usual | — |
| `qwen2.5` (non-coder) | None usual | — |
| **`qwen2.5-coder`** (any size) | Emits `<think>…</think>` in output | `strip_thinking` |
| **`gpt-oss-120b` / `gpt-oss-20b`** | Emits `<think>…</think>` | `strip_thinking` |
| **`deepseek-r1` / `qwq`** (reasoning models) | Full reasoning leaks into user-visible output | `strip_thinking` |
| **Small quantizations** (Q2 / Q3) | Tool-call JSON malformed | `repair_tool_call` |
| **Fine-tunes / Modelfiles with wrong template** | Stop markers leak (`<\|eot_id\|>`, `<\|im_end\|>`) | `strip_stop_markers` |

Takeaway: "Well-behaved model + OpenAI-compatible agent" is the
shape where CodeRouter is doing the **least** work for you.
Reasoning models (`r1`, `qwq`, `gpt-oss`, `qwen-coder`) and
small / niche quantizations are where it starts to matter.

---

## 3. The `num_ctx` footgun (affects everyone)

One thing that bites every local setup regardless of model:
**Ollama's default `num_ctx` is 2048 tokens**. That is too small
for real coding tasks, and Ollama **silently truncates** rather
than erroring.

- Direct path: you set `num_ctx` per session / per API call, and
  you remember to update it when the agent's system prompt grows.
- With CodeRouter: it's centralized in `providers.yaml` once.

Not the biggest reason to adopt CodeRouter, but worth knowing
it exists.

---

## 4. Quick decision tree

If **every** box is checked, the direct path works:

- [ ] Your agent is OpenAI-compatible (Codex CLI, plain SDK, curl)
- [ ] Your model is on the "no issue" list above (or you've
      verified yours)
- [ ] You're running a single provider (no local → cloud fallback)
- [ ] You don't need Anthropic `/v1/messages` ingress
- [ ] You're comfortable passing `num_ctx` / `keep_alive` yourself

Miss any one, and CodeRouter does real work.

---

## 5. Verify it yourself

Before adopting anything, prove your direct path actually works:

```bash
# Point Codex CLI (or any OpenAI-compatible tool) at Ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama  # dummy, Ollama ignores it
codex "write a function that reverses a string in rust"
```

Watch for these four symptoms:

1. **`<think>` tags visible in the reply** → reasoning leak;
   you need `strip_thinking`.
2. **Stray `<|eot_id|>` / `<|im_end|>` / `<|turn|>` at the end
   of replies** → template mismatch; you need `strip_stop_markers`.
3. **The agent says "no tool calls returned" while the model
   clearly printed tool JSON as plain text** → you need
   `repair_tool_call`.
4. **Long prompts get cut off without an error** → `num_ctx`
   too small.

A clean day of real work with none of those symptoms means you
don't need CodeRouter. If one shows up repeatedly, the matching
filter is what fixes it.

---

## 6. When CodeRouter is flat-out required

- **Claude Code against any non-Anthropic model.** `/v1/messages`
  doesn't exist on Ollama. There is no direct path.
- **Automatic local → free cloud → paid fallback with the
  mid-stream safety guard.** Writing that yourself is subtle
  (see [`docs/articles/zenn-02-coderouter-architecture.md`](./articles/zenn-02-coderouter-architecture.md)).
- **`coderouter doctor` for diagnosing a misbehaving setup.**
  Its six probes cover exactly the failure modes above.

## 7. When CodeRouter is the wrong choice

- **GitHub Copilot CLI.** It's locked to GitHub's backend. No
  tool on your side can redirect it.
- **Single-agent + single-model production where you want the
  fewest moving parts.** A direct `base_url` swap is simpler.
- **You need features CodeRouter does not have** — caching,
  embeddings, fine-grained cost tracking, conversation stores.
  That's LiteLLM / a custom wrapper's territory.

---

## Summary

Pull CodeRouter in when **at least one** of these is true:

1. Your agent speaks Anthropic (Claude Code)
2. Your model leaks `<think>` / stop markers / malformed tool JSON
3. You want tier fallback without writing the mid-stream guard
4. You want `doctor` to diagnose setup issues

Otherwise, `OPENAI_BASE_URL=http://localhost:11434/v1` is the
simpler right answer, and skipping CodeRouter is fine.
