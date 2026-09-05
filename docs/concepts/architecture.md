# CodeRouter アーキテクチャ詳細

English: [`architecture.en.md`](./architecture.en.md)

> **README に概要があります → [README.md](../../README.md)**
> 本ページは内部構造、設定リファレンス、機能の仕組みを図入りで解説します。

最終更新: v2.5.0 (2026-05-22)

> **注記(2026-08-10 追記)**: 本図・本文は v2.5.0 時点のコア構造を対象としており、以降の変更に合わせた全面書き換えは行っていません。v2.5.0 以降に加わった主な層・機能は以下の通りです。
> - Language Tax の計測/ルーティング/可視化 (v2.6.0) → [`docs/guides/language-tax.md`](../guides/language-tax.md)
> - Token-savings accounting (v2.6.1)
> - agent_cli(外部コーディングエージェント CLI 連携)は v2.9.0 で in-core 実装から外部プラグイン `coderouter-plugin-agents` へ移設され、**Core からは削除**されました → [`docs/backends/external-agents.md`](../backends/external-agents.md)
> - Launcher の model swap (v2.9.1) と backend variants (v2.11.0) → [`docs/backends/launcher.md`](../backends/launcher.md)
> - context-budget ガードのトークン推定修正 (v2.12.0)
> - 認証情報衛生: `credential.source: cli_session` / ログの秘密スクラブ / `CODEROUTER_METRICS_TOKEN` (v2.14.0) → [`docs/guides/security.md`](../guides/security.md)
>
> 最新の全体像は [`CHANGELOG.md`](../../CHANGELOG.md) を参照してください。

---

## 全体像 — 3 層フォールバック + 6 系統障害ガード

```
┌──────────────────────────────────────────────────────────────┐
│  Claude Code / gemini-cli / codex / OpenAI SDK / curl        │
│  (Anthropic wire /v1/messages  or  OpenAI wire /v1/chat/…)   │
└──────────────────┬───────────────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────────────┐
│                      CodeRouter                              │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ Wire 翻訳   │  │ 6 系統 Guard │  │ Diagnostic Layer   │  │
│  │             │  │              │  │                    │  │
│  │ Anthropic   │  │ L1 Context   │  │ doctor 7-probe     │  │
│  │   ↕         │  │ L2 Memory    │  │ continuous probe   │  │
│  │ OpenAI      │  │ L3 Tool loop │  │ audit log          │  │
│  │             │  │ L4 Drift     │  │ request journal    │  │
│  │ tool-call   │  │ L5 Health    │  │ replay A/B         │  │
│  │ 修復        │  │ L6 Mid-strm  │  │ /dashboard         │  │
│  │             │  └──────────────┘  │ /launcher          │  │
│  └─────────────┘                    └────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │          Fallback Engine (profile → chain)               ││
│  │  ① Local (Ollama/llama.cpp/LM Studio) ─ 無料・最優先    ││
│  │  ② Free Cloud (OpenRouter free / NVIDIA NIM) ─ 無料枠   ││
│  │  ③ Paid Cloud (Claude / GPT) ─ ALLOW_PAID=true のみ     ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │          Persistent Layer (v2.0-K)                       ││
│  │  StateStore (sqlite3) │ AuditLog (JSONL) │ RequestLog    ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
  ┌────────┐  ┌─────────┐  ┌──────────┐
  │ Ollama │  │OpenRouter│  │ Claude   │
  │llama.cp│  │NVIDIA NIM│  │ GPT      │
  │LMStudio│  │(free)    │  │(有料)    │
  └────────┘  └─────────┘  └──────────┘
```

---

## 6 系統障害とその対処

長時間 (8h+) の agent session で発生する障害を体系化し、各層で対処:

