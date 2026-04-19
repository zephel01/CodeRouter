# Changelog

All notable changes to CodeRouter are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).

---

## [v0.3.0] — 2026-04-20

### v0.3: 実運用向け品質改善

Claude Code + ローカル LLM (qwen2.5-coder:14b など) で実運用したときに浮いた
3つの課題を潰すフェーズ。いずれも v0.2 で「仕様通りには動いているが実モデル
が歪んだ出力を返したときに壊れる」領域。

#### Added

- **Tool-call repair (non-streaming / v0.3-A)** — `coderouter/translation/tool_repair.py`
  - 上流モデル（特に qwen2.5-coder:14b）が `tool_calls` フィールドを使わず
    `{"name": ..., "arguments": ...}` を平文 text で返す失敗パターンに対応
  - balanced-brace scanner（文字列/エスケープ認識）で text body から JSON を抽出
  - fenced ``` ```json ``` ブロックも検出
  - リクエストが宣言した tool 名 allowlist に照合し、未知の tool 名は text のまま残す
  - 抽出された JSON は OpenAI `tool_calls` 形式に正規化され、後段で通常どおり
    Anthropic `tool_use` content block に変換される
  - `to_anthropic_response(..., allowed_tool_names=[...])` で呼び出し
- **Mid-stream fallback guard (v0.3-B)** — `coderouter/routing/fallback.py`
  - 新しい例外 `MidStreamError(provider, original)` を追加
  - `FallbackEngine.stream()` は first byte 送出後に AdapterError が出たら
    次 provider に fall through せず `MidStreamError` を raise
  - `_anthropic_sse_iterator` が `MidStreamError` を捕まえ `event: error` /
    `type: api_error` を emit して SSE を閉じる（「最初の 1 byte も出せない」
    `overloaded_error` とは区別）
  - 目的: Claude Code の画面に部分応答 + 重複コンテンツが届く事故を防ぐ
- **Usage aggregation (v0.3-C)** — `coderouter/translation/convert.py`
  - stream 終端の `message_delta.usage.output_tokens` を正しく埋める
  - 優先順位: upstream の `completion_tokens`（authoritative） > `(emitted_chars + 3) // 4` 概算
  - `input_tokens` は upstream が `prompt_tokens` を送ってきた場合のみ反映
  - OpenAI-compat adapter は streaming 時に自動で `stream_options: {"include_usage": true}` を付与。
    provider 側が `extra_body` で上書きしていればそちらが優先
  - Ollama のように flag を無視する upstream でも、char 概算のおかげで 0 にはならない
- **Tool-call repair (streaming / v0.3-D)** — strategy 2: downgrade to non-stream
  - `tools` を宣言した streaming リクエストは内部で `stream=false` に切り替え、
    v0.3-A の repair を通してから `synthesize_anthropic_stream_from_response` で
    Anthropic SSE イベント列を合成して返す
  - クライアントから見た wire はあくまで streaming（`message_start → … → message_stop`）
  - tool を含まない streaming は従来通り真の streaming パス
  - トレードオフ: tool ターンは first-byte latency が full response 時間まで伸びる
    （tool ターンは実質的に「完成してから次の手」が前提なので許容）

#### Changed

- `coderouter/adapters/openai_compat.py` — streaming 時 `stream_options.include_usage` を既定で true
- `coderouter/translation/__init__.py` — `synthesize_anthropic_stream_from_response` を export
- `coderouter/routing/__init__.py` — `MidStreamError` を export
- `_handle_delta` が `emitted_chars` を累積（text_delta + tool name + input_json_delta）

#### Fixed

