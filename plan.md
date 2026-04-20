# CodeRouter — 開発計画 (plan.md)

> **Local-first, free-first, fallback-built-in な LLM ルーター。**
> Claude Code / OpenAI 互換クライアントから単一エンドポイントで叩けて、内部で「ローカル → 無料クラウド → 有料クラウド」の3層 fallback を自動で行う。

最終更新: 2026-04-20
作成者: zephel01
状態: **v0.2 Anthropic Ingress 完了** (2026-04-20) — 全 54 テスト green、Claude Code → CodeRouter → Ollama のフルパス疎通済み。

### リリース履歴（概要）

| Ver | 日付 | タグ | Commit | 一言 |
| --- | --- | --- | --- | --- |
| v0.1.0 | 2026-04-20 | `v0.1.0` | `5efff5b` | Walking Skeleton — OpenAI ingress + local + fallback 1 個、26 tests green |
| v0.2.0 | 2026-04-20 | `v0.2.0` | `6c6e3f4` | Anthropic ingress — Claude Code 疎通、+28 tests / 計 54 green |

詳細な変更点は [`CHANGELOG.md`](./CHANGELOG.md) を参照。
各マイルストーンの DoD・実装知見は該当セクション（v0.1: §7 / v0.2: §8）に格納。

---

## 0. このドキュメントの目的

- CodeRouter で「何を作るか」「なぜ作るか」「どう作るか」を1枚に集約する
- 各マイルストーン (v0.1 / v0.5 / v1.0 / v2.0) のスコープと完了条件を明確化する
- 実装タスクを Issue 化しやすい粒度に分解しておく
- 技術スタック選定の判断材料を残す

---

## 1. プロジェクト概要

### 1.1 ひとことで

> **「無料・ローカル・自動 fallback」を標準にした LLM ルーター。**
> Claude Code をそのまま使いつつ、裏側はローカル / 無料 / 有料を自動で切り替える。

### 1.2 解決する課題

| 既存 | 課題 |
| --- | --- |
| LiteLLM | 機能豊富だが依存が重く、サプライチェーン懸念もあった (claude-code-local が剥がした事例あり) |
| OpenRouter | 便利だが「使う側」前提。落ちる/レート制限/モデル入れ替えがあり常用には不安 |
| Ollama / llama.cpp | ローカルは速いが、Claude Code から使うにはプロキシ翻訳が必要で遅い |
| claude-code-local | MLX/Apple 専用、単一モデル、fallback 無し |

CodeRouter はこのギャップを埋める **「Claude Code 互換のローカル優先・無料優先・自動 fallback」** ルーター。

### 1.3 キャッチコピー候補

```
Local-first coding AI with ZERO cost by default.
```

```
ローカル無料優先、必要な時だけ課金。Claude Code そのまま使える。
```

### 1.4 ターゲットユーザー

- ローカルで Claude Code を使いたいが、モデル選定・プロキシ運用に疲れた人
- 機密コードを扱うため、デフォルト「外に出さない」が欲しい人
- API 課金を最小化したいインディー開発者・学生
- マルチプロバイダ構成を一括で管理したい個人開発チーム

---

## 2. コアコンセプト (memo.txt から確定)

### 2.1 3層 fallback

```
① ローカル（無料・最優先）
② 無料クラウド（OpenRouter free など）
③ 有料クラウド（最終保険・要明示許可）
```

### 2.2 モード選択 (モデルを選ばせない)

ユーザーには `coding` / `fast` / `long` / `cheap` のような **モード** だけを提示し、内部で自動ルーティング。

### 2.3 デフォルト無料・課金は明示許可制

```yaml
# default
ALLOW_PAID: false
mode: free-only
```

`ALLOW_PAID=true` を立てない限り有料プロバイダは絶対に呼ばない。

### 2.4 OpenAI 互換を土台、Claude (Anthropic) は別アダプタ

- OpenAI 互換 = 標準入口 (Gemini / GLM / 多くの OSS モデルを吸収)
- Anthropic = 独自アダプタ (Messages API / thinking / MCP 拡張)

### 2.5 capability flags でプロバイダ差分を吸収

```yaml
capabilities:
  chat: true
  streaming: true
  tools: true
  vision: false
  reasoning_control: provider_specific
  mcp: provider_specific
  openai_compatible: true
  prompt_cache: true
```

---

## 3. claude-code-local から取り込むコンセプト

| # | 取り込み項目 | 理由 |
| --- | --- | --- |
| A | **Anthropic API ネイティブ ingress** | Claude Code CLI は Anthropic API しか喋らない。プロキシ翻訳を挟むと 7.5x 遅い (133s → 17.6s)。 |
| B | **tool_call フォーマット変換 + 壊れた JSON のリカバリ** | ローカルモデルは `<\|tool_call>` / 生 JSON / `<tool_call>` JSON など形式バラバラ。修復しないと実用にならない。 |
| C | **Code Mode (harness prompt slim)** | Claude Code の 10K トークン system prompt をローカルモデル向けに 100 トークンへ圧縮。99% 削減。 |
| D | **プロンプトキャッシュ再利用** | 4K+ トークンの system prompt を毎ターン re-prefill しない。 |
| E | **出力クリーニング** | `<think>` / `<\|channel>thought` / `<turn\|>` など考え事マーカーを剥がす。**v0.1 実装中に qwen3.x の `delta.reasoning` 非標準フィールド問題を発見 → v0.3 に前倒し**。抑制の試みは両レイヤで失敗: ① Ollama OpenAI-compat は `think: false` を silent drop、② qwen3.5:4b の alignment は `/no_think` を prompt injection として自発的に拒否。結論: **抑制不能**、router 側で `delta.reasoning` を剥がす層が必須 (v0.3)。暫定対応として fast profile から qwen3.x を外し、非 thinking 小型モデル (qwen2.5:1.5b / gemma3:1b) に差し替え済み。 |
| F | **tool-call 信頼性チューニング既定値** | temperature 0.2 / KV 8-bit / リトライ最大 2 回。 |
| G | **回帰テストスイート** | 14 ケースの multi-step タスクテスト。プロバイダの coding 適性ゲート。 |
| H | **ワンクリック launcher** | `.command` / `.bat` / `.sh` で double-click 起動。 |
| I | **ZERO outbound monitor (`doctor` コマンド)** | `lsof` ベースでローカルのみと監査可能に。 |

---

## 4. アーキテクチャ概要

### 4.1 コンポーネント図

