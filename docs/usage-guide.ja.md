# CodeRouter 利用ガイド

[`README.md`](../README.ja.md) の実践的な補足です。README が CodeRouter が**何か**を説明するのに対し、本ガイドはハードウェアに合ったモデルの選び方、どのつまみを回すか、OS ごとの起動フローを説明します。

目次:

1. [OS 互換性](#1-os-互換性)
2. [ハードウェア別のモデル選定](#2-ハードウェア別のモデル選定)
3. [ローカルモデルごとのチューニング既定値](#3-ローカルモデルごとのチューニング既定値)
4. [Ollama セットアップ — 要点版](#4-ollama-セットアップ--要点版)
5. [OS 別の Claude Code 起動フロー](#5-os-別の-claude-code-起動フロー)
6. [OpenRouter 無料枠とのペア方針](#6-openrouter-無料枠とのペア方針)
7. [動作確認 (`doctor` + `verify`)](#7-動作確認-doctor--verify)
8. [トラブルシューティングのクイックインデックス](#8-トラブルシューティングのクイックインデックス)
9. [セキュリティとサプライチェーン](#9-セキュリティとサプライチェーン)
10. [Attribution](#10-attribution)

English version: [`docs/usage-guide.md`](./usage-guide.md)

---

## 1. OS 互換性

CodeRouter 自体は純 Python 3.12+ と 5 つの pip 依存 — CPython が動くところならどこでも動きます。制約は隣接する 2 つ、**Ollama**（ほとんどのユーザーが組み合わせるローカルモデルバックエンド）と **Claude Code**（CLI クライアント）から来ます。OS 対応は実質 `min(coderouter, ollama, claude-code)`。

| OS | CodeRouter サーバー | Ollama | Claude Code | 検証済経路 |
|---|---|---|---|---|
| macOS — Apple Silicon (M1–M5) | ✅ | ✅ ネイティブ (Metal) | ✅ `npm install -g @anthropic-ai/claude-code` | **主要開発ターゲット。** v1.0 の実機検証はすべてこの経路。 |
| macOS — Intel | ✅ | ✅ だが遅い (CPU のみ。Metal GPU なし) | ✅ | CodeRouter のワイア層は動作; ローカル推論は非現実的 — クラウドフォールバック限定で使う。 |
| Linux — x86_64 (Ubuntu / Debian / Fedora) | ✅ | ✅ ネイティブ (NVIDIA GPU なら CUDA、他は CPU) | ✅ | フル対応。`uv` + `pip install` 経路は macOS と同一。 |
| Linux — ARM64 (Raspberry Pi 5 / AWS Graviton) | ✅ | ⚠️ Pi では CPU のみ; クラウド Graviton なら問題なし | ✅ | CodeRouter は動く; Pi クラスでは「クラウド中継」プロキシとして有用。 |
| Windows — native (PowerShell / cmd) | ⚠️ 一部 | ✅ ネイティブ (CUDA) | ⚠️ `claude` CLI は Windows native で既知の癖あり | `coderouter serve` は動く。`scripts/verify_*.sh` は bash 専用 — WSL か Git Bash で。 |
| Windows — WSL2 (Ubuntu) | ✅ | ✅ (WSL 内にインストール、または `host.docker.internal:11434` でホスト側 Ollama にブリッジ) | ✅ | **Windows 推奨経路。** WSL2 内からは Linux と同じ UX。 |

判断の目安:

- **Apple Silicon Mac、ユニファイドメモリ 16 GB 以上** — 理想。Ollama + qwen2.5-coder がそのまま動く。
- **NVIDIA GPU 搭載 Linux ワークステーション (VRAM 8 GB 以上)** — 同じく理想。Ollama が自動で CUDA を使う。
- **Windows** — 特別な理由がなければ WSL2。bash シェルスクリプト（`scripts/verify_v1_0.sh`）は POSIX シェルが必要。
- **ローカル GPU なし** — それでも CodeRouter は役立ちます。ローカルティアのプロバイダを飛ばし、チェーンを `openrouter-free` → `openrouter-claude`（有料、オプトイン）に直結。ローカル推論無しでルーティング / フォールバック / ミッドストリームガードの価値は得られます。

既知の隙間:

- `scripts/verify_v0_5.sh` と `scripts/verify_v1_0.sh` は macOS `/bin/bash` 3.2+ を想定（Linux bash 4+ は問題なし）。Windows cmd/PowerShell は対象外。
- Docker イメージは未提供 — `plan.md §11` が v1.1 で追跡。

---

## 2. ハードウェア別のモデル選定

別の 2 つの問い: 「どれくらいの大きさのモデルが載るか」と「推論はどれくらい速いか」。下の表は前者に最適化しています。速度はメモリ帯域（Apple ユニファイドメモリ、CUDA GDDR、CPU RAM の順）で決まります。

| あなたのマシン | ローカルモデル (Ollama タグ) | 理由 |
|---|---|---|
| VRAM 8 GB / RAM 8–16 GB（エントリ Windows / Linux ラップトップ、M1/M2 ベース Mac） | `qwen2.5-coder:1.5b` — そのまま OpenRouter 無料にフォールスルー | 1.5b は ~1 GB で載り、応答が速い。Claude Code のツール使用には品質がギリギリ; 「オフラインの時だけ」と割り切り、本気の作業はチェーンを無料クラウドに流す。 |
| VRAM 16 GB / RAM 16–24 GB（RTX 4070 / M1 Pro / M2 / M3 ベース） | `qwen2.5-coder:7b`（既定 `Q4_K_M` 量子化、~4.5 GB） | スイートスポット。tool 対応、M 系で Claude Code 1 ターン ~30–60s。`examples/providers.yaml` の先頭ローカルプロバイダはこれ。 |
| VRAM 24 GB+ / RAM 24–36 GB（RTX 4090 / M1 Max / M2 Max / M3 Max 32GB） | `qwen2.5-coder:14b`（~8.5 GB） | 7b よりツール選択品質が高い。典型的 1 ターン ~2 分。7b を速い初手、14b を品質フォールバックで組む — `claude-code` プロファイルがこれ。 |
| 48 GB+ / M 系 Max/Ultra 64 GB+ | `qwen2.5-coder:32b`（~19 GB）または異量子化の 14b を 2 本 | この階層ではローカルが十分良く、クラウドフォールバックはメインではなく「あれば良い」。 |
| Mac 96 GB+ / 専用 GPU サーバー 80 GB+ | マルチモデルのホットスワップ（32b + 14b + 7b を全て常駐） | 本ガイドの範囲外 — そのハードウェアを持っているなら既に独自の意見があります。 |

上のタグは既定の `Q4_K_M` 量子化を想定しています。品質を少し上げるなら `Q5_K_M` / `Q6_K_M`（VRAM +25% 程度）、`Q8_0` はこのパラメータ数ではほぼ割に合いません。

VRAM の目安: `Q4_K_M` GGUF は概ね `params × 0.55 GB` の VRAM + 32K コンテキストの KV キャッシュに 1–2 GB。つまり `qwen2.5-coder:14b` Q4 ≈ 7.7 GB 重み + 1.5 GB KV ≈ 9.2 GB — 16 GB GPU に余裕で入りますが、他に使う余地はほぼ残りません。

### qwen2.5-coder 以外で押さえておく価値のあるモデル

CodeRouter はどの OpenAI 互換エンドポイントも同じ扱いで、モデル選択はルーター選択と独立です。よく組み合わされるいくつかを、ファミリ別にグループ化してあげます。Ollama（あるいは任意の OpenAI 互換サーバー）がタグをロードできればルーティング可能です — ベンダー不問。

Dense coder 系（上の 2.5-coder プロファイルがそのまま拡張できます）:

- **`qwen3-coder:7b` / `:14b`** — qwen3 dense コーダ系。2.5-coder と同規模、推論スタイルが違う。`<think>` タグを漏らしがちなので `output_filters: [strip_thinking]` または `append_system_prompt: "/no_think"` を有効化。`examples/providers.yaml` の `ollama-hf-example` にリファレンスプロファイル（コメントアウト）として同梱。
- **`deepseek-coder-v2:16b`** — DeepSeek-v2 MoE アーキテクチャ（アクティブ 2.4B / 総 16B）。macOS ユニファイドメモリでは大きさの割にかなり速い。tool 使用は当たり外れ。`coderouter doctor` の判定で `capabilities.tools: false` を付ける。

MoE コーダ（大きなパラメータ合計、小さなアクティブ — 見出しサイズの割にメモリ帯域にやさしい）:

- **`qwen3-coder:30b-a3b`** — 総 30B / アクティブ ~3B の MoE。tool 対応、アクティブ ~3B 経路しか毎トークン動かないため Apple Silicon では dense 30B より明確に速くストリームします; VRAM はグラフ全体を保持する必要あり (~18 GB Q4)。`strip_thinking` と `strip_stop_markers` を両方有効にし、`coderouter doctor` で tool 呼び出しの信頼性を検証。
- **`qwen3:32b`**（dense, 汎用）— 大型汎用。Claude Code の UI に chain-of-thought チャネルが届く前に `append_system_prompt: "/no_think"` で黙らせる。tool 呼び出し成功率は 2.5-coder:32b より低め; プローブで確認。
- **`qwen3:30b-a3b`**（コーダ MoE の汎用版）— `qwen3-coder:30b-a3b` と同じ MoE フットプリントで汎用。コーダバイアスなしで長文推論が欲しい「fast」プロファイル用途に便利。

Reasoning チューニングの distill（既定で `<think>` ブロックを吐く — 常に `strip_thinking` + `strip_stop_markers` とセット）:

- **`deepseek-r1:distill-qwen-14b` / `:distill-qwen-32b`** — R1 で蒸留した Qwen ベース。計画 → 実行の推論に強く、構造化ツール JSON には弱い; `capabilities.tools: false` のまま、tool 呼び出し層ではなくテキスト回答の品質フォールバックとして使う。

汎用（非コーダ、ファミリが命令チューニングされていなければ通常 `capabilities.tools: false`）:

- **`gemma3:4b` / `:12b` / `:27b`** — Google の Gemma 3 ファミリ。多言語（日本語含む）だが tool 使用のトレーニングは無し — `capabilities.tools: false` 前提で。`gemma3:12b` Q4 は 12 GB VRAM に楽に入り、「fast chat」階層として妥当。`/no_think` 指示はなく、Gemma 3 は既定で `<think>` ブロックを吐かないので漏れが観測されない限り `output_filters` は空でよい。
- **`gemma4:e2b` / `:e4b` / `:latest` / `:26b` / `:31b`** — Google の Gemma 4 ファミリ。Ollama では <https://ollama.com/library/gemma4> で公開されています。Gemma 3 からラインナップが大きく変わりました: `e2b` / `e4b` は**実効パラメータ** (effective) でエッジ/ラップトップ向けに調整されたビルド（コンテキスト 128K、pull サイズ ~7–10 GB）、`:26b` は総 26B / アクティブ ~4B の **Mixture-of-Experts** で **256K** コンテキスト、`:31b` が dense のフラッグシップです。Gemma 4 はマルチモーダル（テキスト + 画像）で、**設定可能な thinking モード**を搭載 — Gemma 3 と違い、推論を有効にすると `<think>` ブロックを吐くことがあります。したがって gemma4 の全タグで **`output_filters: [strip_thinking]` から始め**、マーカー漏れが見えたら `strip_stop_markers` も追加します。tool 使用は世代依存 — Gemma 3 は未対応、Gemma 4 は一定のインストラクトチューニング有り — なので `capabilities.tools: true` にする前に**必ず `coderouter doctor` で検証**してください。`:26b` MoE は Apple Silicon（24–32 GB ユニファイドメモリ）のスイートスポット: 毎トークン動くのはアクティブ ~4B 経路だけで、256K ウィンドウは Claude Code にとって余裕のあるヘッドルームになります。
- **`llama3.3:70b`** — Meta の 70B dense instruct。クリーンに量子化すれば tool 対応; 48 GB 以上の VRAM か Mac 64 GB+ ユニファイドが必要。多くのユーザーにとっては「そのハードウェアがあれば有料ティアの代替」枠。
- **`llama3.2:3b` / `phi4:14b`** — 汎用、コーダチューニングではない。コード以外の短いチャット返信の「fast」プロファイルとして有用。
- **`gpt-oss:20b`** / **`gpt-oss:120b`**（OpenAI OSS 系）— ベンダーミラーでタグが異なる; 24 GB GPU のスイートスポットは 20B。各 choice の `message` / `delta` に非標準 `reasoning` フィールドを吐きますが、v0.5-C の `openai_compat` アダプタが既に剥がします; `coderouter doctor` で 1 回プローブし strip が発火して `reasoning-leak` が `OK` を返すことを確認してください。

新しいモデルを追加したら必ず `coderouter doctor --check-model <provider>` を走らせてください — 6 プローブ（auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming）が、モデルが実際に欲しがる `capabilities.*` と `extra_body.options.*` を教えてくれます。doctor の判定が真実の源で、§3 の表はそれと突き合わせるための既知良好な起点にすぎません。

MoE フットプリントの補足: 「N 総 / M アクティブ」とは、レイテンシ的には M パラメータ相当で流れる一方、VRAM には N パラメータ分の重みを常駐させる必要がある、という意味です。Qwen3 30B-A3B は 30B のようにロードし 3B のように動く — メモリ帯域がボトルネックの Apple Silicon では異例にお得ですが、~18 GB Q4 の重み保持予算が実際にあることが前提です。

---

## 3. ローカルモデルごとのチューニング既定値

下の値は既知良好な起点です。`coderouter doctor --check-model` が、あなたの固有 Ollama ビルドでモデルが別の値を欲しがっているときに教えてくれます。

行はファミリ別に束ねてあるので、pull したタグに合う行を探しやすいはずです。`output_filters` より左の列は `providers.yaml` の `extra_body.options` に入れます; `output_filters` と `capabilities.tools` はプロバイダ直下のトップレベル。`—` は「既定ではフィルタ不要、`coderouter doctor` で確認」の意。

| モデル | `num_ctx` | `num_predict` | `temperature` | `output_filters` | `capabilities.tools` |
|---|---:|---:|---:|---|:---:|
| **qwen2.5-coder (dense, coder-tuned)** | | | | | |
| `qwen2.5-coder:1.5b` | 8192 | 2048 | 0.2 | `[strip_thinking]` | false (小さすぎて確実には tool-call できない) |
| `qwen2.5-coder:7b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true |
| `qwen2.5-coder:14b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true |
| `qwen2.5-coder:32b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true |
| **qwen3 family (coder + general, dense + MoE)** | | | | | |
| `qwen3-coder:7b` / `:14b` | 32768 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` | true (doctor で検証) |
| `qwen3-coder:30b-a3b` (MoE、アクティブ ~3B) | 65536 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` | true (doctor で検証) |
| `qwen3:30b-a3b` (general MoE) | 32768 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` + `append_system_prompt: "/no_think"` | doctor で検証 |
| `qwen3:32b` (dense, general) | 32768 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` + `append_system_prompt: "/no_think"` | doctor で検証 (概ね false) |
| **DeepSeek (coder MoE + R1 distills)** | | | | | |
| `deepseek-coder-v2:16b` | 16384 | 4096 | 0.2 | `[strip_thinking]` | doctor で検証 (概ね false) |
| `deepseek-r1:distill-qwen-14b` / `:distill-qwen-32b` | 16384 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` | false (R1 distill は構造化 tool-call が安定しない) |
| **Gemma 3 (Google、汎用多言語)** | | | | | |
| `gemma3:4b` | 8192 | 2048 | 0.3 | — | false |
| `gemma3:12b` | 16384 | 4096 | 0.3 | — | false |
| `gemma3:27b` | 32768 | 4096 | 0.3 | — | false |
| **Gemma 4 (Google、マルチモーダル + 設定可能 thinking; 2026)** | | | | | |
| `gemma4:e2b` (edge、実効 ~2B) | 32768 | 4096 | 0.3 | `[strip_thinking]` | doctor で検証 |
| `gemma4:e4b` (edge、実効 ~4B) | 32768 | 4096 | 0.3 | `[strip_thinking]` | doctor で検証 |
| `gemma4:latest` (pull 約 9.6 GB、128K ctx) | 32768 | 4096 | 0.3 | `[strip_thinking]` | doctor で検証 |
| `gemma4:26b` (MoE、アクティブ ~4B / 総 26B、256K ctx) | 65536 | 4096 | 0.3 | `[strip_thinking, strip_stop_markers]` | doctor で検証 |
| `gemma4:31b` (dense フラッグシップ) | 32768 | 4096 | 0.3 | `[strip_thinking, strip_stop_markers]` | doctor で検証 |
| **Llama (Meta、汎用 instruct)** | | | | | |
| `llama3.2:3b` | 8192 | 2048 | 0.3 | — | false |
| `llama3.3:70b` | 32768 | 4096 | 0.2 | — | true (doctor で検証; VRAM 48 GB+ または 64 GB+ ユニファイドが必要) |
| **gpt-oss (OpenAI オープンウェイト)** | | | | | |
| `gpt-oss:20b` | 32768 | 4096 | 0.2 | `[strip_thinking]` | true (doctor で検証) |
| `gpt-oss:120b` | 65536 | 4096 | 0.2 | `[strip_thinking]` | true (doctor で検証; VRAM 80 GB+ が必要) |
| **その他** | | | | | |
| `phi4:14b` | 16384 | 4096 | 0.2 | — | false |
| HF-GGUF `hf.co/<user>/<repo>:<quant>` | 8192 | 4096 | 0.2 | `[strip_thinking, strip_stop_markers]` + `append_system_prompt: "/no_think"` | 既定 false（プローブで確認） |

この表は CodeRouter を介した Claude Code 経路に合わせて意図的に強めの推奨です: `num_ctx` の既定は高めに倒し（Claude Code の毎ターンシステムプロンプトは 15–20K トークンなので 2048 は常に間違い、8192 でも際どい）、`temperature` は tool-call 信頼性のため 0.2、`output_filters` は `<think>` や stop マーカーを漏らすファミリに対して事前投入。欲しいファミリが載っていなければ**最も近い行**を採用し（qwen3 ≒ qwen3-coder、gemma3 ≒ tool 非対応の dense 汎用）、`coderouter doctor --check-model <provider>` を走らせてください — プローブの判定が権威で、この表は doctor が比較する相手としての初期ドラフトにすぎません。

なぜ `temperature: 0.2`？ Claude Code は構造化 JSON でツール呼び出しを発行します。temperature が高い（Ollama 既定 0.7）ほど表現が創造的になり、JSON が壊れやすくなります。CodeRouter v0.3-A の tool-call 修復が壊れを救いますが、予防のほうが安上がり。これは [claude-code-local](https://github.com/nicedreamzapp/claude-code-local) が独立に tool-call 信頼性のワークで拾った知見で、CodeRouter も独立して同じ既定にたどり着きました。

値の `providers.yaml` への具体的な入れ方:

```yaml
providers:
  - name: ollama-qwen-coder-7b
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen2.5-coder:7b
    timeout_s: 120
    output_filters: [strip_thinking]
    extra_body:
      options:
        num_ctx: 32768
        num_predict: 4096
        temperature: 0.2
    capabilities:
      tools: true
```

`num_ctx` と `num_predict` が `extra_body.options` の下に居るのは、Ollama のネイティブ JSON 形状がそうだからです（そのまま `/v1/chat/completions` に渡される）。`temperature` は OpenAI 形状リクエストのトップレベルにも置けますが、`options` の中にまとめて置くと 3 つのチューニング値が 1 ブロックに収まり、Ollama はどちらでも同じように尊重します。

### どのつまみが一番効くか

1. **`num_ctx`** が Ollama で #1 のサイレント失敗源。既定 2048 トークン; Claude Code のシステムプロンプトだけで 15–20K。200 ステータスなのに空/意味不明の返信が出るときはほぼこれ。`coderouter doctor` v1.0-B が canary エコーで直接検出します。
2. **`num_predict`** は**出力**長の上限。古い Ollama で既定 128、一部フォークで 256。Claude Code の返信が文の途中で切れるのは大抵これ。`coderouter doctor` v1.0-C が決定的な「1 から 30 まで数えて」ストリーミングプローブで検出します。
3. **`temperature`** は品質のつまみで、正しさのつまみではありません。ログで tool-call 修復が頻発する（`recover_garbled_tool_json` / v0.3-A）なら、まず 0.2 に落とすのが最初の対処。
4. **`output_filters`** はアダプタ境界のレスポンスバイトストリームから `<think>` / `<|turn|>` / 他の stop マーカー漏れを除去。コンテンツを 1 回余計に舐める分のコストで、どのモデル・どのプロバイダ・どのクライアントでも動作します。

ナラティブな症状→修正マップは [README → Ollama 初心者 — サイレント失敗 5 症状](../README.ja.md#ollama-初心者--サイレント失敗-5-症状-v07-c) を参照。

---

## 4. Ollama セットアップ — 要点版

CodeRouter は Ollama をインストールも包みもしません — `http://localhost:11434/v1` の OpenAI 互換エンドポイントと話すだけです。セットアップ:

```bash
# macOS
brew install ollama
brew services start ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows
#   インストーラを https://ollama.com/download からダウンロード
#   または推奨: 上の Linux コマンドで WSL2 内にインストール
```

`providers.yaml` で宣言したモデルを pull:

```bash
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:14b
```

起動確認:

```bash
curl http://localhost:11434/v1/models
# → {"object":"list","data":[{"id":"qwen2.5-coder:7b", ...}, ...]}
```

知っておくと便利な環境変数:

- `OLLAMA_KEEP_ALIVE=30m` — ロード済みモデルの常駐時間。既定 5 分はローカルティアを間欠的に使うと厳しい。
- `OLLAMA_NUM_PARALLEL=2` — サーバーがバッチする同時リクエスト数。同じ Ollama に CodeRouter + 別クライアントを当てるなら上げる。
- `OLLAMA_FLASH_ATTENTION=1` — 実験的な attention 最適化。Apple Silicon で速くなることがある; 常時 on にする前に計測を。

もっと深い Ollama 話（量子化選択、HF-GGUF ロード、カスタム `Modelfile`、マルチ GPU）は Ollama 自身の docs <https://github.com/ollama/ollama> が扱います。CodeRouter の関心は「`/v1/*` エンドポイントに到達可能で、タグが存在する」までです。

---

## 5. OS 別の Claude Code 起動フロー

パターンはどの OS でも同じ: 1 つのターミナルで CodeRouter を起動し、もう 1 つのターミナルで Claude Code を起動しながら環境変数 2 つでそれを指す。

### macOS / Linux

ターミナル 1 — CodeRouter 起動:

```bash
cd /path/to/CodeRouter
uv run coderouter serve --port 8088 --mode claude-code
```

ターミナル 2 — Claude Code を CodeRouter に向けて起動:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude
```

`ANTHROPIC_AUTH_TOKEN` は任意の値で構いません — CodeRouter は無視しますが、Claude Code が非空を要求するので入れる必要があります。

### Windows — WSL2（推奨）

Linux と同じ。両ターミナルを WSL2 内で実行。Claude Code の npm インストールも WSL2 内:

```bash
npm install -g @anthropic-ai/claude-code
```

### Windows — native PowerShell

```powershell
# ターミナル 1
cd C:\path\to\CodeRouter
uv run coderouter serve --port 8088 --mode claude-code

# ターミナル 2
$env:ANTHROPIC_BASE_URL = "http://localhost:8088"
$env:ANTHROPIC_AUTH_TOKEN = "dummy"
claude
```

Windows native の既知の癖: `scripts/verify_*.sh` は bash 必須（Git Bash または WSL2）。サーバー本体と `coderouter doctor` サブコマンドは Linux と同じように動きます。

### 橋の検証

どの OS でも、CodeRouter が起動済なら:

```bash
curl http://localhost:8088/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 64,
    "messages": [{"role":"user","content":"say hi"}]
  }'
```

200 とコンテンツが返れば Claude Code を向ける準備は OK。

---

## 6. OpenRouter 無料枠とのペア方針

OpenRouter は無料ティアのモデルをいくつかホストしており、ローカルと有料 Claude の間の中間層としてチェーンに重ねられます。`examples/providers.yaml` の既定 `claude-code` プロファイルは 2 つを使っています — **異なるベンダー**を並べるとレート制限脱出になります: qwen が毎分上限に達しても gpt-oss は動く。

現在、無料ティアのリファレンスとして同梱しているもの:

| プロバイダ (YAML 名) | モデル | 得意 | 注意 |
|---|---|---|---|
| `openrouter-free` | `qwen/qwen3-coder:free` | 長文コーディング (262K ウィンドウ)、tool 使用 | 日次クォータ; 20 req/min 前後のレート制限 |
| `openrouter-gpt-oss-free` | `openai/gpt-oss-120b:free` | 汎用チャット、qwen からのレート制限脱出 | 非標準 `reasoning` フィールドを吐く — v0.5-C が剥がす; 無害 |

ロスターはローテーションします — 週次差分は [`docs/openrouter-roster/CHANGES.md`](./openrouter-roster/CHANGES.md) 参照。新しい無料モデルが現れたり、古いものが予告なく引き上げられたりします。週次で `scripts/openrouter_roster_diff.py`（または `scripts/` の cron）を走らせて追跡してください。

起動前に `OPENROUTER_API_KEY` を設定 — OpenRouter free でも認証は必要:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...    # https://openrouter.ai/keys で取得
uv run coderouter serve --port 8088
```

現場で効くペア戦略:

1. **ローカル先頭**（速度狙いの 7b）— 短い編集の 95% はここでヒットし、ボックスから出ない。
2. **ローカル 2 番手**（品質狙いの 14b）— 小さなモデルが外した 5% を拾う。
3. **OpenRouter free**（qwen3-coder）— 両ローカルが失敗（タイムアウト / 5xx / オーバーロード）したとき、長文コンテキストが武器に。
4. **OpenRouter free 別ベンダー**（gpt-oss-120b）— qwen からのレート制限脱出。
5. **Claude 有料**（`ALLOW_PAID=true`）— 最終砦。

これがまさに `examples/providers.yaml` の `claude-code` プロファイルです。

---

## 7. 動作確認 (`doctor` + `verify`)

CodeRouter は 2 つの検証ツールを同梱:

**プロバイダ単位の診断** — `coderouter doctor --check-model <provider>`。6 プローブ連鎖（auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming）を 1 プロバイダに対して走らせ、判定表と不一致時のコピペ YAML パッチを出力。`providers.yaml` を編集するたびに使うのが良い。

```bash
uv run coderouter doctor --check-model ollama-qwen-coder-7b
```

**フルシステムの実機検証** — `bash scripts/verify_v1_0.sh`。3 つのペア（bare/tuned）シナリオをエンドツーエンドで回し、変換 + プローブのループが閉じていることを実証。シナリオ内訳は `scripts/verify_v1_0.sh --help`、参照エビデンスドキュメントは [`docs/retrospectives/v1.0-verify.md`](./retrospectives/v1.0-verify.md)。

v0.5 系にも `scripts/verify_v0_5.sh` があり、ケイパビリティゲート (`thinking` / `cache_control` / `reasoning`) の面を網羅。どちらも冪等で再実行して問題ありません。

---

## 8. トラブルシューティングのクイックインデックス

代表的な症状 → README の該当セクションへの短いマップ:

- **空/意味不明の返信、200 ステータス** → [サイレント失敗症状 #1](../README.ja.md#ollama-初心者--サイレント失敗-5-症状-v07-c)（`num_ctx` 低すぎ）。
- **「単語の途中で切れる」** → まだ番号は付けていない症状（`num_predict` 低すぎ）。v1.0-C の doctor プローブが検出。
- **「ファイルが読めません」/ ツール呼び出しなし** → 症状 #2（`capabilities.tools` 不一致）。
- **UI に `<think>` タグ** → 症状 #3（`output_filters: [strip_thinking]`）。
- **初回リクエストが常に 404** → 症状 #4（`model:` のタイポ、または `ollama pull` 漏れ）。
- **クラウドプロバイダ全部 401** → 症状 #5（`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` 未設定）。
- **`capability-degraded` ログ行** → 想定内の観測; README のトラブルシューティング参照。
- **`502 Bad Gateway: all providers failed`** → `provider-failed` ログ行を順に読む; 末尾の `error` フィールドがチェーン終端の理由。

それ以外はどれも `coderouter doctor --check-model <provider>` を最初に、ログ行を 2 番目に。

---

## 9. セキュリティとサプライチェーン

シークレットは環境変数 (`OPENROUTER_API_KEY`、`ANTHROPIC_API_KEY`) に置き、誤ってコミットされうる `providers.yaml` や `.env` には書かないでください。ルーターの既定バインドは `127.0.0.1` です; 認証を強制するリバースプロキシ無しに `0.0.0.0` を公開しないこと。

CI は `gitleaks`（シークレットスキャン）、`pip-audit` + OSV-Scanner（2 つのアドバイザリフィードでの CVE 監査）、`uv sync --frozen`（lockfile ドリフト拒否）、そして禁則 SDK grep（ランタイム経路に `anthropic` / `openai` / `litellm` / `langchain` を入れさせない）を強制します。Dependabot が Python 依存と GitHub Actions バージョンの週次 bump を提案します。

完全なポリシー、脅威モデル、脆弱性報告経路: [`docs/security.md`](./security.md)（英語）。

---

## 10. Attribution

チューニング既定値と「tool-call 信頼性のための temperature 0.2」ヒューリスティクスは、MLX ネイティブの Apple Silicon で同じ問題をエンドツーエンドに解き、効くつまみを文書化した独立した [claude-code-local](https://github.com/nicedreamzapp/claude-code-local)（Matt Macosko）の先行ワークから示唆を得ています。CodeRouter は違う実装経路 — クロスプラットフォームの OpenAI 互換ルーター vs Apple 限定の MLX ネイティブサーバー — を採りましたが、同じチューニング値に収束しました。両プロジェクトが重なるところでは先行実装としてクレジットし、異なるところ（ルーティング / フォールバック / 双方向ワイア変換 / 宣言的フィルタチェーン）は CodeRouter 固有の設計です。