- **`Message.content = None` が pydantic ValidationError で 500 を返す** — Claude Code が
  multi-turn 履歴に「tool_use だけ / text なし」の assistant ターンを含めてくると、
  `_convert_anthropic_message` が `content: None` を吐き、`Message` モデルが reject していた。
  OpenAI spec は `tool_calls` を持つ assistant message に `content: null` を許可しているので、
  `coderouter/adapters/base.py` の `Message.content` 型を `str | list[dict[str, Any]] | None = None`
  に拡張し、`exclude_none=True` のシリアライズで upstream には content キーを送らない挙動に統一。
  regression test を `tests/test_translation_anthropic.py::test_assistant_message_with_only_tool_use_has_null_content` に追加。

#### Tests

v0.2 完了時点 54 件 → v0.3 完了後 **86 件（+32 件）**:

- `tests/test_tool_repair.py` **13 件**（新設） — text 埋込 JSON 抽出の全パターン
- `tests/test_translation_anthropic.py` **+8 件**
  - repair 連携 3 件
  - usage 集計 5 件（upstream 優先 / 概算 fallback / tool args 込み / 空応答 0 / upstream が estimate を override）
  - synthesizer 3 件（text-only / tool_use / mixed）
- `tests/test_ingress_anthropic.py` **+4 件**
  - mid-stream 時の `event: error` / `type: api_error`
  - tool 付き streaming の downgrade + repair
  - tool なし streaming は real streaming のまま
  - downgrade パスでも 502 は error event で surface
- `tests/test_fallback.py` **+2 件** — mid-stream で MidStreamError, 初期エラーは従来どおり fallback
- `tests/test_openai_compat.py` **+2 件** — stream_options.include_usage 自動付与 / extra_body 上書き尊重

lint (ruff): v0.3 で導入した issue は 0。残 11 件はすべて v0.1/v0.2 由来の既知事項。

#### Verified (2026-04-20 実機)

Ollama + qwen2.5-coder:14b + Claude Code (`ANTHROPIC_BASE_URL=http://localhost:8088`) で疎通確認。

- **(a) tool なし text streaming (curl 直撃 `/v1/messages`)** — real streaming path
  (`engine.stream()` → `stream_chat_to_anthropic_events`) で
  `message_start → content_block_start → content_block_delta × N → content_block_stop →
  message_delta → message_stop` まで spec 準拠。
  - 1 回目: `usage: {output_tokens: 122, input_tokens: 46}` ← Ollama が
    `stream_options.include_usage: true` を honor → v0.3-C の **upstream authoritative パス**
    が発火。
  - 2 回目: `usage: {output_tokens: 97}` (input_tokens 欠落) ← Ollama が terminal usage chunk
    を省略 → v0.3-C の **char-based estimate fallback** が発火。2 経路とも実機で踏めた。
- **(b) Claude Code + tool 付き streaming** — `_anthropic_downgraded_tool_iterator` が
  動作 (サーバログに `try-provider ... stream: false` → `provider-ok ... stream: false`)。
  `tool_use` content block が Claude Code UI に tool invocation として描画された
  (`⏺ Glob()` など)。ただしモデルの tool 選択の妥当性は別レイヤの問題
  (qwen2.5-coder:14b は `pwd` 要求に対して Bash でなく Glob を選ぶなどした)。
- **プロファイル経路**: `skip-paid-provider` (openrouter-claude, `ALLOW_PAID=false`)
  → `ollama-qwen-coder-14b` の fallback を実機で確認。
- **Bug fix (実機疎通中に発見)**: `Message.content = None` を pydantic が reject して 500
  を返していた問題を修正。Claude Code は multi-turn 履歴に「tool_use のみ / text なし」の
  assistant ターンを含めるため、2 ターン目以降で必ず踏む構造のバグだった。
  - `coderouter/adapters/base.py` の `Message.content` を
    `str | list[dict[str, Any]] | None = None` に拡張
  - `_prepare_messages` は `exclude_none=True` で dump するので upstream には
    content キーごと送らない → OpenAI spec どおりの shape を維持
  - regression test: `test_translation_anthropic.py::
    test_assistant_message_with_only_tool_use_has_null_content`