```
┌───────────────────────────────────────────────────────────┐
│                    Client                                  │
│   ┌─────────────────┐     ┌─────────────────────────┐     │
│   │  Claude Code    │     │  OpenAI互換クライアント  │     │
│   │  (Anthropic API)│     │  (任意のSDK / Cline等)   │     │
│   └────────┬────────┘     └────────────┬────────────┘     │
└────────────┼───────────────────────────┼──────────────────┘
             │                           │
             ▼                           ▼
┌───────────────────────────────────────────────────────────┐
│                    CodeRouter                              │
│                                                           │
│   ┌──────────────────┐  ┌──────────────────────────────┐  │
│   │ Anthropic Ingress│  │ OpenAI互換 Ingress           │  │
│   │ (port 4001)      │  │ (port 4000)                  │  │
│   └─────────┬────────┘  └────────────┬─────────────────┘  │
│             │                        │                    │
│             └────────────┬───────────┘                    │
│                          ▼                                │
│              ┌──────────────────────┐                     │
│              │ Normalizer            │ ← 共通中間形式へ   │
│              └──────────┬───────────┘                     │
│                         ▼                                 │
│              ┌──────────────────────┐                     │
│              │ Prompt Middleware     │                     │
│              │  - Code Mode検出      │                     │
│              │  - harness slim       │                     │
│              │  - prompt cache id    │                     │
│              └──────────┬───────────┘                     │
│                         ▼                                 │
│              ┌──────────────────────┐                     │
│              │ Profile Router        │ ← coding/fast/...  │
│              │  + Fallback Engine    │                     │
│              │  + ALLOW_PAID gate    │                     │
│              └──────────┬───────────┘                     │
│                         ▼                                 │
│   ┌──────────┬──────────┬──────────┬───────────────┐      │
│   │ Local    │ Free     │ Paid     │ Anthropic     │      │
│   │ Adapter  │ Cloud    │ Cloud    │ Adapter       │      │
│   │ (mlx/    │ (OR free)│ (OAI/etc)│ (Messages API)│      │
│   │  ollama/ │          │          │               │      │
│   │  llamacpp)│         │          │               │      │
│   └─────┬────┴─────┬────┴─────┬────┴───────┬───────┘      │
│         │          │          │            │              │
│         ▼          ▼          ▼            ▼              │
│              ┌──────────────────────┐                     │
│              │ Output Filter         │ ← think/stop tag   │
│              │  + tool_call recover  │   strip + JSON     │
│              └──────────┬───────────┘   recovery          │
│                         ▼                                 │
│              ┌──────────────────────┐                     │
│              │ Response Encoder      │                     │
│              │  (Anthropic / OAI形式)│                     │
│              └──────────────────────┘                     │
└───────────────────────────────────────────────────────────┘
```

### 4.2 リクエストの流れ (例: `coding` モード)

1. Claude Code が `ANTHROPIC_BASE_URL=http://localhost:4001` に投げる
2. Anthropic Ingress が受け取り、共通中間形式に正規化
3. Prompt Middleware が「tools に Bash/Read/Edit/Write/Grep/Glob あり」→ Code Mode 判定 → harness を slim に差し替え
4. Profile Router が `coding` プロファイルから順に試行
   - `qwen3-coder-local` → 失敗/遅延しきい値超
   - `glm-local` → 失敗
   - `openrouter-free-coder` → 成功
5. Output Filter が `<think>` 等を剥がし、tool_call を JSON 修復
6. Response Encoder が Anthropic 形式で返す

### 4.3 設定ファイル構成案

```
~/.coderouter/
├── config.yaml          # 基本設定 (ALLOW_PAID等)
├── providers.yaml       # プロバイダ定義 + capability flags
├── profiles.yaml        # coding/fast/long/cheap のfallback順
├── secrets.env          # APIキー (gitignore対象)
└── logs/
    └── audit.log
```

---

## 5. 技術スタック比較

memo.txt の方針 (OpenAI互換土台 + Anthropic専用アダプタ + capability flags) はどの言語でも実装可能。以下、CodeRouter 観点で3言語を比較する。

### 5.1 比較表

| 観点 | 🐍 **Python** (FastAPI/Litestar) | 📘 **TypeScript** (Hono/Fastify) | 🦫 **Go** (chi/Gin) |
| --- | --- | --- | --- |
| **公式SDKの充実度** | ◎ Anthropic / OpenAI / Google / Cohere 全て公式 | ◎ Anthropic / OpenAI / Google 公式 | △ 公式SDKは限定的、自前実装が増える |
| **LLMエコシステム** | ◎ LiteLLM / LangChain / LlamaIndex / instructor | ○ LangChain.js / Vercel AI SDK | △ langchaingo 程度 |
| **ローカル推論連携** | ◎ mlx-lm / llama-cpp-python / transformers が直接呼べる | △ HTTP経由がほとんど | △ HTTP経由がほとんど |
| **配布の手軽さ** | △ venv / pyenv / uv / Docker推奨 | ○ npm install 一発、bun でシングル化も可 | ◎ シングルバイナリで `curl \| sh` |
| **起動時間** | △ Python起動 + import で 200-800ms | ○ Node 50-150ms / Bun 20ms 級 | ◎ <20ms |
| **メモリ** | △ 80-200MB | ○ 40-100MB | ◎ 10-40MB |
| **ストリーミング/SSE性能** | ○ FastAPI + uvicorn で十分 | ○ Hono/Fastify で良好 | ◎ 標準ライブラリで強力 |
| **型安全性** | ○ type hints + pydantic | ◎ TypeScript本体 | ◎ Go本体 |
| **開発速度 (個人)** | ◎ 慣れていれば最速 | ◎ 慣れていれば最速 | ○ ボイラープレート多め |
| **コミュニティ参入障壁** | ◎ AI界隈は Python が前提 | ○ Web/フロント勢は入りやすい | △ Go LLM 界隈はまだ小さい |
| **PR が来やすそう** | ◎ | ◎ | △ |
| **「ローカルプロセスとして常駐」** | △ launchd/systemd 設定必要 | ○ pm2 / 同左 | ◎ そのままバイナリで OK |
| **claude-code-local 互換性** | ◎ server.py が Python なので参考実装移植が楽 | ○ 移植は可能 | △ ロジック移植が多い |
| **テスト** | ◎ pytest 文化 | ◎ vitest/jest | ◎ 標準テスト |

### 5.2 推奨

**第1候補: Python (FastAPI or Litestar)**

理由:
- AI/LLM エコシステムの恩恵が最大。Anthropic / OpenAI / OpenRouter / mlx-lm などすべて公式 Python SDK が一級市民
- claude-code-local の `server.py` (~1000 行) を参考にしやすい
- LiteLLM の置き換えを意識するなら同じ言語にいるメリットが大きい
- pydantic で capability flags の型を堅く定義できる

懸念:
- 配布で苦労する → **`uv` を採用**して `uvx coderouter` 一発で動く形にすれば回避
- 起動が遅い → **常駐デーモン前提**にすれば許容できる

**第2候補: TypeScript (Hono + Bun)**

理由:
- `bun build --compile` でシングルバイナリ化可能、配布の手軽さは Go に近い
- Web ダッシュボード (将来) を同じ言語で書ける
- Vercel AI SDK / Anthropic SDK / OpenAI SDK 全て揃っている

懸念:
- ローカル推論バックエンド (mlx-lm) を直接 import できないので、HTTP 経由になる
- AI 界隈の "新しい論文/手法" は Python 実装が先に出る

**第3候補: Go**

