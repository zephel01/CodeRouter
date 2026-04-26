# Troubleshooting — つまずいたときの読み物

CodeRouter で「うまく動かない」ときに、どこから疑うかを 1 ページに集約したドキュメントです。
症状ごとに「何が起きているか / 何を打って確認するか / どう直すか」の順で並んでいます。

> **困ったらまず**: `coderouter doctor --check-model <provider>` を実行してください (詳細は §0)。
> それでも解決しなければ、症状名で本ページ内検索して該当節へ。
>
> このページは README の旧 §トラブルシューティング（v1.6.1 まで）を独立化したものです。
> 旧アンカー (`#トラブルシューティング`) からのリンクは README にリダイレクトリンクが残っています。

## 目次

- [0. 最初の一手: `coderouter doctor`](#0-最初の一手-coderouter-doctor)
- [1. 起動・設定で踏みやすい 5 つの罠 (v1.6.2 追加)](#1-起動設定で踏みやすい-5-つの罠-v162-追加)
- [2. ログの読み方とよくあるパターン](#2-ログの読み方とよくあるパターン)
- [3. Ollama 初心者 — サイレント失敗 5 症状 (v0.7-C)](#3-ollama-初心者--サイレント失敗-5-症状-v07-c)
- [4. Claude Code 連携で踏みやすい罠 (v1.6.2 追加)](#4-claude-code-連携で踏みやすい罠-v162-追加)
- [5. `.env` のセキュリティ運用 (v1.6.3 追加)](#5-env-のセキュリティ運用-v163-追加)
- [6. HF-on-Ollama リファレンスプロファイル](#6-hf-on-ollama-リファレンスプロファイル)
- [姉妹リファレンス](#姉妹リファレンス)

---

## 0. 最初の一手: `coderouter doctor`

問題の切り分けに迷ったら、まず疑わしいプロバイダ名で `doctor` を回します。

```bash
coderouter doctor --check-model ollama-qwen-coder-7b
```

doctor は 6 種類の probe (auth + basic-chat / num_ctx / tool_calls / thinking / reasoning-leak / streaming) を実プロバイダに対して走らせ、`providers.yaml` の宣言と観測の不一致があれば**コピペ可能な YAML パッチ**を出します。

終了コードは 3 バケット:

| Exit | 意味 | 次にやること |
|---|---|---|
| `0` | 全 probe OK | 設定は健全。問題は外側 (クライアント側 / ネットワーク) を疑う |
| `2` | NEEDS_TUNING あり | 出力されたパッチを `providers.yaml` に追加 |
| `1` | 致命 (auth fail / 接続不可 etc) | §1 / §4 の起動・認証系から確認 |

doctor を**サーバ起動と同じシェル**から打つことが大事です — env 変数の見え方が同じになるので。

---

## 1. 起動・設定で踏みやすい 5 つの罠 (v1.6.2 追加)

サーバが立ち上がる前 / 立ち上がっても上流に届かない、というクラスのトラブル。「Header of type `authorization` was missing」みたいな上流 401 を見たらここから疑います。

### 1-1. CLI コマンドは `serve`、フラグは `--mode`

旧サンプル YAML に `coderouter start --profile claude-code-nim` という記述が残っていた時期がありますが、**正しくは**:

```bash
coderouter serve --mode claude-code-nim --port 8088
```

| よくある間違い | 正解 |
|---|---|
| `coderouter start ...` | `coderouter serve ...` (サブコマンド名) |
| `--profile <name>` | `--mode <name>` (フラグ名) |
| `--port` 省略 | デフォルト 4000。Claude Code の `ANTHROPIC_BASE_URL` に合わせて `--port 8088` 等を明示 |

`coderouter --help` / `coderouter serve --help` で最新の引数を確認できます。

### 1-2. `.env` には `export` が必須

このプロジェクトの `examples/.env.example` は v1.6.2 以降、各キーに `export` を付けた形式に変更されています。

```bash
# OK — source .env で子プロセス (coderouter serve / doctor) に渡る
export NVIDIA_NIM_API_KEY=nvapi-xxxxxxxxxxxxxxxx
export OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxx
```

```bash
# NG — シェル変数になるだけで、子プロセスからは空に見える
NVIDIA_NIM_API_KEY=nvapi-xxxxxxxxxxxxxxxx
```

CodeRouter は `.env` を**自動 source しません**。手動で `source .env` するか、`set -a && source .env && set +a` で `KEY=value` 形式を一括 export してから `coderouter serve` を起動してください。

### 1-3. 環境変数が子プロセスに届いているかの確認

`echo` だけでは不十分です。`echo` はシェル変数も export 済み変数も両方表示するので、原因の切り分けになりません。**`env` でフィルタするのが決定的**:

```bash
env | grep -E 'NVIDIA|OPENROUTER|ANTHROPIC'
# → 1 行も出なければ export されていない (= 子プロセスから空に見える)

# Python 側からの見え方も確認したいなら
python3 -c "import os; print(len(os.environ.get('NVIDIA_NIM_API_KEY','')))"
# → 0 なら子プロセスに渡っていない、70 なら渡っている (NIM キーは ~70 文字)
```

### 1-4. `Header of type authorization was missing` 401

NIM / OpenRouter / Anthropic 直、いずれも env 変数が空のときに `Authorization` ヘッダ自体が**送られない**動きになります。CodeRouter のコードはこれだけ:

```python
api_key = resolve_api_key(self.config.api_key_env)   # = os.environ.get(..., "").strip()
if api_key:
    headers["Authorization"] = f"Bearer {api_key}"
```

`api_key` が空なら if が成立せず、**Authorization ヘッダが付かない**。NIM は「auth header が無い」と返してきます。チェーン全体が同じパターンで死ぬと CodeRouter は `chain-uniform-auth-failure` + `hint: probable-misconfig` を WARN で出します。

直し方は §1-2 と §1-3。

### 1-5. `~/.zshrc` に書いたのに反映されない

シェルの起動経路によっては `.zshrc` が source されないことがあります (IDE 内蔵ターミナル、`tmux` 経由、`exec` 系)。

```bash
# 手動で読み直して同じシェルで確認
source ~/.zshrc
env | grep NVIDIA_NIM_API_KEY

# どこに書いたか自体が曖昧なら全部 grep
grep -rn NVIDIA_NIM_API_KEY ~/.zshrc ~/.zprofile ~/.bashrc ~/.bash_profile 2>/dev/null
```

`export` キーワードが付いていない / `~/.zprofile` のつもりが `~/.zshrc` だった、などのチェック。

---

## 2. ログの読み方とよくあるパターン

v0.4-D 以降、失敗した上流リクエストはサーバーログに**上流レスポンスボディそのもの**を添えて現れます。リクエストが失敗したときは次のような行を探します:

```
{"level": "WARNING", "msg": "provider-failed", "provider": "...",
 "status": 4xx, "retryable": true|false, "error": "[provider status=4xx] 4xx from upstream: {...}"}
```

よくあるパターンと意味:

- **`"Extra inputs are not permitted"` が body フィールドに対して** — 上流 (通常 Anthropic) が知らないフィールドを拒否。`anthropic-beta` ヘッダでゲートされているフィールド (`context_management`、新しい `cache_control` / `thinking` variant) なら、クライアントが実際にヘッダを付けたか確認。v0.4-D 以降 CodeRouter はそのまま転送しますが、クライアントが送っていなければ上流に届きません。
- **`"adaptive thinking is not supported on this model"`** — v0.5-A 以降ユーザーには届かないはず。ケイパビリティゲートが `thinking: {type: enabled}` リクエストをそのフィールドを受け付けるモデルに流し (ヒューリスティクス: `claude-opus-4-*` / `claude-sonnet-4-6` / `claude-sonnet-4-7` / `claude-haiku-4-*`)、対応なしチェーンではブロックを剥がします。まだこのエラーを見るなら、(a) チェーンにヒューリスティクス未収載の新 Anthropic ファミリがいる — 当該プロバイダに `capabilities.thinking: true` を明示、あるいは (b) モデル slug を添えて issue を立て、ヒューリスティクスを更新。サーバーログの `capability-degraded` 行でゲート発火を確認。
- **`capability-degraded` ログで `reason: "non-standard-field"` かつ `dropped: ["reasoning"]`** (v0.5-C) — 上流が OpenAI spec 非準拠の `reasoning` フィールドを `message` / `delta` に返した。OpenRouter 無料モデル (特に `openai/gpt-oss-120b:free`) で発生。アダプタが下流に渡す前に剥がすのでこのログは純粋に観測用 — 何も壊れていません。本当に reasoning テキストを素通ししたい (reasoning-aware クライアントを前立てている等) 場合は当該プロバイダに `capabilities.reasoning_passthrough: true` を付けると strip が止まります。ストリーミング: いくつチャンクに跨ろうとログは 1 ストリーム最大 1 回。
- **`capability-degraded` ログで `reason: "translation-lossy"` かつ `dropped: ["cache_control"]`** (v0.5-B) — リクエストが `cache_control` マーカー付きだったが、選ばれたプロバイダが `kind: openai_compat` なので Anthropic → OpenAI 変換で消失。エラーではなく (リクエストは成功)、ただ Anthropic のプロンプトキャッシングはそのプロバイダでは効きません。対策は (a) `kind: anthropic` プロバイダをチェーンの前に置く、または (b) 将来 `openai_compat` 上流が cache マーカーを保持するなら `capabilities.prompt_cache: true` で当該ログをオプトアウト。なお Anthropic 側の 1024 トークン最小も注意: これを下回るシステムプロンプトは対応プロバイダでも `cached_tokens: 0` を報告します — 上流の制約で CodeRouter のバグではありません。
- **`rate_limit_error` / 429** — Anthropic 組織レベルの TPM 上限。リトライ可能 (エンジンが次プロバイダを試す)。プロファイル順を調整するか、Claude Code のコンテキストを `/compact` で減らす。
- **`unknown profile 'xxx'` (400)** — リクエスト body の `profile` フィールドあるいは `X-CodeRouter-Profile` ヘッダが設定のどの `profiles[].name` とも一致しない。有効名はレスポンス body に。
- **`502 Bad Gateway: all providers failed`** — チェーン全プロバイダがリトライ可能エラーを返した。`provider-failed` ログ行を順に読む。末尾の `error` フィールドがチェーン終端の理由。
- **`chain-uniform-auth-failure` + `hint: probable-misconfig`** — 全プロバイダが同じ 401/403 で死亡。env 変数か API キーの設定ミス。§1-2 / §1-3 を確認。

ミッドストリーム失敗は SSE ストリーム内で単発の `event: error` / `type: api_error` として出ます (ヘッダは既送出なので 5xx HTTP ステータスは返らない)。これは「どのプロバイダも開始できなかった」 (`type: overloaded_error`) とは区別されます。

---

## 3. Ollama 初心者 — サイレント失敗 5 症状 (v0.7-C)

「新規 Ollama をインストールし、ルーターを向けたらどうもおかしい」は最多の onboarding 失敗です。症状はエラーに見えないことがほとんど — モデルが肩をすくめたように見える。これまで現場で集めた 5 種、各症状の一行診断と修正 YAML を添えます。下の `<provider>` は `providers.yaml` のプロバイダ名 (例: `ollama-qwen-coder-7b`)。

**1. 200 を返しているのに返信が空/意味不明。** Ollama の既定 `num_ctx` は 2048 トークン。Claude Code のシステムプロンプトだけで毎ターン 15–20 K トークンあり、2048 以降はプロンプトの**先頭**から黙って落ちます — ツール定義、タスク記述、全部。モデルは残された末尾から答えています。

```bash
coderouter doctor --check-model <provider>
# → num_ctx: NEEDS_TUNING — canary が返信に欠落; 上流が切り詰め
#   (`extra_body.options.num_ctx` 宣言なし、Ollama 既定は 2048)
```

```yaml
# providers.yaml — doctor 提案パッチ:
- name: <provider>
  extra_body:
    options:
      num_ctx: 32768    # VRAM 節約なら 16384 でも可
```

**v1.0-B** 以降 doctor プローブがこれを直接検出します — 約 5K トークンプロンプトの先頭に canary トークンを埋め込み、エコーを求める。canary が返ってこなければ Ollama が先頭を落とした証拠。プローブは Ollama 形状プロバイダ (base URL にポート 11434、または `extra_body.options.num_ctx` 宣言あり) のみ発火するため、他 `kind: openai_compat` 上流は静かに SKIP。

**2. Claude Code が「ファイルが読めません」と繰り返す。** モデルは `tools` パラメータを受け取ったが混乱し、空のアシスタントメッセージを返した。小さな量子化モデル (≤ 7B、Q4) はツール仕様自体を扱えないことが多い。CodeRouter v0.3-A の tool-call 修復は**壊れた**ツール JSON を復元できますが、このケースは「モデルがそもそもツール呼び出しを試みなかった」 — 修復対象がありません。

```bash
coderouter doctor --check-model <provider>
# → tool_calls: NEEDS_TUNING — model returned no tool_use and registry says tools=true
```

```yaml
# providers.yaml — doctor 提案パッチ:
- name: <provider>
  capabilities:
    tools: false    # observed: model returned no tool_use block
```

`tools: false` にすると、ツール要求リクエスト到来時にチェーンは次のプロバイダに進みます。強いモデル (qwen2.5-coder:14b やクラウドフォールバック) と組み合わせて使ってください。

> **補足資料**: モデル別の tool-call 対応状況や、量子化 / システムプロンプト / chat template の落とし穴は Unsloth の [Tool calling guide for local LLMs (日本語)](https://unsloth.ai/docs/jp/ji-ben/tool-calling-guide-for-local-llms) にきれいにまとまっています。Qwen / Llama / Gemma 各系列で tool-call が動かない原因を踏み込んで知りたい人向け。

**3. UI に `<think>...</think>` タグが漏れる。** Qwen3 蒸留モデル、DeepSeek-R1 蒸留、一部の HF GGUF 変種は chain-of-thought を Anthropic の `thinking` ブロックではなく通常のコンテンツチャネルに吐きます。タグが Claude Code のターミナルにそのまま出ます。

```bash
coderouter doctor --check-model <provider>
# → reasoning-leak: NEEDS_TUNING — observed `<think>` in content,
#   provider has no `output_filters` declared
```

**v1.0-A** 以降 doctor プローブは適用可能なフィルタパッチを出します。独立した 2 つの対処 — どちらでも、両方でも:

```yaml
# providers.yaml — 出力側スクラブ (v1.0-A、常時機能・推奨):
- name: <provider>
  output_filters: [strip_thinking]
  # <|turn|> / <|channel>thought / ... も出るなら strip_stop_markers も追加
```

```yaml
# providers.yaml — 入力側オプトアウト (モデルが従うときは安価;
# Qwen3 / R1-distill 系は `/no_think` を尊重する):
- name: <provider>
  append_system_prompt: "/no_think"
```

`output_filters` はアダプタ境界のバイトストリームに作用するのでどのモデル・どのプロバイダ・どのクライアントでも動作します — コンテンツを 1 回余計に舐める分のコストと引き換え。2 つは重ね掛け可能で、`examples/providers.yaml` のサンプル `ollama-qwen-coder-*` は `output_filters: [strip_thinking]` が有効な状態で出荷されています。

**4. チェーンへの初リクエストが毎回失敗して回復する。** `providers.yaml` の `model` フィールドにタイポがある、または `ollama pull <tag>` を忘れている。Ollama は `404 model not found` を返し、これは retryable 分類 (v0.2-x のバグ修正) なのでチェーンはフォールスルーしますが、毎ターン、ローカルティアのレイテンシ優位を失います。

```bash
coderouter doctor --check-model <provider>
# → auth+basic-chat: UNSUPPORTED — 404 from upstream (run `ollama pull <tag>`)
# → (remaining probes SKIP — no point running them until the model exists)
```

対処: `ollama pull <your-yaml-tag>` またはタイポ修正。404 は Ollama が「そのタグの GGUF は載せていない」と言っている。HF-on-Ollama モデル名は `:Q4_K_M` 形式の量子化サフィックス必須で、省略すると同じ 404 になります。

**5. チェーン全プロバイダが一様に失敗する。** `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` が未設定 (または期限切れ) で、チェーンの全クラウドプロバイダが順に 401。v0.5.1 A-3 以降 `chain-uniform-auth-failure` WARN が事後的にこのパターンを識別しますが、トラフィック開始前に捕まえるほうが楽です。

```bash
coderouter doctor --check-model <the-cloud-provider>
# → auth+basic-chat: AUTH_FAIL — 401 from upstream (check env var <KEY_NAME>)
# → (remaining probes SKIP — auth dominates)
```

対処: 環境変数を設定 (§1-2 参照、`export` 必須)、あるいはサーバー起動時にロードされる `.env` に追加 (`cp examples/.env.example .env`)。`coderouter doctor` は動作中のサーバーと同じ env を読むので、シェルからのプローブ成功はサーバーも動くという信頼できるシグナルです。

**全部まとめて走らせる**にはプロバイダごとに `doctor`:

```bash
for p in ollama-qwen-coder-7b ollama-qwen-coder-14b openrouter-free openrouter-gpt-oss-free; do
  coderouter doctor --check-model "$p" || true
done
```

終了コードは 3 バケット (0 クリーン / 2 パッチ可能 / 1 ブロッカ) に集約されるので、上のループは CI に繋げられます。

---

## 4. Claude Code 連携で踏みやすい罠 (v1.6.2 追加)

doctor が全部 OK で、CodeRouter ログにも 401 / 5xx も出ていないのに、Claude Code 上で挙動が変、というクラス。

### 4-1. 挨拶がツール呼び出しに化ける (Llama-3.3-70B 系)

```
❯ こんにちは
⏺ Skill(hello)
  ⎿  Initializing…
  ⎿  Error: Unknown skill: hello. Did you mean help?
```

または Claude Code が `AskUserQuestion` 風の elicitation widget を勝手に出してきて「What is your name? 1. John / 2. Jane / ...」と聞いてくる。

**原因**: バックエンドモデルが Claude Code の system prompt にある tool / skill 宣言に過剰反応して、**ユーザーの自然文をすべて何かしらのツール呼び出しに変換**しようとする。Llama-3.3-70B はこの傾向が特に強い (agentic tuning が強めなため)。

**対処**: profile の先頭を agentic coding 専用設計のモデルに差し替える。

```yaml
# providers.yaml の profile 順序を変更:
profiles:
  - name: claude-code-nim
    providers:
      - nim-qwen3-coder-480b   # ← 第一候補に
      - nim-kimi-k2            # 第二候補
      - nim-llama-3.3-70b      # ← Llama は退避線 (最後尾)
      - openrouter-free
      - ...
```

`examples/providers.nvidia-nim.yaml` (v1.6.2 以降) は Qwen-first の順序になっています。Llama-3.3-70B 自体は動作確認済みですが、Claude Code 単独の対話用途では Qwen3-Coder-480B / Kimi-K2 のほうが運用上は安定します。

> **モデル別 tool-call 挙動の深掘り**: Llama-3.3-70B 系の「自然文を tool 呼び出しに変換しがち」性質は、agentic tuning の RLHF signal とシステムプロンプトの相性に起因します。各モデルの傾向と回避策は Unsloth の [Tool calling guide for local LLMs (日本語)](https://unsloth.ai/docs/jp/ji-ben/tool-calling-guide-for-local-llms) が読みやすく、CodeRouter v1.8.0 で導入した `claude_code_suitability` 判定の背景理解にも役立ちます。

### 4-2. ローカル Ollama 経由で踏みやすい既知問題 (v1.8.1 追記、v1.8.2 改訂)

2026-04-26 の実機検証 (M3 Max 64GB / Ollama 0.21.2 / CodeRouter v1.8.0 → v1.8.2) で、**note 記事や HF で評価が高いモデルでも Ollama 経由では動かないケース**が判明したのでまとめます。

> **v1.8.2 重要更新**: 当初 v1.8.1 で「Qwen3.6 / Gemma 4 ともに num_ctx silent cap」「streaming 0 chars 打ち切り」と判定していた問題は、深掘りの結果 **doctor の `num_ctx` / `streaming` probe が thinking モデルの reasoning トークン消費分を見ていない `max_tokens=32` / `128` バジェットで偽陽性 NEEDS_TUNING を出していた** ことが判明。v1.8.2 で probe バジェットを reasoning モデル時に 1024 まで拡大、registry で `gemma4:*` / `qwen3.6:*` に `thinking: true` 宣言を追加。**Gemma 4 26B は実機で完全動作確定** (`/v1/messages` Anthropic 互換で "Hello." を 2 秒応答)、**Qwen3.6 系の `tool_calls [NEEDS TUNING]` だけが真の課題として残る** (thinking 起因とは別の Ollama tool 仕様未成熟)。

#### 4-2-A. **Qwen3.6:27b / 35b** が Claude Code で実用厳しい

v1.8.2 の偽陽性除去後、`coderouter doctor --check-model ollama-qwen3-6-27b` の結果：

| Probe | 結果 | 症状 |
|---|---|---|
| auth+basic-chat | OK | 短い chat なら動く |
| num_ctx | OK or NEEDS_TUNING (model依存) | thinking バジェット 1024 で偽陽性は解消 |
| **tool_calls** | **NEEDS_TUNING** ← 残る真の課題 | native tool_calls / 修復可能 JSON のいずれも返さず |
| streaming | OK or NEEDS_TUNING | thinking バジェット 1024 で偽陽性は解消 |

`/no_think` を `append_system_prompt` に入れても tool_calls は改善せず。Ollama 0.21.2 / llama.cpp 側の Qwen3.6 family の **tool 仕様** がまだ完全でない (chat template / tool スキーマ整合) 可能性が高い。

**回避**: `claude-code-nim` profile の primary に Qwen3.6 を置かず、**Gemma 4 26B または Qwen2.5-Coder 14b を上位に**。bundled `model-capabilities.yaml` も v1.8.1 で `qwen3.6:*` の `claude_code_suitability: ok` を撤回 (declaration 過信の例)、v1.8.2 で `thinking: true` 追加 (doctor probe 偽陽性除去のため)。

> **コミュニティ証拠 (2026-04-26 偵察)**: X / Reddit (r/ollama, r/LocalLLaMA) で **Qwen3.6 + Ollama の組み合わせは現状コミュニティ全体で詰んでいる** ことが確認できた:
>
> - Qwen3.6 35B-A3B で hard crash / リブート (Mac Metal、複数報告)
> - 「available memory 不足」でロード失敗 (Ollama 側の memory 計算バグ)、最新 Ollama で一部改善
> - Claude Code / OpenCode 連携でタイムアウト・コンテキスト切れ・loop
> - `think=False` 時の構造化出力バグ
>
> 回避策として複数経路：
>
> 1. **Modelfile `PARAMETER num_ctx 131072`** で context を焼き込み (Ollama 経由維持の最小変更)
> 2. **Unsloth GGUF + llama.cpp / llama-server で直叩き** (最有力、複数の「これで解決した」報告あり) — CodeRouter は `kind: openai_compat` + `base_url: http://localhost:8080/v1` で接続可能
> 3. 低 quant (Q4_K_M) / coding 特化 tag を試す
> 4. Ollama 最新版へアップデート (memory bug 部分改善)
>
> CodeRouter として **Unsloth GGUF + llama.cpp 直叩きの providers.yaml 例** を追加するロードマップは plan.md 「v1.8.x patch 候補 — llama.cpp 直叩き backend 検証」に記録済 (実機検証で動いたら有効化)。

> **v1.8.3 update (2026-04-26)**: 実機検証完了。**Qwen3.6:35b-a3b + llama.cpp 直叩きで native `tool_calls` 完璧動作確認**。`finish_reason: "tool_calls"` + 正規 OpenAI `tool_calls[]` array が返る。**Ollama 経由詰みの真因 = Ollama chat template / tool 仕様未成熟、モデル本体は健全** が完全確定。検証済み recipe:
>
> ```bash
> # 1. llama.cpp build (Metal)
> git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
> cd ~/llama.cpp && cmake -B build -DGGML_METAL=ON -DLLAMA_CURL=ON
> cmake --build build --config Release -j
>
> # 2. Unsloth Dynamic Quantization GGUF (~22GB)
> huggingface-cli download unsloth/Qwen3.6-35B-A3B-GGUF \
>   --include "*UD-Q4_K_M*" "*tokenizer*" "*chat_template*" \
>   --local-dir ~/models/qwen3.6-35b-a3b-unsloth
>
> # 3. llama-server 起動
> ./build/bin/llama-server \
>   --model ~/models/qwen3.6-35b-a3b-unsloth/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
>   --port 8080 --ctx-size 32768 --n-predict 4096 \
>   --jinja --threads 8 -ngl 999 --host 127.0.0.1
> ```
>
> CodeRouter `providers.yaml` で `kind: openai_compat` + `base_url: http://localhost:8080/v1` で接続可能。`capabilities.thinking: true` を宣言すると doctor probe が thinking-aware budget (1024) を使うので `tool_calls [OK]` が出る。
>
> v1.8.3 では (a) **`tool_calls` probe も thinking 対応** (旧 `max_tokens=64` で偽陽性 NEEDS_TUNING、suggested patch も真逆 `tools: false` を出していた active-harmful 誤診断を解消)、(b) **adapter で `reasoning_content` (llama.cpp 命名) を `reasoning` と並んで strip** を実装。これで llama.cpp 直叩き経路が CodeRouter の正規サポート対象に。

#### 4-2-B. **Qwen3.5 系の HF 蒸留モデル** (Qwopus3.5 等) は llama.cpp 未対応

例えば `Jackrong/Qwopus3.5-9B-v3-GGUF` (Qwen3.5-VL ベース + Claude Opus 蒸留、Apache-2.0、Vision) を `ollama pull` すると **blob は完全に落ちてくる**が `ollama run` で：

```
Error: 500 Internal Server Error: unable to load model:
  ...sha256-19d52ddc.../...
```

Ollama server log を見ると：

```
llama_model_load: error loading model: error loading model architecture:
  unknown model architecture: 'qwen35'
```

**原因**: Qwen3.5 は新アーキテクチャ (hybrid Transformer-SSM、`qwen35.ssm.*` 系のキーが GGUF metadata に含まれる) で、llama.cpp / Ollama に **`qwen35` architecture 実装が未マージ**。Ollama version の問題ではなく、フレームワーク本体の対応待ち。

**回避**: 現状は HF / Transformers / vLLM 直接ロード経由で使うしかない (CodeRouter は Ollama OpenAI-compat 経由なので非対応)。llama.cpp が `qwen35` を実装したら再評価。

> **教訓**: HF で「Qwen3.5 + Opus 蒸留」のような新しい組み合わせは note / r/LocalLLaMA で評判が立っていても、**Ollama 経由ですぐ使えるとは限らない**。`ollama pull` → `ollama run` で 500 が出たら、まず Ollama server log で `unknown model architecture` を確認。出たら今は諦めて他のモデルに行くのが時間効率的に正解。

#### 4-2-C. **Gemma 4 26B** は実機で完全動作 (v1.8.2 で確定)

`coderouter doctor --check-model ollama-gemma4-26b` の `tool_calls` probe が無加工で `[OK]`。v1.8.2 で thinking モデル対応 probe バジェット (1024) を入れた後は **`num_ctx` / `streaming` も `[OK]`** で 6 probe 全クリア。`/v1/messages` Anthropic 互換経由で "Hello." を 2 秒応答 (M3 Max 64GB)、`tool_calls native OK`、`reasoning strip` 動作。

ただし **interactive UX は若干重い** — Gemma 4 は thinking モデルなので `reasoning` フィールドにも応答時間を使う + 26B サイズ + Claude Code の agent loop (1 プロンプトで 3〜6 round-trip) で総応答時間が 30〜90 秒 / プロンプトになる。daily driver には `qwen2.5-coder:14b` のほうが速い。**Gemma 4 は tool_calls native + 高品質が要るときの選択肢**。

note 記事の「Gemma 4 が日常の王者」評価は **Claude Code agentic 用途でも裏付けられた**形。v1.8.1 で `coding` profile primary を Gemma 4 / Qwen-Coder 14b へ調整、v1.8.2 で registry に `thinking: true` を宣言。

#### 4-2-D. ベスト実践 — 「枯れたモデル + 観測ツール」

実機運用での提案：

1. **第一候補は枯れたもの**: `qwen2.5-coder:14b` / `qwen2.5-coder:7b` / `gemma4:26b` / `gemma4:e4b`
2. **doctor で確認**: `cr doctor --check-model <provider>` で 6 probe を回す
3. **`--apply` で patch 適用**: NEEDS_TUNING の YAML パッチを非破壊書き戻し (`pip install 'coderouter-cli[doctor]'` 必要)
4. **新興モデルは慎重に**: HF で見つけた新モデルは Ollama 0.20+ でも未対応のことあり、`ollama run` → server log で確認
5. **fallback chain で守る**: ローカル primary が落ちても NIM / OpenRouter free に流れるように chain を厚く

#### 4-2-E. doctor probe 自体の限界 — thinking モデル対応 (v1.8.2)

v1.8.2 までの doctor `num_ctx` / `streaming` probe は **`max_tokens=32` / `128`** で出力を要求していた。これは canary token (~5 tokens) や "1..30" (~40 tokens) を返すには十分だったが、**thinking モデル** (Gemma 4、Qwen3 系、gpt-oss、deepseek-r1) は `reasoning` フィールドに思考トークンを吐く設計のため、可視 `content` が出る前に max_tokens に到達して `finish_reason='length'` で打ち切られる **偽陽性 NEEDS_TUNING** を出していた。

v1.8.2 で：

- `_NUM_CTX_PROBE_MAX_TOKENS_DEFAULT = 256` (旧 32)、`_NUM_CTX_PROBE_MAX_TOKENS_THINKING = 1024`
- `_STREAMING_PROBE_MAX_TOKENS_DEFAULT = 512` (旧 128)、`_STREAMING_PROBE_MAX_TOKENS_THINKING = 1024`
- `provider.capabilities.thinking` / `provider.capabilities.reasoning_passthrough` / registry の `thinking` / `reasoning_passthrough` のいずれかが true なら thinking バジェットを採用

つまり **provider 宣言不要**: bundled `model-capabilities.yaml` で `gemma4:*` / `qwen3.6:*` などに `thinking: true` を宣言してあれば、user の providers.yaml には何も書かなくても doctor が自動的に正しいバジェットを使う。

**メタ教訓**: diagnostic ツール自身も diagnostic され続ける必要がある (plan.md §5.4 「実機 evidence first」原則の補強)。

### 4-3. `UserPromptSubmit hook error` が出る (第三者 Claude Code プラグイン)

```
❯ こんにちは
  ⎿  UserPromptSubmit hook error - Failed with non-blocking status code: No stderr output
```

**原因**: Claude Code 側に入っているサードパーティプラグイン (例: `claude-mem@thedotmack`) が `UserPromptSubmit` フックで内部 LLM 呼び出しをしているが、その呼び出し先が**本物の Anthropic API を期待しているのに `ANTHROPIC_BASE_URL=http://localhost:8088` で CodeRouter (→ ローカル LLM) に向いている**ため、独自プロンプトをローカルモデルが解釈できず無音で死ぬ。

CodeRouter 側の問題ではなく、プラグイン側の構造的なミスマッチ。

**対処**: 切り分けは `~/.claude/settings.json` の `enabledPlugins` を一旦 `false` にして再現するか確認。

```jsonc
{
  "enabledPlugins": {
    "claude-mem@thedotmack": false   // ← 一時的に false
  }
}
```

エラーが消えればプラグイン側の問題。プラグイン作者に「CodeRouter / OpenAI-compat 相手でも動くオプションが欲しい」とフィードバックするのが本筋。

### 4-3. 応答はあるが「会話の要約中」が長い

```
✻ Compacting conversation… (34s)
```

Claude Code の auto-compact (古い対話を要約してコンテキスト圧縮) が遅い場合、それは**バックエンドモデルが要約タスクで遅い**だけです。CodeRouter は無関係。Llama-3.3-70B はこれも遅め、Qwen3-Coder-480B / ローカル Ollama に切り替えると改善します。

`DISABLE_CLAUDE_CODE_SM_COMPACT=1` で smart compact (LLM ベース要約) を切れますが、古い truncate-based compact は動きます。手動で `/compact` / `/clear` でセッションを切るのが確実。

### 4-4. ダッシュボードを開いておくと罠が 10 秒で見える

`http://localhost:8088/dashboard` を別タブで開きっぱなしにしておくと、上記すべてが目で見て即座に分かります:

- どのプロバイダが応答したか / どこで失敗してフォールバックしたか (RECENT EVENTS パネル)
- `Capability degraded` の発火回数 (FALLBACK & GATES パネル)
- `chain-uniform-auth-failure` の発生 (`failed` 列に積まれる)

ログを `grep` するより 10 倍速いので、Claude Code を動かすセッション中はこれを開いておくのが推奨運用。

---

## 5. `.env` のセキュリティ運用 (v1.6.3 追加)

`.env` には API キーが平文で並びます。ローカル開発でも次の 4 つは最低限揃えておきたいラインで、CodeRouter はそれぞれを支援する仕組みを v1.6.3 から提供しています。

### 5-1. 脅威モデル — 何から守るのか / 何は守れないのか

| # | 脅威 | 暗号化 (at-rest) で防げる？ | CodeRouter v1.6.3 のサポート |
|---|---|---|---|
| 1 | 誤って `git add .env && push` した | △ commit 時点で鍵ローテート必須なのは同じ | `coderouter doctor --check-env` で `.gitignore` / 追跡状態を事前検出 |
| 2 | ノート PC 盗難 / ディスクイメージ取得 | ◎ | OS 側の FileVault / dm-crypt / BitLocker に委ねる (アプリ側で暗号化 ≠ 上位互換) |
| 3 | 同一 OS の他ユーザーが `ps` / `/proc/$pid/environ` を覗く | ✕ 復号後は env 丸出し | パーミッション 0600 を `--check-env` で検証 |
| 4 | `swap` / coredump / シェル history | ✕ | キーをコマンドラインに直書きしない (= `--env-file` か外部 secret manager 経由) |

「アプリで AES 自前実装」は脅威 2 / 6 (バックアップ含み) にしか効かず、しかも復号鍵を**結局どこかに置く**必要が出てきます。代わりに既存ツール (1Password / sops / OS Keychain / direnv) を `coderouter` の `--env-file` 経由で噛ませるのが、設計上もコスト的にも筋が良いというのが v1.6.3 の方針です。

### 5-2. クイックチェックリスト

```bash
# 1. .env 自身のパーミッション (0600)
chmod 0600 .env

# 2. .gitignore に入っているか
echo '.env' >> .gitignore
git check-ignore -q .env && echo "ignored OK"

# 3. すでに git に追跡されていないか
git ls-files --error-unmatch .env 2>/dev/null && echo "ALREADY TRACKED — rotate keys!"

# 上の 3 つを 1 コマンドで自動チェック
coderouter doctor --check-env .env
```

`doctor --check-env` は 4 項目 (existence / permissions / .gitignore / git-tracking) を一度に検査し、`OK` / `WARN` / `ERROR` を出します。WARN だけならコピペできる修正コマンド (`chmod 0600 ...` / `echo '.env' >> .gitignore`) が、ERROR (= 既に追跡されてる) なら `git rm --cached` 入りの手順が出力されます。

### 5-3. 1Password CLI と連携する (推奨)

`.env` をディスクに置かずに、起動時だけ 1Password から取り出して env として inject する構成です。Mac / Linux / Windows すべて対応していて、`.env.tpl` (テンプレート) だけが git 管理対象になります。

**準備**:

```bash
# 1Password CLI をインストール
brew install 1password-cli   # macOS
# または: https://developer.1password.com/docs/cli/get-started

# サインイン (一度だけ)
op signin
```

**`.env.tpl` の例** — シークレットの代わりに 1Password の参照を書きます:

```bash
export NVIDIA_NIM_API_KEY=op://Personal/NVIDIA NIM/credential
export OPENROUTER_API_KEY=op://Personal/OpenRouter/credential
export ANTHROPIC_API_KEY=op://Personal/Anthropic/credential
```

**起動 — `op run` が `.env.tpl` の参照を解決して env として inject**:

```bash
op run --env-file=.env.tpl -- coderouter serve --mode claude-code-nim --port 8088
```

このパターンの何がいいかというと:

- **`.env.tpl` は git にコミットしてよい** (シークレットの実体は入っていない)
- **ディスク上に平文の `.env` が一切存在しない** (脅威 2 / 6 を完全に防げる)
- **チームでメンバーごとに別の Vault** を使えるので、退職時の鍵差し替えが Vault 切り替えだけで済む
- **`coderouter` 側は `--env-file` を介さない** (1Password が直接 env として inject するため、`coderouter serve` から見ると単に export 済み env が見えているだけ)

> **`coderouter serve --env-file <path>` は、1Password の出力を一度ファイルに落としてから渡したい場合や、direnv / sops のようにファイル経由で受け渡したい場合に使います。** 1Password を `op run` で直接渡せるなら、`--env-file` は不要です。

### 5-4. direnv + sops で git 管理の暗号化 secret

チーム共有の secret を git にコミットしたい (= バックアップ / レビュー / 履歴を残したい) ケース向けの構成です。`.env.enc` (PGP / age 暗号化済み) を git に置き、`cd` 時に direnv が sops 経由で復号して env に展開します。

```bash
# 1. sops + age を準備
brew install sops age

# 2. 鍵を生成 (~/.config/sops/age/keys.txt に保存)
age-keygen -o ~/.config/sops/age/keys.txt

# 3. 暗号化 (公開鍵で暗号化、git にコミットして OK)
sops -e --age <PUBLIC_KEY> .env > .env.enc
echo '.env' >> .gitignore   # 平文の方は当然 ignore
echo '!.env.enc' >> .gitignore   # 暗号化版は明示的に許可

# 4. .envrc (direnv が自動 source するファイル) を書く
cat > .envrc <<'EOF'
eval "$(sops -d .env.enc)"
EOF
direnv allow .

# 5. cd するだけで自動 export
cd ~/works/CodeRouter   # direnv が裏で sops -d する
coderouter serve --port 8088
```

`coderouter` 側は通常通り env から鍵を読むだけ。`--env-file` も不要。

### 5-5. OS Keychain (macOS / Linux libsecret / Windows Credential Manager)

OS 標準のキーチェーンに鍵を入れて、起動時にそこから引き出す構成。1Password を入れたくない / Mac だけで完結したい人向け。

**macOS**:

```bash
# 鍵を Keychain に保存 (一度だけ。プロンプトで実値を入力)
security add-generic-password -s NVIDIA_NIM_API_KEY -a $USER -w

# 起動時に env として export
export NVIDIA_NIM_API_KEY=$(security find-generic-password -s NVIDIA_NIM_API_KEY -a $USER -w)
coderouter serve --port 8088
```

`~/.zshrc` の末尾に上の 2 行を書いておけば、新しいシェルを開くたびに自動で展開されます (Touch ID / パスワード認証ダイアログが出ることもあります — これも 1 つのセキュリティ層)。

**Linux (libsecret)**:

```bash
# 鍵を保存
secret-tool store --label='NVIDIA NIM' service NVIDIA_NIM_API_KEY

# 起動時
export NVIDIA_NIM_API_KEY=$(secret-tool lookup service NVIDIA_NIM_API_KEY)
coderouter serve --port 8088
```

### 5-6. `--env-file` の積み重ねパターン (中級)

`coderouter serve --env-file <path>` は複数指定でき、左から右へ「**先勝ち**」で適用されます。**既に環境変数が設定されている場合、ファイルの値は無視される** (`--env-file-override` で挙動を反転可) という設計なので、層を分けて運用しやすいのが利点。

```bash
# 例: グローバル defaults + プロジェクト個別 override
coderouter serve \
  --env-file ~/.coderouter/global.env \
  --env-file ./project.env \
  --port 8088
```

```bash
# 例: 1Password + project-local の合わせ技
op run --env-file=.env.tpl -- \
  coderouter serve \
    --env-file ./project-local-overrides.env \
    --port 8088
```

`coderouter serve --env-file` 起動時は、ロードされたキーの**名前のみ** (値ではなく) を stderr に 1 行で要約します:

```
serve: --env-file ./project.env: loaded 2 variable(s): CODEROUTER_MODE, OPENROUTER_API_KEY
```

値は意図的にログに出さない (= secret は決して stdout / stderr に漏らさない) 設計です。

### 5-7. キースコープを最小化する (鍵そのものを安く / 短命にする)

最後に、暗号化以前の問題として「**鍵そのものを安く / 短命に保つ**」のは効果が大きい一方で見過ごされがちです:

- **NVIDIA NIM**: キー単位で **使用上限 / 期限** を切れる ([build.nvidia.com/account/keys](https://build.nvidia.com/account/keys))。本番は無制限、開発用は月 100 リクエスト、のような分け方が可能
- **OpenRouter**: `:free` SKU だけに絞った keyspec を発行可能。万一漏れても無料モデルしか叩かれない
- **Anthropic**: `disable rotation` で月 1 回程度ローテートする習慣付けは、漏洩時の被害ウィンドウを縮める

詳細はそれぞれのプロバイダ管理画面で。これらは**暗号化より優先度が高い**運用習慣です (鍵が短命なら漏れても被害が小さい / 鍵がスコープ最小なら漏れても何も できない)。

---

## 6. HF-on-Ollama リファレンスプロファイル

Ollama の `hf.co/<user>/<repo>:<quant>` ローダ経由で HF ホスト GGUF を動かすと、§3 の 5 症状がすべて増幅されます — HF GGUF はチャットテンプレートなしで出荷されることが多く、蒸留元の `<think>` タグを引き継ぎ、症状 4 を踏む `:<quant>` サフィックスが必須です。

`examples/providers.yaml` にはコメントアウトされた `ollama-hf-example` スタンザがあり、各つまみ (`extra_body.options.num_ctx`、`append_system_prompt: "/no_think"`、`capabilities.tools: false`、`reasoning_passthrough`) を例示し、インラインコメントで各対応症状を示しています。コピーし、`model:` を pull した HF タグに書き換え、`coderouter doctor --check-model ollama-hf-example` で検証してください。

---

## 姉妹リファレンス

同じローカル Ollama に対して CodeRouter (ルーター層) と [lunacode](https://github.com/zephel01/lunacode) (エディタハーネス) を両方走らせている場合、lunacode の [`docs/MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) が姉妹リファレンスです — 同じ 5 症状を、CodeRouter のプロバイダ粒度宣言が届かないエディタ/ハーネス層 (モデル別設定、チャットテンプレート上書き、`/no_think` バリアント) でカバーします。

NIM / OpenRouter 無料枠の使い分けは [`docs/free-tier-guide.md`](./free-tier-guide.md)、設定の細かい意味は [`docs/usage-guide.md`](./usage-guide.md)、最短セットアップは [`docs/quickstart.md`](./quickstart.md) を参照。