- **(c) mid-stream guard**: unit test
  (`test_ingress_anthropic.py::test_streaming_midstream_failure_emits_api_error_event`
  ほか) でカバー。実機 pkill の timing を qwen の生成速度に合わせるのは困難で、
  かつ Ollama の runner/serve 2 プロセス構成で graceful close を返されるケースがあり、
  実機 smoke は optional とした。ロジック自体は代数的にテスト済み。
- **Claude Code の tool 宣言挙動**: Claude Code は毎ターン全 tool (Bash/Glob/Read/Write/...)
  を `tools: [...]` で送ってくるため、**Claude Code 経由では常に v0.3-D downgrade path
  に入る**。real streaming path は tool を宣言しない OpenAI-shape 互換クライアントや
  Anthropic direct curl でのみ使われる。この構造は CHANGELOG の Known Limitations
  「tool を含む streaming は実質的に非 streaming と同じ遅延プロファイル」と一致。

総テスト件数: **87 passed** (86 + 実機疎通中の bug fix regression 1)。lint clean。

#### Known Limitations

- qwen2.5-coder:14b のような tool-call を text で返すモデルでも現在は repair で
  wire 準拠に戻せるが、実際に tool が呼び出せるかは「モデルが引数を正しく組み立てるか」
  という別レイヤの問題。repair は信号経路の話であり、モデル能力の補完ではない
- tool を含む streaming は実質的に非 streaming と同じ遅延プロファイル。「tool 判断を
  ユーザに見せながら stream」は v0.4+ の課題（strategy 1: 投機的 emit + rollback）
- `input_tokens` は upstream が `prompt_tokens` を送った場合のみ。ローカル
  tiktoken 同梱での事前計測は依存 5 パッケージ制約（plan.md §5.4）があるため v1.0+ で検討
- OpenRouter / Claude 本家 API 経由では未検証（v0.3-E で補完予定）

---

## [v0.2.0] — 2026-04-20

### Anthropic Ingress

Claude Code などの Anthropic クライアントから `ANTHROPIC_BASE_URL=http://localhost:8088`
で直接 CodeRouter を叩けるようになりました。

#### Added

- **`POST /v1/messages`** — Anthropic Messages API 互換 ingress
  - 非 streaming / streaming (SSE) 両対応
  - `message_start → content_block_start → content_block_delta(×N) → content_block_stop → message_delta → message_stop` の spec 準拠順で event 発火
  - `tool_use` / `tool_result` / `image` / `text` の content block 4 種を双方向変換
  - `system` は string / block list の両形を受け、内部では 1 本の system message に flatten
  - `stop_sequences` / `temperature` / `top_p` / `top_k` を passthrough
  - `anthropic-version` ヘッダを受理（enforce はしない、debug ログに残すのみ）
  - profile 選択は既存 OpenAI route と同じく body > `X-CodeRouter-Profile` ヘッダ > default の順
  - 未知 profile は 400、プロバイダ全滅は 502（非 stream）/ `event: error`（stream）
- **`coderouter/translation/`** 新モジュール
  - `anthropic.py` — Anthropic wire-format の pydantic models（request / response / stream event + content block 4 種）
  - `convert.py` — Anthropic ⇄ 共通 `ChatRequest`/`ChatResponse` の双方向変換
    - `to_chat_request`, `to_anthropic_response`
    - `stream_chat_to_anthropic_events` は stateful に block index を管理（text→tool_use 切替時は text block を先に閉じる、multi tool_call に個別 index）
  - malformed tool_call JSON は `_raw` 退避で素通しし、後段で修復可能に
- **`/` と `HEAD /`** に tiny handler — Claude Code 起動時の preflight で 404 を返さないように
- **テスト +28 件 / 総数 54 件**
  - `tests/test_translation_anthropic.py` 17 件 — request / response / stream 変換ユニット
  - `tests/test_ingress_anthropic.py` 11 件 — HTTP 境界、profile 経路、SSE event 順序、エラーマッピング