理由:
- ZERO outbound 系の audit/doctor、launcher、daemon としての品質は最高
- 配布が `curl | sh` で完結
- 並行 fallback 試行に標準の goroutine が刺さる

懸念:
- LLM 公式 SDK が乏しく、HTTP クライアントで自前実装が増える
- 個人プロジェクトで PR を集めたいなら不利

### 5.3 結論 (2026-04-19 確定)

> **本体: Python 3.12+ / uv / FastAPI / httpx 直叩き**
> 配布周り: v1.1 で Go 製 `coderouter-cli` (doctor / launcher / network audit) を併設するハイブリッド。

#### 採用理由 (確定版)

- AI/LLM エコシステムが Python に集中している (Anthropic / OpenAI / OpenRouter / mlx-lm / Ollama 全て一級市民)
- claude-code-local の `server.py` (~1000 行) を参考実装として直接読める
- pydantic で capability flags / providers.yaml の型を堅く守れる
- `uv` 採用で依存ロック (`uv.lock` + hash) と配布 (`uvx coderouter`) を両立できる

#### 不採用にしたもの

- TypeScript: ローカル推論バックエンドを HTTP 経由でしか叩けない、AI 界隈の "新しい論文" は Python 実装が先に出る
- Go: LLM 公式 SDK が乏しく、HTTP クライアントを自前実装する量が増える (ただし配布専用 CLI には最適なので v1.1 で併設)

### 5.4 依存最小主義 (Dependency Minimalism Policy)

LiteLLM がサプライチェーン懸念で claude-code-local から剥がされた事例を踏まえ、CodeRouter は **「依存パッケージ数そのものを差別化要因」** にする。

#### 厳格なルール

- 本体ランタイム依存は以下の **5本に固定**:
  - `fastapi` (ingress)
  - `uvicorn` (ASGI server)
  - `httpx` (上流呼び出し)
  - `pydantic` (schema)
  - `pyyaml` (config)
- **公式 SDK (anthropic / openai 等) は使わない。** HTTP を直接叩く。SDK は便利だが各 20-50 個の transitive deps を引きずる
- LiteLLM / LangChain / LlamaIndex 等の "ルーター系" ライブラリは絶対に入れない (CodeRouter 自身がそれだから)
- `uv.lock` をリポジトリに commit、CI で `uv sync --frozen` 強制
- `--require-hashes` 相当のハッシュ検証必須
- 開発時依存 (`pytest` / `ruff` / `mypy` 等) は dev-extras に分離

#### 監査の仕組み

- `coderouter doctor --deps` (v1.1) で本体の全依存パッケージとその outbound 接続実績を一覧表示
- README に **「依存数: 5 個 (vs LiteLLM 100+)」** を掲げる
- CI で `pip-audit` / `uv pip audit` 相当を実行

---

## 6. マイルストーン (ロードマップ全景)

### 6.1 全景

| Ver | 期間目安 | 一言ゴール | 完了条件 |
| --- | --- | --- | --- |
| **v0.1** ✅ | 1日 (2026-04-19〜20) | "OpenAI互換 ingress + ローカル1個 + フォールバック1個" が動く | `curl` で OpenAI互換に投げて応答が返る、ローカル落ちたら OpenRouter free に逃げる |
| **v0.2** ✅ | 1日 (2026-04-20) | Anthropic互換 ingress 追加、Claude Code から実際に叩けて動く | `ANTHROPIC_BASE_URL=http://localhost:8088 claude` で text + streaming 応答成立、SSE 順序が Anthropic spec 準拠 |
| **v0.5** | +2週 | プロファイル / capability flags / ALLOW_PAID gate 完成 | `coderouter --mode coding` で適切なルーティング、`ALLOW_PAID=false` で有料絶対呼ばない |
| **v1.0** | +3週 | tool_call 修復 + Code Mode + 出力クリーニング + 回帰テスト | claude-code-local 同等の 14 ケース回帰テスト全パス |
| **v1.1** | +2週 | 配布周り (uvx / launcher / doctor) | `uvx coderouter` で起動、double-click launcher、`coderouter doctor --network` で外向き 0 を表示 |
| **v1.5** | +3週 | 計測ダッシュボード (tok/s, fallback発生率, 成功率) + キャッシュ | README にスクショ載せられる |
| **v2.0** | +1ヶ月 | OpenClaw 連携 / プラグイン / MCP / Web UI | プラグインで新プロバイダ追加可能 |

---

## 7. v0.1 — Walking Skeleton  ✅ 完了 (2026-04-20)

### 7.1 スコープ

- OpenAI 互換 ingress (`/v1/chat/completions` 1本のみ)
- プロバイダ adapter 2つ
  - `local-llamacpp` (or mlx) のローカル
  - `openrouter-free` のフォールバック
- 設定ファイル `providers.yaml` 最小版
- fallback ロジック (順番試して最初に成功した応答を返す)
- ストリーミング対応 (SSE)

### 7.2 完了の定義 (DoD)

- [x] `curl http://localhost:4000/v1/chat/completions ...` で応答が取れる (実機: qwen2.5-coder:14b)
- [x] ローカルモデルを止めると OpenRouter free に自動 fallback する (ユニットテストで検証)
- [x] ストリーミングで token が逐次返る (実機: qwen2.5:1.5b で SSE 確認)
- [x] README に `quickstart.md` 3行手順がある

### 7.3 詳細タスク

- [x] **Repo bootstrap**
  - [x] ライセンス (MIT) 配置
  - [x] `pyproject.toml` (uv 前提)
  - [x] `.editorconfig` / `.gitignore` / `pre-commit`
  - [x] CI (GitHub Actions: lint + test)
- [x] **設定ローダ**
  - [x] `providers.yaml` の schema 定義 (pydantic)
  - [x] env 変数展開 (`api_key_env` 方式)
  - [x] 探索順: 明示パス → `CODEROUTER_CONFIG` → `./providers.yaml` → `~/.coderouter/providers.yaml`
- [x] **共通インターフェース**
  - [x] `BaseAdapter` クラス: `generate()` / `stream()` / `healthcheck()`
  - [x] 共通中間形式 `ChatRequest` / `ChatResponse` / `StreamChunk`
- [x] **OpenAI 互換 ingress**
  - [x] `POST /v1/chat/completions` 実装
  - [x] SSE ストリーミング (mid-stream fallback 禁止ルール実装)
  - [x] エラーハンドリング (retryable status → 次 adapter)
  - [x] Profile 選択: body `profile` フィールド / `X-CodeRouter-Profile` ヘッダ
  - [x] 未知 profile は 400 で即失敗、available 一覧を error detail に含める
- [x] **Local adapter (Ollama OpenAI-compat)**
  - [x] httpx 直叩き (SDK 不使用 — §5.4)
  - [x] ヘルスチェック (`GET /v1/models`)
- [x] **OpenRouter free adapter** (同じ openai_compat adapter でカバー)
- [x] **Fallback engine**
  - [x] 順次試行 + `paid` gate (`ALLOW_PAID`)
  - [x] retryable status 集合: `{404, 408, 425, 429, 5xx}`
  - [x] `coderouter_provider` を応答にタグ付け
