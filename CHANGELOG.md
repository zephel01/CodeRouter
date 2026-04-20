# Changelog

All notable changes to CodeRouter are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).

---

## [v0.5-B] — 2026-04-20

### cache_control observability

v0.5-A (thinking) に続く capability gate の 2 ピース目。thinking が「未対応
model に投げると 400」というハードエラーだったのに対して、cache_control は
もっと性質が違う — Anthropic → OpenAI translation の段階で **silent に落ちる**
(content block 上のマーカーに OpenAI wire 側の等価物がない)。エラーにならず、
上流の Anthropic prompt cache の課金最適化が単に無効化されるだけ。

v0.5-B はこの非対称性を踏まえて **observability-only** (no chain reorder /
no strip) で着地させる:

- cache_control 付きリクエストが openai_compat provider に渡る際、構造化ログ
  `capability-degraded` (`reason: "translation-lossy"`) を出す
- chain 順序は **変えない** — ユーザーの provider 順序は latency / cost の意
  図を反映しており、cache-hit の節約でそれを上書きしない方針
- strip もしない — `to_chat_request` の既存 translation が自動で marker を落
  とすので router 側で追加処理する必要がない

#### Added

- **`coderouter/routing/capability.py`** — 関数 2 つ + helper 1 つ:
  - `provider_supports_cache_control(provider)` — `kind: anthropic` は常に
    True (native passthrough で end-to-end 保持)、`kind: openai_compat` は
    デフォルト False (wire 等価物なし)。`capabilities.prompt_cache: true` を
    YAML で明示すると openai_compat でも True に昇格 (escape hatch: 将来
    OpenAI wire を拡張した upstream が出た場合用)
  - `anthropic_request_has_cache_control(request)` — `system` (list 形式)
    `tools[*]` (Pydantic extras 経由) `messages[*].content` (list 形式) を
    再帰的に walk し、`cache_control` key を持つブロックが 1 つでもあれば
    True
  - `_block_has_cache_control(block)` — 内部 helper (dict 判定 + key 存在判定)

#### Changed

- **`coderouter/routing/fallback.py`** — `generate_anthropic` / `stream_anthropic`
  の両方で:
  - ループ内の provider ごとに `anthropic_request_has_cache_control(request)`
    かつ `not provider_supports_cache_control(adapter.config)` なら
    `capability-degraded` ログ (`provider` / `dropped: ["cache_control"]` /
    `reason: "translation-lossy"`) を発火
  - v0.5-A の thinking gate (`provider-does-not-support`) とは `reason` が
    違うので運用側で絞り込み可能
  - `_resolve_anthropic_chain` は **変更なし** — cache_control では reorder
    しない。同じメソッド内で 2 種類のログが出るようになっただけ

#### Tests

- **+21 件** (合計 **210 件 green**, 189 → 210)
  - `test_capability.py` +13:
    - `provider_supports_cache_control`: anthropic デフォルト True, openai_compat
      デフォルト False, `prompt_cache: true` で openai_compat を昇格, anthropic
      に prompt_cache: true は redundant だが壊れない
    - `anthropic_request_has_cache_control`: plain request / bare-string system /
      system block with marker / system block without marker / tool-level marker /
      message content block marker / string-form content は常に False / 2nd
      message でも検出 / image block 上の marker (type 非依存)
  - `test_fallback_cache_control.py` (新規) +8:
    - openai_compat + cache_control → log 発火 (reason=translation-lossy, dropped=["cache_control"])
    - anthropic kind + cache_control → log 発火しない
    - plain request + openai_compat → log 発火しない
    - **chain 順序が reorder されない** (v0.5-A との重要な差分テスト)
    - `prompt_cache: true` escape hatch でログ抑制
    - fallback chain で複数の openai_compat を踏む場合、1 provider ごとにログ発火
    - streaming path の mirror (openai_compat 発火 / anthropic 発火しない)

#### Notes

