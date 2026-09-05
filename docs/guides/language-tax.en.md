# Language Tax guide

Supplement to [`README.en.md`](../../README.en.md). Explains the **Language Tax tracking** added in CodeRouter v2.6.0 — how it works, how to configure it, and how to tune it.

日本語版: [`language-tax.md`](./language-tax.md)

Contents:

1. [What the language tax is](#1-what-the-language-tax-is)
2. [Measured data](#2-measured-data)
3. [Enable measurement (`tokenizer_path`)](#3-enable-measurement-tokenizer_path)
4. [Avoid it by routing (`cjk_ratio_min`)](#4-avoid-it-by-routing-cjk_ratio_min)
5. [See it on the dashboard](#5-see-it-on-the-dashboard)
6. [How it works & confidence](#6-how-it-works--confidence)
7. [Tuning](#7-tuning)

---

## 1. What the language tax is

Cloud LLM tokenizers split CJK text (Japanese / Chinese / Korean) into more pieces than English. So **the same meaning costs more tokens in Japanese**, inflating cloud API spend. That's the "language tax."

Local models don't bill per token, so the tax only applies to the **cloud leg**. CodeRouter sits exactly at the local↔cloud boundary, so it can measure, avoid, and visualize it.

Two distinct axes:

- **Economic tax (same meaning, JA vs EN)** — how many more tokens the same task costs in Japanese vs English. This is the "expensiveness" operators feel.
- **char/4-baseline multiplier (CodeRouter's metric)** — the router estimates tokens with a `char/4` heuristic (English-calibrated, ~4 chars/token). For CJK that heuristic badly under-counts, so the real-tokenizer multiplier is larger than the economic tax.

---

## 2. Measured data

Measured with real production tokenizers (reproduce: `_OUTPUTS/01-機能実装/language-tax/measure/`).

### Economic tax — same meaning, JA vs EN (avg of 5 translation pairs)

| Tokenizer | Representative models | JA/EN token ratio |
|---|---|---|
| `cl100k_base` | GPT-4 / Claude-3 era | **2.04×** |
| `o200k_base` | GPT-4o era | **1.60×** |

Short instructions drop to **1.28–1.31×** on o200k. **Newer tokenizers are more JA-efficient** — the widely cited "1.2–1.5×" matches modern tokenizers on shorter text.

### char/4-baseline multiplier (CodeRouter's `tax_multiplier`)

Accurate tokenizer = a real **Qwen2.5** `tokenizer.json`.

| Sample | CJK ratio | char/4 | real tokens | multiplier |
|---|---|---|---|---|
| English code | 0% | 18 | 22 | 1.22× |
| English prose | 0% | 29 | 19 | 0.66× |
| Mixed (JA comment + code) | 51% | 16 | 40 | **2.50×** |
| Japanese instructions | 100% | 14 | 36 | **2.57×** |
| Japanese technical prose | 98% | 29 | 87 | **3.00×** |

> Note: English prose at 0.66× is because char/4 *over*-counts English prose (~6 chars/token in reality). char/4 is an approximation, so `tax_multiplier` is "tax relative to CodeRouter's own char/4 baseline" (confidence: MODERATE).

---

## 3. Enable measurement (`tokenizer_path`)

Point a provider at the model's **local `tokenizer.json`**. **Unset = measurement disabled (multiplier 1.0)** and the hot path is untouched.

```yaml
providers:
  - name: cloud-sonnet
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    tokenizer_path: ~/.coderouter-t/tokenizers/sonnet.json
```

### Getting a tokenizer.json (once)

CodeRouter itself never hits the network (local file only). Download the tokenizer once as the operator:

```bash
pip install "coderouter-t[accuracy]"   # accurate-count backend (tokenizers)

python - <<'PY'
from huggingface_hub import hf_hub_download
p = hf_hub_download(repo_id="Qwen/Qwen2.5-0.5B", filename="tokenizer.json")
print(p)   # copy into ~/.coderouter-t/tokenizers/ and set tokenizer_path
PY
```

> If you don't have a cloud model's exact tokenizer, a public tokenizer with similar billing behavior (e.g. an o200k-equivalent) works as a proxy; the multiplier is then approximate.

---

## 4. Avoid it by routing (`cjk_ratio_min`)

When the latest user message's **CJK character ratio ≥ threshold**, auto-route to a tax-free local profile; code/English turns fall through to the cloud. Per-turn matcher, like `code_fence_ratio_min`.

```yaml
auto_router:
  rules:
    - match: { cjk_ratio_min: 0.3 }   # >=30% CJK chars -> local
      profile: local
    - match: { has_tools: true }      # tool use -> cloud
      profile: cloud
  default_rule_profile: cloud
```

Fires when `default_profile: auto`. `cjk_ratio_min` is the fraction of non-whitespace characters that are CJK (0.0–1.0).

---

## 5. See it on the dashboard

The **"Cost & Language Tax"** panel at `http://localhost:8088/dashboard` shows total spend, cache savings, and **language-tax spend (aggregate + per-provider)**, polled every 2s. With no `tokenizer_path`, it shows "no tax measured."

CLI users can read `counters.language_tax_usd_aggregate` / `counters.language_tax_usd` from `/metrics.json`; it's also exposed on the Prometheus `/metrics` endpoint.

---

## 6. How it works & confidence

- **Measure**: `language_tax.estimate_language_tax(text, tokenizer_path=...)` counts with both char/4 (heuristic) and the real tokenizer (accurate), returning `tax_multiplier = accurate / char4` and `extra_tokens = accurate − char4`.
- **Cost**: `compute_cost_for_attempt(..., language_tax=...)` computes the tax USD as `extra_tokens × provider input rate`; free/local providers contribute 0.
- **Aggregate**: the `cache-observed` log carries `language_tax_usd` / `language_tax_multiplier`; `MetricsCollector` keeps per-provider + aggregate totals.

Invariants:

- **No new core dependency** (accurate counting is the existing optional `accuracy` extra, `tokenizers`).
- **No network** (local `tokenizer.json` only; never contacts the HF Hub).
- **Backward compatible** (everything defaulted; inert when `tokenizer_path` is unset).

Confidence is **MODERATE**: char/4 is itself an English approximation, so the multiplier is "tax relative to CodeRouter's own English baseline." Still, it's a fully reproducible measurement with no network and no guessing.

---

## 7. Tuning

- **Threshold**: start `cjk_ratio_min` around 0.3. Raise to 0.5 if you don't want code with a few JA comments going local; lower to 0.2 if your sessions are JA-dialogue heavy.
- **Mixed turns**: ASCII-code-heavy requests with a little JA tend to land at 1.2–1.5× economic tax and low `cjk_ratio`. Keep tool turns on cloud by putting `has_tools: true` above the CJK rule.
- **Measure first**: set `tokenizer_path`, watch real numbers on `/dashboard`, then A/B the routing avoidance with `coderouter replay`.
