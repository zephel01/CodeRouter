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
- [5. HF-on-Ollama リファレンスプロファイル](#5-hf-on-ollama-リファレンスプロファイル)
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

### 4-2. `UserPromptSubmit hook error` が出る (第三者 Claude Code プラグイン)

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

## 5. HF-on-Ollama リファレンスプロファイル

Ollama の `hf.co/<user>/<repo>:<quant>` ローダ経由で HF ホスト GGUF を動かすと、§3 の 5 症状がすべて増幅されます — HF GGUF はチャットテンプレートなしで出荷されることが多く、蒸留元の `<think>` タグを引き継ぎ、症状 4 を踏む `:<quant>` サフィックスが必須です。

`examples/providers.yaml` にはコメントアウトされた `ollama-hf-example` スタンザがあり、各つまみ (`extra_body.options.num_ctx`、`append_system_prompt: "/no_think"`、`capabilities.tools: false`、`reasoning_passthrough`) を例示し、インラインコメントで各対応症状を示しています。コピーし、`model:` を pull した HF タグに書き換え、`coderouter doctor --check-model ollama-hf-example` で検証してください。

---

## 姉妹リファレンス

同じローカル Ollama に対して CodeRouter (ルーター層) と [lunacode](https://github.com/zephel01/lunacode) (エディタハーネス) を両方走らせている場合、lunacode の [`docs/MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) が姉妹リファレンスです — 同じ 5 症状を、CodeRouter のプロバイダ粒度宣言が届かないエディタ/ハーネス層 (モデル別設定、チャットテンプレート上書き、`/no_think` バリアント) でカバーします。

NIM / OpenRouter 無料枠の使い分けは [`docs/free-tier-guide.md`](./free-tier-guide.md)、設定の細かい意味は [`docs/usage-guide.md`](./usage-guide.md)、最短セットアップは [`docs/quickstart.md`](./quickstart.md) を参照。
