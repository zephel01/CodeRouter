# Tool-call repair benchmark

Measures how well CodeRouter's `repair_tool_calls_in_text`
(`coderouter/translation/tool_repair.py`) rescues tool calls that local
models write into assistant *text* instead of the structured `tool_calls`
field — and how often a live model needs that rescue at all.

Two layers:

- **L1 — `run_offline.py`**: deterministic, no network, stdlib only. Runs the
  repairer over a fixed corpus of broken outputs (`corpus.jsonl`, 84 cases:
  59 expect-repair across 11 categories + 25 negatives, including 10
  adversarial-review counterexamples) and scores **recovery** and **false
  positives**. This is the regression gate for the repairer itself; it also
  runs in CI via `tests/test_toolrepair_bench.py`.
- **L2 — `run_live.py`**: talks to a real endpoint (a backend directly, or
  CodeRouter in front of it), applies the same 3-value verdict CodeRouter's
  `doctor` uses (**native** / **repair** / **fail**), and reports per-model
  rates.

Both load `tool_repair.py` by file path (`--tool-repair` to override), so you
can point the benchmark at any branch's repairer and diff before/after on the
same corpus.

## L1 — offline

```bash
python benchmarks/tool-repair/run_offline.py
```

Writes `results_offline.json` / `results_offline.md` next to the script.

Outcome classes: `recovered` (expected repair, got the right call),
`correct_pass` (expected NO repair, repairer stayed quiet), `missed`
(expected repair, got nothing/wrong), `false_positive` (repairer fabricated
a call — the dangerous direction; the `negative` category guards it and must
stay at zero).

Corpus policy: cases the current repairer *cannot* fix are still marked
`expect.repaired: true` so they show up as `missed` — the benchmark surfaces
gaps instead of rubber-stamping today's behaviour.

## L2 — live

```bash
# Self-test (no server needed) — run this first:
python benchmarks/tool-repair/run_live.py --dry-run

# Backend directly (Ollama's OpenAI-compatible endpoint):
python benchmarks/tool-repair/run_live.py \
  --base-url http://localhost:11434/v1 --wire openai \
  --model qwen2.5-coder:7b --reps 20 --tag direct

# Through CodeRouter (start it with the bundled bench config first):
#   coderouter-t serve --port 8088 --config benchmarks/tool-repair/providers.bench.yaml
python benchmarks/tool-repair/run_live.py \
  --base-url http://localhost:8088 --wire anthropic \
  --model qwen2.5-coder:7b --profile bench-qwen7b --reps 20 --tag coderouter
```

Notes:

- CodeRouter treats the client-sent `model` as a routing placeholder (the
  provider's configured model wins), so on the router path you select the
  backend model with `--profile` (see `providers.bench.yaml`).
- `--temperature 0` is the default so a direct-vs-router comparison measures
  the path, not the sampler. Use `--temperature none` for backend-default
  sampling.
- Requires `httpx` (already a CodeRouter runtime dep).
- `bench_l3_p1.sh` runs the whole model matrix (direct + router per model);
  `REPS=5` for a smoke pass, `P2=1` to include the second-priority models.

## Measured results (2026-07-05, M3 Max, 100 requests per cell, zero errors)

Kept under `results/` as evidence for the write-ups. Repairer-version A/B
snapshots live in `results/archive-v2.7.0/` and `results/archive-v2.7.1/`;
top-level files are the latest (v2.7.2 / R4, plus the fallback-chain run).

| model | direct | v2.7.0 | v2.7.1 | **v2.7.2 (R4)** | reading |
|---|:-:|:-:|:-:|:-:|---|
| qwen2.5-coder:7b | 0% | **100%** | 100% | 100% | fenced text-JSON: base repairer does all the work |
| qwen2.5-coder:1.5b | 0% | **100%** | 100% | 100% | second 0→100 model, size-independent |
| mistral:7b | 0% | 80% | 80% | **100%** | colon call-syntax (`echo(message: 'x')`) closed by R4c |
| phi4-mini:3.8b | 0% | 20% | 40% | **80%** | +20pt = R2 aliases, +40pt = R4c; the last 20% is the semantically-corrupted case the repairer refuses **by design** |
| llama3.2:3b / 3.1:8b | 100% | 100% | 100% | 100% | zero degradation (negative result: they don't break at temp 0) |
| qwen3-coder:30b | 100% | 100% | 100% | 100% | zero degradation |
| gemma4:26b | 80% | 80% | 80% | 80% | empty 200s — no text to repair |
| gemma4:26b → qwen3-30b chain | — | — | — | **100%** | `empty_response_action: fallback` rescues all 20 blank turns |

Three-tier summary the numbers support: **(1)** broken-but-present tool
calls are repaired (four 0→100/0→80 models), **(2)** healthy models pass
through undegraded, **(3)** what repair cannot touch — blank responses,
semantically corrupted arguments — is fallback territory, and the chain row
shows fallback closing it.

Offline (post-v2.7.2 repairer): recall **100%** (59/59), false positives
**0/25**. The pre-R4 repairer scored 78.0% on the same corpus (nested-XML
0/5, JSON-envelope 1/4, call-syntax 0/5) — that gap, measured live in the
mistral/phi4 rows above, is what drove the R4 upgrade shipped in v2.7.2.

Note: gemma3:27b was excluded — its Ollama build ships without the `tools`
capability and 400s any tool-bearing request (verify with `ollama show`).

## Growing the corpus

Real-world broken tool-call outputs are welcome — add a line to
`corpus.jsonl` with an honest `expect` and a `note` explaining the failure
mode, and keep the `negative` cases passing (false positives stay at zero,
no exceptions).
