# HuggingFace 配布モデルを Ollama 経由で使う

> CodeRouter の `examples/providers.yaml` には、Ollama 公式 registry に
> まだ登録されていない trending モデル（Gemma 4 26B-A4B、GLM-4.5-Air、
> Qwen3 系の Opus 蒸留 fine-tune 等）を **コメントアウトされた provider
> stanza** として用意しています。本ドキュメントは、それらを実際に
> 動かす手順をまとめたものです。

---

## 前提

- Ollama 0.3.13 以降（HF 直接実行サポート）
- ローカル GPU/Mac で十分な VRAM／統合メモリ
- HuggingFace のアカウント（gated repo を pull する場合のみ。
  Qwen3-Coder / Gemma 4 / GLM 系は概ね free public）

ご自身の Ollama version 確認：

```bash
ollama --version
# ollama version is 0.3.13 以上であれば OK
```

---

## 基本手順

### 1. HF GGUF を pull

```bash
# 例: Qwen3-Coder 30B-A3B (Q4_K_M 量子化版) を pull
ollama pull hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M
```

ポイント：
- `:<quant>` サフィックスは **必須**。省略すると Ollama が `404 model
  not found` を返します（CodeRouter v0.7-B doctor の `auth+basic-chat`
  プローブはこれを `UNSUPPORTED` として検出してくれます）。
- 量子化バリアントの選び方：
  - `Q4_K_M`: サイズ／品質バランスの定番。**まず試すならこれ**。
  - `Q5_K_M`, `Q6_K_M`: メモリに余裕があれば品質向上。
  - `Q8_0`: 元モデルにほぼ無損失。VRAM は約 2 倍必要。
  - `IQ3_XS` 等: 極小化（軽量機向け）。品質劣化は明確。

### 2. （任意）短い alias を付ける

`hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M` のような長い名前
を `providers.yaml` に書くのは見づらいので、`ollama cp` で
ローカル alias を切るのを推奨：

```bash
ollama cp hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M qwen3-coder:30b-a3b
```

これで `qwen3-coder:30b-a3b` という名前で扱えるようになります。

### 3. providers.yaml の該当 stanza を有効化

`examples/providers.yaml` の HF-on-Ollama セクションから該当 stanza の
コメントを外し、`model:` フィールドを **手順 1 で pull した名前**
（または手順 2 の alias）に書き換えます：

```yaml
# 編集前 (コメントアウト):
# - name: ollama-qwen3-coder-480b-hf
#   kind: openai_compat
#   ...

# 編集後:
- name: ollama-qwen3-coder-30b-hf
  kind: openai_compat
  base_url: http://localhost:11434/v1
  # 手順 2 で alias を切ったなら:
  model: qwen3-coder:30b-a3b
  # alias を切らなかったなら:
  # model: hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M
  paid: false
  timeout_s: 240
  output_filters: [strip_thinking, strip_stop_markers]
  capabilities:
    chat: true
    streaming: true
    tools: true
```

### 4. 該当プロファイルの `providers:` リストにも追記

例えば `coding` プロファイルのローカル primary に置きたいなら：

```yaml
profiles:
  - name: coding
    append_system_prompt: |
      ...
    providers:
      - ollama-qwen3-coder-30b-hf  # ← 追加
      - ollama-qwen-coder-14b
      - ...
```

### 5. `coderouter doctor --check-model` で検証

```bash
coderouter doctor --check-model ollama-qwen3-coder-30b-hf
```

期待出力：
- `auth+basic-chat`: OK
- `num_ctx`: NEEDS_TUNING（Ollama default 2048 だと canary 失敗）
- `tool_calls`: OK
- `streaming`: OK or NEEDS_TUNING

NEEDS_TUNING が出たら、v1.8.0 で実装した自動 patch 適用を使えます：

```bash
coderouter doctor --check-model ollama-qwen3-coder-30b-hf --apply
# → providers.yaml に extra_body.options.num_ctx: 32768 等を非破壊書き戻し
```

---

## 推奨モデル別の登録例

### コーディング向け

```bash
# Qwen3-Coder 30B-A3B (24GB+ VRAM 推奨、coding profile primary)
ollama pull hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M
ollama cp  hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M qwen3-coder:30b-a3b

# Qwen3-Coder 480B-A35B (Mac M3 Ultra 192GB / NVIDIA H100x2 級が必要)
ollama pull hf.co/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF:Q4_K_M
ollama cp  hf.co/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF:Q4_K_M qwen3-coder:480b-a35b
```

### 雑用 / 一般向け（note 記事推奨）

> **2026-04 update**: Gemma 4 / Qwen3.6 は Ollama 公式 registry に
> 登録されました。HF 経由は不要です。`ollama pull gemma4:26b` / `ollama
> pull qwen3.6:35b` でそのまま使えます。providers.yaml の
> `ollama-gemma4-*` / `ollama-qwen3-6-*` stanza は既に有効化されています。

### Reasoning 向け（GLM / Opus 蒸留）

