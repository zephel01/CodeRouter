# コミット点 — 本文を送り出す前の失敗を、どこまで救えるか

> 対象: v2.16.0 以降（`ttft_timeout_s` と `x-should-retry` は v2.17.0）
> English: [commit-point.en.md](./commit-point.en.md)

ストリーミング応答には「ここから先は取り消せない」という点があります。クライアントへ 1 バイトでも本文を送った後に上流が倒れると、別の provider に切り替えることはできません（途中まで届いた文章と、新しい provider の文章がつながらないため）。CodeRouter はこの点を **コミット点** と呼び、その手前で起きた失敗はできるだけ次の provider に回します。

## コミット点の手前で起きることと、その扱い

| 状況 | 扱い | 必要な設定 |
|---|---|---|
| 上流が接続を拒否・タイムアウト・4xx/5xx（最初のイベントより前） | 次の provider へ。全滅なら **HTTP ステータス付きの Anthropic 形式エラー**（v2.16.0） | なし |
| 上流がいつまでも最初のトークンを返さない（モデル読み込み中、長いプロンプト処理） | `ttft_timeout_s` を過ぎたら次の provider へ（v2.17.0） | provider に `ttft_timeout_s` |
| `message_start` などの前置イベントは来たが、本文が来ないまま切れる／空で終わる | 前置イベントをクライアントに見せずに保留し、次の provider へ | profile に `empty_response_action: fallback` |
| 本文を送り出した**後**に切れる | **切り替えない**。検知して通知する（`stream_truncation_action`、v2.15.0）、または受け取り済みの本文で閉じる（`partial_stitch_action: surface`） | 各設定 |

最後の行は CodeRouter でも救えません。2026-10 時点で確認した範囲では、LiteLLM や Bifrost も本文送出後には切り替えません。

## `empty_response_action: fallback` が実質的なコミット点

この設定にすると、エンジンは `message_start` などの前置イベントを手元に保留し、**最初の本物の出力**（text / tool_use / thinking の delta）が来た時点でまとめて送り出します。そこがコミット点です。それまでに起きた失敗は、クライアントから見えないまま次の provider に回ります。

Claude Code で使うなら、まずこれを有効にしてください。

```yaml
profiles:
  - name: claude-code
    providers: [local-llamacpp, openrouter-free]
    empty_response_action: fallback
```

## `ttft_timeout_s` — 最初のトークンの期限（v2.17.0）

```yaml
providers:
  - name: local-llamacpp
    kind: anthropic                  # llama-server の /v1/messages にそのまま流す
    base_url: http://127.0.0.1:8080
    model: ""
    ttft_timeout_s: 20     # 20 秒以内に最初の出力が無ければ次の provider へ
```

- **数え始め**: その provider への試行を始めた時点。
- **止まる条件**: 最初の本物の出力が来たとき。前置イベント（`message_start`）だけでは止まりません。ただし、前置イベントが来た後も期限を見続けるには `empty_response_action: fallback` が必要です（それが無いと前置イベントはすぐクライアントに送られ、コミット済みになるため）。
- **チェーンの最後の provider には適用しません**。次が無いので、待つ方がましだからです。
- 非ストリーミングの要求には適用しません（そちらは `timeout_s` が上限）。
- **`/v1/messages`（Anthropic 形式）のストリーミングだけが対象**です。`/v1/chat/completions` には効きません。
- **`kind: openai_compat` で tools 付きの要求（Claude Code はほぼ毎回 tools を送ります）には効きません。** この組み合わせは、tool call を修復するために上流を非ストリーミングで呼ぶ経路（tool-turn downgrade）を通るので、「最初の出力」が応答全体になってしまうからです。ここに期限をかけると、ゆっくりだが正常なローカルの応答を途中で捨てることになります。ローカルの llama.cpp / Ollama で TTFT カットオフを使うなら、上の例のように `kind: anthropic`（ネイティブの `/v1/messages`）で接続してください。
- 打ち切った試行は通常の失敗として扱われます。理由は `X-CodeRouter-Fallback-Reason: ttft-timeout`、self-healing のガードもこれを学習します。

典型的な使い方: ローカルの llama-server がモデルを読み込み中、または長いプロンプトを処理中で、クラウドなら今すぐ答えられる、という場面です。値は「ふだんのローカルの TTFT の 2〜3 倍」程度から始めてください。短すぎると、毎回クラウドに逃げることになります。

## 全滅したときに返すもの（v2.16.0 / v2.17.0）

チェーン全体が失敗したときは、Anthropic API と同じ形のエラーを返します。

| 上流の状況 | HTTP | `error.type` | `x-should-retry` |
|---|---|---|---|
| 文脈超過（どの provider でも） | 400 | `invalid_request_error`（`prompt is too long: …`） | `false` |
| 再試行しても無駄な 400 / 422 | 400 | `invalid_request_error`（上流の文言そのまま） | `false` |
| 413 | 413 | `request_too_large` | `false` |
| 429 | 429 | `rate_limit_error`（上流の `retry-after` を転送） | `true` |
| その他（5xx、タイムアウト、401/403 など） | 502 | `api_error` | 再試行可能な失敗が 1 つでもあれば `true` |

ストリーミングの要求でも、最初のイベントより前に全滅した場合はこの HTTP ステータスで返ります（`200` の中に `error` イベントを入れる形ではありません）。Claude Code は `prompt is too long` を見て会話を要約し、`x-should-retry` / `retry-after` を見て再試行するかを決めます。

## 既知の制限

- **前置イベントを保留している間は、クライアントに何も送れません。** ローカルの長いプロンプト処理でこの時間が長いと、クライアント側の無通信タイムアウト（Claude Code の `CLAUDE_STREAM_IDLE_TIMEOUT_MS`）に当たる可能性があります。`ttft_timeout_s` をその値より短くしてください。
- 保留中に `ping` を送って接続を保つ機能はまだありません。
