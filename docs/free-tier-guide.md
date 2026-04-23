# 無料枠ガイド — NVIDIA NIM × OpenRouter Free でコストゼロ運用

> このドキュメントは「CodeRouter をクレカ登録なしで、できるだけ長く無料で回す」ための実運用ガイドです。
> [`README.md`](../README.md) が**何か**を、[`docs/usage-guide.md`](./usage-guide.md) が**どう使うか**を説明するのに対し、本書は**どの無料枠をどの順で並べ、どこでハマるか**にだけ絞って書いています。

English version: [`docs/free-tier-guide.en.md`](./free-tier-guide.en.md)

---

## 目次

1. [3 つの無料枠を比較する](#1-3-つの無料枠を比較する)
2. [推奨 fallback チェーン — `claude-code-nim` プロファイル](#2-推奨-fallback-チェーン--claude-code-nim-プロファイル)
3. [セットアップ手順（3 コマンド）](#3-セットアップ手順3-コマンド)
4. [live 検証済みの NIM モデル一覧](#4-live-検証済みの-nim-モデル一覧)
5. [OpenRouter 無料枠の現在のロスター](#5-openrouter-無料枠の現在のロスター)
6. [よくあるハマり所 5 点](#6-よくあるハマり所-5-点)
7. [`coderouter doctor` で健康診断](#7-coderouter-doctor-で健康診断)
8. [関連ドキュメント](#8-関連ドキュメント)

---

## 1. 3 つの無料枠を比較する

CodeRouter の fallback チェーンに並べられる「クレカ不要で使える」選択肢は 3 つあります。

| 層 | 具体例 | Rate limit | 課金モデル | tool calling | 備考 |
|---|---|---|---|---|---|
| **ローカル** | Ollama / llama.cpp (qwen2.5-coder:7B 等) | ハードウェア上限 | ゼロ | モデル依存（7B〜 が実用域） | 最速・最安・オフライン可 |
| **NVIDIA NIM 開発者枠** | `meta/llama-3.3-70b-instruct` 等 | **40 req/min** | 初期クレジット消費型 | ✓（Llama-3.3 / Qwen3-Coder-480B / Kimi-K2 で確認） | 70B〜480B クラスが無料で叩ける |
| **OpenRouter 無料枠** | `qwen/qwen3-coder:free` / `openai/gpt-oss-120b:free` 等 | ~20 req/min / ~200 req/day（モデル別） | 完全無料（クレジットすら使わない） | ✓（SKU 次第） | SKU が動く、429 が頻発 |

**運用上の使い分け**:

- ローカルが答えられる内容は **常にローカル**。速度・latency・コスト全てで勝つ。
- ローカルが落ちたら **まず NIM** に逃げる。40 req/min の余裕と 70B〜480B モデルの品質で、Claude Code の system prompt（15-20K token）を生で流しても破綻しない。
- NIM が 429 orクレジット切れになったら **OpenRouter 無料枠** に逃げる。モデル品質は NIM より細いが、`:free` SKU は文字通り無料。
- それでも回答が必要なら、**`ALLOW_PAID=true`** で有料 API に逃げる（最終砦）。

3 つの無料枠を上から順に縦に積むのが、`examples/providers.nvidia-nim.yaml` の `claude-code-nim` プロファイルの並びです。

---

## 2. 推奨 fallback チェーン — `claude-code-nim` プロファイル

`examples/providers.nvidia-nim.yaml` を開くと、この 8 段 fallback チェーンが既に組まれています:

```yaml
profiles:
  - name: claude-code-nim
    providers:
      - ollama-qwen-coder-7b       # 1. ローカル（速度）
      - ollama-qwen-coder-14b      # 2. ローカル（品質）
      - nim-llama-3.3-70b          # 3. NIM 無料（第一選択、tool calling OK）
      - nim-qwen3-coder-480b       # 4. NIM 無料（480B MoE、agentic coding 特化）
      - nim-kimi-k2                # 5. NIM 無料（tool calling 強度）
      - openrouter-free            # 6. OpenRouter 無料（qwen3-coder:free）
      - openrouter-gpt-oss-free    # 7. OpenRouter 無料（別ベンダーで 429 回避）
      - openrouter-claude          # 8. 有料 Claude（ALLOW_PAID=true 時のみ）
```

設計意図:

- **ローカルを 2 段** — 7B で latency、14B で品質。小さい編集の 95% が 1-2 段目で終わる。
- **NIM を 3 段並べる** — Meta / Qwen / Moonshot と**違うモデル族**を並べることで、1 つの SKU が deprecation や 429 で死んでも NIM レーン全体は止まらない。
- **OpenRouter 無料を 2 段** — 同じく別ベンダー（Qwen + OpenAI）で並べて `:free` レーンの可用性を上げる。
- **有料は最後** — `ALLOW_PAID=false`（既定）では枝ごと skip される。

ローカルを持たない環境なら `nim-first` プロファイルで Ollama 2 段をスキップ:

```yaml
  - name: nim-first
    providers:
      - nim-llama-3.3-70b
      - nim-qwen3-coder-480b
      - nim-kimi-k2
      - openrouter-free
      - openrouter-gpt-oss-free
      - openrouter-claude
```

---

## 3. セットアップ手順（3 コマンド）

### 3.1 無料 API キーを取る

**NVIDIA NIM**: [build.nvidia.com](https://build.nvidia.com) にサインアップ（クレカ不要） → **API Key** タブで `nvapi-...` を発行。

**OpenRouter**: [openrouter.ai/keys](https://openrouter.ai/keys) で `sk-or-v1-...` を発行。無料 SKU だけ使うならチャージ不要。

### 3.2 `.env` に入れる

```bash
# リポジトリルートに .env（`.gitignore` 済み）
NVIDIA_NIM_API_KEY=nvapi-...
OPENROUTER_API_KEY=sk-or-v1-...
ALLOW_PAID=false            # 既定。有料 API を使うときだけ true に
```

### 3.3 サンプル config をコピーして起動

```bash
cp examples/providers.nvidia-nim.yaml ~/.coderouter/providers.yaml
coderouter start --profile claude-code-nim
```

これで `http://localhost:8088/v1/messages`（Claude Code ingress）と `http://localhost:8088/v1/chat/completions`（OpenAI ingress）が待ち受けます。
Claude Code なら:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 claude
```

---

## 4. live 検証済みの NIM モデル一覧

2026-04-23 時点で `integrate.api.nvidia.com/v1` に対して chat + tool-call 両プローブで実地確認した結果。
`examples/providers.nvidia-nim.yaml` はこの結果を反映して採用可否を決めています。

### 4.1 採用（`tools: true` で使える）

| モデル ID | chat (pong) | tool_calls | YAML 上の名前 |
|---|---|---|---|
| `meta/llama-3.3-70b-instruct` | 540 ms | ✅ | `nim-llama-3.3-70b` |
| `qwen/qwen3-coder-480b-a35b-instruct` | 634 ms | ✅ | `nim-qwen3-coder-480b` |
| `moonshotai/kimi-k2-instruct` | 2,838 ms | ✅ | `nim-kimi-k2` |

### 4.2 採用（`tools: false`、chat 専用）

| モデル ID | chat | tool_calls | 備考 |
|---|---|---|---|
| `qwen/qwen2.5-coder-32b-instruct` | 160 ms | **HTTP 400** | ツール付き要求は NIM 側で明示的に reject (`"Tool use has not been enabled"`)。YAML で `tools: false` を宣言、capability gate が tool-laden 要求を回避する |

### 4.3 不採用（観測した症状と理由）

| モデル ID | 症状 | 対処 |
|---|---|---|
| `nvidia/llama-3.1-nemotron-70b-instruct` | HTTP 404 | アカウントロスター外 |
| `deepseek-ai/deepseek-r1` | HTTP 410 `"reached its end of life on 2026-01-26"` | NIM 上では EOL、`deepseek-v3.2` 系を使う |
| `nvidia/llama-3.3-nemotron-super-49b-v1.5` | 200 OK だが `content: null` で `tool_calls` 無し | Claude Code 用途に向かない出力形状 |
| `deepseek-ai/deepseek-v3.2` | TIMEOUT @ 15s | コールドスタートが重い |
| `z-ai/glm4.7` | TIMEOUT @ 10s | 同上 |

> NIM のモデルロスターは数ヶ月単位でローテーションします。
> 現在のロスターは `GET https://integrate.api.nvidia.com/v1/models`（`Authorization: Bearer nvapi-...`）で取得できます（2026-04-23 時点で 133 モデル）。

### 4.4 `nim-reasoning` プロファイルの reasoning 系モデル

`moonshotai/kimi-k2-thinking` は応答を `content` ではなく `reasoning_content` に `<think>...</think>` で包んで返してきます。
YAML では `output_filters: [strip_thinking]` を併記してあり、`reasoning_content` が将来 `content` 側に漏れた場合も tag を剥がします（現時点では no-op）。
latency は ≈ 4s と重いので、reasoning trace が欲しいときだけ `--profile nim-reasoning` を選んでください。

---

## 5. OpenRouter 無料枠の現在のロスター

`examples/providers.nvidia-nim.yaml` と `examples/providers.yaml` の両方に入っている `:free` SKU:

| プロバイダ名 | モデル | 役割 |
|---|---|---|
| `openrouter-free` | `qwen/qwen3-coder:free` | 262K コンテキスト、agentic coding 特化、tool calling 対応 |
| `openrouter-gpt-oss-free` | `openai/gpt-oss-120b:free` | 117B MoE (5.1B active)、131K コンテキスト、qwen の 429 からのレート制限脱出 |

無料枠の SKU は 3 ヶ月単位でローテーションします（例: `deepseek/deepseek-r1:free` は 2026 Q1 に消えました）。
週次の差分が `docs/openrouter-roster/CHANGES.md` に記録されます（`scripts/openrouter_roster_diff.py` 経由）。
新しい `:free` SKU を採用するときは、チェーンの末尾ではなく**中間**に入れること — 既存の fallback 順が崩れないように。

---

## 6. よくあるハマり所 5 点

### 6.1 NIM の「無料」はクレジット消費型

クレカは不要ですが、リクエストごとにクレジットを消費します。480B MoE は 1 リクエストあたりのコストが重いので、雑に叩くと数日で初期クレジットが切れます。残量は [build.nvidia.com/account/usage](https://build.nvidia.com/account/usage) で確認。切れたら OpenRouter 無料枠に自動 fallback するので CodeRouter 側は壊れませんが、NIM レーンが死んでいることには気づく運用設計が必要です（`coderouter stats` / `/dashboard` の fallback event を見る）。

### 6.2 NIM の一部モデルは OpenAI 仕様外の `reasoning` フィールドを吐く

`nim-llama-3.3-70b` は、レスポンスの `message` オブジェクトに `reasoning` キーを勝手に付けて返してきます。
厳格な OpenAI クライアントは `unknown field: reasoning` で落ちますが、CodeRouter の v0.5-C パススルーフィルタが Adapter 境界で剥がすので下流には見えません。
生で API を叩く検証スクリプトを書く場合は自分で strip する必要があります。

### 6.3 NIM 上の Qwen2.5-Coder-32B はツール無効

`qwen/qwen2.5-coder-32b-instruct` はツール付きリクエストに対して明示的に HTTP 400 を返します（`"Tool use has not been enabled"`）。
**`tools: true` で宣言すると毎ターン 400**、CodeRouter は fallback しますが無駄なラウンドトリップが発生します。
YAML で `tools: false` を明示宣言するのが正解（`examples/providers.nvidia-nim.yaml` に実装済み）。

### 6.4 OpenRouter 無料枠は 1 モデル 200 req/day

Claude Code を Agent として雑に使うと、1 日 200 req は意外と早く使い切ります。
`claude-code-nim` プロファイルのように **NIM を先に置き OpenRouter を下流に置く**のが、OpenRouter の日次上限を守る最も簡単な方法です。

### 6.5 NIM モデル ID は case-sensitive かつ drift する

`meta/Llama-3.3-70B-Instruct` では 404 です（大文字）。
数ヶ月単位で slug が deprecation されます（今回 `deepseek-r1` が消えました）。
新しい NIM モデルを YAML に追加したら、必ず `coderouter doctor --check-model nim-<name>` で auth + tool_calls + reasoning-leak を実地プローブしてから本番投入してください。

---

## 7. `coderouter doctor` で健康診断

config ロード直後、あるいは新しい NIM モデルを追加した直後に回します:

```bash
coderouter doctor --check-model nim-llama-3.3-70b
```

6 プローブ（auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming）が回り、宣言と実挙動が食い違えば**コピペ可能な YAML パッチ**を出します。全 probe OK なら Exit 0 が返ります。

2026-04-23 時点の実出力例:

```text
provider:   nim-llama-3.3-70b
  kind:     openai_compat
  base_url: https://integrate.api.nvidia.com/v1
  model:    meta/llama-3.3-70b-instruct

Probes:
  [1/6] auth+basic-chat …… [OK]
      200 OK (in=44, out=3)
  [3/6] tool_calls …… [OK]
      native `tool_calls` observed; matches declaration.
  [5/6] reasoning-leak …… [OK]
      upstream emits non-standard `reasoning`; v0.5-C adapter strips it
      before it reaches the client (expected — expect `capability-degraded`
      log lines for this provider).
Summary: all probes match declarations.
Exit: 0
```

`[5/6] reasoning-leak` が意図的に `[OK]` なのは、YAML の declaration（`reasoning_passthrough: false`）と adapter の実挙動（strip する）が一致しているからです。
ログで `capability-degraded` 行が出ていても、この一致があれば想定内 — 無視してかまいません。

---

## 8. 関連ドキュメント

- [`docs/usage-guide.md`](./usage-guide.md) — 利用ガイド全般（ハードウェア別モデル選定、`doctor` / `verify` の使い方、OS 別起動フロー）
- [`docs/quickstart.md`](./quickstart.md) — Claude Code / codex を local Ollama で動かす最短手順
- [`examples/providers.nvidia-nim.yaml`](../examples/providers.nvidia-nim.yaml) — 本書で参照している YAML 本体。コメント内に観測済みハマり所を全て記載
- [`examples/providers.yaml`](../examples/providers.yaml) — NIM 無しの標準サンプル（local + OpenRouter free + 有料）
- [`docs/openrouter-roster/CHANGES.md`](./openrouter-roster/CHANGES.md) — OpenRouter `:free` SKU の週次差分（`scripts/openrouter_roster_diff.py` 出力）
- [`docs/articles/note-nvidia-nim.md`](./articles/note-nvidia-nim.md) — NIM 検証の経緯を書いた note 記事ドラフト