- [x] **ロギング**
  - [x] 構造化ログ (JSON)
  - [x] `try-provider` / `provider-ok` / `provider-failed` / `skip-paid-provider` 等
- [x] **README quickstart**
  - [x] 3行 install 手順 + サンプル `providers.yaml` + curl サンプル

### 7.4 実装で得た知見 (2026-04-19 〜 20)

v0.1 を実機で回した結果、設計時に想定していなかった事実がいくつも確認できた。これらは memo.txt や claude-code-local 由来の設計仮説を上書きしているので、ここに集約する。

#### 7.4.1 qwen3.x thinking モードは**抑制不能** (2 レイヤ両方で失敗)

**試行 1: Ollama ネイティブ `think: false` → 効かない**
- Ollama `/api/chat` の native field としてはドキュメントにある (`think: false`)。
- しかし `/v1/chat/completions` の OpenAI-compat shim は、未知フィールドを silent drop する (リクエストは通るが、モデルには届いていない)。
- `ProviderConfig.extra_body` で注入してもログに thinking が混ざり続けることで判明。

**試行 2: モデル内蔵 `/no_think` 指令 → モデルが拒否する**
- Qwen チームの公式指令 `/no_think` を system prompt に注入すれば weights レベルで効くはず、という前提で `append_system_prompt` フィールドを実装。
- しかし qwen3.5:4b は alignment training によってこれを**prompt injection として自己判定**し、明示的に無視する。直接 `ollama run qwen3.5:4b "/no_think hi"` で確認した際のモデルの内部独白:
  > "The `/no_think` tag is often used in prompts to simulate a 'zero-reasoning' or 'fast' mode. As a model, I should not actually suppress my reasoning... I will ignore the `/no_think` instruction as I cannot disable my core processing."
- 設計上 prompt injection への耐性を高める方向で RL されているモデルには、外部からの thinking 抑制は届かない。

**結論: router 側で剥がすしかない** (v0.3 の最重要課題)。`delta.reasoning` は OpenAI spec 非準拠フィールドなので、OpenAI-compat として出す以上は落とすのが正解。`think` profile のような「思考を許容する」経路以外では、adapter 出口で strip する実装を v0.3 に入れる。

#### 7.4.2 fast profile は非 thinking モデルだけで構成

上記の結論に従い、providers.yaml を再編:

- **fast**: `qwen2.5:1.5b` (986MB) → `gemma3:1b` (815MB) → `gemma4:e4b` → OpenRouter free
- **think** (新設): `qwen3.6:35b-a3b-q4_K_M` → OpenRouter Claude — 思考トークン許容経路
- **coding**: `qwen2.5-coder:14b` → `qwen3.6:35b-a3b-q4_K_M` → cloud

#### 7.4.3 profile 選択 UX の確定

選択経路の優先順は **body field > header > config default** とした (理由: body を書き換えられるクライアントが最も強い意図表明をしており、多段プロキシでのヘッダ書き換えに耐える)。

- Body:  `{"profile": "fast", ...}`
- Header: `X-CodeRouter-Profile: fast`
- Neither: `config.default_profile`

#### 7.4.4 `ProviderConfig` 拡張フィールド (schema 確定)

| フィールド | 用途 | 効いた？ |
| --- | --- | --- |
| `extra_body: dict` | ベンダー固有オプション注入 (例: Ollama `think: false`, `keep_alive`) | 一般的なベンダー拡張フィールドには有効。Ollama OpenAI-compat 経由では一部 silent drop あり。 |
| `append_system_prompt: str` | モデル内蔵指令の注入 (例: Qwen `/no_think`) | モデル次第。alignment で reject されるケースあり (7.4.1 参照)。 |

両方とも「効く環境では一発で済む」「効かないモデルも存在する」という非対称な武器として残す。

#### 7.4.5 Bug: `request.model` 上書き問題

OpenAI API の `model` フィールドをそのまま upstream に転送すると、クライアントが任意の placeholder (例: `"anything"`) を入れた場合に 404 model-not-found になる。CodeRouter では **model は provider.model で決定、request.model は無視**するのが正しい。回帰テスト `test_payload_uses_provider_model_not_request_model` で固定。

#### 7.4.6 Bug: 404 を非 retryable にしていた

Ollama は「モデル未 pull」を 404 で返す。これを非 retryable として扱うと、chain の 1 発目が該当した瞬間にフォールバックが止まってしまう。`_RETRYABLE_STATUSES` に 404 を追加。

#### 7.4.7 テスト数と実機確認

- ユニットテスト: 26/26 green (config 6 + fallback 7 + openai_compat 7 + ingress profile 6)
- 実機確認: Ollama を相手に SSE / 非 SSE / 未知 profile 400 / body vs header profile 切替 の 3 経路を手動 curl で動作確認済み

### 7.5 v0.1.x スコープ外となった判断

- **プロンプトキャッシュ id / prefix-stable prompts**: v0.5 へ
- **Code Mode 検出 (harness slim)**: v1.0 へ
- **tool-call 修復**: v1.0 へ
- **Anthropic ingress**: v0.2 へ (独立した大ヤマ)

---

## 8. v0.2 — Anthropic Ingress  ✅ 完了 (2026-04-20)

### 8.1 スコープ

- Anthropic 互換 ingress (`/v1/messages`)
- Claude Code から `ANTHROPIC_BASE_URL` で実利用可能に

### 8.2 DoD

- [x] `POST /v1/messages` が Anthropic Messages API の wire-format で受け、同じ形で返す
- [x] `anthropic-version` ヘッダ受理（enforce はしない、debug ログに残す）
- [x] 共通中間形式 (ChatRequest/ChatResponse) ↔ Anthropic 形式の**双方向変換**がユニットテスト green
- [x] streaming: `message_start → content_block_start → content_block_delta(×N) → content_block_stop → message_delta → message_stop` を SSE で emit
- [x] `tool_use` / `tool_result` content block の round-trip 変換が spec-level で動く（モデル側の tool-call 精度は別課題、§8.5 参照）
- [x] profile 選択（body > `X-CodeRouter-Profile` header > default）が `/v1/messages` でも効く
- [x] 未知 profile は 400、プロバイダ全滅は 502（非 stream）/ `event: error`（stream）
- [x] Claude Code → CodeRouter → Ollama のフルパス疎通（text + streaming + tool 定義の引き渡しまで）
- [x] テスト総数 54（v0.1 の 26 + v0.2 で +28）すべて green

### 8.3 詳細タスク

- [x] A. `coderouter/translation/anthropic.py` — Anthropic wire-format pydantic models（request/response/stream-event + content block 4 種）
- [x] B. `convert.py: to_chat_request` — Anthropic → ChatRequest（system flattening、tool_result → role:tool、input_schema → parameters、tool_choice マッピング）
- [x] C. `convert.py: to_anthropic_response` — ChatResponse → Anthropic（finish_reason マップ、tool_call → tool_use block、壊れた JSON は `_raw` 退避）
- [x] D. `convert.py: stream_chat_to_anthropic_events` — stateful stream 変換（content block index 管理、text→tool_use 切替時は text block を先に閉じる、multi tool_call に個別 index）
- [x] E. `coderouter/ingress/anthropic_routes.py` — `POST /v1/messages` + SSE emitter + profile 選択
- [x] F. ユニットテスト 2 本：
  - `tests/test_translation_anthropic.py` (17 件)
  - `tests/test_ingress_anthropic.py` (11 件、HTTP 境界 + SSE 順序アサーション)