| 障害 | 症状 | CodeRouter の対処 | 導入バージョン |
|---|---|---|---|
| **L1 Context overflow** | messages が context window に漸近 → backend 400 | warn (80%) → auto trim (90%)、tool_use/tool_result ペア atomic 保全 | v2.0.0 |
| **L2 Memory pressure** | Ollama/LM Studio が VRAM 不足で OOM | エラー本文の OOM 文字列を検知 (warn / skip、default warn)。`skip` 時は該当 provider を cooldown (既定 120s) で除外 → チェーンの次の provider へフォールスルー | v1.10.0 |
| **L3 Tool loop** | 同じ tool args を繰り返す stuck loop | 重複検知 → warn / inject / break 3 段階 | v1.10.0 |
| **L4 Drift** | KV cache 汚染で応答品質劣化 (空応答/短縮/tool silence) | 6 シグナル rolling window → warn / promote / reload (default off) | v2.1.0 |
| **L5 Health** | backend crash / 連続失敗 | 状態機械 (HEALTHY→DEGRADED→UNHEALTHY) + self-healing (自動除外 + restart + 回復 probe) | v1.10.0 + v2.2.0 |
| **L6 Mid-stream** | streaming 途中で backend が落ちる | 蓄積テキスト返却 (partial stitching) + clean error event | v2.1.0 |

---

## `kind: openai_compat` と `kind: anthropic` の選び方

`providers.yaml` の各プロバイダに `kind` があります。どちらを選ぶかでワイアレベル機能の生存範囲が変わります:

| 観点 | `kind: openai_compat` | `kind: anthropic` |
|---|---|---|
| `/v1/chat/completions` から到達 | ✅ 変換不要 | ✅ 逆変換経由 |
| `/v1/messages` から到達 | ✅ 変換 + tool-call 修復経由 | ✅ ネイティブパススルー |
| 対象 | Ollama, llama.cpp, OpenRouter, LM Studio, Groq, ... | `api.anthropic.com`, Bedrock Anthropic シム |
| `cache_control` / `thinking` | ❌ ロスト（OpenAI に等価物なし） | ✅ end-to-end 保持 |
| tool-call 修復 | ✅ 壊れた JSON を吐くローカルモデル向け | n/a |

**判断の目安:**

- **ローカルモデル / OpenRouter 無料枠** → `kind: openai_compat`
- **公式 Claude API + `cache_control` / `thinking`** → `kind: anthropic`
- **混在チェーン** (ローカル先頭 + Claude 最終砦) → 同プロファイルに両 kind を並べる

---

## プロファイルとルーティング

### 基本構造

```yaml
# providers.yaml
default_profile: claude-code

profiles:
  - name: claude-code
    providers:
      - ollama-qwen-coder-7b         # ① ローカル (最速)
      - ollama-qwen-coder-14b        # ② 品質フォールバック
      - openrouter-free              # ③ 無料クラウド
      - openrouter-claude            # ④ 有料 (ALLOW_PAID=true 時のみ)

providers:
  - name: ollama-qwen-coder-7b
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen3-coder:7b
```

### プロファイル単位のパラメータ上書き

同じプロバイダ一覧を別プロファイルで違った挙動に:

```yaml
profiles:
  - name: claude-code-long
    timeout_s: 600             # このプロファイルでは timeout を拡大
    append_system_prompt: ""   # プロバイダ指示を明示クリア
    providers:
      - ollama-qwen-coder-14b
      - openrouter-free
```

### Mode エイリアス

クライアントが**意図**で指定:

```yaml
mode_aliases:
  coding: claude-code
  long:   claude-code-long
  fast:   ollama-only
```

```bash
curl http://localhost:8088/v1/chat/completions \
  -H 'X-CodeRouter-Mode: coding' \
  -d '{"messages": [{"role":"user","content":"hi"}]}'
```

**優先度** (先着勝ち): body `profile` > `X-CodeRouter-Profile` > `X-CodeRouter-Mode` > `default_profile`

---

## Doctor — プロバイダ診断

```bash
coderouter doctor --check-model ollama-qwen-coder-14b
```

指定プロバイダに対し 7 プローブ (各 ≤100 トークン) を走らせ、宣言と実挙動の食い違いをコピペ可能な YAML パッチで出力:

```
provider: ollama-qwen-coder-14b  (kind=openai_compat, model=qwen2.5-coder:14b)

probe                     verdict        detail
auth+basic-chat           OK             200 in 1.4s, 18 tokens in / 6 tokens out
tool_calls                NEEDS_TUNING   model emitted a tool_use block but registry says tools=false
thinking                  N/A            kind=openai_compat; thinking probe is anthropic-only
reasoning-leak            OK             no stray `reasoning` field on choice.message

suggested patch for ~/.coderouter-t/providers.yaml:
  providers:
    - name: ollama-qwen-coder-14b
      capabilities:
        tools: true
```