- **Anthropic prompt cache の 1024-token 下限**: v0.4 retrospective §What was
  sharp で既出の footgun。system prompt が 1024 token 未満だと、supported
  provider でも Anthropic 側が `cached_tokens: 0` を返す。v0.5-B の gate は
  この Anthropic 側の制約には関知しない (そもそもマーカーを保持することだけを
  扱う層) — なので「小さい prompt でキャッシュヒットが 0 なのは CodeRouter の
  バグ」という誤解を招かないよう docstring にコメント済み
- **実機 verify**: v0.4-D retro で「1321 tokens written on call 1, 1321 read
  on call 2」を実機で確認済み (native anthropic 経由)。v0.5-B は routing 側の
  gate なので、translation layer の既存挙動 + 新規ログのみが差分
- **運用上の使い方**: `reason: "translation-lossy"` で grep すると「ユーザー
  は cache 意図を送ったが本リクエストは openai_compat に流れた」イベントが
  全部拾える。頻度が高ければ YAML 側で anthropic-direct を上に挙げるか、
  openai_compat 側に `prompt_cache: true` を立てる判断材料になる

---

## [v0.5-A] — 2026-04-20

### thinking capability gate

v0.4-D retrospective で follow-on に挙げた「capability gate」の最初のピース。
Anthropic の `thinking: {type: "enabled"}` を対応モデルだけにルーティングし、
未対応モデルには silent strip + 構造化ログで degrade する。

背景: v0.4-D 実機テストで `claude-sonnet-4-5-20250929` が adaptive thinking
リクエストに 400 を返す問題にぶつかり、`claude-sonnet-4-6` に差し替えて回避し
た。ユーザーのモデル選択が「正当性に影響する決定」になっていた状態を、v0.5-A
で「純粋に経済性の決定」に降格させる。

#### Added

- **`coderouter/routing/capability.py`** (新規) — 純粋関数 3 つ:
  - `provider_supports_thinking(provider)` — YAML flag 優先、未指定なら
    model 名 heuristic (`^claude-(opus|sonnet|haiku)-4-(6|7)`, `claude-opus-4-`,
    `claude-haiku-4-` にマッチすれば capable)。`kind: openai_compat` は
    model 名にかかわらず常に incapable (OpenAI wire に thinking field なし)
  - `anthropic_request_requires_thinking(request)` — `model_extra["thinking"]`
    が `{"type": "enabled"}` かどうかを判定。disabled / 欠落 / 非 dict は False
  - `strip_thinking(request)` — extras から `thinking` を除いた複製を返す
    (mutation-free)。`profile` / `anthropic_beta` (exclude=True fields) は保持
- **`coderouter/config/schemas.py`**
  - `Capabilities.thinking: bool = False` 追加。YAML で明示的に `true` を
    立てると heuristic を上書きできる (新モデルファミリーが出た時の escape
    hatch)。`reasoning_control: Literal[...]` (v1.0+ abstract interface) とは
    別物なので併存
- **`coderouter/routing/fallback.py`**
  - `_resolve_anthropic_chain(request)` — `request` が thinking を要求して
    いる場合、chain を `capable` / `degraded` の 2 バケットに stable-sort し
    て返す。要求なしの場合は従来通り declared order を保つ

#### Changed

- **`coderouter/routing/fallback.py`** — `generate_anthropic` / `stream_anthropic`
  の両方で:
  - `_resolve_chain(...)` → `_resolve_anthropic_chain(...)` に差し替え。戻り値が
    `list[tuple[BaseAdapter, bool]]` になり、各 provider について
    `will_degrade` フラグが付く
  - `will_degrade=True` の provider を呼ぶ前に `strip_thinking(request)` + 構造化
    ログ `capability-degraded` (`provider` / `dropped: ["thinking"]` / `reason`)
  - 既存の `try-provider` ログに `"degraded": will_degrade` を追加
- OpenAI ingress (`/v1/chat/completions`) 経路は変更なし。ChatRequest に
  thinking field がそもそもないため、capability logic を通す必要がない

#### Tests

