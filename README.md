<h1 align="center">CodeRouter</h1>

<p align="center">
  <strong>Claude Code でローカル LLM を使うと tool calling が壊れる問題、<br>ルーター側で直します。</strong>
</p>

<p align="center">
  qwen2.5-coder:7B、phi-4、mistral-nemo など小型・量子化モデルがしばしばやる<br>
  <strong>「<code>{"name":..., "arguments":...}</code> を plain text として吐いてしまう」</strong>現象を、<br>
  CodeRouter の <strong>tool-call 修復パス</strong>が Claude Code に届く前に<br>
  有効な <code>tool_use</code> ブロックへ復元します。
</p>

<p align="center">
  <strong>「ローカル LLM にしたら agentic coding ができない」と諦めていた人へ。</strong><br>
  これで本気で使える local-first agent が組めます。
</p>

<p align="center">
  <a href="https://github.com/zephel01/CodeRouter/actions/workflows/ci.yml"><img src="https://github.com/zephel01/CodeRouter/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href=""><img src="https://img.shields.io/badge/status-stable-brightgreen" alt="status"></a>
  <a href=""><img src="https://img.shields.io/badge/version-1.6.1-blue" alt="version"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/runtime%20deps-5-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <a href="./README.en.md">English</a> · <strong>日本語</strong> · <a href="./docs/usage-guide.md">利用ガイド</a> · <a href="./docs/security.md">Security</a>
</p>

<!-- TODO: before/after GIF を docs/assets/before-after-toolcall.gif に配置予定。
     暫定で ダッシュボードのスクショ だけリンク。 -->
<!-- ![Before / After tool calling demo](./docs/assets/before-after-toolcall.gif) -->

**CodeRouter が他に何をやってくれるか**

- `coderouter doctor --check-model <provider>` でそのモデルが tool call / streaming / thinking に対応しているかを**実プローブで即診断**し、足りない宣言をコピペ可能な YAML パッチで教えてくれる
- reasoning leak（`<think>...</think>` タグや `<|turn|>` など 6 種の stop マーカー漏れ）を SSE チャンク境界を跨いで自動スクラブ
- ローカル → 無料クラウド（OpenRouter free / NVIDIA NIM 40 req/min 無料枠）→ 有料 API の自動フォールバック。既定で `ALLOW_PAID=false` なので課金はオプトイン制
- ランタイム依存 5 個（`fastapi` / `uvicorn` / `httpx` / `pydantic` / `pyyaml`）— 純 Python、MIT、テスト 601 本緑

→ **Claude Code / gemini-cli / codex + Ollama / llama.cpp / NVIDIA NIM で、破綻しない local-first agent が組める**

## CodeRouter で何が楽になるか

CodeRouter は、コーディングエージェント（Claude Code / gemini-cli / codex / 素の OpenAI SDK）と、その裏の LLM の間に挟まる小さなルーターです。ツールの向き先を 1 本のエンドポイントにまとめておけば、CodeRouter がプロバイダを順に選びます — まずローカル Ollama / llama.cpp、次に無料クラウド (OpenRouter free)、有料 API は明示的に opt-in したときだけ。

初心者が普通に使うとぶつかる「地雷」を、CodeRouter がまとめて面倒見てくれます:

- **API キー無し・Anthropic 課金無しのまま Claude Code を回せる。** ローカルモデル（または OpenRouter 無料枠）が答えます。有料プロバイダは `ALLOW_PAID=true` を明示したときだけ呼ばれます。
- **返答が途中で消えない。** 1 プロバイダが途中で落ちてもクライアントには綺麗な `event: error` が 1 本届くだけ — 2 モデルを継ぎ接ぎしたフランケン応答にはなりません。
- **うっかり課金しない。** `ALLOW_PAID=false` が既定。有料プロバイダをチェーンから外したときは理由を 1 行ログに出すので、なぜ使われなかったかが後で grep できます。
- **ローカル Ollama の上で Claude Code / gemini-cli / codex が動く。** Claude Code は Anthropic のワイアフォーマット、Ollama / llama.cpp / LM Studio は OpenAI。CodeRouter が双方向に変換し、小さいローカルモデルがテキストで吐いてしまう `{"name":..., "arguments":...}` を tool_use ブロックへ復元してからエージェントに渡します。
- **「なぜか動かない」の原因を教えてくれる。** `coderouter doctor --check-model <provider>` が 6 種類の典型的な失敗モード（コンテキスト切り詰め / ストリーム早期終了 / ツール呼び出し欠落 / reasoning フィールド漏れ / 認証 / Anthropic `thinking`）を実地プローブし、コピペ可能な YAML パッチを出します。
- **監査しやすい。** ランタイム依存 5 個（LiteLLM は 100+）。Pure Python、MIT、テスト 601 本緑。

```
クライアント (Claude Code / OpenAI SDK / gemini-cli / codex / curl)
        │
        ▼
  CodeRouter  ──►  ① ローカルモデル (Ollama / llama.cpp — 無料・最優先)
                   ② 無料クラウド (OpenRouter qwen3-coder:free, gpt-oss-120b:free, …)
                   ③ 有料クラウド (Claude / GPT — ALLOW_PAID=true のときだけ)
```

### ライブダッシュボード

`coderouter dashboard` は、障害調査中に実際に知りたい「いま何が起きているか」に、ログを grep せずに即答するための画面です:

- どのプロバイダが生きていて、いま応答しているのは誰か
- フォールバックは直近で発火したか、発火したなら何が理由か
- 有料ゲートは閉じたままか（= まだ無料経路で走っているか）
- 直近数分のリクエスト流量はどうか
- ついさっき何が起きたか — 直近 N 件のイベントが時系列で

![CodeRouter ダッシュボード — プロバイダ状態 / フォールバック & ゲート / requests/min スパークライン / 直近イベント / usage mix](./docs/assets/dashboard-demo.png)

上のスクリーンショットは `scripts/demo_traffic.sh` で混合トラフィック（normal / stream / burst / fallback / paid-gate）をローカルのモックに流している最中のものです。左上から: プロバイダ状態、フォールバック & ゲート状態、requests/min スパークライン、直近イベント（新しい順）、usage mix。

## CodeRouter は自分に必要か？

CodeRouter は wire 翻訳 + 絆創膏の層です。エージェントが既に OpenAI を喋り、モデルがお行儀良く動くなら、多くの場合不要です。下の 2 つの表が短縮版で、フルの判断ガイドは [`docs/when-do-i-need-coderouter.md`](./docs/when-do-i-need-coderouter.md) にあります。

**エージェント別** — Ollama に直接向けられるか:

| エージェント | wire | Ollama 直続き | CodeRouter 必要？ |
|---|---|---|---|
| Claude Code | Anthropic | ✕ | **必須** — wire 翻訳 |
| Codex CLI / 素の OpenAI SDK | OpenAI | ◯（`OPENAI_BASE_URL`） | オプション |
| gemini-cli | Gemini | ✕ | 必要（アダプタ） |
| GitHub Copilot CLI | GitHub 独自 | **✕（バックエンド固定）** | 無力 — 差し替え不可 |

**モデル別** — 直続きで崩れるか:

| モデル | 出力綺麗？ | 効く CodeRouter フィルタ |
|---|---|---|
| `llama3.1` / `mistral-nemo` / `phi-4` / `qwen2.5`（`-coder` でない） | ◯ | — |
| `qwen2.5-coder` | ✕ — `<think>` 漏れ | `strip_thinking` |
| `gpt-oss` / `deepseek-r1` / `qwq` | ✕ — 推論過程漏れ | `strip_thinking` |
| 小さい量子化（Q2 / Q3）、テンプレ不整合 Modelfile | ✕ — tool JSON 壊れ / stop marker 漏れ | `repair_tool_call` / `strip_stop_markers` |

OpenAI 互換エージェント + お行儀の良いモデル + フォールバック不要、の構成なら `OPENAI_BASE_URL=http://localhost:11434/v1` の一行で済みます。それ以外 — 特に Claude Code、reasoning 系モデル、mid-stream ガード付きの多段フォールバック — が CodeRouter の仕事どころです。

## 直接つなげばいいのでは？

- **Ollama / llama.cpp / LM Studio を直接叩く。** 確かに速くて無料ですが、Claude Code（および `/v1/messages` を前提にした多くのエージェント）は Anthropic 形式で話します — 上記サーバは OpenAI 形式だけ。結果、"unsupported endpoint" で弾かれるか、モデルが tool 呼び出しを素のテキストで吐いて tool-use が静かに壊れるかのどちらかです。CodeRouter は双方向に変換し、エージェントに渡る前に JSON を修復します。
- **LiteLLM。** 良いプロダクトですが依存が重く（推移的依存 100+）、`/v1/messages` をネイティブに出しません。
- **OpenRouter 単独。** 無料枠はレート制限があり、たまに落ちます。有料オンリーは新規ユーザーや CI には敷居が高い。

設計不変項と今後のロードマップは [`plan.md`](./plan.md) を参照。初心者向けに「同じローカルモデルでも、なぜ人によって動く/動かないが分かれるのか」を解説する記事は Zenn / Note で別途公開しています。

## クイックスタート（3 コマンド）

```bash
# 1. インストール (uv を利用 — 高速・lockfile フレンドリー)
uv sync

# 2. サンプル設定を置く
mkdir -p ~/.coderouter
cp examples/providers.yaml ~/.coderouter/providers.yaml

# 3. 起動
uv run coderouter serve
```

あとは任意の OpenAI クライアントを `http://127.0.0.1:4000` に向けるだけです:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ignored",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

`model` フィールドは現状プレースホルダです — ルーティングは `profile` フィールド（`providers.yaml` の `default` がデフォルト）で決まります。

はじめての方は [利用ガイド](./docs/usage-guide.md) を参照してください。ハードウェア別のモデル選定、チューニング既定値、OS ごとの起動フロー、OpenRouter 無料枠とのペア方針を一通り解説しています。(English: [usage guide](./docs/usage-guide.en.md))

**NVIDIA NIM 無料枠（40 req/min）と OpenRouter 無料枠をどう重ねるか**は [無料枠ガイド](./docs/free-tier-guide.md) にまとめてあります。live 検証済みのモデル一覧、`claude-code-nim` プロファイルの設計意図、よくあるハマり所 5 点 込み。(English: [free-tier guide](./docs/free-tier-guide.en.md))

## OS 対応

CodeRouter 自体は純 Python 3.12+ で、実質的な OS 対応範囲は `min(coderouter, ollama, claude-code)` です。

