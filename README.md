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
  <a href=""><img src="https://img.shields.io/badge/version-1.9.0-blue" alt="version"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/runtime%20deps-5-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <a href="./README.en.md">English</a> · <strong>日本語</strong> · <a href="./docs/usage-guide.md">利用ガイド</a> · <a href="./docs/security.md">Security</a>
</p>

<p align="center">
  <strong>10 分で動かす →</strong> <a href="./docs/quickstart.md">Quickstart</a>
  ｜ <strong>詳しく →</strong> <a href="./docs/usage-guide.md">利用ガイド</a>
  ｜ <strong>無料で回す →</strong> <a href="./docs/free-tier-guide.md">無料枠ガイド</a>
  ｜ <strong>要るか判定 →</strong> <a href="./docs/when-do-i-need-coderouter.md">要否判定</a>
</p>

<!-- TODO: before/after GIF を docs/assets/before-after-toolcall.gif に配置予定。
     暫定で ダッシュボードのスクショ だけリンク。 -->
<!-- ![Before / After tool calling demo](./docs/assets/before-after-toolcall.gif) -->

**CodeRouter が他に何をやってくれるか**

- `coderouter doctor --check-model <provider>` でそのモデルが tool call / streaming / thinking に対応しているかを**実プローブで即診断**し、足りない宣言をコピペ可能な YAML パッチで教えてくれる
- reasoning leak（`<think>...</think>` タグや `<|turn|>` など 6 種の stop マーカー漏れ）を SSE チャンク境界を跨いで自動スクラブ
- ローカル → 無料クラウド（OpenRouter free / NVIDIA NIM 40 req/min 無料枠）→ 有料 API の自動フォールバック。既定で `ALLOW_PAID=false` なので課金はオプトイン制
- v1.9.0 から **Anthropic prompt cache の hit/miss を全リクエストで記録**、`/dashboard` で hit_rate / saved tokens / USD コストが見える（cache savings は別計算）
- v1.9.0 から **adaptive routing** で「いま遅い provider」を自動降格（profile に `adaptive: true` を付けるだけ）、**tool-loop guard** で stuck loop を検出（`warn` / `inject` / `break` の 3 段階 policy）
- ランタイム依存 5 個（`fastapi` / `uvicorn` / `httpx` / `pydantic` / `pyyaml`）— 純 Python、MIT、テスト 830 本緑

→ **Claude Code / gemini-cli / codex + Ollama / llama.cpp / NVIDIA NIM で、破綻しない local-first agent が組める**

## ドキュメント

| 目的 | ドキュメント | 内容 |
|---|---|---|
| **動かす** | [Quickstart](./docs/quickstart.md) | Claude Code / codex を local Ollama で 10〜15 分で動かす最短手順 |
| **使いこなす** | [利用ガイド](./docs/usage-guide.md) | HW 別モデル選定・チューニング既定値・OS ごとの起動フロー・`doctor` / `verify` の読み方 |
| **無料で回す** | [無料枠ガイド](./docs/free-tier-guide.md) | NVIDIA NIM 40 req/min × OpenRouter 無料枠の使い分け・live 検証済みモデル表・地雷 5 点 |
| **要るか判断する** | [要否判定ガイド](./docs/when-do-i-need-coderouter.md) | エージェント × モデルの詳細マトリクスで「そもそも自分に必要か」を決める |
| **詰まったとき** | [トラブルシューティング](./docs/troubleshooting.md) | `doctor` の使い方、`.env` の export 必須、Ollama サイレント失敗 5 症状、Claude Code 連携の罠 |
| **llama.cpp 直叩き** | [llama.cpp 直叩きガイド](./docs/llamacpp-direct.md) | Qwen3.6 を Ollama 詰みから救出する経路。`llama.cpp` build → Unsloth GGUF → `llama-server` → CodeRouter 接続を 7 step で（v1.8.3 実機検証済）|
| **LM Studio 直接** | [LM Studio 直接ガイド](./docs/lmstudio-direct.md) | `qwen35` / `qwen35moe` を救う第 2 経路。LM Studio 0.4.12+ Local Server 経由で OpenAI 互換 + Anthropic 互換 (`/v1/messages`) 両対応、prompt caching 透過（v1.8.4 実機検証済）|
| **安全に使う** | [セキュリティ方針](./docs/security.md) | 脅威モデル・秘密情報の扱い・脆弱性報告経路 |
| **履歴** | [CHANGELOG](./CHANGELOG.md) | 全リリース履歴（最新: v1.9.0 — Cache observability (A) + Cross-backend cache passthrough (B) + Adaptive routing (C) + Cost-aware dashboard (D) + Tool-loop guard (E) を 1 minor で出荷） |
| **設計を追う** | [plan.md](./plan.md) | 設計不変項・マイルストーン・今後のロードマップ |

