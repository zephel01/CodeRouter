# llama.cpp 直叩き backend ガイド (Qwen3.6 を Ollama 詰みから救出する経路)

> **このドキュメントは何か**: Ollama 経由で詰みやすいモデル (とくに Qwen3.6:35b-a3b 系) を、`llama.cpp` の `llama-server` で直接動かして CodeRouter に接続する手順。CodeRouter v1.8.3 で実機検証済 (M3 Max 64GB / Unsloth UD-Q4_K_M GGUF / native `tool_calls` 完璧動作確認)。

---

## なぜ必要か

v1.8.1 〜 v1.8.3 の実機検証 + コミュニティ報告 (X / Reddit / r/ollama / r/LocalLLaMA) で、**Qwen3.6 系 (qwen3.6:35b / qwen3.6:27b / qwen3.6:35b-a3b-coding-nvfp4 等) は Ollama 経由だと現状詰みやすい**ことが判明：

- `tool_calls [NEEDS TUNING]` (Ollama の chat template / tool 仕様が未成熟、native tool_calls も修復可能 JSON も返さない)
- hard crash / リブート (Mac Metal、複数報告)
- メモリ計算バグで「available memory 不足」(実際は十分 RAM ある)
- `qwen3.6:35b-a3b-coding-nvfp4` などの variant が MLX backend + NVFP4 quant の組み合わせで 500 Internal Server Error

**モデル本体は健全**で、フレームワーク (Ollama) 側の対応待ちが原因。Unsloth が出している GGUF を `llama.cpp` 本体の `llama-server` で叩くと、native `tool_calls` が完璧に出る — これが現時点で最も安定した経路です。

---

## 前提

- macOS (Apple Silicon、Metal で GPU offload)
  - Linux + CUDA でも基本同じ手順、`-DGGML_CUDA=ON` に置き換え
- 推奨スペック: M3 Max 64GB (Q4_K_M で 22GB GGUF + KV cache + headroom が余裕で乗る)
  - 32GB Mac でも動くが headroom 少なめ
- 必須ツール: `git`, `cmake`, `huggingface-cli` (`uv tool install huggingface_hub[cli]` で入る)
- 所要時間: build 5-10 分 + GGUF download 5-10 分 (回線次第) ≒ 計 15-20 分

---

## 手順

### Step 1. llama.cpp を build

```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cd ~/llama.cpp
cmake -B build -DGGML_METAL=ON -DLLAMA_CURL=ON
cmake --build build --config Release -j
```

完了後、`./build/bin/llama-server --version` でバージョンが出れば OK。

> **Linux + CUDA の場合**: `cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=ON` に置換。NVIDIA driver と CUDA toolkit が必要。

### Step 2. Unsloth の GGUF をダウンロード

```bash
# huggingface-cli が無ければ
uv tool install huggingface_hub[cli]

# Q4_K_M variant (UD = Unsloth Dynamic Quantization、~22GB)
huggingface-cli download unsloth/Qwen3.6-35B-A3B-GGUF \
  --include "*UD-Q4_K_M*" "*tokenizer*" "*chat_template*" \
  --local-dir ~/models/qwen3.6-35b-a3b-unsloth

# 確認
ls -lah ~/models/qwen3.6-35b-a3b-unsloth/
# → Qwen3.6-35B-A3B-UD-Q4_K_M.gguf (~22GB) と tokenizer 関連ファイル
```

> **`UD-Q4_K_M` の意味**: Unsloth Dynamic Quantization。同 GGUF サイズで通常の Q4_K_M より精度が高い variant。コミュニティ報告でも de facto 標準として使われています。
>
> **Q5_K_M / Q6_K も載せる余裕がある場合**: `--include "*UD-Q5_K_M*"` 等で別 quant を取得。M3 Max 64GB なら Q5_K_M (~26GB) も余裕。

### Step 3. llama-server を起動

```bash
~/llama.cpp/build/bin/llama-server \
  --model ~/models/qwen3.6-35b-a3b-unsloth/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
  --port 8080 \
  --ctx-size 32768 \
  --n-predict 4096 \
  --jinja \
  --threads 8 \
  -ngl 999 \
  --host 127.0.0.1
```