### 7 プローブの役割

| プローブ | 何を検査するか |
|---|---|
| **auth+basic-chat** | 到達性・認証・基本応答。失敗時は残り全 SKIP |
| **num_ctx** | 宣言コンテキスト長と実挙動の食い違い (大入力時の暗黙切り詰め検知) |
| **tool_calls** | ダミーツールでの tool_use 生成能力 |
| **thinking** | Anthropic `thinking` ブロック受付 (anthropic kind のみ) |
| **reasoning-leak** | strip 前の生ボディに `reasoning` フィールドが漏れていないか |
| **streaming** | SSE ストリーミング応答の整合性 |
| **cache** | Anthropic prompt cache の read/creation 動作 |

### 終了コード (CI 投入想定)

| コード | 意味 |
|---|---|
| `0` | 全プローブ一致、パッチ不要 |
| `2` | `NEEDS_TUNING` あり、YAML パッチは出力内 |
| `1` | プローブ実行不能 (auth 失敗/到達不能) |

`--apply` で YAML パッチを非破壊書き戻し (ruamel.yaml round-trip、コメント・key 順序保持)。`--dry-run` で unified diff プレビュー。

---

## モデルケイパビリティレジストリ

`coderouter/data/model-capabilities.yaml` にパッケージ同梱。ユーザー上書きは `~/.coderouter-t/model-capabilities.yaml`:

```yaml
version: 1
rules:
  - match: "qwen3-coder:*"
    kind: openai_compat
    capabilities:
      tools: true
      max_context_tokens: 32768
```

**優先度**: `providers.yaml` capabilities > ユーザー YAML > 同梱 YAML > 未設定 (False)

---

## v2.2.0 新機能: Self-healing + 永続化 + Replay

### Self-healing routing (v2.0-J)

UNHEALTHY provider を自動除外し、restart helper + 回復 probe で自動復帰:

```
   HEALTHY ──(連続失敗)──→ UNHEALTHY ──(除外)──→ EXCLUDED
                                                      │
                              ┌─(restart command)─────┤
                              │                       │
                              │  ┌─(回復 probe)───────┤
                              │  │ 30s → 60s → 120s → 300s (指数 backoff)
                              │  │                    │
                              │  └─(probe 成功)───→ RESTORED (元の位置に復帰)
                              │
                              └─(restart 成功 + probe 成功)───→ RESTORED
```

```yaml
profiles:
  - name: self-healing
    backend_health_action: exclude    # UNHEALTHY → 除外 + 自己修復
    backend_health_threshold: 3       # 連続失敗回数

providers:
  - name: ollama-qwen3
    restart_command: "ollama serve"   # 自動再起動コマンド
```

### 永続化レイヤ (v2.0-K)

```yaml
state_dir: "~/.coderouter-t/state/"    # sqlite3 KV store + JSONL logs
audit_log: active                     # 22 種イベントを JSONL 記録
request_log: active                   # per-request metadata journal
```

- **StateStore**: sqlite3 KV (WAL mode) で budget/health/self-healing 状態を再起動越しに保持
- **Audit log**: guard 発火・chain fallback・self-healing 等を JSONL 記録。`coderouter audit --tail 20`
- **Request journal**: cache-observed イベントの metadata (provider, tokens, cost) を JSONL 記録。body 非記録 = privacy safe

### Replay — 統計 A/B 比較

provider 切替の効果を数値で確認:

```bash
coderouter replay --compare anthropic-api openrouter-free --since 2026-05-01
```

```
Metric                    anthropic-api        openrouter-free           Delta
─────────────────────────────────────────────────────────────────────────────────
Requests                  150                  89                          -61
Avg input tokens          1234                 1180                        -54
Avg cost (USD)            $0.0082              $0.0000                 -0.0082
Total cost (USD)          $1.2300              $0.0000                 -1.2300
Cache hit ratio           42.3%                0.0%                     -42.3
Streaming ratio           85.0%                100.0%                   +15.0

Per-request: openrouter-free is 100.0% cheaper than anthropic-api
```

---

## Claude Code と一緒に使う