- **+36 件** (合計 **189 件 green**)
  - `test_capability.py` (新規) +27: heuristic の capable/incapable ファミリー
    (パラメトリック), openai_compat 常時 incapable, YAML 明示 true が両 kind で
    wins, `requires_thinking` の enabled/disabled/missing/非 dict 各種, `strip`
    の除去 / 保持 / noop / wire-body clean / 他 extras 非破壊
  - `test_fallback_thinking.py` (新規) +9: capable-pull-to-front, plain-request
    順序保持, degraded fallback + `capability-degraded` ログ発火, strip 後の
    adapter 引数が wire-body レベルで clean, no-degraded-log when capable 成功
    / plain request, openai_compat は Claude-like slug でも incapable 扱い, YAML
    thinking:true で heuristic 外モデルを capable に昇格, streaming path も
    同じ preference

#### Notes

- **v0.5-B で予定**: `cache_control` の normalization。thinking と違って
  「400 vs 200」の二値ではなく「openai_compat 経由だと lossy で pass-through
  する / anthropic で preserve」という非対称性なので、別リリースで扱う
- **heuristic table のメンテナンス**: 新しい Claude family が出たら
  `capability.py` の `_THINKING_CAPABLE_PATTERNS` に regex を追加。allow-list
  なので古いパターンを削る必要はない (deprecated 家族がマッチしても害はない)
- **実機 verify は任意**: 本リリースの挙動は 36 件の unit/engine tests で確認
  済み。実機で chain 再選択を見たい場合は `providers.yaml` に capable/incapable
  の 2 つを置き、thinking 付きリクエストを `/v1/messages` に投げると
  `capability-degraded` ログの有無で確認できる

---

## [v0.4-D] — 2026-04-20

### `anthropic-beta` header passthrough (Claude Code 400 fix)

Claude Code → CodeRouter → `anthropic-direct` を実機で叩くと Anthropic から
`400 Bad Gateway` が返ってくる件の修正。ルートコーズは body field
`context_management` が `anthropic-beta: context-management-2025-06-27` header
なしでは拒否されること。Claude Code は header を送ってきていたが CodeRouter が
それを `api.anthropic.com` まで転送していなかった。

#### Added

- **`coderouter/translation/anthropic.py`**
  - `AnthropicRequest.anthropic_beta: str | None = Field(default=None, exclude=True)`
    — header-hop 用の stash。`exclude=True` なので `model_dump()` には出てこず、
    wire body にリークしない
- **`coderouter/ingress/anthropic_routes.py`**
  - `anthropic_beta: str | None = Header(alias="anthropic-beta")` を `messages()`
    ハンドラ引数に追加
  - 値が来ていれば `anth_req.anthropic_beta = anthropic_beta` で request に積む
- **`coderouter/adapters/anthropic_native.py`**
  - `_headers(request: AnthropicRequest | None = None)` シグネチャ変更。
    `request.anthropic_beta` が set なら `headers["anthropic-beta"]` に verbatim
    forward。`/v1/chat/completions` 逆翻訳パスは request を渡さないので OpenAI
    クライアントの既存挙動は変わらない (OpenAI 側は header を持たない前提)
  - `generate_anthropic` / `stream_anthropic` の `self._headers()` コールを
    `self._headers(request)` に置換。`healthcheck()` は request 文脈なしで呼ぶ
    ので引数なしのまま

#### Changed

- **`coderouter/routing/fallback.py`** — 診断性能の底上げ。
  `provider-failed` / `provider-failed-midstream` ログ 6 箇所に
  `"error": str(exc)[:500]` を追加。今回の 400 の中身 (`context_management`
  rejection の正確な wording) がこれで構造化ログに乗った。将来の同種のバグも
  server log を見るだけで当たりがつく

#### Tests

- **+6 件** (合計 **153 件 green**)
  - `test_adapter_anthropic.py` +4:
    `test_headers_omit_anthropic_beta_when_not_set` /
    `test_headers_forward_anthropic_beta_when_set` /
    `test_generate_anthropic_forwards_anthropic_beta_header` /
    `test_stream_anthropic_forwards_anthropic_beta_header`
  - `test_ingress_anthropic.py` +2:
    `test_anthropic_beta_header_threads_through_to_request` /
    `test_missing_anthropic_beta_header_leaves_field_none`
