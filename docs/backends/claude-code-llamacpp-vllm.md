# Claude Code × ローカル LLM(llama.cpp / vLLM)接続ガイド — Ollama なしの設定とトラブルシュート

> English: [`claude-code-llamacpp-vllm.en.md`](./claude-code-llamacpp-vllm.en.md)

Ollama を使わず、**ダウンロード済みの `.gguf` を llama.cpp で**、または **vLLM で** 動かし、CodeRouter 経由で Claude Code に繋ぐための実践ガイドです。
構成の作り方と、実際に踏みやすいエラー（400 / 接続拒否 / 502 / コネクタ無効化 / `n_ctx=4096` のまま）の直し方をまとめています。

> 関連: [Launcher ガイド](./launcher.md) / [バックエンド インストール手順書](./install-backends.md) / [examples/README](../../examples/README.md)

---

## 全体構成

```
Claude Code ──(ANTHROPIC_BASE_URL)──► CodeRouter (:8088) ──► ① llama.cpp (:8080)
                                                          ├─► ② vLLM (:8000)
                                                          └─► ③ 無料クラウド (fallback)
```

CodeRouter が求めるのは「OpenAI 互換 API を話すバックエンド」だけです。Ollama はその選択肢の 1 つにすぎず、llama.cpp / vLLM でも等価に使えます。

---

## セットアップ手順

### 1. バックエンドを起動する（Ollama なし）

**llama.cpp（ダウンロード済み GGUF を使う）**

```bash
brew install llama.cpp           # macOS / Linux（Windows: winget install ggml.llamacpp）

llama-server -m ~/models/<model>.gguf --host 127.0.0.1 --port 8080 \
  -ngl 99 --ctx-size 32768
# 起動ログの「n_ctx = 32768」を必ず確認する
```

**vLLM（NVIDIA GPU）**

```bash
uv venv ~/.coderouter-t/backends/vllm
~/.coderouter-t/backends/vllm/bin/python -m pip install vllm
~/.coderouter-t/backends/vllm/bin/python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-7B-Instruct --port 8000 --max-model-len 32768
```

> 起動を GUI に任せたい場合は [Launcher](./launcher.md)。ただし Launcher が面倒を見るのは**起動まで**で、下記の provider 登録は別作業です。

### 2. CodeRouter に provider として登録する（`~/.coderouter-t/providers.yaml`）

```yaml
allow_paid: false
default_profile: default

providers:
  - name: llama-cpp-local
    kind: openai_compat
    base_url: http://localhost:8080/v1     # ← 起動したポートと一致させる
    model: ""                              # llama-server は model 名を問わない
    timeout_s: 120

  - name: vllm-local
    kind: openai_compat
    base_url: http://localhost:8000/v1     # ← vLLM の既定は 8000
    model: Qwen/Qwen2.5-Coder-7B-Instruct
    timeout_s: 120

  - name: openrouter-free                  # 逃げ先（任意だが推奨）
    kind: openai_compat
    base_url: https://openrouter.ai/api/v1
    model: qwen/qwen3-coder:free
    api_key_env: OPENROUTER_API_KEY

profiles:
  - name: default
    providers: [llama-cpp-local, vllm-local, openrouter-free]
```

`examples/providers.llamacpp-vllm.yaml` をコピーして始めると速いです。

### 3. CodeRouter を起動して Claude Code を繋ぐ

```bash
# ターミナル1
coderouter-t serve --port 8088

# ターミナル2（このシェルだけに env を効かせる。グローバルに置かない）
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

`ANTHROPIC_AUTH_TOKEN` はダミーで可（CodeRouter は検証しない）。実 API キーは `providers.yaml` 側で管理します。

---

## 設定の 4 つの要点

### ① ポートを一致させる

`base_url` のポートは起動した推論サーバーと一字一句一致させること。ズレると `transport error: All connection attempts failed`（接続拒否）になります。

| バックエンド | 既定ポート | 確認 |
|---|---|---|
| llama.cpp | `8080` | `curl localhost:8080/health` |
| vLLM | `8000` | `curl localhost:8000/v1/models` |
| Ollama | `11434` | `curl localhost:11434/api/version` |

### ② context size を Claude Code 用に上げる

Claude Code は毎ターン **15〜35K tokens** を送ります。`--ctx-size` / `--max-model-len` が小さいと
`exceed_context_size_error (400)` になります。**32768 以上**を既定に。
モデルの学習時 context を超える場合は YaRN 等の rope 拡張を使います。

```bash
llama-server -m <model>.gguf --port 8080 -ngl 99 \
  --ctx-size 65536 --rope-scaling yarn --yarn-orig-ctx 32768