各オプションの意味：

- `--port 8080`: OpenAI 互換 API のポート (CodeRouter から接続する先)
- `--ctx-size 32768`: コンテキスト窓 (Claude Code の system prompt 15-20K tokens に余裕)
- `--n-predict 4096`: 1 回の生成上限 token 数
- `--jinja`: GGUF metadata 埋め込みの chat template を使う (Unsloth GGUF は通常埋め込み済)
- `--threads 8`: CPU 補助 thread 数
- `-ngl 999`: 全 layer を GPU にオフロード (Metal)
- `--host 127.0.0.1`: localhost からのみ受け付け (security)

起動 log で以下が出れば OK：

```
load_model: model loaded successfully
chat template: ... qwen3 ...
tool calling: enabled
HTTP server listening on 127.0.0.1:8080
```

### Step 4. 動作確認 (CodeRouter なし、curl 直接)

```bash
# 4-A. 基本動作 (max_tokens は thinking モデルなので generous に)
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.6",
    "messages": [{"role":"user","content":"Say hello in one word."}],
    "max_tokens": 500
  }' | jq '.choices[0].message'

# 期待: {"role":"assistant","content":"Hello","reasoning_content":"..."}

# 4-B. tool_calls (これが本丸)
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.6",
    "messages": [{"role":"user","content":"Call the echo tool with message=\"hi\""}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "echo",
        "description": "Echo a message back.",
        "parameters": {
          "type": "object",
          "properties": {"message": {"type":"string"}},
          "required": ["message"]
        }
      }
    }],
    "tool_choice": "auto",
    "max_tokens": 500
  }' | jq '.choices[0]'

# 期待: finish_reason="tool_calls" + tool_calls[] array が返る
```

### Step 5. CodeRouter に provider として登録

`~/.coderouter/providers.yaml` の `providers:` リストに以下を追加：

```yaml
- name: llamacpp-qwen3-6-35b-a3b
  kind: openai_compat
  base_url: http://localhost:8080/v1
  model: qwen3.6                # llama-server は任意の model 名を受ける
  paid: false
  api_key_env: null              # llama-server は無認証 default
  timeout_s: 120                 # thinking モデルなので generous に
  capabilities:
    chat: true
    streaming: true
    tools: true                  # native tool_calls 動作確認済
    thinking: true               # thinking モデル、doctor が 1024 token budget を使う
  output_filters:
    - strip_thinking             # `<think>...</think>` ブロックは念のため除去
  # llama.cpp は extra_body.options を受けない (Ollama 専用)、context /
  # max_tokens は server 起動引数 (--ctx-size / --n-predict) で確定済
```

`coding` profile の primary 候補に組み込む例：

```yaml
profiles:
  - name: coding
    providers:
      - llamacpp-qwen3-6-35b-a3b   # ← 第一候補 (要事前準備)
      - ollama-qwen-coder-14b      # 事前準備不要なら top-tier
      - ollama-gemma4-26b          # 実機検証済み
      ...
```

> **完全な examples/providers.yaml** には CodeRouter 同梱の `llamacpp-qwen3-6-35b-a3b` provider 例 + コメント詳細あり。`cp examples/providers.yaml ~/.coderouter/providers.yaml` で丸ごと持ってくることも可能。

### Step 6. doctor で動作確認

```bash
coderouter doctor --check-model llamacpp-qwen3-6-35b-a3b
```

期待される結果：

```
[1/6] auth+basic-chat …… [OK]
[2/6] num_ctx ………………… [SKIP]    ← port 8080 (Ollama 専用 knob は使わない)、設計通り
[3/6] tool_calls ………… [OK]    ← native tool_calls observed
[4/6] thinking ………… [SKIP]
[5/6] reasoning-leak …… [OK]    ← reasoning_content 検出 → adapter で strip 済
[6/6] streaming ………… [SKIP]

Summary: all probes match declarations.
Exit: 0
```

`tool_calls [OK]` が出ればこのドキュメントの目的達成です。

### Step 7. CodeRouter 経由 end-to-end (Anthropic 互換)