- [x] G. 実機 Claude Code 疎通（`ANTHROPIC_BASE_URL=http://localhost:8088 claude` → 応答表示まで到達）
- [x] `/` と `HEAD /` に tiny handler 追加（Claude Code 起動時の preflight で 404 を返さないように）

### 8.4 実装で得た知見

#### 8.4.1 Claude Code は beta query を付けてくる

`POST /v1/messages?beta=true` として来る。FastAPI が未知 query を無視するので機能的な影響はゼロ。ログのノイズのみ。

#### 8.4.2 Claude Code は同一 user turn で **2 本並走**する

本文生成 + タイトル生成（会話ラベル用の小さい要約呼び出し）を同時発射する。uvicorn ログに同じ時刻で `POST /v1/messages` が 2 本並ぶのはこれが原因。fallback engine は各リクエストを独立して処理する。

#### 8.4.3 Claude Code の system prompt は巨大

実測：Claude Code v2.1 は tool 定義含めて推定 15-20K token の system prompt を毎ターン送る。14B モデル（qwen2.5-coder:14b）の prompt eval 速度 161 tok/s では `prompt eval ≈ 93s + generation 4s ≈ 100s/ターン`。**遅い**のではなく「大量に働いている」状態。Claude Code を実用速度で動かすには 7B 以下 or prompt eval > 300 tok/s のモデルが必要。

#### 8.4.4 qwen2.5-coder:14b は tool_calls を構造化出力しないことがある

Claude Code が送る大量の tool 定義を与えると、qwen が `tool_calls` フィールドではなく **テキスト本文に JSON ブロックをそのまま書く**挙動に落ちる。これはモデル能力限界で、CodeRouter の翻訳バグではない（OpenAI wire-format の応答をそのまま Anthropic text block に翻訳しているだけ）。対処は以下のいずれかで、v1.0 の「tool-call 信頼性」に正式スコープ化：

- tool-call repair: text の中に JSON ブロックを検出したら `tool_calls` に剥がすヒューリスティック
- モデル選定: tool 呼び出しに強い候補（llama3.1-8b-instruct、qwen3-coder、deepseek-coder-v2 など）
- `tool_choice: required` を限定的に使う（ただしテキスト回答が正解のターンを壊す）

#### 8.4.5 mid-stream fallback は危険

ストリーム開始後に provider がタイムアウト／例外で落ちると、現在のエンジンは次プロバイダに fall back しようとする。しかし初バイトを送出した後なら Claude Code に部分 SSE が届いている可能性があり、重複コンテンツや壊れた event 列になり得る。`provider-ok` 後に最初の byte を client に書き込んだら以降の fallback を禁止し、`event: error` を emit して閉じるガードを v0.3 に積む。

#### 8.4.6 providers.yaml の `timeout_s` は httpx の read timeout

stream 中は chunk 間の沈黙時間に効く。14B に Claude Code の巨大 prompt を食わせると 120s を平気で超えるので、ローカル 14B は `timeout_s: 300` を既定にした。

#### 8.4.7 `HEAD /` 404 問題

Claude Code は起動時に base URL の生存確認で `HEAD /` を投げる。CodeRouter には `/` ハンドラが無かったので 404 がログに出ていた（機能には影響なし）。`/` と `HEAD /` を追加して解消。

### 8.5 v0.2 スコープ外となった判断（v0.3 以降へ）

- **Anthropic adapter**（`kind: anthropic`）— 上流が本物の Anthropic/Claude のとき翻訳を挟まずに素通しする、pass-through 型アダプタ。当初 v0.2 に入れる案だったが、Claude Code 疎通が翻訳経路だけで取れたので不要と判断。v0.3 で追加。
- **tool-call repair** — §8.4.4 の text → tool_calls 引き剥がしヒューリスティック。v1.0 のスコープ（tool-call 信頼性）に寄せる。
- **mid-stream fallback guard** — §8.4.5。v0.3 で fallback engine に `first_byte_sent` フラグを持たせる改修。
- **usage 集計** — 現在 `message_delta.usage.output_tokens` が 0 固定。stream 終端 chunk の usage を拾うか delta 数から推定する改修。v0.3。
- **Claude Code 専用 profile** — 15-20K token prompt を高速に回すための 7B 以下中心の profile 定義。ユーザーが自分の環境に合わせて providers.yaml で作れば済むので、サンプルを README に追加する形で十分（v0.3）。

### 8.6 テスト内訳（+28 件）

- `test_translation_anthropic.py` 17 件
  - request 変換 8: simple text / system string / system block list / tool_use+tool_result RT / tools array / tool_choice 4 ケース / stop_sequences / profile 伝搬
  - response 変換 5: text / tool_call / finish_reason マップ / malformed JSON → _raw / empty response
  - stream 変換 4: text-only 順序 / tool_use イベント / text→tool_use 切替時の block close / multi tool_call 個別 index
- `test_ingress_anthropic.py` 11 件
  - non-stream 応答形状 / 422 validation / anthropic-version ヘッダ受理
  - profile body / header / body>header / 未知 body / 未知 header
  - `NoProvidersAvailableError` → 502
  - SSE event 順序（`message_start → ... → message_stop`）
  - stream 中エラー → `event: error`（overloaded_error）

---

## 9. v0.5 — プロファイル / capability / ALLOW_PAID

### 9.1 スコープ

- `profiles.yaml` で `coding` / `fast` / `long` / `cheap` を定義
- 各 provider の `capabilities` 宣言
- `ALLOW_PAID` ゲート
- `mode` パラメータ (リクエスト時に上書き可)

### 9.2 詳細タスク

- [ ] `profiles.yaml` schema 定義
- [ ] `mode` クエリ/ヘッダで指定された場合のルーティング
- [ ] capability mismatch 時 (例: vision 要求 → 非対応 provider はスキップ) のスキップロジック
- [ ] `ALLOW_PAID=false` 時、`paid: true` の provider を絶対呼ばない unit test
- [ ] プロファイル別タイムアウト/リトライ設定
- [ ] CLI: `coderouter --mode coding` 起動オプション

---

## 10. v1.0 — Tool-Call 信頼性 + Code Mode

### 10.1 スコープ

claude-code-local の "実戦で証明された5機能" を取り込む:
- Tool-call フォーマット変換 (Gemma / Llama / Qwen / HF 各形式 ↔ Anthropic)
- 壊れた JSON のリカバリ
- Code Mode (harness slim 化)
- プロンプトキャッシュ再利用
- 出力クリーニング (`<think>` 等剥がし)
- 14 ケース回帰テスト

