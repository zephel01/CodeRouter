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
6. [よくあるハマり所 6 点](#6-よくあるハマり所-6-点)
7. [`coderouter doctor` で健康診断](#7-coderouter-doctor-で健康診断)
8. [関連ドキュメント](#8-関連ドキュメント)

---

## 1. 3 つの無料枠を比較する

CodeRouter の fallback チェーンに並べられる「クレカ不要で使える」選択肢は 3 つあります。

| 層 | 具体例 | Rate limit | 課金モデル | tool calling | 備考 |
|---|---|---|---|---|---|
| **ローカル** | Ollama / llama.cpp (qwen3.5:9B / gemma4:e4b 等) | ハードウェア上限 | ゼロ | モデル依存（7B〜 が実用域） | 最速・最安・オフライン可 |
| **NVIDIA NIM 開発者枠** | `qwen/qwen3-coder-480b-a35b-instruct` / `moonshotai/kimi-k2-instruct` 等 | **40 req/min** | 初期クレジット消費型 | ✓（Qwen3-Coder-480B / Kimi-K2 / Llama-3.3-70B で確認） | 70B〜480B クラスが無料で叩ける |
| **OpenRouter 無料枠** | `qwen/qwen3-coder:free` / `openai/gpt-oss-120b:free` 等 | ~20 req/min / ~200 req/day（モデル別） | 完全無料（クレジットすら使わない） | ✓（SKU 次第） | SKU が動く、429 が頻発 |

**運用上の使い分け**:

- ローカルが答えられる内容は **常にローカル**。速度・latency・コスト全てで勝つ。
- ローカルが落ちたら **まず NIM** に逃げる。40 req/min の余裕と 70B〜480B モデルの品質で、Claude Code の system prompt（15-20K token）を生で流しても破綻しない。
- NIM が 429 orクレジット切れになったら **OpenRouter 無料枠** に逃げる。モデル品質は NIM より細いが、`:free` SKU は文字通り無料。
- それでも回答が必要なら、**`ALLOW_PAID=true`** で有料 API に逃げる（最終砦）。

3 つの無料枠を上から順に縦に積むのが、`examples/providers.nvidia-nim.yaml` の `claude-code-nim` プロファイルの並びです。

---

## 2. 推奨 fallback チェーン — `claude-code-nim` プロファイル

`examples/providers.nvidia-nim.yaml` を開くと、この 8 段 fallback チェーンが既に組まれています（**v1.6.2 で実機検証を踏まえて NIM レーンの順序を入れ替え済み**):

```yaml
profiles:
  - name: claude-code-nim
    providers:
      - ollama-qwen-coder-7b       # 1. ローカル（速度）
      - ollama-qwen-coder-14b      # 2. ローカル（品質）
      - nim-qwen3-coder-480b       # 3. NIM 無料（第一選択、agentic coding 特化）
      - nim-kimi-k2                # 4. NIM 無料（第二候補、tool calling 強度）
      - nim-llama-3.3-70b          # 5. NIM 無料（退避線、Claude Code 単独利用は注意）
      - openrouter-free            # 6. OpenRouter 無料（qwen3-coder:free）
      - openrouter-gpt-oss-free    # 7. OpenRouter 無料（別ベンダーで 429 回避）
      - openrouter-claude          # 8. 有料 Claude（ALLOW_PAID=true 時のみ）
```

設計意図:

- **ローカルを 2 段** — 7B で latency、14B で品質。小さい編集の 95% が 1-2 段目で終わる。
- **NIM を 3 段並べる** — Qwen / Moonshot / Meta と**違うモデル族**を並べることで、1 つの SKU が deprecation や 429 で死んでも NIM レーン全体は止まらない。
- **Qwen3-Coder-480B を最前面** — agentic coding 専用に訓練されており、ツール選択が安定している。Llama-3.3-70B は速度では勝るが、Claude Code の system prompt に過剰反応してツール呼び出しを乱発する症状（[§6.6](#66-llama-3-3-70b-が-claude-code-で過剰なツール呼び出しを起こす)）が出るので退避線に下げています（v1.6.2 での実機検証反映）。
- **OpenRouter 無料を 2 段** — 同じく別ベンダー（Qwen + OpenAI）で並べて `:free` レーンの可用性を上げる。
- **有料は最後** — `ALLOW_PAID=false`（既定）では枝ごと skip される。

ローカルを持たない環境なら `nim-first` プロファイルで Ollama 2 段をスキップ:

```yaml
  - name: nim-first
    providers:
      - nim-qwen3-coder-480b
      - nim-kimi-k2
      - nim-llama-3.3-70b
      - openrouter-free
      - openrouter-gpt-oss-free
      - openrouter-claude
```

---

## 3. セットアップ手順（3 コマンド）

### 3.1 無料 API キーを取る

**NVIDIA NIM**: [build.nvidia.com](https://build.nvidia.com) にサインアップ（クレカ不要） → **API Key** タブで `nvapi-...` を発行。

**OpenRouter**: [openrouter.ai/keys](https://openrouter.ai/keys) で `sk-or-v1-...` を発行。無料 SKU だけ使うならチャージ不要。

### 3.2 `.env` に入れる（**`export` 必須** — v1.6.2 以降の規約）

```bash
# リポジトリルートに .env（`.gitignore` 済み）
# `source .env` で子プロセスに継承させるため、各キーに必ず `export` を付ける。
export NVIDIA_NIM_API_KEY=nvapi-...
export OPENROUTER_API_KEY=sk-or-v1-...
export ALLOW_PAID=false            # 既定。有料 API を使うときだけ true に
```

`export` を付け忘れると、上流から `Header of type 'authorization' was missing` で 401 が返って一見原因不明のフォールバックループになります。**v1.6.3 で `coderouter doctor --check-env .env` を入れたので、起動前にこれで検査するのが推奨**です（[`docs/troubleshooting.md` §1-2 / §5](./troubleshooting.md#1-2-env-には-export-が必須)）。

> **1Password などの secret manager を使う場合**: `.env` をディスクに置かずに `op run --env-file=.env.tpl -- coderouter serve ...` で env として inject する構成が組めます。レシピは [`docs/troubleshooting.md` §5-3](./troubleshooting.md#5-3-1password-cli-と連携する-推奨) 参照。

### 3.3 サンプル config をコピーして起動

```bash
cp examples/providers.nvidia-nim.yaml ~/.coderouter/providers.yaml
coderouter serve --mode claude-code-nim --port 8088
```

> サブコマンド名は `serve`（旧記載の `start` は誤り）、プロファイル指定は `--mode`（`--profile` ではない）、ポートは Claude Code 側の `ANTHROPIC_BASE_URL` に合わせて明示的に。これらは v1.6.2 の hygiene パスで整理しました。

これで `http://localhost:8088/v1/messages`（Claude Code ingress）と `http://localhost:8088/v1/chat/completions`（OpenAI ingress）が待ち受けます。
Claude Code なら:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

または 1Password 経由で:

```bash
op run --env-file=.env.tpl -- \
  coderouter serve --mode claude-code-nim --port 8088
```

---

## 4. live 検証済みの NIM モデル一覧

2026-04-23 時点で `integrate.api.nvidia.com/v1` に対して chat + tool-call 両プローブで実地確認した結果。
`examples/providers.nvidia-nim.yaml` はこの結果を反映して採用可否を決めています。

### 4.1 採用（`tools: true` で使える）

`claude-code-nim` プロファイルでの推奨優先順:

| 優先 | モデル ID | chat (pong) | tool_calls | YAML 上の名前 | Claude Code 適性 |
|---|---|---|---|---|---|
| ★ 第一 | `qwen/qwen3-coder-480b-a35b-instruct` | 634 ms | ✅ | `nim-qwen3-coder-480b` | **◎** agentic coding 専用設計 |
| 第二 | `moonshotai/kimi-k2-instruct` | 2,838 ms | ✅ | `nim-kimi-k2` | ○ tool 安定、latency やや重 |
| 退避線 | `meta/llama-3.3-70b-instruct` | 540 ms | ✅ | `nim-llama-3.3-70b` | △ [§6.6](#66-llama-3-3-70b-が-claude-code-で過剰なツール呼び出しを起こす) を参照 |

**Llama-3.3-70B は素の chat / tool_calls 単体では問題なく動くものの、Claude Code の system prompt 込みのリクエストでは「`こんにちは` を `Skill(hello)` として実行しようとする」のような過剰反応**が観測されました（v1.6.2 実機検証）。プロファイル順序は v1.6.2 で Qwen-first に変更済み。直接単発で叩く API クライアントなら Llama でも十分使えます。

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
latency は ≈ 4s と重いので、reasoning trace が欲しいときだけ `--mode nim-reasoning` を選んでください。

### 4.5 検証候補モデル（NIM ロスター上に存在、未採用）

NIM 上には他にも tool calling を試す価値のある SKU が並んでいます。**まだ実機 probe を通していない / 採用判定が取れていない**ので公式採用はしていませんが、自分の用途に合うか試したい場合は以下を `providers.yaml` に追加して `coderouter doctor --check-model <name>` で auth + tool_calls + reasoning-leak の 3 点を実地確認してから本番投入してください。

| モデル ID | 想定用途 | 注意点 |
|---|---|---|
| `qwen/qwen3-235b-a22b-instruct` | Qwen3 系の高品質候補（235B MoE / 22B active）。Qwen3-Coder-480B より小さくレスポンスが軽いはず | NIM ロスター上の存在は要確認。tool_calls 対応も要 probe |
| `mistralai/mixtral-8x22b-instruct-v0.1` | Mistral 系の MoE。Qwen / Llama / Moonshot に**別ベンダー**を 1 本足して NIM レーンの多様性を強化 | 古めの SKU、deprecation 候補。先に doctor で auth と tool_calls を確認 |
| `mistralai/codestral-latest` | コーディング特化の Mistral 系 | tool_calls 対応有無は要 probe |
| `nv-mistralai/mistral-nemo-12b-instruct` | NVIDIA + Mistral の小型協調モデル。latency は速い | tool_calls の安定性が未知数 |
| `nvidia/usdcode-llama3-70b-instruct` | NVIDIA 製のコード補完特化 Llama | Claude Code 用 tool calling のテストが必要 |
| `deepseek-ai/deepseek-v3.1` | v3.2 が timeout する一方で v3.1 が応答するなら採用候補 | コールドスタート長め、初回 timeout に注意 |

> 採用したら `examples/providers.nvidia-nim.yaml` の `providers:` セクションに追加し、`profiles.claude-code-nim` の Qwen と Kimi の間あたりに差し込むと、既存の挙動を壊さずに枝が増えます。**実証済みベンダーを 4 本以上揃える**と NIM レーンの可用性は飛躍的に上がります（同じベンダーのみ並べると同時障害のリスク）。

新しいモデルを採用するワークフロー：

```bash
# 1. providers.nvidia-nim.yaml に nim-mixtral-8x22b エントリを追加
# 2. doctor で 3 点プローブ
coderouter doctor --check-model nim-mixtral-8x22b
# 3. tool_calls が [OK] かつ reasoning-leak が [OK] なら採用
# 4. profile に挿入して serve 再起動
```

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

## 6. よくあるハマり所 6 点

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

### 6.6 Llama-3.3-70B が Claude Code で過剰なツール呼び出しを起こす

doctor で全 probe が緑、API 単発でも `tool_calls` が普通に返ってくる Llama-3.3-70B ですが、**Claude Code の system prompt（15-20K token / 多数の Skill / AskUserQuestion 宣言）込みで叩くと、無害な発話まで何かしらのツール呼び出しに変換しようとする症状**が観測されました。実際の挙動例：

```
❯ こんにちは
⏺ Skill(hello)
  ⎿  Initializing…
  ⎿  Error: Unknown skill: hello. Did you mean help?
```

または:

```
❯ こんにちは
[ AskUserQuestion: What is your name? ]
  1. John
  2. Jane
  3. Type something
  4. Chat about this
```

agentic tuning が強めなため、「ユーザー発話 → 必ず何かしらツールを噛ませるのが正解」とモデルが過学習している、というのが推定原因です（v1.6.2 retrospective）。

**対策**: `claude-code-nim` プロファイルでは Llama-3.3-70B を最後尾に配置して、Qwen3-Coder-480B（agentic coding 専用設計）と Kimi-K2 を先に並べる構成に変更しました。**API 単発用途や、単純な chat / 翻訳 / 要約には Llama-3.3-70B でも問題ない**ので、用途別にプロファイルを分けるのも一案です（例: `chat-nim` プロファイルを別途定義して Llama を頭にする）。

詳しくは [`docs/troubleshooting.md` §4-1](./troubleshooting.md#4-1-挨拶がツール呼び出しに化ける-llama-3-3-70b-系) で対処手順とログ例を解説しています。

---

## 7. `coderouter doctor` で健康診断

config ロード直後、あるいは新しい NIM モデルを追加した直後に回します:

```bash
# 第一選択を確認
coderouter doctor --check-model nim-qwen3-coder-480b

# NIM レーン全部を一気に
for p in nim-qwen3-coder-480b nim-kimi-k2 nim-llama-3.3-70b; do
  coderouter doctor --check-model "$p" || true
done
```

6 プローブ（auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming）が回り、宣言と実挙動が食い違えば**コピペ可能な YAML パッチ**を出します。全 probe OK なら Exit 0 が返ります。

2026-04-24 時点の `nim-qwen3-coder-480b` 実出力例（**1Password 経由で env を inject した状態**）:

```text
$ op run --env-file=.env.tpl -- coderouter doctor --check-model nim-qwen3-coder-480b
provider:   nim-qwen3-coder-480b
  kind:     openai_compat
  base_url: https://integrate.api.nvidia.com/v1
  model:    qwen/qwen3-coder-480b-a35b-instruct

Probes:
  [1/6] auth+basic-chat …… [OK]
      200 OK (in=17, out=3)
  [3/6] tool_calls …… [OK]
      native `tool_calls` observed; matches declaration.
  [5/6] reasoning-leak …… [OK]
      no `reasoning` field observed and no content-embedded markers — nothing to strip.
Summary: all probes match declarations.
Exit: 0
```

`nim-llama-3.3-70b` も同じ流れで OK が出ますが、出力に `reasoning-leak` が `[OK]` で「upstream emits non-standard `reasoning`; v0.5-C adapter strips it」と出ます。これは Llama-3.3-70B が OpenAI 仕様外の `reasoning` フィールドを返してくるが CodeRouter が剥がしている、という想定通りの挙動なので問題ありません（`capability-degraded` ログも同様）。

### 7.1 v1.6.3 で追加された `--check-env`

API キーそのものではなく、**`.env` ファイル自体のセキュリティ衛生**を見たい場合は v1.6.3 で入った `--check-env` を使います:

```bash
coderouter doctor --check-env .env
```

- `permissions` (POSIX 0600 か)
- `gitignore` (`.gitignore` でマッチしているか)
- `git-tracking` (既に追跡されていないか)

の 3 項目を一気に検査して、WARN / ERROR にはコピペ可能な fix を出します。詳細は [`docs/troubleshooting.md` §5](./troubleshooting.md#5-env-のセキュリティ運用-v163-追加)。

---

## 8. 関連ドキュメント

- [`docs/usage-guide.md`](./usage-guide.md) — 利用ガイド全般（ハードウェア別モデル選定、`doctor` / `verify` の使い方、OS 別起動フロー）
- [`docs/quickstart.md`](./quickstart.md) — Claude Code / codex を local Ollama で動かす最短手順
- [`docs/troubleshooting.md`](./troubleshooting.md) — つまずいたときの読み物（doctor の使い方、`.env` 罠、Llama 過剰呼び出し対処、1Password 連携レシピ）
- [`examples/providers.nvidia-nim.yaml`](../examples/providers.nvidia-nim.yaml) — 本書で参照している YAML 本体。コメント内に観測済みハマり所を全て記載
- [`examples/providers.yaml`](../examples/providers.yaml) — NIM 無しの標準サンプル（local + OpenRouter free + 有料）
- [`docs/openrouter-roster/CHANGES.md`](./openrouter-roster/CHANGES.md) — OpenRouter `:free` SKU の週次差分（`scripts/openrouter_roster_diff.py` 出力）
- [`docs/articles/note-nvidia-nim.md`](./articles/note-nvidia-nim.md) — NIM 検証の経緯を書いた note 記事ドラフト
- [`docs/articles/note-1password-coderouter-2026.md`](./articles/note-1password-coderouter-2026.md) — 1Password Vault Item から CodeRouter に env を inject する運用記