- カバー範囲: (a) field が body に leak しないこと (`Field(exclude=True)` の
  実挙動を outbound JSON で検証) / (b) header が outbound request に乗ること
  (streaming / non-streaming 両パス) / (c) ingress が header を抽出して
  request に積むこと / (d) 負のケース (header 未指定 → None のまま)

#### Notes

- 将来、他の beta feature も同じ経路で通せる。`anthropic-beta` はカンマ区切りで
  複数 feature flag を取る仕様なので、値は触らず verbatim forward が正しい
- v0.2 §8.4.1 の `?beta=true` クエリ文字列問題とは別件。あちらは Anthropic 側が
  黙殺するだけだが、今回は body field 不許可で 400 を返す heavier failure mode

---

## [v0.4-A] — 2026-04-20

### ChatRequest → AnthropicRequest 逆翻訳 (OpenAI ingress → kind:anthropic provider)

v0.3.x-1 の設計決定 F で意図的に out of scope としていた「OpenAI クライアントから
Anthropic-native provider を叩く」経路を埋める。`AnthropicAdapter.generate` /
`.stream` が retryable=False で reject していたのをやめ、
`ChatRequest → AnthropicRequest` および `AnthropicResponse → ChatResponse` /
`AnthropicStreamEvent* → StreamChunk*` の逆方向翻訳で上流 Anthropic Messages API
を呼ぶようにする。これにより `/v1/chat/completions` ingress と `kind: anthropic`
provider の組み合わせが対称的に動作するようになる。

#### Added

- **`coderouter/translation/convert.py`** — 逆方向の翻訳ヘルパを追加 (~300 lines)
  - `to_anthropic_request(ChatRequest) → AnthropicRequest`
    - `role: "system"` メッセージを top-level `system` フィールドに集約（複数 system
      メッセージは `\n` で結合）
    - 連続する `role: "tool"` メッセージを 1 つの user turn にまとめ、複数の
      `tool_result` block として格納（Anthropic canonical shape）
    - assistant の `tool_calls` を `tool_use` content block に変換
    - `image_url` content part を `data:` URI 判定で base64 / url source に振り分け
    - OpenAI `tools` → Anthropic `tools`（`parameters` → `input_schema`）
    - `tool_choice` 双方向マップ: `"auto"↔{type:auto}` / `"required"↔{type:any}` /
      `"none"↔{type:none}` / `{type:function}↔{type:tool}`
    - `max_tokens` 省略時は 4096 をデフォルト（Anthropic は必須、OpenAI は optional）
    - malformed JSON な `tool_calls.arguments` は `{"_raw": <string>}` に保持
  - `to_chat_response(AnthropicResponse) → ChatResponse`
    - 複数 text block は連結、`tool_use` block は top-level `tool_calls` に昇格
    - stop_reason 逆マップ: `end_turn→stop` / `max_tokens→length` /
      `tool_use→tool_calls` / `stop_sequence→stop`
    - `usage.input_tokens`/`output_tokens` → OpenAI `prompt_tokens` /
      `completion_tokens` / `total_tokens`
  - `stream_anthropic_to_chat_chunks(AnthropicStreamEvent*) → StreamChunk*`
    - stateful 翻訳: Anthropic の per-block index → OpenAI `tool_calls[].index` を
      `_ReverseStreamState.block_idx_to_tool_idx` で対応付け
    - 初期 `message_start` で `delta.role = "assistant"` を emit（OpenAI 慣例）
    - `text_delta` → `delta.content`
    - `tool_use` block_start → `delta.tool_calls[].function.name`（args 空）
    - `input_json_delta` → `delta.tool_calls[].function.arguments` 断片
    - 終端で finish_reason 付きチャンク + `choices: []` な usage チャンクを emit
      （OpenAI `stream_options.include_usage=true` と同形式）
    - Anthropic `event: error` は `AdapterError(retryable=False)` を raise。engine の
      v0.3-B mid-stream guard が `MidStreamError` に変換する経路はそのまま