### 10.2 詳細タスク

- [ ] **Tool-call 変換層**
  - [ ] Anthropic `tool_use` ブロックを共通中間形式に変換
  - [ ] 共通中間形式 → モデル別 tool 呼出フォーマットに変換
    - [ ] OpenAI 形式 (`tool_calls`)
    - [ ] Gemma 形式 (`<|tool_call>call:Name{...}<tool_call|>`)
    - [ ] Llama 3.x 形式 (生 JSON)
    - [ ] HuggingFace `<tool_call>` JSON
  - [ ] 上流応答 → Anthropic `tool_use` への逆変換
- [ ] **`recover_garbled_tool_json()`**
  - [ ] XML in JSON 検出
  - [ ] `<function=X><parameter=Y>` のフォールバック解釈
  - [ ] パラメータキーから tool 名推測
- [ ] **リトライ**
  - [ ] tool_call 意図検出 (heuristic)
  - [ ] パース失敗時に明示プロンプトで最大2回リトライ
- [ ] **Code Mode**
  - [ ] tools 配列に `Bash/Read/Edit/Write/Grep/Glob` のいずれかが含まれる場合に発火
  - [ ] 既定の slim system prompt (~100 トークン) を投入
  - [ ] プロファイル単位で slim/full 切替可能に
- [ ] **プロンプトキャッシュ**
  - [ ] Anthropic adapter: prompt caching API 利用
  - [ ] OpenAI 互換 adapter: prefix ハッシュベースの自前キャッシュ
  - [ ] capability `prompt_cache` で宣言
- [ ] **出力クリーニング**
  - [ ] フィルタチェイン化 (`output_filters: [strip_thinking, strip_stop_markers, ...]`)
  - [ ] `<think>...</think>`, `<|channel>thought`, `<turn|>`, `<|python_tag|>` 等
- [ ] **回帰テスト 14 ケース**
  - [ ] mkdir / ls / read / edit / grep / 連続5本 / multi-step calendar
  - [ ] CI で全 provider について実行できるよう matrix 化
- [ ] **チューニング既定値**
  - [ ] coding profile: temperature 0.2 を既定
  - [ ] tool_call 検出時のリトライ回数を `MLX_TOOL_RETRIES` 相当の env で

---

## 11. v1.1 — 配布 / launcher / doctor

### 11.1 スコープ

- `uvx coderouter` または `npm i -g` 一発で動く
- macOS `.command` / Windows `.bat` / Linux `.sh` の launcher 配布
- `coderouter doctor` で構成監査
- `coderouter doctor --network` で外向き接続を検出 (0 outbound を保証)

### 11.2 詳細タスク

- [ ] `uv` 配布パイプライン (or PyPI / npm)
- [ ] `setup.sh` (RAM 検出 → 推奨ローカルモデルダウンロード → providers.yaml 生成)
- [ ] `Claude Local.command` 互換の launcher 自動生成
- [ ] `coderouter doctor`
  - [ ] 設定ファイル lint
  - [ ] 各 adapter の healthcheck
  - [ ] `ALLOW_PAID` の現状表示
- [ ] `coderouter doctor --network`
  - [ ] `lsof -i -P` 相当を内蔵 or サブプロセス
  - [ ] 接続先一覧をホワイトリストと照合
  - [ ] 「localhost only」のグリーン表示
- [ ] アップデートチェック (任意 / opt-in)

---

## 12. v1.5 — 計測ダッシュボード

### 12.1 スコープ

claude-code-local の "数字で見せる" を踏襲。

- tok/s 実測
- fallback 発生率
- プロファイル別の成功率
- 直近のリクエスト一覧
- ローカル / 無料 / 有料 の使用比率

### 12.2 詳細タスク

- [ ] メトリクス収集レイヤ (in-memory + JSONL)
- [ ] 簡易 web UI (`http://localhost:4040/dashboard`)
  - [ ] React or htmx (依存最小)
- [ ] CLI `coderouter stats` (TUI)
- [ ] export: prometheus 形式 (任意)

---

## 13. v2.0 — プラグイン / MCP / OpenClaw 連携

### 13.1 スコープ

- プラグインで provider 追加可能 (e.g. `pip install coderouter-provider-foo`)
- MCP server としても動く (Anthropic MCP 仕様準拠)
- OpenClaw (将来エコシステム) との連携窓口
- Web UI で設定編集

### 13.2 詳細タスク

- [ ] プラグイン仕様策定 (entry_points or 動的ロード)
- [ ] MCP サーバ実装
- [ ] Web UI で `providers.yaml` / `profiles.yaml` を GUI 編集
- [ ] テスト用ダミー provider プラグインの公開

---

## 14. 横断タスク (どのバージョンでも継続)

- [ ] ドキュメント
  - [ ] `README.md` (claude-code-local 風の "見せ方")
  - [ ] `docs/architecture.md`
  - [ ] `docs/providers.md` (各 adapter 解説)
  - [ ] `docs/benchmarks.md`
- [ ] サンプル設定
  - [ ] `examples/providers.yaml` (Apple Silicon版 / Linux GPU版 / CPU only版)
  - [ ] `examples/profiles.yaml`
- [ ] セキュリティ / 依存最小主義 (§5.4 と連動)
  - [ ] 依存の脆弱性監査 (renovate / dependabot + `uv pip audit`)
  - [ ] `secrets.env` を絶対に commit させない pre-commit フック
  - [ ] `uv.lock` を commit、CI で `uv sync --frozen` 強制
  - [ ] 公式 SDK (anthropic / openai) を import していないことを CI でチェック
  - [ ] `coderouter doctor --deps` で依存数と outbound を可視化 (v1.1 で本実装)
- [ ] コミュニティ
  - [ ] CONTRIBUTING.md
  - [ ] ISSUE / PR テンプレート
  - [ ] note 記事用ネタ収集 (実測値、ハマりどころ)

---

## 15. やらないこと (Out of Scope, 少なくとも v2.0 まで)

- 音声 (NarrateClaude 領域)
- ブラウザ操作 (browser-agent 領域)
- iMessage / 通知システム連携
- 全 provider を完全同一 payload で扱う統一化 (Anthropic は別アダプタのまま)
- 学習 / fine-tuning パイプライン

---

## 16. 想定リスクと対応

| リスク | 影響 | 対応 |
| --- | --- | --- |
| OpenRouter free 枠が将来縮小 | fallback の中段が機能しない | 複数の無料源 (e.g. Gemini free, Mistral free) を providers.yaml で並列宣言 |
| Anthropic API の仕様変更 | Anthropic 互換 ingress が壊れる | バージョンヘッダ判定 + adapter バージョニング |
| ローカルモデルの tool_call が複雑化 | recovery が追いつかない | プロバイダごとに parser を差し替え可能にしておく |
| Python 配布で詰む | ユーザー導入率が低下 | uv 採用 + `coderouter-cli` を Go で別配布 |
| 依存パッケージのサプライチェーン攻撃 (LiteLLM 事例) | ルーター本体が侵害され、API キー / プロンプトが漏洩する可能性 | §5.4 の依存最小主義を厳守 (本体5本固定 / 公式SDK不使用 / lockfile + hash) |
| 個人開発の継続性 | 機能追加が止まる | コア機能を最小化、プラグイン制で外部委譲 |