```

大きい ctx で OOM する場合は、ctx を下げる / KV 量子化（`--cache-type-k q8_0 --cache-type-v q8_0`）/ 小さいモデルへ。

### ③ チェーン末尾に「効くフォールバック」を置く

`providers:` がローカルだけだと、落ちた瞬間に逃げ先が無く **502** になります。末尾に無料クラウドを 1 本足すと、ローカルが落ちても・context が載らなくても Claude Code が止まりません。有料は `ALLOW_PAID=true` を立てない限り呼ばれません（既定 `allow_paid: false`）。

### ④ 編集の反映は 3 ステップ

動いているプロセスには即時反映されません。

1. 設定を **`~/.coderouter-t/providers.yaml`** に置く（`examples/` を直すだけでは効かない）
2. **`coderouter-t serve` を再起動**（`launcher:` / プロファイルは起動時に読まれる）
3. **バックエンドを起動し直す**（`--ctx-size` 等は再起動して初めて効く）

---

## トラブルシュート早見表

| 症状 / ログ | 原因 | 対処 |
|---|---|---|
| `400 exceed_context_size_error` `n_ctx:4096` | ctx-size が小さく Claude Code の prompt が入らない | `--ctx-size 32768` 以上で**起動し直す**。学習 ctx 超は YaRN |
| `transport error: All connection attempts failed` (status: null) | バックエンドが落ちている / **ポート不一致** | サーバを起動 + `base_url` のポートを実ポートに合わせる |
| すべて `provider-failed` → **502 Bad Gateway** | チェーンの逃げ先が全滅 | バックエンドを 1 つ復旧 + 末尾に無料クラウド fallback |
| 起動し直しても `n_ctx=4096` のまま | 旧プロセスが残存 / 設定が `~/.coderouter-t` に未反映 / serve 未再起動 | `ps aux \| grep llama-server` で実コマンド確認 → 反映3ステップ |
| OOM でバックエンドが即落ち | ctx を上げすぎ | ctx を下げる / KV 量子化 / 小さいモデル |
| `capability-degraded: cache_control` | OpenAI 形式へ翻訳時に Anthropic prompt-cache マーカーを落とすだけ | **無害**。対処不要 |
| `claude.ai connectors are disabled …` | env の `ANTHROPIC_AUTH_TOKEN`/`API_KEY` が claude.ai ログインより優先 | CodeRouter 用 env はそのシェル/フォルダ限定に（`direnv`）。普段使いシェルでは `unset` |
| `/mcp` に `✘ failed` が残る | 不要な MCP サーバ登録 | `claude mcp remove <name> -s user` |

### 切り分けコマンド

```bash
# バックエンドが生きているか
curl -s localhost:8080/health ; echo            # llama.cpp
curl -s localhost:8000/v1/models ; echo         # vLLM
ps aux | grep -E '[l]lama-server|[v]llm'

# CodeRouter 側の状態
open http://localhost:8088/dashboard            # 誰が落ちているか色で分かる
coderouter doctor --check-model llama-cpp-local # provider を 7 プローブで診断
```

---

## 3 つのバックエンド早見

| | 何が要るか | モデル形式 | 起動の単位 |
|---|---|---|---|
| Ollama | `ollama pull` | Ollama 管理 | デーモン 1 つが on-demand |
| **llama.cpp** | `.gguf` + `llama-server` | `.gguf`（手元のファイル） | **1 起動 = 1 モデル = 1 ポート** |
| **vLLM** | NVIDIA GPU + venv | HF ID / ローカルパス | 1 起動 = 1 モデル = 1 ポート |

llama.cpp / vLLM は「起動したモデルだけ」が使えます（Ollama のように 1 つの口で複数モデルを出し分けることはしません）。複数使うならポートを分けてそれぞれ provider 登録します。

---

最終更新: 2026-06-24
