# LM Studio 直接 backend ガイド (qwen35 / qwen35moe を救出する第 2 経路)

> **このドキュメントは何か**: Ollama や llama.cpp 単体では当面動かしにくい `qwen35` (Qwen3.5 Dense) / `qwen35moe` (Qwen3.6 MoE) architecture を、LM Studio 0.4.12+ の Local Server 経由で動かして CodeRouter に接続する手順。CodeRouter v1.8.4 で実機検証済 (M3 Max 64GB / LM Studio 0.4.12 / Qwen3.5 9B / Qwen3.6 35B-A3B / Jackrong/Qwopus3.5-9B-v3-GGUF)。OpenAI 互換 (`/v1/chat/completions`) と Anthropic 互換 (`/v1/messages`) の両ルートを記述。

---

## なぜ必要か

v1.8.1 〜 v1.8.4 の実機検証で 3 つの状況が判明：

1. **`qwen35` architecture (Qwen3.5 Dense / 派生 Qwopus3.5)** は、執筆時点で llama.cpp 単体ビルドの一部 channel ではまだ unstable / 詰みやすい。`unable to load model: unknown model architecture: 'qwen35'` で開発者の手元では落ちることがあった
2. **`qwen35moe` architecture (Qwen3.6 35B-A3B)** は llama.cpp 直叩きで動くが、Ollama 経由では `tool_calls [NEEDS TUNING]` / hard crash / メモリ計算バグの報告
3. **LM Studio 0.4.12** で Qwen 3.6 公式サポート + Qwen 3.5 性能改善 + Anthropic 互換 `/v1/messages` 公式化 が一気に入った

結果、**LM Studio 経由が現時点で最も stable に `qwen35` / `qwen35moe` 系を動かせる経路** と確定。さらに Anthropic 互換ルートを使えば CodeRouter から `kind: anthropic` で adapter 翻訳ゼロで透過でき、**Anthropic prompt caching まで成立** (`cache_read_input_tokens` 観測済)。

`docs/llamacpp-direct.md` (CLI build + Unsloth GGUF + `llama-server`) と並ぶ第 2 の canonical 経路として位置づけます。

---

## 前提

- macOS (Apple Silicon、Metal で GPU offload) / Windows / Linux すべてサポート
- 推奨スペック: M3 Max 64GB (Q4_K_M で 22GB GGUF + KV cache + headroom が余裕で乗る)
  - 32GB Mac でも Qwen3.5 9B (Q4_K_M、6.5GB) は余裕で動く
