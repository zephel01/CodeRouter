# examples — 設定サンプル早わかり

CodeRouter の `providers.yaml` サンプル集です。**「どれを使えばいいか分からない」を解消するための索引**です。
まず下の決定表で 1 つ選び、`~/.coderouter-t/providers.yaml` にコピーして使ってください。

```bash
mkdir -p ~/.coderouter-t
cp examples/<選んだファイル> ~/.coderouter-t/providers.yaml
coderouter-t serve --port 8088
```

> どのファイルも、先頭に `# 【カテゴリ】 …` 行を入れてあります。開いた瞬間に用途が分かります。

---

## まずこれ — あなたの状況 → 使うファイル

| あなたの状況 | 使うファイル | カテゴリ |
|---|---|---|
| **とりあえず動かしたい / 迷っている** | `providers.yaml` | 汎用・全部入り |
| Ollama で、**何も考えず**用途別に振り分けたい | `providers.ollama-auto.yaml` | Ollama 通常 |
| Ollama で、**振り分けルールを自分で**書きたい | `providers.ollama-auto-custom.yaml` | Ollama 通常（上級） |
| Ollama で、**ローカル→無料クラウド**だけで完結させたい | `providers.ollama-free-chain.yaml` | Ollama 通常 |
| **Ollama を使わず**、手元の GGUF を llama.cpp / vLLM で | `providers.llamacpp-vllm.yaml` | llama.cpp / vLLM |
| 手元の GGUF を **model 名で自動起動・自動アンロード**したい（llama-swap 相当） | `providers.swap.yaml` | llama.cpp / vLLM |
| 無料の高品質クラウド（**NVIDIA NIM**）を足したい | `providers.nvidia-nim.yaml` | クラウド無料枠 |
| **Raspberry Pi** など小型 SBC で動かしたい | `providers.raspberrypi.yaml` | 特殊ハード |
| Claude Code のサブエージェント（planner/coder/reviewer）を役割ごとに別バックエンドへ振り分けたい | `providers-multiagent.yaml` | マルチエージェント |
| **Plan は Opus、実行はローカル/中位**に任せる opusplan 型で振り分けたい | `providers.opusplan.yaml`（要 `coderouter-plugin-agents`） | マルチエージェント |
| 外部の agent CLI（claude / codex / grok / antigravity）を1プロバイダとして呼びたい | `providers-agent-cli.yaml`（要 `coderouter-plugin-agents`） | マルチエージェント |
| **サブスク認証**（CLI ログイン済みトークン）を普通の provider としてフォールバックチェーンに組み込みたい | `providers.cli-session.yaml`（v2.14.0+） | 認証 |
| （開発者向け）Context Budget の検証をしたい | `providers.context-budget-test.yaml` | 内部検証用 |

どれを選んでも、別途 `.env`（`OPENROUTER_API_KEY` など）が要る場合は `cp examples/.env.example .env` してキーを入れます。無料クラウドを使わないなら不要です。

---

## カテゴリ別の中身

### 🟢 汎用 — 迷ったらこれ

- **`providers.yaml`** … 全部入りのフルリファレンス。Ollama / llama.cpp / LM Studio / OpenRouter / Anthropic まで多数の provider と 13 プロファイル（`multi` / `coding` / `general` / `reasoning` / `claude-code` ほか）を定義済み。Claude Code 用に最適化された既定スターターでもあります。**「まず動かす」「他の設定の書き方の見本帳」**として使う。`README` の `curl` ワンライナーが取得するのもこのファイルです。

### 🟦 Ollama 通常 — いちばん手軽な構成

前提: `ollama pull` でモデルを落としてある（各ファイル冒頭にコマンドあり）。

- **`providers.ollama-auto.yaml`** … v1.6 `auto_router` の**ゼロ設定**版。リクエスト本文を見て、画像→`multi` / コード濃→`coding` / その他→`writing` に自動で振り分け。`auto_router:` ブロックを書かなくても内蔵ルールが効く。`multi` / `coding` / `writing` の 3 プロファイルだけ用意すればよい。**最短で「賢い振り分け」を体験したい人向け。**
- **`providers.ollama-auto-custom.yaml`** … 上の**振り分けルールを自分で書く**版。`auto_router:` ブロックを足すと内蔵ルールは丸ごと置き換わる（マージされない）。「翻訳依頼は文章モデル」等の独自ルールを入れたくなったらここから。
- **`providers.ollama-free-chain.yaml`** … Qwen3.5 / Gemma4 を主役に、**ローカル → 無料クラウド**だけで完結する鎖。有料 API は `ALLOW_PAID=true` を明示しない限り呼ばれない。Note 記事連動のシンプル構成。