```bash
# GLM-4.5-Air ("intent 理解が Claude Opus 級")
# 推奨: Z.AI の cloud API 経由 (zai-coding-glm-4-5-air) を使えば
# 登録不要です。下記はローカルで動かしたい場合のみ:
ollama pull hf.co/unsloth/GLM-4.5-Air-Instruct-GGUF:Q4_K_M
ollama cp  hf.co/unsloth/GLM-4.5-Air-Instruct-GGUF:Q4_K_M glm-4.5-air

# Qwen3 系の Opus 蒸留 fine-tune は「qwen3 opus distill」「claude-distill qwen3」
# で HF 検索してください。コミュニティ fine-tune が複数並びます。
# 例 (実在 repo に置き換えてください):
# ollama pull hf.co/<author>/Qwen3-Opus-Distill-30B-GGUF:Q4_K_M
```

### 注意: Z.AI Coding Plan の "unauthorized tool" 警告

GLM family を本格的に使いたい場合、Z.AI の Coding Plan ($18/月〜) が
費用効率最良です。ただし公式 docs (docs.z.ai/devpack/overview) には
「未認可サードパーティツール経由のアクセスは benefit 制限の可能性」と
明記されています。CodeRouter は Anthropic API 互換 ingress を提供する
ため認可ツールに見えるはずですが、検出ロジック次第です。

確実に使いたい場合は次のいずれか：

1. **Claude Code に Z.AI を直結**（CodeRouter 経由しない）
2. **Z.AI General API (`/api/paas/v4`) を pay-as-you-go で使う** —
   `examples/providers.yaml` の `zai-paas-glm-4-7` (commented) を有効化

---

## 既知の落とし穴

### 1. `:<quant>` サフィックス忘れ

```
$ ollama pull hf.co/unsloth/Qwen3-Coder-30B-A3B-GGUF
Error: 404 page not found
```

→ 量子化サフィックスは必須。HF 上の repo を確認して `Q4_K_M` 等を付ける。

### 2. Ollama default の `num_ctx: 2048` が小さすぎる

Claude Code は 15-20K トークンの system prompt を毎ターン送ります。
Ollama default の context window 2048 だと、**プロンプトの先頭から
silent truncate** されて tool 宣言が消え、Claude Code が「なぜか
ツールが使えない」状態になります。

対処：CodeRouter は v1.0-B で `extra_body.options.num_ctx: 32768` を
patch として emit します。`coderouter doctor --check-model <name>
--apply` で自動適用。

### 3. 量子化サイズと VRAM のミスマッチ

| 量子化 | サイズ目安（30B モデル） | 必要 VRAM |
|---|---|---|
| Q4_K_M | ~18 GB | 20 GB+ |
| Q5_K_M | ~22 GB | 24 GB+ |
| Q6_K_M | ~26 GB | 28 GB+ |
| Q8_0 | ~32 GB | 36 GB+ |

VRAM が足りないと CPU offload が発生して激遅になります。`ollama ps`
で実行中モデルが GPU に乗ってるか確認できます。

### 4. CodeRouter capability registry との不整合

`hf.co/...` 形式のモデル名は CodeRouter の bundled
`model-capabilities.yaml` の glob (`qwen3-coder:*` 等) と一致しない
ので、capability 自動解決が効きません。`providers.yaml` 側で
`capabilities.tools: true` などを **明示宣言** するか、`ollama cp`
で短い alias を切って glob にマッチさせてください。

```yaml
# 例: HF 名そのまま使う場合は capabilities を明示
- name: ollama-qwen3-coder-30b-hf
  model: hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M
  capabilities:
    tools: true       # ← registry の glob にマッチしないので明示
```

または推奨：

```bash
ollama cp hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M qwen3-coder:30b-a3b
```

→ これで bundled registry の `qwen3-coder:*` glob にマッチし、
`tools: true` / `claude_code_suitability: ok` が自動解決されます。

---

## 参考リンク

- Ollama HF integration: <https://huggingface.co/docs/hub/en/ollama>
- Unsloth (高速量子化版の代表的アップローダー): <https://huggingface.co/unsloth>
- **Unsloth: Tool calling guide for local LLMs (日本語)**: <https://unsloth.ai/docs/jp/ji-ben/tool-calling-guide-for-local-llms>
  — Qwen / Llama / Gemma など local LLM で tool-call が動かない／壊れる原因と対策をモデル別に整理。CodeRouter で `tool_calls: NEEDS_TUNING` が出たときの背景理解にちょうど良い。
- bartowski (品質重視の量子化版): <https://huggingface.co/bartowski>
- Qwen3-Coder (Alibaba 公式): <https://huggingface.co/collections/Qwen/qwen3-coder>
- Gemma 4 (Google 公式): <https://huggingface.co/collections/google/gemma-4>
- CodeRouter doctor 詳細: [`docs/troubleshooting.md`](./troubleshooting.md)
- providers.yaml 全体構造: [`examples/providers.yaml`](../examples/providers.yaml)