- **`coderouter/adapters/anthropic_native.py`** — `generate` / `stream` を実装に差替
  - `generate(ChatRequest) → ChatResponse`:
    `to_anthropic_request` → `self.generate_anthropic` → `to_chat_response`
  - `stream(ChatRequest) → AsyncIterator[StreamChunk]`:
    `to_anthropic_request` → `self.stream_anthropic` → `stream_anthropic_to_chat_chunks`
  - retryable semantics は内部で呼ぶ `generate_anthropic` / `stream_anthropic` の
    ステータスコード分類をそのまま引き継ぐ（429 は retryable、400 は not）
  - `coderouter_provider` タグは両方向で保持
- **`coderouter/translation/__init__.py`** — 新規 export
  - `to_anthropic_request` / `to_chat_response` / `stream_anthropic_to_chat_chunks`

#### Changed

- **`FallbackEngine.generate` / `.stream`** — コード変更なし。`AnthropicAdapter` の
  OpenAI-shape メソッドが正しく動くようになったため、engine の polymorphic ループが
  自然に kind:anthropic provider を含む profile を扱えるようになる（混在 chain も含む）
- **`coderouter/ingress/openai_routes.py`** — 変更なし。`/v1/chat/completions` が
  `kind: anthropic` provider に到達できる経路が開通（従来は即 500）

#### Tests

v0.3.x-1 完了時点 110 件 → **147 件 (+37 件)**:

- `tests/test_adapter_anthropic.py` — OpenAI-shape エントリポイントの 2 件を
  「retryable=False で reject」テストから「reverse 翻訳で正常動作」テストに差替 (+2 net)
  - `test_openai_shaped_generate_reverse_translates`: system / user / assistant+tool_calls /
    tool / user の 5 メッセージ → 送信 body（system 昇格 / tool_result batching /
    tools shape / tool_choice map / max_tokens default）を検証、text+tool_use の
    レスポンスが `ChatResponse` に戻ることを確認
  - `test_openai_shaped_generate_429_is_retryable`: 429 → retryable=True が reverse
    経路でも保持される
  - `test_openai_shaped_stream_reverse_translates`: SSE を `adapter.stream` で消費し、
    role 初期チャンク / content delta / finish / trailing usage の順を検証
  - `test_openai_shaped_stream_anthropic_error_event_is_non_retryable`: upstream
    `event: error` が `AdapterError(retryable=False)` として surface する
- `tests/test_translation_reverse.py` **31 件（新設）**
  - `to_anthropic_request`: simple text / system 昇格 / 複数 system join / system list /
    assistant tool_calls / 連続 tool batching / tool-then-user flush / image data URI /
    image URL / tools 変換 / tool_choice 4 ケース / max_tokens passthrough /
    malformed JSON args / 空 user 省略 / 空 assistant placeholder / stream+profile+stop
  - `to_chat_response`: text only / tool_use only / mixed / 複数 text 連結 /
    stop_reason 4 ケース
  - `stream_anthropic_to_chat_chunks`: text stream / tool_use stream (args 断片結合) /
    parallel tool_use blocks の index 分離 / `event: error` → retryable=False
- `tests/test_fallback_anthropic.py` **+4 件**
  - `test_openai_generate_routes_to_kind_anthropic_via_reverse_translation`
  - `test_openai_stream_routes_to_kind_anthropic_via_reverse_translation`
  - `test_openai_generate_mixed_chain_falls_over_openai_to_anthropic`
  - `test_openai_stream_midstream_kind_anthropic_raises_midstream_error`

テスト合計: **147 passed**。lint: v0.4-A で導入した issue は 0。

#### Design Decisions

- **A**: adapter 層で透過的に変換する（engine を変えない）。`FallbackEngine.generate` /
  `.stream` は provider kind を気にせずループするため、reverse 翻訳は
  `AnthropicAdapter.generate` / `.stream` の内部実装で閉じる
- **B**: client 送信の `model` は placeholder 扱いとし、provider config の `model` が
  常に優先（v0.3.x-1 の openai_compat / anthropic-native ルールと同じ）
- **C**: OpenAI の `role: "tool"` を複数連続して受けた場合、Anthropic の canonical shape
  （1 つの user turn に複数の `tool_result` block）にまとめる