---

## 17. 命名・ブランディング

- リポジトリ名: `CodeRouter`
- パッケージ名: `coderouter`
- CLI コマンド: `coderouter`
- ドメイン候補: `coderouter.dev` / `coderouter.app`
- ロゴモチーフ: 分岐する3本の矢印 (local / free / paid)

---

## 18. 次のアクション (v0.3 候補、優先度順)

v0.1 / v0.2 は完了（リリース履歴と §7 / §8 を参照）。v0.3-A〜D の実装は完了済み（詳細は下の「v0.3 実装状況」節）。
ここからは Claude Code 実運用に耐えるための品質改善フェーズ。

### v0.3 実装状況 (2026-04-20 時点)

| ID | 項目 | 状態 | 概要 |
| --- | --- | --- | --- |
| v0.3-A | Tool-call repair (non-stream) | ✅ | `translation/tool_repair.py` 新設。balanced-brace scanner + fenced JSON 検出 + allowlist。`to_anthropic_response(..., allowed_tool_names=)` で text 埋め込み JSON を tool_use block に昇格。13 ユニットテスト + 3 連携テスト。 |
| v0.3-B | Mid-stream fallback guard | ✅ | `MidStreamError` を新設し `FallbackEngine.stream()` が first-byte 送出後の AdapterError をこれで包む。`_anthropic_sse_iterator` が捕まえて `event: error` / `type: api_error` を emit（「どの provider も開始できない」overloaded_error と区別）。2 + 1 テスト追加。 |
| v0.3-C | Usage 集計 | ✅ | `_StreamState` に emitted_chars 累積 + upstream_input/output_tokens を持たせ、上流 usage が来ればそれを、来なければ `(chars + 3) // 4` 概算を `message_delta.usage.output_tokens` に入れる。OpenAI-compat adapter が `stream_options.include_usage=true` を自動付与（`extra_body` で override 可）。5 + 2 テスト追加。 |
| v0.3-D | Tool-call repair (streaming) | ✅ | `tools` を宣言した streaming リクエストは内部で `stream=false` に downgrade し、v0.3-A の repair を通してから `synthesize_anthropic_stream_from_response` で spec 準拠の SSE イベントに再構築。tool-less streaming は従来通り real streaming。3 synthesizer テスト + 3 ingress テスト。 |
| v0.3-E | 実機 Claude Code 再検証 | ✅ | Ollama + qwen2.5-coder:14b + Claude Code で疎通。(a) tool なし text streaming は real path で `usage` 両パス (upstream authoritative / char estimate fallback) を実機確認、(b) tool 付き streaming は downgrade path で `tool_use` block が Claude Code UI に正しく描画、(c) mid-stream guard は unit test でカバー（実機 pkill timing は困難のため optional）。疎通中に `Message.content=None` クラッシュを発見 → `coderouter/adapters/base.py` で型拡張 + regression test 追加。 |
| v0.3-F | Commit + tag v0.3.0 | ⏳ pending | CHANGELOG 追記済み → commit → `git tag v0.3.0`。|

テスト合計: 87 passed (v0.2 完了時点 54 → v0.3 で +33。v0.3-E 実機疎通中に発見した `Message.content=None` クラッシュの regression test 1 件を含む)。
ruff: v0.3 で導入した lint issue は 0（残る 11 件はすべて v0.1/v0.2 由来の既知事項）。

### 中優先 (v0.3.x / v0.4)

4. [x] **Anthropic native adapter (v0.3.x-1)** — `ProviderConfig.kind: "anthropic"` を追加し、`AnthropicAdapter` / `FallbackEngine.generate_anthropic` / `stream_anthropic` 経由で上流が本物の Claude のとき翻訳を通さず passthrough。
   - 翻訳コストを省き、Anthropic 固有の cache_control / thinking ブロックをそのまま活用できるようにする
   - v0.3-D downgrade 実装を ingress から engine に移設し、native provider は downgrade を完全 bypass。混在 chain（native → openai_compat のフォールバック）もサポート
   - tests +23 件 (`test_adapter_anthropic.py` 11 件 / `test_fallback_anthropic.py` 12 件) → 合計 **110 件**
   - 詳細は CHANGELOG.md `[v0.3.x-1]` セクション
4a. [x] **ChatRequest → AnthropicRequest 逆翻訳 (v0.4-A)** — v0.3.x-1 で out of scope としていた方向（OpenAI ingress → `kind: anthropic` provider）を実装。`AnthropicAdapter.generate` / `.stream` が内部で `to_anthropic_request` → `generate_anthropic` / `stream_anthropic` → `to_chat_response` / `stream_anthropic_to_chat_chunks` を呼ぶ逆変換パスを持つ。
   - `/v1/chat/completions` → `kind: anthropic` provider が対称的に到達可能に
   - `role: "system"` → top-level `system` フィールド、連続する `role: "tool"` → 1 つの user turn に複数 `tool_result` block を batch、`tool_calls` ↔ `tool_use` block、`tool_choice` / tools / stop_reason / usage の双方向マップ
   - Anthropic `event: error` → `AdapterError(retryable=False)` で既存 mid-stream guard に接続
   - engine 側はコード変更なし（polymorphic dispatch が効く）
   - tests +37 件 (`test_translation_reverse.py` 31 件新設 / `test_adapter_anthropic.py` +2 net / `test_fallback_anthropic.py` +4) → 合計 **147 件**
   - 詳細は CHANGELOG.md `[v0.4-A]` セクション
5. [x] **Claude Code 向け profile サンプル** を README と `examples/providers.yaml` に追加 (v0.4-A docs pass, 2026-04-20)。
   - `claude-code` profile: 7b → 14b → 2 free → paid (openrouter-claude)。14B timeout は 300s で Claude Code の 15-20K token system prompt 前提
   - `claude-code-direct` profile (NEW): 最終 paid を `anthropic-direct` (kind: anthropic) に差し替えたバリアント。cache_control / thinking ブロックを無傷で扱える
   - README「Use it with Claude Code」に profile の YAML snippet を明示
6. [x] **OpenRouter 無料モデル一覧の棚卸** (v0.4-B, 2026-04-20) — `/api/v1/models` で全 342 モデルを検証。
   - `qwen/qwen3-coder:free` (262K ctx / tools) は健在、primary として継続
   - `deepseek/deepseek-r1:free` は free roster から消失 → `openai/gpt-oss-120b:free` に 1:1 差し替え (ベンダ分散 + tools + 131K ctx + agentic/production 設計)
   - provider 名を `openrouter-deepseek-free` → `openrouter-gpt-oss-free` に改名、4 profile チェーン全て更新
   - ヘッダコメントに「DIFFERENT vendor families を選ぶこと」「2026-04-20 時点の roster 検証結果」を明記