```bash
# CodeRouter 起動 (test profile を作って llama.cpp 経路だけテストする例)
coderouter serve --port 8088 --mode test-llamacpp &
sleep 2

# Anthropic 互換 API で 1 round-trip
curl -s -X POST http://localhost:8088/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'x-api-key: dummy' \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 500,
    "messages": [{"role":"user","content":"Say hello in one word."}]
  }' | jq '.content'

# 期待: [{"type":"text","text":"Hello"}]
```

CodeRouter ログに：

```
try-provider provider=llamacpp-qwen3-6-35b-a3b stream=false
HTTP Request: POST http://localhost:8080/v1/chat/completions "HTTP/1.1 200 OK"
capability-degraded provider=llamacpp-qwen3-6-35b-a3b
  dropped=["reasoning", "reasoning_content"] reason=non-standard-field
provider-ok provider=llamacpp-qwen3-6-35b-a3b
```

`dropped: ["reasoning", "reasoning_content"]` ← v1.8.3 で追加した llama.cpp 用 strip が動作している証拠。

---

## トラブルシューティング

### `tool_calls [NEEDS TUNING]` が出る (CodeRouter v1.8.2 以前)

- v1.8.2 以前は `tool_calls` probe の `max_tokens=64` が thinking モデルの `reasoning_content` で食い切られて偽陽性を出します
- **v1.8.3 にアップグレード必須** (`uv tool upgrade coderouter-cli`)

### `reasoning_content` フィールドが client に漏れる (v1.8.2 以前)

- v1.8.2 以前の adapter は `reasoning` (Ollama 命名) のみ strip、`reasoning_content` (llama.cpp 命名) は strip していなかった
- **v1.8.3 にアップグレード必須**

### llama-server 起動時に `chat template not found`

- GGUF metadata に template が埋め込まれていない可能性
- Unsloth の GGUF は通常 OK、別配布の GGUF を使ってる場合は `--chat-template-file path/to/template.jinja` で明示的に指定
- Qwen3 系の jinja template は `models/templates/` 配下にあるか、HF の tokenizer_config.json から抽出

### Mac で `-ngl 999` だが GPU offload されてない

- `cmake` の `-DGGML_METAL=ON` が効いていない可能性
- `cmake -B build -DGGML_METAL=ON --fresh` で再生成 → 再 build
- `./build/bin/llama-server --version` で `Metal: yes` が出るか確認

### 別ポートで起動したい

- `--port 8081` 等で変更、`providers.yaml` の `base_url` も合わせる

### CUDA 環境で動かしたい

- `cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=ON --fresh`
- `-DGGML_METAL=ON` は外す (Apple Silicon 限定)

---

## 関連ドキュメント

- [troubleshooting.md §4-2-A](./troubleshooting.md#4-2-a-qwen36) — Qwen3.6 + Ollama の既知問題
- [hf-ollama-models.md](./hf-ollama-models.md) — Ollama 経由 HF モデル取得
- [examples/providers.yaml](../examples/providers.yaml) — `llamacpp-qwen3-6-35b-a3b` provider 例同梱
- 実機検証の経緯: [v1.8.1 note](./articles/note-v1-8-1-reality-check.md) → [v1.8.2 note](./articles/note-v1-8-2-doctor-self-diagnosis.md) → [v1.8.3 note](./articles/note-v1-8-3-llamacpp-rescue.md)

---

## 他のモデルでも同じ経路が使える

llama.cpp の `llama-server` は基本どの GGUF でも上の手順で動きます。`--jinja` で chat template が efektiv 適用されればだいたい OK。Unsloth は多くのモデルで Dynamic Quantization GGUF を出しているので、HF で `unsloth/<モデル名>-GGUF` を検索すれば類似経路で他モデルにも応用可能：

- `unsloth/Llama-3.3-70B-Instruct-GGUF`
- `unsloth/gemma4-26b-GGUF`
- `unsloth/gpt-oss-120B-GGUF`
- など

provider name は `llamacpp-<モデル>` の命名で揃えると `examples/providers.yaml` のパターンと整合します。