### 🟨 llama.cpp / vLLM — Ollama を使わない

- **`providers.llamacpp-vllm.yaml`** … 手元の `.gguf` を **llama.cpp** で、または **vLLM** で動かす最小構成。Launcher（`/launcher`）で起動したサーバーを provider として登録する形。**Ollama を入れたくない / DL 済み GGUF をそのまま使いたい人向け**（詳細は連作 note 第 22 話、`docs/backends/launcher.md`）。`backends` に `llama.cpp-cuda` / `-vulkan` / `-rocm` を登録して**特化ビルドを起動ごとに選ぶ**例もコメントで併記（v2.11.0+）。
- **`providers.swap.yaml`** … **モデル自動スワップ**（`launcher.swap`）。リクエストの model 名を見て llama-server をオンデマンド起動し、ロード完了まで保留、アイドル TTL で自動アンロードする llama-swap 相当の構成。カタログに列挙したモデルだけが起動可能（詳細は `docs/designs/launcher-model-swap.md`）。

### 🟧 特殊 — 環境・用途が限定的

- **`providers.nvidia-nim.yaml`** … `ローカル → NVIDIA NIM 無料枠 → OpenRouter 無料 →（有料）`。NIM は OpenRouter より緩いレート（~40 req/min）の高品質エスケープハッチ。
- **`providers.raspberrypi.yaml`** … Raspberry Pi 4/5 8GB 等の CPU 推論前提。**ローカルは tool 無し**、tool が必要なリクエストだけ無料クラウドへ逃がす割り切り構成（`has_tools` matcher）。

### 🟪 マルチエージェント・サブエージェント振り分け

- **`providers-multiagent.yaml`** … 自前マルチエージェントの Phase 0（テスト）構成。planner / coder / reviewer をローカル（Ollama）の別モデルに割り当てる。`X-CodeRouter-Profile` ヘッダでの明示駆動が主経路、`auto_router` は役割を明示しない単発リクエストの保険。
- **`providers.opusplan.yaml`** … Plan 役を Claude Opus（`agent_cli`、read-only）、実行をローカル→クラウド中位のフォールバックチェーン、レビューは監査を Opus・軽微をローカルに振り分ける構成。`docs/guides/subagent-routing.md` §5(a) から抽出。**Claude Code 純正の `opusplan` エイリアス（Plan モード中は opus、実行に移ると sonnet へ自動切替）とは別物**なので混同しないこと（詳細はファイル冒頭コメント参照）。
- **`providers-agent-cli.yaml`** … 外部コーディングエージェント CLI（claude / codex / grok / antigravity）を `kind: agent_cli` で1プロバイダとして登録する構成。**v2.9.0 で in-core 実装が削除され、外部プラグイン `coderouter-plugin-agents` の導入（`plugins.enabled: [agents]`）が必須**になった（未導入のまま `kind: agent_cli` の provider があると `coderouter-t serve` が起動時エラーで止まる）。

### 🟫 認証

- **`providers.cli-session.yaml`** … **v2.14.0 の新機能** `credential.source: cli_session` のサンプル。ベンダー CLI（kimi / grok など）が既にディスクに書いた OAuth トークンを読むだけで、そのサブスク枠を openai_compat / anthropic provider として使う。従来は `kind: agent_cli` しか選択肢が無く、サブスク provider はストリーミングもフォールバックチェーンも効かない「孤立した島」だったが、cli_session なら普通の provider としてチェーンの一リンクに組み込める。

### ⚙️ 内部検証用 — 通常は使わない

- **`providers.context-budget-test.yaml`** … Context Budget（L1）の動作検証用に閾値を低くした構成。**本番には使わない**。検証後は通常の `providers.yaml` に戻すこと。

---

## 動作確認済み

このフォルダの `providers*.yaml` は、CodeRouter 本体の設定ローダ（`load_config`、スキーマ＋起動時バリデーション）と、`coderouter-t serve` の実起動で確認済みです（設定ロード → サーバ起動 → `/dashboard` 応答まで到達）。実際のルーティングには対応バックエンド（Ollama / llama.cpp など）の起動が別途必要です。

> 例外: **`providers.opusplan.yaml`**（2026-08-10 追加）はスキーマとの突き合わせのみで、`coderouter-t serve` での実起動確認はまだ行っていません。`kind: agent_cli` を使うため `coderouter-plugin-agents` の導入が前提です。

---

最終更新: 2026-08-10