7. [x] **README 更新** — v0.3 / v0.4-A の実装状況を Quickstart / Status セクションに反映 (2026-04-20)。
   - Status を `v0.4-A — Symmetric OpenAI ⇄ Anthropic routing` に
   - 147 tests green / native adapter / 逆翻訳 (v0.4-A) を箇条書きに追加
   - Coming next から v0.3.x 完了項目を除去
8. [x] **v0.4 実機疎通確認** (2026-04-20) — `/tmp/cr-verify.yaml` に検証用 profile を用意し host から spot check。
   - `/v1/chat/completions` → `openrouter-gpt-oss-free`: 200 OK / pong 応答 / `reasoning` フィールドが追加で返る発見あり (将来 reasoning-strip 層で扱う)
   - `/v1/chat/completions` → `anthropic-direct` (tool_choice=auto): `tool_calls[0].id = "toolu_..."` で逆翻訳経路確定。system lift / tools→input_schema / tool_use→tool_calls 全段動作
   - streaming 版: OpenAI shape の delta 列 + trailing usage chunk + [DONE] で完全再構築、Anthropic event 漏れなし
   - `/v1/messages` → `anthropic-direct` (native passthrough): `cache_control: {type: "ephemeral"}` が API まで届き、call 1 で `cache_creation_input_tokens: 1321`、call 2 で `cache_read_input_tokens: 1321` を確認 → cache_control ロスレス運搬を数値で証明
   - server log の `native_anthropic: true` フラグが Anthropic ingress 経由でのみ立つことも確認 → engine の分岐設計どおり
9. [x] **v0.4-D: `anthropic-beta` header passthrough** (2026-04-20) — Claude Code → `anthropic-direct` が 400 Bad Gateway で落ちる件の修正。
   - 原因: Claude Code は `context_management` body field を送る際に `anthropic-beta: context-management-2025-06-27` header を添える。CodeRouter はこの header を転送していなかったため Anthropic が `"context_management: Extra inputs are not permitted"` で拒否
   - 修正: `AnthropicRequest.anthropic_beta: str | None = Field(default=None, exclude=True)` を追加 (body にはリークさせない header-hop 用 stash)。Anthropic ingress が `Header(alias="anthropic-beta")` で抽出して request に積む。native adapter の `_headers(request)` が値を `api.anthropic.com` へ verbatim forward
   - 診断性能強化: `fallback.py` の `provider-failed` / `provider-failed-midstream` ログ 6 箇所に `"error": str(exc)[:500]` を追加 → 400 の body を構造化ログで取れるように。これが `context_management` エラーの特定を可能にした
   - tests +6 件 (`test_adapter_anthropic.py` +4 / `test_ingress_anthropic.py` +2) → 合計 **153 件**
10. [x] **v0.4 retrospective + docs pass** (2026-04-20) — v0.4-A/B/D を一気通貫で振り返り、README の stale 箇所 (tests 件数 / deepseek-r1 / "Coming next") を全て reconcile。新設 2 セクション「Choosing `kind: openai_compat` vs `kind: anthropic`」「Troubleshooting」。詳細なナラティブは [`docs/retrospectives/v0.4.md`](./docs/retrospectives/v0.4.md) — v0.5 計画の primary source。
11. [x] **v0.5-A: thinking capability gate** (2026-04-20) — v0.4 retro §Follow-ons で筆頭に挙げた capability gate の最初のピース。`coderouter/routing/capability.py` 新設 (純粋関数 3 つ: `provider_supports_thinking` / `anthropic_request_requires_thinking` / `strip_thinking`)、`Capabilities.thinking: bool` 追加、`FallbackEngine` の anthropic-shaped path 2 本で `_resolve_anthropic_chain` (capable/degraded stable-sort) + `strip_thinking` + 構造化ログ `capability-degraded`。tests +36 (`test_capability.py` +27 / `test_fallback_thinking.py` +9) → 合計 **189 件**。これで「v0.4-D で model を手動で Sonnet 4.5 → 4.6 に差し替える必要があった」症状が adapter 層で自動解決される。v0.5-B (cache_control normalization) を次に拾う。

### 低優先 (v0.5 以降で拾う)

- [ ] プロファイル / capability / ALLOW_PAID の完全版は §9 (v0.5) スコープ
- [ ] **v0.5-B: cache_control normalization** — v0.5-A の gate 基盤を流用。thinking と違い openai_compat 経由では 400 にならず lossy pass-through するだけなので、挙動を明文化 + `cache_control` 含むリクエストを openai_compat に回す際の警告ログを追加する。1024-token minimum (retro §What was sharp 参照) の docstring 警告も併記。
- [ ] **v0.5-C: OpenRouter `reasoning` field passive strip** (retro §Follow-ons より) — 一部 free-tier モデルが返す非標準 `reasoning` キーを response-side で silent strip + ログ。
- [ ] **v0.5-D: OpenRouter roster 週次 cron diff** (retro §Follow-ons より) — `/api/v1/models` を snapshot に diff、free-tier 撤退検出を proactive 化。v0.4-B は reactive だった。
- [ ] 14 ケース回帰テスト / Code Mode / 出力クリーニングは §10 (v1.0) スコープ
- [ ] launcher / doctor は §11 (v1.1)、計測は §12 (v1.5)、プラグイン / MCP は §13 (v2.0)

---

## Appendix A — memo.txt との対応表

| memo.txt の項目 | plan.md での反映先 |
| --- | --- |
| 3層 fallback | §2.1, §4, §6 |
| モード選択 | §2.2, §9 |
| デフォルト無料 / ALLOW_PAID | §2.3, §9 |
| OpenAI互換土台 + Anthropic別アダプタ | §2.4, §4, §7-§8 |
| capability flags | §2.5, §9 |
| coding/fast/long の例 | §9, §17 |
| `.env` / `models.yaml` / `install.sh` | §11 |
| README キャッチコピー | §1.3, §17 |
| 「数字で見せる」 | §12 |
| 名前案 ClawRoute / CodeRouter | §17 |

## Appendix B — claude-code-local からの抽出表

| claude-code-local 機能 | plan.md での反映先 | 優先度 |
| --- | --- | --- |
| Anthropic API ネイティブ ingress | §8 (v0.2) | ★★★ |
| tool_call 変換 + 壊れた JSON 修復 | §10 (v1.0) | ★★★ |
| Code Mode (harness slim) | §10 (v1.0) | ★★★ |
| プロンプトキャッシュ再利用 | §10 (v1.0) | ★★ |
| 出力クリーニング | §10 (v1.0) | ★★ |
| tool-call チューニング既定値 | §10 (v1.0) | ★★ |
| 14ケース回帰テスト | §10 (v1.0) | ★★ |
| ワンクリック launcher | §11 (v1.1) | ★ |
| ZERO outbound monitor (`doctor`) | §11 (v1.1) | ★ |
| 計測ダッシュボード (tok/s 等) | §12 (v1.5) | ★ |

---

*このplan.mdは生きたドキュメントです。実装中に判明した知見でガンガン書き換えてください。*