English versions: [Quickstart](./docs/quickstart.en.md) · [Usage guide](./docs/usage-guide.en.md) · [Free-tier guide](./docs/free-tier-guide.en.md) · [When you need it](./docs/when-do-i-need-coderouter.en.md) · [Troubleshooting](./docs/troubleshooting.en.md) · [llama.cpp direct](./docs/llamacpp-direct.en.md) · [LM Studio direct](./docs/lmstudio-direct.en.md) · [Security](./docs/security.en.md)

## CodeRouter で何が楽になるか

CodeRouter は、コーディングエージェント（Claude Code / gemini-cli / codex / 素の OpenAI SDK）と、その裏の LLM の間に挟まる小さなルーターです。ツールの向き先を 1 本のエンドポイントにまとめておけば、CodeRouter がプロバイダを順に選びます — まずローカル Ollama / llama.cpp、次に無料クラウド (OpenRouter free)、有料 API は明示的に opt-in したときだけ。

初心者が普通に使うとぶつかる「地雷」を、CodeRouter がまとめて面倒見てくれます:

- **API キー無し・Anthropic 課金無しのまま Claude Code を回せる。** ローカルモデル（または OpenRouter 無料枠）が答えます。有料プロバイダは `ALLOW_PAID=true` を明示したときだけ呼ばれます。
- **返答が途中で消えない。** 1 プロバイダが途中で落ちてもクライアントには綺麗な `event: error` が 1 本届くだけ — 2 モデルを継ぎ接ぎしたフランケン応答にはなりません。
- **うっかり課金しない。** `ALLOW_PAID=false` が既定。有料プロバイダをチェーンから外したときは理由を 1 行ログに出すので、なぜ使われなかったかが後で grep できます。
- **ローカル Ollama の上で Claude Code / gemini-cli / codex が動く。** Claude Code は Anthropic のワイアフォーマット、Ollama / llama.cpp / LM Studio は OpenAI。CodeRouter が双方向に変換し、小さいローカルモデルがテキストで吐いてしまう `{"name":..., "arguments":...}` を tool_use ブロックへ復元してからエージェントに渡します。
- **「なぜか動かない」の原因を教えてくれる。** `coderouter doctor --check-model <provider>` が 6 種類の典型的な失敗モード（コンテキスト切り詰め / ストリーム早期終了 / ツール呼び出し欠落 / reasoning フィールド漏れ / 認証 / Anthropic `thinking`）を実地プローブし、コピペ可能な YAML パッチを出します。
- **監査しやすい。** ランタイム依存 5 個（LiteLLM は 100+）。Pure Python、MIT、テスト 710 本緑。

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

## エコシステム

CodeRouter は **backend ルーター層** として独立して動くため、ローカル LLM を消費する他プロジェクトと組み合わせて使えます。`OPENAI_BASE_URL` (もしくは `OLLAMA_BASE_URL`) を CodeRouter に向けるだけで、相手プロジェクトを無改造で吸収:

- **[Voice Bridge](https://github.com/zephel01/voice-bridge)** — リアルタイム音声翻訳 + AI 音声チャット (ずんだもん / リリンちゃん対応 + Live2D アバター連携)。chat mode の `OLLAMA_BASE_URL` を CodeRouter に向けると、ローカル LLM が不安定でも openrouter free / anthropic-direct に自動 fallback、**ずんだもんが沈黙しなくなる**。

```bash
# 例: Voice Bridge を CodeRouter 経由で動かす
$ coderouter serve --port 8088 --mode coding &
$ export OLLAMA_BASE_URL=http://localhost:8088/v1
$ python main.py --mode chat --vad   # voice-bridge 側
```

CodeRouter / Voice Bridge ともに独立した repo で進化していて、HTTP 経由で疎結合に繋がります。プラグイン化はせず、それぞれが自分の責務に集中する設計です。

## クイックスタート（3 コマンド）

**v1.7.0 で PyPI 公開**、**v1.8.0 で用途別 4 プロファイル + Z.AI/GLM 連携**、**v1.8.2 で doctor probe を thinking モデル対応**、**v1.9.0 で Cache observability / Adaptive routing / Cost-aware dashboard / Tool-loop guard を pillar 化**しました。`uvx` 一発で動きます (Python 3.12 以上必須):

```bash
# 1. サンプル設定を置く
mkdir -p ~/.coderouter
curl -fsSL https://raw.githubusercontent.com/zephel01/CodeRouter/main/examples/providers.yaml \
  > ~/.coderouter/providers.yaml

# 2. uvx で起動 (インストール + 起動が 1 行)
#    PyPI 配布名 (coderouter-cli) と console script 名 (coderouter) が異なるため、
#    uv 0.11+ では --from 形式が必須 (旧 uv でも動く canonical 形式)
uvx --from coderouter-cli coderouter serve --port 8088
```

恒久的にインストールしておきたい場合:

```bash
uv tool install coderouter-cli
coderouter serve --port 8088
```

git clone して開発にも参加したい場合 (中級者向け):

```bash
git clone https://github.com/zephel01/CodeRouter.git
cd CodeRouter
uv sync
uv run coderouter serve --port 8088
```

> **注**: PyPI 上のパッケージ名は `coderouter-cli` ですが、コマンド名と Python import 名は `coderouter` のままです。詳しくは [CHANGELOG `[v1.7.0]`](./CHANGELOG.md#v170--2026-04-25-pypi-公開-uvx-coderouter-cli-一発で動く) 参照。
>
> **`--apply` 自動化を使う場合** (v1.8.0+): `ruamel.yaml` を optional dep として一緒に入れます (`pip install 'coderouter-cli[doctor]'` または `uv pip install ruamel.yaml`)。基本機能には不要です。

あとは任意の OpenAI クライアントを `http://127.0.0.1:8088` に向けるだけです:

```bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ignored",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

`model` フィールドは現状プレースホルダです — ルーティングは `profile` フィールド（`providers.yaml` の `default` がデフォルト）で決まります。

はじめての方は [利用ガイド](./docs/usage-guide.md) を参照してください。ハードウェア別のモデル選定、チューニング既定値、OS ごとの起動フロー、OpenRouter 無料枠とのペア方針を一通り解説しています。(English: [usage guide](./docs/usage-guide.en.md))

**NVIDIA NIM 無料枠（40 req/min）と OpenRouter 無料枠をどう重ねるか**は [無料枠ガイド](./docs/free-tier-guide.md) にまとめてあります。live 検証済みのモデル一覧、`claude-code-nim` プロファイルの設計意図、よくあるハマり所 5 点 込み。(English: [free-tier guide](./docs/free-tier-guide.en.md))

**API キーの管理が気になる方** (1Password / direnv + sops / OS Keychain 連携 + `.env` の安全運用) は v1.6.3 で `coderouter serve --env-file` と `coderouter doctor --check-env` を入れています。詳細は [トラブルシューティング §5](./docs/troubleshooting.md#5-env-のセキュリティ運用-v163-追加)。

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

**テスト 710 本通過。ランタイム依存 5 個。macOS / Linux / Windows WSL2 で動作。** ルーターは日常的な Claude Code 用途で安定しています。v1.0 の総まとめは [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md)。

今日の CodeRouter が届ける価値:

- **どのクライアントもどのプロバイダに橋渡し。** OpenAI 互換クライアントからのリクエストと Claude Code（`/v1/messages` 経由）両方を受け入れ、ストリーミング/非ストリーミングを問わず、ローカル Ollama / OpenRouter 無料 / Anthropic / それらの混在にルーティングします。
- **部分レスポンスを垂れ流さず安全にフォールバック。** 最初のバイト前にプロバイダが失敗したら次を試す。**最初のバイト以降**に失敗したら、クライアントには綺麗な `event: error` が 1 本届くだけ — 2 つのプロバイダを継ぎ接ぎしたフランケン応答は起きません。
- **明示的にオプトインしたときだけ課金。** `ALLOW_PAID=false`（既定）がチェーンから有料プロバイダを外し、ブロック時は 1 行の明確なログを出します。
- **小さいローカルモデルが壊すものを修復。** Qwen / DeepSeek 系がテキストとして吐いた `{"name":..., "arguments":...}` は、Claude Code に届く前に有効な `tool_use` ブロックへ復元されます。
- **何がおかしいかを教えてくれる。** `coderouter doctor --check-model <provider>` が 6 プローブ（認証 / コンテキスト切り詰め / ストリーム中断 / ツール呼び出し能力 / reasoning フィールド漏れ / Anthropic `thinking` 対応）を回し、宣言と実挙動が食い違えばコピペ可能な YAML パッチを出します。
- **reasoning 漏れをスクラブ。** プロバイダに `output_filters: [strip_thinking, strip_stop_markers]` を付ければ `<think>…</think>` と 6 種の stop マーカー variants が SSE チャンク境界を跨いでも安定して剥がれます。
- **Anthropic ネイティブ機能は Anthropic に届いたら保持。** `cache_control` / `thinking` / `anthropic-beta` ヘッダでゲートされる body フィールドは `kind: anthropic` プロバイダでそのまま通り、OpenAI 形状へ落ちる場合のロッシー変換は（沈黙ではなく）ログで可視化されます。

**リリース単位の詳細が欲しい？** v0.x と v1.0-A/B/C の各スライス — 何が入り、何本のテストが増え、なぜ必要だったのか — は [CHANGELOG.md](./CHANGELOG.md) に揃っています。設計の不変項と今後のロードマップは [plan.md](./plan.md)。

**次の予定**（v1.0 は [plan.md §10](./plan.md)、v1.0+ は §18）: v1.5 ✅ メトリクス / `/dashboard` / `coderouter stats` TUI / `scripts/demo_traffic.sh`、v1.6 ✅ `auto_router` (task-aware routing) + NVIDIA NIM 無料枠 + トラブルシュートドキュメント分離 + `--env-file` / `doctor --check-env`、v1.7 ✅ PyPI 公開 (`uvx --from coderouter-cli coderouter`)、v1.8 ✅ 用途別 4 プロファイル (multi/coding/general/reasoning) + Gemma 4 / Qwen3.6 / Z.AI (GLM) 登録 + `setup.sh` onboarding ウィザード + `coderouter doctor --check-model --apply` (非破壊 YAML 書き戻し) + `claude_code_suitability` startup チェック + Trusted Publishing 自動化。残り (v1.9 候補) は `coderouter doctor --network` (CI 用) / launcher スクリプト / 起動時アップデートチェック (opt-in)。

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

- v1.0 ✅ — 14 ケースのリグレッションスイート、Code Mode (スリム版 Claude Code ハーネス); 出力クリーニングは **v1.0-A** で `output_filters` チェーンとして完了
- v1.5 ✅ — **メトリクスダッシュボード（出荷済み）** — `MetricsCollector` + `GET /metrics.json` + `GET /metrics` (Prometheus) + `GET /dashboard` (HTML 1 ページ) + `coderouter stats` curses TUI + `scripts/demo_traffic.sh` トラフィックジェネレータ + `display_timezone` 設定
- v1.6 ✅ — `auto_router` (task-aware routing、`default_profile: auto` で画像/コード濃度/その他を自動振り分け) + NVIDIA NIM 無料枠 8 段チェーン + ドキュメント言語スワップ (JA primary) + トラブルシュート独立ドキュメント + `--env-file` / `doctor --check-env`
- v1.7 ✅ — PyPI 公開 (`uvx --from coderouter-cli coderouter` で 1 行起動) + Trusted Publishing 経路 (release.yml で自動 publish)
- v1.8 ✅ — **用途別 4 プロファイル + GLM/Gemma 4/Qwen3.6 公式化 + apply 自動化**: `multi` (default) / `coding` / `general` / `reasoning` の 4 プロファイル + 全プロファイルに `append_system_prompt` で Claude 風応答 nudge + `mode_aliases` (default/fast/vision/think/cheap)、Ollama 公式 tag 化された `gemma4:e4b/26b/31b` / `qwen3.6:27b/35b` を active stanza に格上げ、Z.AI を OpenAI-compat で 2 base_url 提供 (Coding Plan / General API)、`coderouter doctor --check-model --apply` で YAML パッチを非破壊書き戻し (`ruamel.yaml` round-trip でコメント・key 順序保持、冪等)、`setup.sh` onboarding ウィザード、`claude_code_suitability` startup チェック (Llama-3.3-70B 系を `claude-code-*` profile で WARN)。残り (v1.9 以降): `coderouter doctor --network` (CI 用)、launcher スクリプト (`.command` / `.sh` / `.bat`)、opt-in 起動時アップデートチェック

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

> **詳細は独立ドキュメント [`docs/troubleshooting.md`](./docs/troubleshooting.md) (v1.6.2 で分離)** を参照してください。
> 本節は 30 秒で済む早見表。

**まず第一に**: 失敗中のプロバイダに対して **[`coderouter doctor --check-model <provider>`](#doctor--coderouter-doctor---check-model-provider-v07-b)** を走らせてください。6 probe を回し、宣言と観測の不一致があればコピペ YAML パッチを出します。

**症状別の入口** (詳細はリンク先):

- 起動して上流に 401: `Header of type authorization was missing` → [§1 起動・設定の罠](./docs/troubleshooting.md#1-起動設定で踏みやすい-5-つの罠-v162-追加) (`.env` の `export` 必須、`coderouter serve --mode <profile>` の正しい使い方)
- ログに `provider-failed` / `capability-degraded` / `chain-uniform-auth-failure` → [§2 ログの読み方](./docs/troubleshooting.md#2-ログの読み方とよくあるパターン)
- Ollama に向けたら無音 / `<think>` タグ漏れ / 「ファイルが読めません」 → [§3 Ollama 5 症状](./docs/troubleshooting.md#3-ollama-初心者--サイレント失敗-5-症状-v07-c)
- Claude Code 上で挨拶がツール呼び出しに化ける / `UserPromptSubmit hook error` → [§4 Claude Code 連携の罠](./docs/troubleshooting.md#4-claude-code-連携で踏みやすい罠-v162-追加)

ダッシュボード `http://localhost:8088/dashboard` を別タブで開いておくと、ほとんどの罠が**目で見て 10 秒で**特定できます。

ミッドストリーム失敗は SSE ストリーム内で単発の `event: error` / `type: api_error` として出ます (ヘッダは既送出なので 5xx HTTP ステータスは返らない)。これは「どのプロバイダも開始できなかった」 (`type: overloaded_error`) とは区別されます。

<!-- 旧アンカーの後方互換 — 古い記事 / 検索結果からのリンクが切れないように -->
<a id="ollama-初心者--サイレント失敗-5-症状-v07-c"></a>
<a id="ollama-beginner--5-silent-fail-symptoms-v07-c"></a>

### Ollama 初心者 — サイレント失敗 5 症状

詳細は [`docs/troubleshooting.md` §3](./docs/troubleshooting.md#3-ollama-初心者--サイレント失敗-5-症状-v07-c) に移動しました。

- 症状 1: 200 が返るのに返信が空/意味不明 → `num_ctx` 既定 2048 に切り詰められた
- 症状 2: 「ファイルが読めません」を繰り返す → 小さなモデルが tools 仕様を扱えていない (`tools: false` 宣言)
- 症状 3: `<think>...</think>` が漏れる → `output_filters: [strip_thinking]`
- 症状 4: 初回リクエストが毎回失敗 → `ollama pull <tag>` 忘れ / `model:` タイポ
- 症状 5: 全プロバイダが一様に失敗 → クラウド API キー未 export ([§1-2 / §1-3](./docs/troubleshooting.md#1-2-env-には-export-が必須))

各症状の `coderouter doctor` 出力例とコピペ可能 YAML パッチは [docs/troubleshooting.md](./docs/troubleshooting.md) に。HF-on-Ollama 構成 / lunacode との関係も同じドキュメントに集約。

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