| OS | サーバー | ローカル推論 | メモ |
|---|---|---|---|
| macOS — Apple Silicon (M1–M5) | ✅ | ✅ Metal ネイティブ | **主要開発ターゲット** |
| macOS — Intel | ✅ | ⚠️ CPU のみ | 実用はクラウドフォールバックのみ |
| Linux — x86_64 (Ubuntu / Debian / Fedora) | ✅ | ✅ CUDA または CPU | フル対応 |
| Linux — ARM64 (Pi 5 / Graviton) | ✅ | ⚠️ Pi では CPU | クラウド中継プロキシとして使える |
| Windows — WSL2 (Ubuntu) | ✅ | ✅ | **Windows ではこの経路を推奨** |
| Windows — native | ⚠️ 一部 | ✅ CUDA | `scripts/verify_*.sh` は bash 必須 (Git Bash/WSL2) |

注意点や「ローカル GPU なし」向けレシピを含むフル版マトリクス: [利用ガイド §1](./docs/usage-guide.md#1-os-互換性)

## ステータス — v1.0 安定版 (2026-04)

**テスト 601 本通過。ランタイム依存 5 個。macOS / Linux / Windows WSL2 で動作。** ルーターは日常的な Claude Code 用途で安定しています。v1.0 の総まとめは [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md)。

今日の CodeRouter が届ける価値:

- **どのクライアントもどのプロバイダに橋渡し。** OpenAI 互換クライアントからのリクエストと Claude Code（`/v1/messages` 経由）両方を受け入れ、ストリーミング/非ストリーミングを問わず、ローカル Ollama / OpenRouter 無料 / Anthropic / それらの混在にルーティングします。
- **部分レスポンスを垂れ流さず安全にフォールバック。** 最初のバイト前にプロバイダが失敗したら次を試す。**最初のバイト以降**に失敗したら、クライアントには綺麗な `event: error` が 1 本届くだけ — 2 つのプロバイダを継ぎ接ぎしたフランケン応答は起きません。
- **明示的にオプトインしたときだけ課金。** `ALLOW_PAID=false`（既定）がチェーンから有料プロバイダを外し、ブロック時は 1 行の明確なログを出します。
- **小さいローカルモデルが壊すものを修復。** Qwen / DeepSeek 系がテキストとして吐いた `{"name":..., "arguments":...}` は、Claude Code に届く前に有効な `tool_use` ブロックへ復元されます。
- **何がおかしいかを教えてくれる。** `coderouter doctor --check-model <provider>` が 6 プローブ（認証 / コンテキスト切り詰め / ストリーム中断 / ツール呼び出し能力 / reasoning フィールド漏れ / Anthropic `thinking` 対応）を回し、宣言と実挙動が食い違えばコピペ可能な YAML パッチを出します。
- **reasoning 漏れをスクラブ。** プロバイダに `output_filters: [strip_thinking, strip_stop_markers]` を付ければ `<think>…</think>` と 6 種の stop マーカー variants が SSE チャンク境界を跨いでも安定して剥がれます。
- **Anthropic ネイティブ機能は Anthropic に届いたら保持。** `cache_control` / `thinking` / `anthropic-beta` ヘッダでゲートされる body フィールドは `kind: anthropic` プロバイダでそのまま通り、OpenAI 形状へ落ちる場合のロッシー変換は（沈黙ではなく）ログで可視化されます。

**リリース単位の詳細が欲しい？** v0.x と v1.0-A/B/C の各スライス — 何が入り、何本のテストが増え、なぜ必要だったのか — は [CHANGELOG.md](./CHANGELOG.md) に揃っています。設計の不変項と今後のロードマップは [plan.md](./plan.md)。

**次の予定**（v1.0 は [plan.md §10](./plan.md)、v1.0+ は §18）: v1.5 ✅ — メトリクス / `/dashboard` / `coderouter stats` TUI / `scripts/demo_traffic.sh` (出荷済み)。v1.6 — CI 向け `coderouter doctor --network` とランチャースクリプト（当初は v1.1 に予定されていたが、v1.5 が先行出荷されたため v1.6 に繰り下げ）。

### Claude Code と一緒に使う

```bash
# ターミナル 1: Claude Code 向けにチューニングしたプロファイルで CodeRouter を起動
uv run coderouter serve --port 8088

# ターミナル 2: Claude Code を CodeRouter に向け、ヘッダでチューニング済みプロファイルを選ぶ
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude
```

`examples/providers.yaml` の `claude-code` プロファイル（7b を先頭、14b を品質フォールバック、14b のタイムアウトを 300s に拡大）を使うには、設定で既定にします:

```yaml
# ~/.coderouter/providers.yaml
default_profile: claude-code
```

もしくは起動時に `--mode` フラグで選択 (v0.6-A):

```bash
uv run coderouter serve --port 8088 --mode claude-code
# 同等: CODEROUTER_MODE=claude-code uv run coderouter serve --port 8088
```

`--mode` はこのプロセス限定で YAML の `default_profile` を上書きします。リクエスト単位の上書き (`X-CodeRouter-Profile` ヘッダ、または body の `profile` フィールド) は依然勝つので、`--mode` は「設定ファイルを編集せずに別のチェーンを試したい」ときのつまみです。未知のプロファイル名は初回リクエストではなく起動時 fast-fail です。

`examples/providers.yaml` 側のプロファイルはこのような形です — そのままコピーし、各 `providers:` エントリの `base_url` / `model` をあなたのローカルスタックに合わせて書き換えてください:

```yaml
# ANTHROPIC_BASE_URL=http://localhost:8088 claude 用にチューニング。
# Claude Code は毎ターン全ツール (Bash/Glob/Read/Write/...) を宣言するので、
# ルーターは常に v0.3-D の tool-downgrade 経路を使う。ユーザー体感レイテンシ
# ≒ 上流のトータル応答時間。先頭に最速の tool 対応モデル、2 番手に 14b を品質
# フォールバックに、レート制限脱出に無料クラウド 2 種、最後の砦に Claude。
profiles:
  - name: claude-code
    providers:
      - ollama-qwen-coder-7b         # M 系で ~30–60s/ターン、tool 対応
      - ollama-qwen-coder-14b        # 品質フォールバック (timeout_s: 300)
      - openrouter-free              # qwen/qwen3-coder:free (262K コンテキスト)
      - openrouter-gpt-oss-free      # openai/gpt-oss-120b:free (別ベンダー = レート制限脱出)
      - openrouter-claude            # 有料、ALLOW_PAID=true が必要
```

有料ティアを Anthropic のネイティブ API にしたい場合（Anthropic ingress 経由での `cache_control` / `thinking` ブロック生存が目的なら）、`openrouter-claude` を `anthropic-direct` に差し替えます — `examples/providers.yaml` の `claude-code-direct` プロファイルがまさにそれです。

#### プロファイル単位のパラメータ上書き (v0.6-B)

プロファイルはチェーン中の全試行に対し 2 つのパラメータを上書き可能で、同じプロバイダ一覧を別プロファイルで違った挙動にしたいとき（例: 長文 `/no_think` モード vs 短文チャットモード）に便利です:

```yaml
profiles:
  - name: claude-code-long
    timeout_s: 600             # このプロファイルでは ProviderConfig.timeout_s を置換
    append_system_prompt: ""   # 空文字 = プロバイダ指示を明示的にクリア
    providers:
      - ollama-qwen-coder-14b
      - openrouter-free
```

セマンティクス: プロファイル値は設定されていればプロバイダ値を**置き換え**ます（append ではない）。`timeout_s` のようなスカラでの素直な挙動と一致。`append_system_prompt: ""` はこのプロファイル限定でプロバイダ指示を明示消去します（「未設定」= プロバイダ既定にフォールバック、と区別）。未設定フィールドはプロバイダ既定をそのまま残します。`retry_max` はアダプタレイヤのリトライが未実装のため後続マイナーに持ち越し — 現状はフォールバックチェーン自体がリトライ機構です。

#### Mode エイリアス — `X-CodeRouter-Mode` (v0.6-D)

具体プロファイル名ではなく**意図**を表現したいクライアントは `X-CodeRouter-Mode` ヘッダを送れます。CodeRouter は YAML `mode_aliases:` ブロックで解決します:

```yaml
# providers.yaml
mode_aliases:
  coding: claude-code          # クライアントが Mode: coding → プロファイル claude-code
  long:   claude-code-long
  fast:   ollama-only
```

```bash
curl http://localhost:8088/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-CodeRouter-Mode: coding' \
  -d '{ "messages": [{"role":"user","content":"hi"}] }'
```

優先度（先着勝ち）: body `profile` > `X-CodeRouter-Profile` ヘッダ > `X-CodeRouter-Mode` ヘッダ > `default_profile`。Mode が Profile より下なのは **Profile は実装、Mode は意図** であるため — 呼び出し側が具体プロファイルを指定したときはそのまま尊重します。CodeRouter の前段プロキシが Mode を自動付与する構成でも、呼び出し側の明示 body/ヘッダ `profile` が勝つ、これが大事な理由です。

ガードレール: 壊れたエイリアス対象は起動時 fast-fail（`default_profile` バリデーションと同じ哲学）、未知 Mode は宣言済エイリアス一覧付き 400、解決ごとに `mode-alias-resolved` INFO を出すので運用者はマッピングを後で grep できます。

#### モデルケイパビリティレジストリ — `model-capabilities.yaml` (v0.7-A)

「どの Anthropic ファミリが `thinking: {type: enabled}` を受け付けるか」の知識は、以前は `coderouter/routing/capability.py` 内の正規表現リテラルでした。v0.7-A からは `coderouter/data/model-capabilities.yaml`（パッケージ同梱）に移行し、任意で `~/.coderouter/model-capabilities.yaml` によるユーザー上書きが可能です。Anthropic が新ファミリを出したら YAML 1 行で追加 — コード変更もリリースサイクルも不要。

```yaml
# ~/.coderouter/model-capabilities.yaml — 任意のユーザー上書き
version: 1
rules:
  # 仮に Anthropic が新ファミリを出し、CodeRouter の同梱既定更新前に使いたい場合。
  - match: "claude-sonnet-5-*"
    kind: anthropic
    capabilities:
      thinking: true

  # このタグのローカル Ollama では確実にツール呼び出しできるので宣言。
  # v0.7-B doctor の判定と合わせ、将来の glob 消費者からも
  # 方針のある既定として参照されるようにする。
  - match: "qwen3-coder:*"
    kind: openai_compat
    capabilities:
      tools: true
```

スキーマ: 各ルールは `match` (`provider.model` に対する fnmatch glob、大小区別)、任意の `kind` フィルタ (`"anthropic"` / `"openai_compat"` / `"any"`、既定 `"any"`)、`capabilities:` マップ（`thinking` / `reasoning_passthrough` / `tools` / `max_context_tokens` を宣言可能）。ルールは上から順に評価され、**各フラグごとに最初に宣言したルール**が勝ちます。ルールは 1 つのケイパビリティだけ上書きし、他はフォールスルーさせる使い方も可能。

レイヤ間の優先度は `providers.yaml` `capabilities.*`（プロバイダ単位の明示 opt-in）> ユーザー `model-capabilities.yaml` > 同梱 `model-capabilities.yaml` > 未設定（False 扱い）。ユーザーは常に上書き権限を失いません — `providers.yaml` のプロバイダで明示的に `capabilities.thinking: true` を付ければレジストリに勝つ点は v0.5-A から変わりません。

タイポはロード時に検出: 未知のトップレベルフィールド、未知のフラグ名、不正な `kind` はいずれも `ValidationError` を上げてサーバーがトラフィックを受け付ける前に停止します。`default_profile` / `mode_aliases` バリデーションと同じ fast-fail 姿勢です。

#### Doctor — `coderouter doctor --check-model <provider>` (v0.7-B)

「Ollama を立ててルーターを向けたけど、どうも何かがおかしい」は最多の onboarding 失敗です。v0.7-B の `doctor` サブコマンドはこれを一行診断に変えます:

```bash
coderouter doctor --check-model ollama-qwen-coder-14b
```

フォールバックチェーン全体ではなく、**指定したプロバイダ単体**に対し小さな 4 プローブ（各 ≤100 トークン）を走らせ、観測挙動が `providers.yaml` + `model-capabilities.yaml` の現宣言と食い違えば判定表とコピペ可能な YAML パッチを出力します:

```
provider: ollama-qwen-coder-14b  (kind=openai_compat, model=qwen2.5-coder:14b)

probe                     verdict        detail
auth+basic-chat           OK             200 in 1.4s, 18 tokens in / 6 tokens out
tool_calls                NEEDS_TUNING   model emitted a tool_use block but registry says tools=false
thinking                  N/A            kind=openai_compat; thinking probe is anthropic-only
reasoning-leak            OK             no stray `reasoning` field on choice.message

suggested patch for ~/.coderouter/providers.yaml:
  providers:
    - name: ollama-qwen-coder-14b
      capabilities:
        tools: true        # observed: model returned a well-formed tool_use block
```

4 プローブとその存在意義:

- **auth+basic-chat** — 些細な 1 ターン。「API キー未設定」「`base_url` 違い」「プロバイダ到達不能」のクラスを先に捕まえる。失敗した場合、残り 3 プローブは `SKIP` となり症状追いで時間（とトークン）を浪費しない。
- **tool_calls** — ダミー `echo(text: string)` ツール仕様と、それを発火させるはずのプロンプトを送る。意図的に非破壊（上流への副作用なし）。モデルが有効な `tool_use` を吐いたがレジストリが `tools: false` と言っている（あるいは逆）とき `NEEDS_TUNING`。
- **thinking** — Anthropic 限定。`thinking: {type: enabled, budget_tokens: 16}` をネイティブ送出（OpenAI 形状アダプタをバイパス）し、上流がフィールドを受け付けるか確認。ファミリが `model-capabilities.yaml` に未登録ならレジストリパッチ（`providers.yaml` パッチではない）を出す。
- **reasoning-leak** — chat ターンを発行し、v0.5-C strip が走る**前**の上流生ボディを検査。「モデルが `reasoning` を返した」と「アダプタが既に除去済」を区別できる。OpenRouter 無料モデル（`openai/gpt-oss-120b:free` など）で意味がある。

終了コード（CI 投入想定で設計）:

| コード | 意味 |
|------|---------|
| `0`  | 全プローブが宣言ケイパビリティと一致、パッチ不要 |
| `2`  | 1 つ以上の `NEEDS_TUNING`、YAML パッチは出力内 |
| `1`  | プローブ実行不能 — auth 失敗、プロバイダ到達不能、または未知プロバイダ名。前提条件を直して再実行 |

複数シグナル同時発火時の優先度は `1`（ブロッカ）> `2`（チューニング）> `0`（クリーン）。Unix lint 規約（`2` = 自動修復可、`1` = 諦め）と揃えています。1 プローブの失敗で他が抑制されることはなく、auth 短絡時でもスキップされたプローブは `SKIP: upstream auth failed` と明記して透明性を維持します。

このサブコマンドは**プロバイダ 1 つ**を対象とする設計です: doctor のプローブが、同ファミリを共有する他プロバイダに波及するレジストリ glob 変更を提案すべきではないからです。チェーン内の各プロバイダについて `--check-model` を変えて再実行してください。

#### 体感の目安

- **初バイトレイテンシ**: Claude Code は毎ターン全ツール (Bash/Glob/Read/Write/…) を宣言するので、CodeRouter は常に v0.3-D の tool-downgrade 経路（内部非ストリーミング + SSE リプレイ）を使います。体感レイテンシ ≒ 上流のトータル応答時間。
- **M 系 macOS では** qwen2.5-coder:7b が ~30–60s/ターン、14b が ~2 分。主因は Claude Code が毎ターン送る 15–20K トークンのシステムプロンプト prefill で、**CodeRouter のオーバーヘッドではありません**。
- **ツール選択の品質**はモデル側の限界で、ワイア層の問題ではありません。CodeRouter はワイア（テキスト JSON → `tool_use` ブロック）を修復しますが、モデルが**正しいツール**を選んだかは別問題。qwen2.5-coder:14b は `Bash` が正解の場面で `Glob` を選ぶことがあり — 対策はより強いローカルモデル、あるいは `ALLOW_PAID=true` で Claude にフォールスルーさせることです。
- **ミッドストリーム失敗**（Ollama が最初のチャンク後に落ちる等）は単発の `event: error` としてクライアントに届き、リトライはありません — 部分レスポンスは保持され、ストリームはクリーンに閉じます。

予定（v1.0 は [plan.md §10](./plan.md)、v1.0+ は §18）:

- v1.0 — 14 ケースのリグレッションスイート、Code Mode (スリム版 Claude Code ハーネス); 出力クリーニングは **v1.0-A** で `output_filters` チェーンとして完了
- v1.5 — **メトリクスダッシュボード（出荷済み）** — `MetricsCollector` + `GET /metrics.json` + `GET /metrics` (Prometheus) + `GET /dashboard` (HTML 1 ページ) + `coderouter stats` curses TUI + `scripts/demo_traffic.sh` トラフィックジェネレータ + `display_timezone` 設定
- v1.6 — `coderouter doctor --network` (CI 用の明示的ネット許可ラン)、ランチャー（当初 v1.1 に予定されていたが、v1.5 が先行出荷されたため v1.6 に繰り下げ）

## `kind: openai_compat` と `kind: anthropic` の選び方

`providers.yaml` の各プロバイダに `kind` があります。2 択です。どちらを選ぶかでホップを超えて生存するワイアレベル機能と、到達可能なクライアントが変わります。

| 観点 | `kind: openai_compat` | `kind: anthropic` |
|---|---|---|
| `/v1/chat/completions` から到達 | ✅ 変換不要 | ✅ v0.4-A 逆変換経由 |
| `/v1/messages` から到達 | ✅ 変換 + tool-call 修復経由 | ✅ ネイティブパススルー |
| 対象 | llama.cpp, Ollama, OpenRouter, LM Studio, Together, Groq, ... | `api.anthropic.com`、Bedrock の Anthropic シム、Messages ワイアを話す任意サーバー |
| `cache_control` ブロック | ❌ ロスト（OpenAI 側に等価物なし） | ✅ `/v1/messages` 経由で end-to-end 保持 |
| `thinking` ブロック | ❌ ロスト | ✅ `/v1/messages` 経由で保持 |
| 構造化 `tool_use` SSE イベント | 修復から合成 (v0.3-D downgrade) | 上流からパススルー |
| tool-call 修復 (素テキスト JSON → `tool_use`) | ✅ 壊れた JSON を吐くローカルモデル向けに必要 | n/a (Anthropic は壊れた JSON を出さない) |
| `anthropic-beta` ヘッダ転送 (v0.4-D) | n/a | ✅ そのまま |

**判断の目安:**

- **ローカルモデルまたは OpenRouter 無料枠** → `kind: openai_compat`。逆経路は存在しますが、OpenAI ワイアをネイティブに話すプロバイダに対し変換コストを払う理由はありません。
- **公式 API 経由の Claude で、`cache_control` / `thinking` を効かせたい** → `kind: anthropic`、`/v1/messages` 経由（= Claude Code から `ANTHROPIC_BASE_URL=http://localhost:8088`）。`examples/providers.yaml` の `claude-code-direct` プロファイルがこのケース用に事前配線されています。
- **OpenAI クライアントから Claude に到達**（`openai` SDK / curl → `/v1/chat/completions`）→ `kind: anthropic` は引き続き動きます — 基本 chat / tools / vision は v0.4-A 逆経路で生き残ります。ただし OpenAI に等価形状が無いため `cache_control` / `thinking` は送れません。
- **混在チェーン**（ローカル先頭、Claude を有料最終砦に）→ 同プロファイルに両 `kind` を並べます。エンジンの多態ディスパッチが各境界のホップを扱います。

## トラブルシューティング

まず第一に: 失敗中のプロバイダに対して **[`coderouter doctor --check-model <provider>`](#doctor--coderouter-doctor---check-model-provider-v07-b)** を走らせてください。4 プローブを回し、宣言と観測の不一致があればコピペ YAML パッチを出します。`doctor` がクリーンを返すのに問題が続くなら、下のログ読みワークフローにフォールスルー。

v0.4-D 以降、失敗した上流リクエストはサーバーログに**上流レスポンスボディそのもの**を添えて現れます。リクエストが失敗したときは次のような行を探します:

```
{"level": "WARNING", "msg": "provider-failed", "provider": "...",
 "status": 4xx, "retryable": true|false, "error": "[provider status=4xx] 4xx from upstream: {...}"}
```

よくあるパターンと意味:

- **`"Extra inputs are not permitted"` が body フィールドに対して** — 上流（通常 Anthropic）が知らないフィールドを拒否。`anthropic-beta` ヘッダでゲートされているフィールド（`context_management`、新しい `cache_control` / `thinking` variant）なら、クライアントが実際にヘッダを付けたか確認。v0.4-D 以降 CodeRouter はそのまま転送しますが、クライアントが送っていなければ上流に届きません。
- **`"adaptive thinking is not supported on this model"`** — v0.5-A 以降ユーザーには届かないはず。ケイパビリティゲートが `thinking: {type: enabled}` リクエストをそのフィールドを受け付けるモデルに流し（ヒューリスティクス: `claude-opus-4-*` / `claude-sonnet-4-6` / `claude-sonnet-4-7` / `claude-haiku-4-*`）、対応なしチェーンではブロックを剥がします。まだこのエラーを見るなら、(a) チェーンにヒューリスティクス未収載の新 Anthropic ファミリがいる — 当該プロバイダに `capabilities.thinking: true` を明示、あるいは (b) モデル slug を添えて issue を立て、ヒューリスティクスを更新。サーバーログの `capability-degraded` 行でゲート発火を確認。
- **`capability-degraded` ログで `reason: "non-standard-field"` かつ `dropped: ["reasoning"]`** (v0.5-C) — 上流が OpenAI spec 非準拠の `reasoning` フィールドを `message` / `delta` に返した。OpenRouter 無料モデル（特に `openai/gpt-oss-120b:free`）で発生。アダプタが下流に渡す前に剥がすのでこのログは純粋に観測用 — 何も壊れていません。本当に reasoning テキストを素通ししたい（reasoning-aware クライアントを前立てている等）場合は当該プロバイダに `capabilities.reasoning_passthrough: true` を付けると strip が止まります。ストリーミング: いくつチャンクに跨ろうとログは 1 ストリーム最大 1 回。
- **`capability-degraded` ログで `reason: "translation-lossy"` かつ `dropped: ["cache_control"]`** (v0.5-B) — リクエストが `cache_control` マーカー付きだったが、選ばれたプロバイダが `kind: openai_compat` なので Anthropic → OpenAI 変換で消失。エラーではなく（リクエストは成功）、ただ Anthropic のプロンプトキャッシングはそのプロバイダでは効きません。対策は (a) `kind: anthropic` プロバイダをチェーンの前に置く、または (b) 将来 `openai_compat` 上流が cache マーカーを保持するなら `capabilities.prompt_cache: true` で当該ログをオプトアウト。なお Anthropic 側の 1024 トークン最小も注意: これを下回るシステムプロンプトは対応プロバイダでも `cached_tokens: 0` を報告します — 上流の制約で CodeRouter のバグではありません。
- **`rate_limit_error` / 429** — Anthropic 組織レベルの TPM 上限。リトライ可能（エンジンが次プロバイダを試す）。プロファイル順を調整するか、Claude Code のコンテキストを `/compact` で減らす。
- **`unknown profile 'xxx'` (400)** — リクエスト body の `profile` フィールドあるいは `X-CodeRouter-Profile` ヘッダが設定のどの `profiles[].name` とも一致しない。有効名はレスポンス body に。
- **`502 Bad Gateway: all providers failed`** — チェーン全プロバイダがリトライ可能エラーを返した。`provider-failed` ログ行を順に読む。末尾の `error` フィールドがチェーン終端の理由。

ミッドストリーム失敗は SSE ストリーム内で単発の `event: error` / `type: api_error` として出ます（ヘッダは既送出なので 5xx HTTP ステータスは返らない）。これは「どのプロバイダも開始できなかった」（`type: overloaded_error`）とは区別されます。

### Ollama 初心者 — サイレント失敗 5 症状 (v0.7-C)

「新規 Ollama をインストールし、ルーターを向けたらどうもおかしい」は最多の onboarding 失敗です。症状はエラーに見えないことがほとんど — モデルが肩をすくめたように見える。これまで現場で集めた 5 種、各症状の一行診断と修正 YAML を添えます。下の `<provider>` は `providers.yaml` のプロバイダ名（例: `ollama-qwen-coder-7b`）。

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

**v1.0-B** 以降 doctor プローブがこれを直接検出します — 約 5K トークンプロンプトの先頭に canary トークンを埋め込み、エコーを求める。canary が返ってこなければ Ollama が先頭を落とした証拠。プローブは Ollama 形状プロバイダ（base URL にポート 11434、または `extra_body.options.num_ctx` 宣言あり）のみ発火するため、他 `kind: openai_compat` 上流は静かに SKIP。

**2. Claude Code が「ファイルが読めません」と繰り返す。** モデルは `tools` パラメータを受け取ったが混乱し、空のアシスタントメッセージを返した。小さな量子化モデル（≤ 7B、Q4）はツール仕様自体を扱えないことが多い。CodeRouter v0.3-A の tool-call 修復は**壊れた**ツール JSON を復元できますが、このケースは「モデルがそもそもツール呼び出しを試みなかった」 — 修復対象がありません。

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

`tools: false` にすると、ツール要求リクエスト到来時にチェーンは次のプロバイダに進みます。強いモデル（qwen2.5-coder:14b やクラウドフォールバック）と組み合わせて使ってください。

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
# providers.yaml — 入力側オプトアウト（モデルが従うときは安価;
# Qwen3 / R1-distill 系は `/no_think` を尊重する）:
- name: <provider>
  append_system_prompt: "/no_think"
```

`output_filters` はアダプタ境界のバイトストリームに作用するのでどのモデル・どのプロバイダ・どのクライアントでも動作します — コンテンツを 1 回余計に舐める分のコストと引き換え。2 つは重ね掛け可能で、`examples/providers.yaml` のサンプル `ollama-qwen-coder-*` は `output_filters: [strip_thinking]` が有効な状態で出荷されています。

**4. チェーンへの初リクエストが毎回失敗して回復する。** `providers.yaml` の `model` フィールドにタイポがある、または `ollama pull <tag>` を忘れている。Ollama は `404 model not found` を返し、これは retryable 分類（v0.2-x のバグ修正）なのでチェーンはフォールスルーしますが、毎ターン、ローカルティアのレイテンシ優位を失います。

```bash
coderouter doctor --check-model <provider>
# → auth+basic-chat: UNSUPPORTED — 404 from upstream (run `ollama pull <tag>`)
# → (remaining probes SKIP — no point running them until the model exists)
```

対処: `ollama pull <your-yaml-tag>` またはタイポ修正。404 は Ollama が「そのタグの GGUF は載せていない」と言っている。HF-on-Ollama モデル名は `:Q4_K_M` 形式の量子化サフィックス必須で、省略すると同じ 404 になります。

**5. チェーン全プロバイダが一様に失敗する。** `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` が未設定（または期限切れ）で、チェーンの全クラウドプロバイダが順に 401。v0.5.1 A-3 以降 `chain-uniform-auth-failure` WARN が事後的にこのパターンを識別しますが、トラフィック開始前に捕まえるほうが楽です。

```bash
coderouter doctor --check-model <the-cloud-provider>
# → auth+basic-chat: AUTH_FAIL — 401 from upstream (check env var <KEY_NAME>)
# → (remaining probes SKIP — auth dominates)
```

対処: 環境変数を設定、あるいはサーバー起動時にロードされる `.env` に追加 (`cp examples/.env.example .env`)。`coderouter doctor` は動作中のサーバーと同じ env を読むので、シェルからのプローブ成功はサーバーも動くという信頼できるシグナルです。

**全部まとめて走らせる**にはプロバイダごとに `doctor`:

```bash
for p in ollama-qwen-coder-7b ollama-qwen-coder-14b openrouter-free openrouter-gpt-oss-free; do
  coderouter doctor --check-model "$p" || true
done
```

終了コードは 3 バケット（0 クリーン / 2 パッチ可能 / 1 ブロッカ）に集約されるので、上のループは CI に繋げられます — 完全な表は [Doctor サブセクション](#doctor--coderouter-doctor---check-model-provider-v07-b)。

同じローカル Ollama に対して CodeRouter（ルーター層）と [lunacode](https://github.com/zephel01/lunacode)（エディタハーネス）を両方走らせている場合、lunacode の [`docs/MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) が姉妹リファレンスです — 同じ 5 症状を、CodeRouter のプロバイダ粒度宣言が届かないエディタ/ハーネス層（モデル別設定、チャットテンプレート上書き、`/no_think` バリアント）でカバーします。

#### HF-on-Ollama リファレンスプロファイル

Ollama の `hf.co/<user>/<repo>:<quant>` ローダ経由で HF ホスト GGUF を動かすと、5 症状がすべて増幅されます — HF GGUF はチャットテンプレートなしで出荷されることが多く、蒸留元の `<think>` タグを引き継ぎ、症状 4 を踏む `:<quant>` サフィックスが必須です。`examples/providers.yaml` にはコメントアウトされた `ollama-hf-example` スタンザがあり、各つまみ（`extra_body.options.num_ctx`、`append_system_prompt: "/no_think"`、`capabilities.tools: false`、`reasoning_passthrough`）を例示し、インラインコメントで各対応症状を示しています。コピーし、`model:` を pull した HF タグに書き換え、`coderouter doctor --check-model ollama-hf-example` で検証してください。

## 依存ポリシー

厳密 — [`plan.md` §5.4](./plan.md) 参照。ランタイム依存:

| パッケージ | 目的 |
|---------|-----|
| `fastapi` | HTTP ingress |
| `uvicorn` | ASGI サーバー |
| `httpx` | アウトバウンド HTTP（あえて Anthropic/OpenAI SDK を使わない） |
| `pydantic` | スキーマ検証 |
| `pyyaml` | 設定パース |

以上。`litellm` なし、`langchain` なし、`openai`/`anthropic` SDK なし。

## プログラムから例外をキャッチする (v1.0.1)

CodeRouter を組み込んで使う場合 (engine を直接呼ぶ / `coderouter serve` を harness でラップする等)、CodeRouter が内部で raise する全例外は `CodeRouterError` を継承しています。1 つの `except` で全部拾えます：

```python
from coderouter import CodeRouterError

try:
    response = await engine.generate(chat_request)
except CodeRouterError as exc:
    # AdapterError / NoProvidersAvailableError / MidStreamError の全てに該当
    logger.error("coderouter-failed", extra={"reason": str(exc)})
```

leaf 例外は従来の場所 (`AdapterError` は `coderouter.adapters.base`、`NoProvidersAvailableError` と `MidStreamError` は `coderouter.routing.fallback`) に残っているので、既存の `except AdapterError:` のような catch はそのまま動きます。root class は downstream が leaf を個別 import して enumerate しなくて済むよう public API surface を固定するためだけに存在し、今後 leaf が増えても呼び出し側のコードを触る必要がありません。

## Security

シークレットは設定ファイルではなく環境変数に置きます。CI はシークレット
スキャン (`gitleaks`)、多重ソースの依存 CVE 監査 (`pip-audit` +
OSV-Scanner)、lockfile 固定インストールを強制します —
[`docs/security.md`](./docs/security.md) に完全な方針と
報告手順があります。

## License

MIT