- 必須ツール: LM Studio 0.4.12+ (https://lmstudio.ai/)
  - 0.4.12 未満では Qwen 3.6 公式サポートと Anthropic 互換 `/v1/messages` が無いので必ずアップグレード
- 所要時間: install 5 分 + GGUF download 5-10 分 (回線次第) ≒ 計 10-15 分

---

## 手順

### Step 1. LM Studio をインストール & モデルをダウンロード

1. https://lmstudio.ai/ から LM Studio 0.4.12+ をダウンロード + インストール
2. GUI の **Discover** タブを開き、以下のいずれかを検索 + ダウンロード:

   | モデル | quant | サイズ | 用途 |
   |---|---|---|---|
   | `lmstudio-community/Qwen3.5 9B` | Q4_K_M | ~6.5 GB | 軽量、起動が速い、検証用 first choice |
   | `lmstudio-community/Qwen3.6 35B A3B` | Q4_K_M | ~22 GB | 大型、本番候補 (M3 Max 64GB 推奨) |
   | `Jackrong/Qwopus3.5-9B-v3-GGUF` | Q4_K_M | ~5.6 GB | Claude Opus 蒸留、`qwen35` architecture |

> **GGUF と MLX の使い分け**: 執筆時点で **Qwen3.6 系は GGUF only**。MLX backend の Qwen3.6 サポートはまだ先。Apple Silicon の MLX 最適化メリットは Qwen3 30B-A3B / GPT-OSS 20B / Qwen3-VL 系でのみ享受できます。Qwen3.6 系は当面 GGUF (llama.cpp 系) ルート一択。

### Step 2. Chat タブで Load Model

LM Studio の **Chat** タブでダウンロードしたモデルを Load する際、以下のロード設定を確認します：

- **Context Length: 32768** (default 4096 では Claude Code system prompt が詰まる、必ず変更)
- **GPU Offload: max** (全 layer を Metal / CUDA に offload)
- **Flash Attention: ON** (推奨)
- **K/V Cache Quantization: OFF** (default F16、検証時は実験機能オフを推奨)

ロード完了後、Chat タブで「Hello」を送ると 1-2 秒で応答すれば成功。`Estimated memory` の表示が unified memory の上限に対して余裕があるかも確認してください (M3 Max 64GB 想定)。

### Step 3. Local Server タブで Server を起動

**Developer** (旧 Local Server) タブを開き、以下を設定して **Start Server**:

- **Port: 1234** (LM Studio default、CodeRouter `providers.yaml` の `base_url` と一致)
- **Just-in-time Model Loading: ON** (推奨)
- **Cross-Origin Resource Sharing (CORS): OFF** (default、CodeRouter は localhost からなので不要)
- **Serve on Local Network: OFF** (default、外部公開しない)

起動 log で以下が出れば OK:

```
[2026-04-27 21:00:00] [INFO] Server starting on port 1234
[2026-04-27 21:00:00] [INFO] Server started.
```

### Step 4. 動作確認 (CodeRouter なし、curl 直接)

#### 4-A. モデル一覧の確認

```bash
curl -s http://localhost:1234/v1/models | jq '.data[] | {id, owned_by}'
```

期待:

```json
{"id": "qwen/qwen3.5-9b",      "owned_by": "organization_owner"}
{"id": "qwen/qwen3.6-35b-a3b", "owned_by": "organization_owner"}
```

#### 4-B. OpenAI 互換 `/v1/chat/completions` で native tool_calls

```bash
curl -s http://localhost:1234/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5-9b",
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
    "max_tokens": 500,
    "temperature": 0
  }' | jq '.choices[0]'
```

期待:

```json
{
  "index": 0,
  "message": {
    "role": "assistant",
    "content": "\n\n",
    "reasoning_content": "The user wants me to call the echo tool with...",
    "tool_calls": [{
      "type": "function",
      "id": "573125855",
      "function": {"name": "echo", "arguments": "{\"message\":\"hi\"}"}
    }]
  },
  "finish_reason": "tool_calls"
}
```

✅ `finish_reason: "tool_calls"` + 正規 OpenAI `tool_calls[]` array が native で返る。`reasoning_content` 付帯は CodeRouter v1.8.3 以降の adapter が strip するので client には漏れない。

#### 4-C. Anthropic 互換 `/v1/messages` で native tool_use (LM Studio 0.4.12+ の目玉)

```bash
curl -s http://localhost:1234/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "qwen3.5-9b",
    "max_tokens": 500,
    "messages": [{"role":"user","content":"Call the echo tool with message=\"hi\""}],
    "tools": [{
      "name": "echo",
      "description": "Echo a message back.",
      "input_schema": {
        "type": "object",
        "properties": {"message": {"type":"string"}},
        "required": ["message"]
      }
    }]
  }' | jq '.'
```

期待:

```json
{
  "id": "msg_4w8746j0jnngapmqvl0zi",
  "type": "message",
  "role": "assistant",
  "content": [
    {"type": "text", "text": "\n\n"},
    {"type": "tool_use", "id": "998041094", "name": "echo", "input": {"message": "hi"}}
  ],
  "model": "qwen3.5-9b",
  "stop_reason": "tool_use",
  "usage": {"input_tokens": 284, "output_tokens": 59, "cache_read_input_tokens": 0}
}
```

✅ `stop_reason: "tool_use"` + Anthropic native `tool_use` block が完璧に返る。`cache_read_input_tokens` field 付き。これで CodeRouter から `kind: anthropic` で直接接続できる前提が成立。

### Step 5. CodeRouter に provider として登録

`~/.coderouter/providers.yaml` の `providers:` リストに 2 ルートを並べて登録します (`examples/providers.yaml` にも同じ pattern が同梱されているので参考に):

```yaml
# OpenAI 互換ルート (従来通り、CodeRouter の adapter 翻訳経由)
- name: lmstudio-qwen3-5-9b
  kind: openai_compat
  base_url: http://localhost:1234/v1
  model: qwen3.5-9b              # LM Studio は fuzzy match (qwen/qwen3.5-9b でも可)
  paid: false
  api_key_env: null               # LM Studio default は無認証
  timeout_s: 120
  capabilities:
    chat: true
    streaming: true
    tools: true                   # native tool_calls 動作確認済
    thinking: true                # thinking モデル、doctor が 1024 token budget を使う
  output_filters:
    - strip_thinking              # `<think>...</think>` は念のため除去

# Anthropic 互換ルート (v1.8.4 の目玉、adapter 翻訳ゼロ)
- name: lmstudio-qwen3-5-9b-anthropic
  kind: anthropic                 # ← ここがポイント、OpenAI 互換層を skip
  base_url: http://localhost:1234  # /v1/messages は LM Studio 側で受ける
  model: qwen3.5-9b
  paid: false
  api_key_env: null
  timeout_s: 120
  capabilities:
    chat: true
    streaming: true
    tools: true
    thinking: true

# (35B-A3B 版を追加する場合は model 名と timeout_s を調整して上の 2 stanza を複製)
```

profile に組み込む例 (test 用):

```yaml
profiles:
  # OpenAI 互換ルート (CodeRouter の adapter 翻訳経由)
  - name: test-lmstudio-openai
    providers:
      - lmstudio-qwen3-5-9b           # 軽量、起動が速い
      - lmstudio-qwen3-6-35b-a3b      # 大型、本番候補
      - openrouter-free               # 安全網

  # Anthropic 互換ルート (adapter 翻訳ゼロ)
  - name: test-lmstudio-anthropic
    providers:
      - lmstudio-qwen3-5-9b-anthropic
      - lmstudio-qwen3-6-35b-a3b-anthropic
      - openrouter-free
```

### Step 6. doctor で動作確認

#### OpenAI 互換ルート

```bash
coderouter doctor --check-model lmstudio-qwen3-5-9b
```

期待される結果:

```
[1/6] auth+basic-chat …… [OK]    200 OK (in=19, out=16)
[2/6] num_ctx ………………… [SKIP]    not Ollama-shape (port 1234)、設計通り
[3/6] tool_calls ………… [OK]    native `tool_calls` observed; matches declaration.
[4/6] thinking ………… [SKIP]    capabilities.thinking=true on openai_compat is informational
[5/6] reasoning-leak …… [OK]   upstream emits non-standard `reasoning_content`; v1.8.3 adapter strips it
[6/6] streaming ………… [SKIP]    streaming-path detection is Ollama-shape gated

Summary: all probes match declarations.
Exit: 0
```

#### Anthropic 互換ルート

```bash
coderouter doctor --check-model lmstudio-qwen3-5-9b-anthropic
```

期待される結果:

```
[1/6] auth+basic-chat …… [OK]    200 OK (in=19, out=16)
[2/6] num_ctx ………………… [SKIP]    not Ollama-shape
[3/6] tool_calls ………… [OK]    native `tool_calls` observed; matches declaration.
[4/6] thinking ………… [OK]    thinking block emitted; matches declaration.   ← !!
[5/6] reasoning-leak …… [SKIP]    only openai_compat emits non-standard reasoning field
[6/6] streaming ………… [SKIP]

Summary: all probes match declarations.
Exit: 0
```

★ **Anthropic 互換ルートでは `thinking [OK]` が出る** — opt-in (`thinking: {type: enabled}`) を request body に入れると、LM Studio 0.4.12 が thinking content block を Anthropic native 形式で返します。これは正規 Anthropic API (Claude Sonnet 4.5+) と完全互換の挙動。

### Step 7. CodeRouter 経由 end-to-end (Anthropic 互換)

```bash
coderouter serve --port 8088 --mode test-lmstudio-anthropic &
sleep 2

curl -s -X POST http://localhost:8088/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'x-api-key: dummy' \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 500,
    "messages": [{"role":"user","content":"Call the echo tool with message=\"hi\""}],
    "tools": [{
      "name": "echo",
      "description": "Echo a message back.",
      "input_schema": {
        "type": "object",
        "properties": {"message": {"type":"string"}},
        "required": ["message"]
      }
    }]
  }' | jq '.'
```

期待:

```json
{
  "id": "msg_pgpz5kcbudsu61h77z3yn",
  "type": "message",
  "role": "assistant",
  "model": "qwen3.5-9b",
  "content": [
    {"type": "text", "text": "\n\n"},
    {"type": "tool_use", "id": "311305849", "name": "echo", "input": {"message": "hi"}}
  ],
  "stop_reason": "tool_use",
  "usage": {
    "input_tokens": 284,
    "output_tokens": 59,
    "cache_read_input_tokens": 280   ← !!! prompt caching が動いている
  },
  "coderouter_provider": "lmstudio-qwen3-5-9b-anthropic"
}
```

CodeRouter ログ:

```
try-provider provider=lmstudio-qwen3-5-9b-anthropic stream=false native_anthropic=true
HTTP Request: POST http://localhost:1234/v1/messages "HTTP/1.1 200 OK"
provider-ok provider=lmstudio-qwen3-5-9b-anthropic stream=false native_anthropic=true
```

`capability-degraded` ログは出ない (Anthropic native shape は strip するものがない)。

★ **`cache_read_input_tokens: 280`** がここで出ているのが超重要 — **ローカル LLM (LM Studio) で Anthropic prompt caching が成立**しています。同 prompt を投げ続けるセッション (Claude Code の典型的な動作) で 2 回目以降の prefix が cache hit すると、上流のトークン処理を skip できる効果が実機で確認できます。

---

## トラブルシューティング

### `unable to load model: unknown model architecture: 'qwen35'`

- LM Studio 0.4.12 未満の可能性。**0.4.12 以降に必ずアップグレード**
- 旧 LM Studio で動かない場合、別経路 ([llama.cpp 直叩き](./llamacpp-direct.md)) も試す

### Context が 4096 のまま (Claude Code system prompt が詰む)

- Chat タブで Load Model 時の **Context Length: 32768** 設定を見直す
- 既にロード済みのモデルを Eject → 再 Load する必要あり (ロード後の動的変更は反映されない)

### `tool_calls [NEEDS TUNING]` が出る (CodeRouter v1.8.2 以前)

- v1.8.2 以前は `tool_calls` probe の `max_tokens=64` が thinking モデルの `reasoning_content` で食い切られて偽陽性
- **v1.8.3 以降に必ずアップグレード** (`uv tool upgrade coderouter-cli`)

### `reasoning_content` フィールドが client に漏れる (v1.8.2 以前)

- v1.8.2 以前の adapter は `reasoning` (Ollama 命名) のみ strip、`reasoning_content` (LM Studio / llama.cpp 命名) は strip していなかった
- **v1.8.3 以降に必ずアップグレード**

### Anthropic 互換 `/v1/messages` が 404

- LM Studio 0.4.12 未満。**0.4.12 以降に必ずアップグレード**
- LM Studio Developer タブで Server が Running 状態か確認

### Server が起動しない / 別 application が port 1234 を使用中

- LM Studio Developer タブで **Port** を任意の値 (例: 1235) に変更
- `providers.yaml` の `base_url` も合わせて変更

### memory pressure (Mac で `Estimated memory > unified memory`)

- 全 layer GPU offload を維持する場合は別 quant (Q3_K_M / IQ3_XS) を選ぶ
- Q4_K_M を維持したい場合は **GPU Offload: max → 部分 offload** に変更 (`-1` で残り layer を CPU に)

### 別ポートで起動したい

- LM Studio Developer タブで **Port** を変更、`providers.yaml` の `base_url` も合わせる

---

## 関連ドキュメント

- [llamacpp-direct.md](./llamacpp-direct.md) — llama.cpp 直叩き経路 (CLI build + Unsloth GGUF)、Qwen3.6 35B-A3B 検証済
- [troubleshooting.md §4-2](./troubleshooting.md) — Ollama / llama.cpp / LM Studio の既知問題
- [hf-ollama-models.md](./hf-ollama-models.md) — Ollama 経由 HF モデル取得
- [examples/providers.yaml](../examples/providers.yaml) — `lmstudio-*` provider 例同梱 + `test-lmstudio-openai` / `test-lmstudio-anthropic` profile
- 実機検証の経緯: [v1.8.4 note](./articles/v1-saga/note-4-v1-8-4-lmstudio-revival.md) — LM Studio 0.4.12 リリースで詰みが翌日解消した話

---

## llamacpp-direct.md との使い分け

| 観点 | LM Studio (本ドキュメント) | llama.cpp 直叩き (`llamacpp-direct.md`) |
|---|---|---|
| インストール | GUI installer | git clone + cmake build |
| モデル取得 | GUI Discover タブ | `huggingface-cli download` |
| モデル切替 | GUI で Load / Eject | プロセス再起動 |
| `qwen35` (Qwen3.5 Dense) | ✅ 0.4.12+ で公式対応 | ⚠️ 本体未対応のことがある |
| `qwen35moe` (Qwen3.6 MoE) | ✅ 0.4.12+ で公式対応 | ✅ 対応済 |
| Anthropic 互換 `/v1/messages` | ✅ 0.4.12+ で公式対応 | ❌ 未提供 (CodeRouter adapter で OpenAI→Anthropic 変換が要る) |
| Anthropic prompt caching | ✅ `cache_read_input_tokens` 観測 | ❌ openai_compat なので不可 |
| MoE active params 表示 | GUI で詳細 | server log で確認 |
| 推奨用途 | GUI 操作派 / Anthropic 互換が欲しい場合 | CLI 派 / build を制御したい場合 |

両経路は排他的でなく、**併設可能**。`providers.yaml` に `lmstudio-*` と `llamacpp-*` の両方を載せて、profile chain で fallback 順を指定できます。