- **D**: Anthropic `event: error` → `AdapterError(retryable=False)`。初期の
  `message_start` で既に role チャンクを emit 済みなので engine の mid-stream guard
  が `MidStreamError` に変換する動作も検証済み

#### Known Limitations

- 「OpenAI ingress → kind:anthropic provider」経路で送る場合、`max_tokens` を
  client が省略すると 4096 にデフォルトされる。精密に制御したいユーザは
  `/v1/chat/completions` body に `max_tokens` を明示する必要あり
- Anthropic 独自の `cache_control` / `thinking` ブロックは OpenAI 側に等価表現が
  ないため、OpenAI ingress からは設定不可。cache_control を活かしたい場合は
  v0.3.x-1 で追加した `/v1/messages` ingress を使う

---

## [v0.3.x-1] — 2026-04-20

### Anthropic Native Adapter (passthrough)

Claude 本家 / OpenRouter の Anthropic 互換エンドポイントに対し、翻訳コスト
ゼロで素通しする native adapter。`ProviderConfig.kind: "anthropic"` で有効化し、
`/v1/messages` → `AnthropicAdapter` → upstream Anthropic Messages API の経路で
cache_control / thinking / structured tool_use などの Anthropic 固有フィールドを
そのまま活用できるようにする。openai_compat provider と混在した fallback chain
もサポート（native 先頭 → openai_compat 後続、あるいはその逆）。

#### Added

- **`coderouter/adapters/anthropic_native.py`** — `AnthropicAdapter(BaseAdapter)`
  - 認証: `x-api-key` ヘッダ（Authorization: Bearer ではない）、`api_key_env` から取得
  - `anthropic-version: 2023-06-01` をデフォルト付与。`extra_body.anthropic_version` で上書き可
  - `base_url` は `/v1` 終端有無の両方を正規化して `{base}/v1/messages` を叩く
  - `generate_anthropic(AnthropicRequest) → AnthropicResponse` — httpx 直叩きの passthrough
  - `stream_anthropic(AnthropicRequest) → AsyncIterator[AnthropicStreamEvent]`
    - SSE を `event:` / `data:` ペアで buffer、空行境界で block 確定
    - heartbeat コメント行と malformed block は silently skip
  - OpenAI-shape の `generate` / `stream` は `retryable=False` の `AdapterError` を raise
    （逆翻訳 `ChatRequest → AnthropicRequest` は設計決定 F で out of scope）
  - retryable status code: `{404, 408, 425, 429, 500, 502, 503, 504}`
  - クライアント送信の `model` は strip、provider config の `model` が常に優先
- **`coderouter/routing/fallback.py`** — Anthropic 用 dispatch 追加 (~110 lines)
  - `generate_anthropic(AnthropicRequest) → AnthropicResponse`:
    adapter ごとに `isinstance(adapter, AnthropicAdapter)` で native / openai_compat を切替。
    native は passthrough、openai_compat は `to_chat_request` → `adapter.generate`
    → `to_anthropic_response(allowed_tool_names=...)` の経路（v0.3-A repair が発火）
  - `stream_anthropic(AnthropicRequest) → AsyncIterator[AnthropicStreamEvent]`:
    native は `adapter.stream_anthropic` を直 passthrough、openai_compat + tools は
    v0.3-D downgrade（内部非 stream → repair → `synthesize_anthropic_stream_from_response`）、
    openai_compat no-tools は `stream_chat_to_anthropic_events` 経由の真 streaming
  - mid-stream ガードは既存 `stream()` と同一セマンティクスを維持
    （first event 送出後の `AdapterError` → `MidStreamError`、fallback 禁止）

#### Changed

- **`coderouter/config/schemas.py`** — `ProviderConfig.kind` の `Literal` に `"anthropic"` を追加
  （`openai_compat` と並列）。既存設定は default のまま `openai_compat` が継続
- **`coderouter/adapters/registry.py`** — `build_adapter` が `kind="anthropic"` で
  `AnthropicAdapter` をインスタンス化するよう分岐