```bash
# ターミナル 1: CodeRouter 起動
coderouter-t serve --port 8088

# ターミナル 2: Claude Code を向ける
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude
```

### 体感の目安

- **初バイトレイテンシ** ≒ 上流のトータル応答時間 (CodeRouter のオーバーヘッドは微小)
- **M 系 macOS**: 7b で ~30-60s/ターン、14b で ~2 分 (主因は Claude Code の 15-20K トークン system prompt)
- **ツール選択の品質** はモデル側の限界 — CodeRouter はワイア修復のみ
- **ミッドストリーム失敗** は clean `event: error` 1 本で通知

---

## 依存ポリシー

ランタイム依存 5 個のみ:

| パッケージ | 目的 |
|---|---|
| `fastapi` | HTTP ingress |
| `uvicorn` | ASGI サーバー |
| `httpx` | アウトバウンド HTTP |
| `pydantic` | スキーマ検証 |
| `pyyaml` | 設定パース |

`litellm` なし、`langchain` なし、`openai`/`anthropic` SDK なし。

---

## プログラムから例外をキャッチする

```python
from coderouter import CodeRouterError

try:
    response = await engine.generate(chat_request)
except CodeRouterError as exc:
    # AdapterError / NoProvidersAvailableError / MidStreamError 全て
    logger.error("coderouter-failed", extra={"reason": str(exc)})
```

---

## Launcher — llama.cpp / vllm プロセス管理 (v2.5.0)

`/launcher` で開くブラウザ UI。llama.cpp や vllm をコマンドラインなしで起動・管理する。

### 構成

```
coderouter/ingress/launcher_routes.py
  ├── LauncherRegistry  (app.state.launcher)
  │     └── ManagedProcess  × N プロセス
  ├── API  /api/launcher/*
  └── UI   GET /launcher  (HTML + inline JS)
```

### プロセスライフサイクル

```
POST /api/launcher/start
  → asyncio.create_subprocess_exec (llama-server / python -m vllm…)
  → _tail_logs() バックグラウンドタスク (stdout+stderr → deque[200])
  → ManagedProcess.status = "running"
        │
        ├─ POST /api/launcher/stop/{id}
        │      → SIGTERM → (5s) → SIGKILL
        │      → status = "stopped"
        │
        └─ プロセス自然終了
               → status = "stopped" or "error"  (returncode に応じて)

DELETE /api/launcher/processes/{id}  ← stopped のみ削除可
```

### YAML 設定リファレンス

```yaml
launcher:
  # スキャン対象ディレクトリ (.gguf / .safetensors / .bin / .pt / .ggml を再帰検索)
  model_dirs:
    - ~/models
    - /data/gguf

  # バックエンド別オプションプリセット
  # キー名 = バックエンド名 ("llama.cpp" / "vllm")
  option_profiles:
    llama.cpp:
      - name: "GPU フル活用"
        args:
          "-ngl": 99           # int → "--flag value" として渡す
          "--ctx-size": 4096
          "--no-mmap": false   # bool false → フラグ省略
          "--mlock": true      # bool true  → "--mlock" のみ (値なし)
    vllm:
      - name: "標準"
        args:
          "--dtype": "auto"
          "--max-model-len": 4096
```

`args` の型変換ルール:

| YAML 値 | CLI 出力 |
|---|---|
| `"-ngl": 99` | `-ngl 99` |
| `"--mlock": true` | `--mlock` (値なし) |
| `"--no-mmap": false` | 省略 |
| `"--dtype": "auto"` | `--dtype auto` |

### 追加オプション（自由入力）

UI に「追加オプション」テキスト欄が常時表示。プロファイルに定義されていないフラグをその場で指定できる。`shlex.split()` でパースされるため、スペース入りパスはクォートで囲む。`-m` / `--model` によるモデルの再指定は 400 で拒否される (モデルは「モデルパス」欄でのみ指定)。

### プロセスレジストリの永続化

意図的に **非永続**。CodeRouter 再起動時にプロセスレジストリは空になる (GPU メモリ確保の多重起動防止)。実行中の llama-server / vllm プロセス自体は OS 上で継続するが、Launcher UI からは見えなくなる。

詳細 → [Launcher ガイド](../backends/launcher.md)