### Changed

- `providers.yaml` — `ollama-qwen-coder-14b` の `timeout_s` を 120 → 300
  （Claude Code は 15-20K token の巨大 system prompt を毎ターン送るので、14B クラスでは 120s を平気で超えるため）
- `plan.md` §8 を完了形に更新、§8.4 に実装知見 7 項目、§8.5 に v0.3 以降へ送った項目を明記

### Verified

- `ANTHROPIC_BASE_URL=http://localhost:8088 claude` でフルパス疎通
  - text 応答・streaming SSE 順序・tool 定義引き渡しまで一周
- 全 54 テスト green
- 本家 Ollama / qwen2.5-coder:14b 実機で動作

### Known Limitations (→ v0.3 以降)

- **tool-call 構造化出力の不安定性**: qwen2.5-coder:14b に Claude Code の 10+ tool 定義を渡すと、
  `tool_calls` フィールドではなく text 本文に JSON ブロックで返してくることがある。これは翻訳バグではなくモデル能力限界で、
  v1.0 の「tool-call 信頼性」スコープで text → tool_calls 引き剥がしヒューリスティックを入れる。
- **mid-stream fallback**: 初バイト送出後に provider が落ちた場合の fallback を現状は禁止していない。
  v0.3 で `first_byte_sent` ガード + `event: error` emit に変更予定。
- **`message_delta.usage.output_tokens`** が 0 固定（stream 終端で usage を集計していない）。v0.3 で改修。
- **Anthropic native adapter** (`kind: "anthropic"`, 翻訳を通さない pass-through) は未実装。v0.3 以降。

---

## [v0.1.0] — 2026-04-20

### Walking Skeleton

"OpenAI 互換 ingress + ローカル 1 個 + フォールバック 1 個が動く" の最小骨組み。

#### Added

- **`POST /v1/chat/completions`** — OpenAI Chat Completions 互換 ingress（非 streaming / streaming SSE）
- **adapter 層** — `BaseAdapter` + `OpenAICompatAdapter`（llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq を 1 枚でカバー）
- **`FallbackEngine`** — 順次 fallback、`retryable=False` で中断、`paid=true` は `ALLOW_PAID=false` 環境で skip
- **`providers.yaml` / `profiles`** — provider 定義 + fallback chain 名前付け
- **profile 選択** — body `profile` フィールド > `X-CodeRouter-Profile` ヘッダ > `default_profile` の順
- **`ProviderConfig.extra_body` / `append_system_prompt`** — モデル固有オプション
- **JSON 構造化ログ** — `coderouter.routing.fallback` から `try-provider` / `provider-ok` / `provider-failed` / `skip-paid-provider`
- **`/healthz`** エンドポイント
- **テスト 26 件**（config / fallback / openai 互換 / profile 選択）

### Verified

- curl で fizzbuzz 生成成功
- fallback: 1 つ目の provider を外すと 2 つ目に自動遷移
- fast profile 実機確認 (qwen2.5:1.5b → gemma3:1b の 2 ホップ成功)
- 全 26 テスト green

### Notable Decisions / Implementation Learnings

- **qwen3.x の thinking モードは抑制不能**
  - Ollama は `think: false` を落とす / qwen3.5:4b は RL で `/no_think` を拒否
  - fast profile からは qwen3.x を外し、dedicated `think` profile に移管
- **Lazy module-level `app`** via `__getattr__`
  - `uvicorn coderouter.ingress.app:app` は機能させつつ、テスト import で providers.yaml を eager load しない
- **Bug fix**: `request.model` が provider の model を上書きしていた問題を修正（provider 固有 model を送る仕様に）
- **Bug fix**: 404 を retryable に変更（ルート違いの fallback を許容）

---

## Unreleased

v0.3 以降の候補は [`plan.md` §8.5](./plan.md) と [`plan.md` §18](./plan.md) を参照。