- **`coderouter/ingress/anthropic_routes.py`** — v0.3-D downgrade ロジックを engine に移設した
  副作用で大幅に簡素化。`messages()` ハンドラは `engine.generate_anthropic` /
  `engine.stream_anthropic` を呼ぶだけになり、ingress は HTTP 境界 + SSE wire format の
  責務のみ保持。`_anthropic_sse_iterator` は engine から流れる event を wrap しつつ
  `NoProvidersAvailableError → overloaded_error` / `MidStreamError → api_error` に変換
- **`examples/providers.yaml`** — `anthropic-direct` サンプル provider を追記
  (`kind: anthropic`, `paid: true`, `ANTHROPIC_API_KEY` 参照)

#### Tests

v0.3 完了時点 87 件 → **110 件 (+23 件)**:

- `tests/test_adapter_anthropic.py` **11 件（新設）**
  - URL 正規化 (`/v1` 終端有無両対応)
  - `x-api-key` / `anthropic-version` ヘッダ（default / override 両方）
  - OpenAI-shape `generate` / `stream` は retryable=False で reject
  - `generate_anthropic`: payload shape（client の model は無視、provider config が勝つ）、
    429 / 400 / 500 の status マッピング
  - `stream_anthropic`: SSE パースで `event:`/`data:` ペアを AnthropicStreamEvent に、
    `stream: true` が body に入る、初期 4xx は AdapterError、heartbeat / malformed block skip
- `tests/test_fallback_anthropic.py` **12 件（新設）**
  - native passthrough / openai_compat 経由の round-trip
  - tool-call repair が openai_compat 経由の generate_anthropic でも発火
  - 混在 chain（native → openai_compat / openai_compat → native）の fallback 双方向
  - 全 provider 失敗 → `NoProvidersAvailableError`、non-retryable は即中断
  - stream: native 真 streaming、openai_compat no-tools 真 streaming、
    openai_compat + tools は downgrade（`generate_calls` のみ埋まり `stream_calls == []`）、
    **native + tools は downgrade せず** native の structured tool_use を passthrough
  - mid-stream 失敗 → `MidStreamError`、初期失敗は従来どおり fallback
- `tests/test_ingress_anthropic.py` — engine への責務移譲に合わせ stub engines を
  `AnthropicRequest` / `AnthropicResponse` / `AnthropicStreamEvent` を直接やり取りする
  形にリライト。downgrade 関連の ingress 側テストは engine 側 (`test_fallback_anthropic.py`)
  に移譲

テスト合計: **110 passed**。lint: v0.3.x-1 で導入した issue は 0
（新規 `anthropic_native.py` の SIM117 は既存 `openai_compat.py` と同じパターンで意図的に踏襲）。

#### Design Decisions

- **A-1**: Anthropic-shape entry points を engine に追加（adapter 側だけで完結させず、
  既存の fallback / mid-stream guard / profile resolution をそのまま再利用する）
- **B**: 混在 chain（native + openai_compat が 1 profile に共存）を第一級サポート
- **C**: SSE は parse ベース（line-based → block-based）で受ける。mid-stream guard を
  event 単位で効かせるため
- **D**: 認証は `api_key_env` + `x-api-key` 固定、`anthropic-version` ヘッダ追加
- **E**: 5 依存原則を維持（`anthropic` SDK は使わず httpx 直叩き）
- **F**: 逆翻訳（`ChatRequest → AnthropicRequest`）は out of scope。OpenAI クライアントから
  Anthropic-native provider を叩く経路は今後のスコープ（`generate` / `stream` は
  retryable=False で即 reject）

#### Known Limitations

- client が `/v1/messages` に送る `model` は無視され、provider config の `model` が勝つ
  （OpenAI-compat adapter と同じ挙動）。profile 経由でしかモデルを切替できないが、
  これは CodeRouter のルーティング設計として意図的
- `ChatRequest` → `AnthropicRequest` の逆方向翻訳は未実装（設計決定 F）。
  OpenAI クライアントから Anthropic-native provider を叩きたい場合は v0.4+ の課題

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
